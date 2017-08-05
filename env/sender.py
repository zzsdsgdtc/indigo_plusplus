import sys
import json
import socket
import select
from os import path
import numpy as np
import project_root
from helpers.helpers import (
    curr_ts_ms, apply_op,
    READ_FLAGS, ERR_FLAGS, READ_ERR_FLAGS, WRITE_FLAGS, ALL_FLAGS)


def format_actions(action_list):
    """ Returns the action list, initially a list with elements "[op][val]"
    like /2.0, -3.0, +1.0, formatted as a dictionary.

    The dictionary keys are the unique indices (to retrieve the action) and
    the values are lists ['op', val], such as ['+', '2.0'].
    """
    return {idx: [action[0], float(action[1:])] 
                  for idx, action 
                  in enumerate(action_list)}


class Sender(object):

    # RL exposed class/static variables
    max_steps = 1000
    state_dim = 4
    action_mapping = format_actions(
            ["/2.0", "-10.0", "-4.0", "+0.0", "+4.0", "+10.0", "*2.0"])
    action_cnt = len(action_mapping)

    def __init__(self, port=0, train=False, debug=False):
        self.train = train
        self.debug = debug

        # UDP socket and poller
        self.peer_addr = None

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('0.0.0.0', port))
        sys.stderr.write('[sender] Listening on port %s\n' %
                         self.sock.getsockname()[1])

        self.poller = select.poll()
        self.poller.register(self.sock, ALL_FLAGS)

        # UDP datagram template
        self.data = {}
        self.data['payload'] = 'x' * 1350

        # congestion control related
        self.seq_num = 0
        self.next_ack = 0
        self.cwnd = 10.0
        self.step_len_ms = 10

        # state variables for RLCC
        self.min_rtt = float("inf")
        self.rtt_ewma = None

        self.delivered_time = 0
        self.delivered = 0
        self.delivery_rate_ewma = None  # BBR's delivery rate

        self.sent_bytes = 0
        self.send_rate_ewma = None    # Vegas sending rate

        self.alpha = 0.875  #  how much weight to give to the current avg 

        self.step_start_ms = None
        self.running = True

        if self.train:
            self.step_cnt = 0

            self.first_recv_ts = float('inf')
            self.last_recv_ts = 0
            self.total_delays = []

            history_path = path.join(project_root.DIR, 'history')
            self.history = open(history_path, 'a')

    def cleanup(self):
        self.sock.close()

    def reset(self):
        """Reset the sender. Must be called after every training iteration."""

        self.seq_num = 0
        self.next_ack = 0
        self.cwnd = 10.0

        if self.train:
            self.running = True
            self.step_cnt = 0
            self.sent_bytes = 0
            self.acked_bytes = 0
            self.first_recv_ts = float('inf')
            self.last_recv_ts = 0
            self.total_delays = []

            self.drain_packets()

    def drain_packets(self):
        """Drain all the packets left in the channel."""

        TIMEOUT = 1000  # ms
        self.poller.modify(self.sock, READ_ERR_FLAGS)

        while True:
            events = self.poller.poll(TIMEOUT)

            if not events:  # timed out
                break

            for fd, flag in events:
                assert self.sock.fileno() == fd

                if flag & ERR_FLAGS:
                    sys.exit('Channel closed or error occurred')

                if flag & READ_FLAGS:
                    self.sock.recvfrom(1600)

    def handshake(self):
        """Handshake with peer receiver. Must be called before run()."""

        while True:
            msg, addr = self.sock.recvfrom(1600)

            if msg == 'Hello from receiver' and self.peer_addr is None:
                self.peer_addr = addr
                self.sock.sendto('Hello from sender', self.peer_addr)
                sys.stderr.write('[sender]: Handshake success! '
                                 'Receiver\'s address is %s:%s\n' % addr)
                break

        self.sock.setblocking(0)  # non-blocking UDP socket

    def set_sample_action(self, sample_action):
        """Set the policy. Must be called before run()."""

        self.sample_action = sample_action

    def update_state(self, ack):
        """ Update the state variables listed in __init__() """
        self.next_ack = max(self.next_ack, int(ack['ack_seq_num']) + 1)

        send_ts = ack['ack_send_ts']
        recv_ts = ack['ack_recv_ts']

        # Update the RTT and minRTT
        curr_time_ms = curr_ts_ms()
        rtt = curr_time_ms - send_ts
        self.min_rtt = min(self.min_rtt, rtt)
        if self.rtt_ewma is not None:
            self.rtt_ewma *= self.alpha
            self.rtt_ewma += (1-self.alpha) * rtt
        else:
            self.rtt_ewma = rtt

        # Update BBR's delivery rate
        self.delivered += ack['ack_bytes']
        self.delivered_time = float(curr_time_ms)
        delivery_rate = float((self.delivered - ack['delivered'])) /\
                              (self.delivered_time - ack['delivered_time'])
        delivery_rate  = delivery_rate * 8 * 0.001      # B/s to Mb/s
        if self.delivery_rate_ewma is not None:
            self.delivery_rate_ewma *= self.alpha
            self.delivery_rate_ewma += (1 - self.alpha) * delivery_rate
        else:
            self.delivery_rate_ewma = delivery_rate

        # Update Vegas sending rate
        send_rate = (self.sent_bytes - ack['sent_bytes']) / float(rtt)
        send_rate = send_rate * 8 * 0.001      # B/s to Mb/s
        if self.send_rate_ewma is not None:
            self.send_rate_ewma *= self.alpha
            self.send_rate_ewma += (1 - self.alpha) * send_rate
        else:
            self.send_rate_ewma = send_rate

        if self.train:
            curr_owd = recv_ts - send_ts
            self.total_delays.append(curr_owd)

            self.first_recv_ts = min(recv_ts, self.first_recv_ts)
            self.last_recv_ts = max(recv_ts, self.last_recv_ts)

    def take_action(self, action_idx):
        op, val = self.action_mapping[action_idx]

        self.cwnd = apply_op(op, self.cwnd, val)

        self.cwnd = min(max(5.0, self.cwnd), 1000.0)

        if self.debug:
            sys.stderr.write('cwnd %.2f\n' % self.cwnd)

    def compute_reward(self):
        duration = self.last_recv_ts - self.first_recv_ts

        if duration > 0:
            avg_throughput = float(self.delivered * 8) * 0.001 / duration
        else:
            avg_throughput = 0.0

        delay_percentile = float(np.percentile(self.total_delays, 95))
        loss_rate = 1.0 - float(self.delivered) / self.sent_bytes

        reward = np.log(max(1e-3, avg_throughput))
        reward -= np.log(max(1.0, delay_percentile))

        self.history.write('Average throughput: %.2f Mbps\n' % avg_throughput)
        self.history.write('95th percentile one-way delay: %d ms\n' %
                           delay_percentile)
        self.history.write('Loss rate: %.2f\n' % loss_rate)
        self.history.write('Reward: %.3f\n' % reward)

        return reward

    def window_is_open(self):
        return self.seq_num - self.next_ack < self.cwnd

    def send(self):
        self.data['sent_bytes'] = self.sent_bytes
        self.data['delivered'] = self.delivered
        self.data['delivered_time'] = self.delivered_time
        self.data['seq_num'] = str(self.seq_num).zfill(10)
        self.seq_num += 1
        self.data['send_ts'] = curr_ts_ms()

        serialized_data = json.dumps(self.data)
        self.sock.sendto(serialized_data, self.peer_addr)

        self.sent_bytes += len(serialized_data)

        if self.debug:
            sys.stderr.write('Sent seq_num %d\n' % int(self.data['seq_num']))

    def recv(self):
        serialized_ack, addr = self.sock.recvfrom(1600)

        if addr != self.peer_addr:
            return

        try:
            ack = json.loads(serialized_ack)
        except ValueError:
            return

        self.update_state(ack)

        if self.step_start_ms is None:
            self.step_start_ms = curr_ts_ms()

        # At each step end, feed the state: 
        if curr_ts_ms() - self.step_start_ms > self.step_len_ms:  # step's end
            features = [self.rtt_ewma / self.min_rtt,
                        self.delivery_rate_ewma,
                        self.send_rate_ewma,
                        self.cwnd]

            print '%f %f %f %f' % tuple(features)

            action = self.sample_action(features)
            self.take_action(action)

            self.step_start_ms = curr_ts_ms()

            if self.train:
                self.step_cnt += 1
                if self.step_cnt >= Sender.max_steps:
                    self.step_cnt = 0
                    self.running = False

        if self.debug:
            sys.stderr.write('Received ack_seq_num %d\n' %
                             int(ack['ack_seq_num']))

    def run(self):
        TIMEOUT = 1000  # ms

        self.poller.modify(self.sock, ALL_FLAGS)
        curr_flags = ALL_FLAGS

        while self.running:
            if self.window_is_open():
                if curr_flags != ALL_FLAGS:
                    self.poller.modify(self.sock, ALL_FLAGS)
                    curr_flags = ALL_FLAGS
            else:
                if curr_flags != READ_ERR_FLAGS:
                    self.poller.modify(self.sock, READ_ERR_FLAGS)
                    curr_flags = READ_ERR_FLAGS

            events = self.poller.poll(TIMEOUT)

            if not events:  # timed out
                self.send()

            for fd, flag in events:
                assert self.sock.fileno() == fd

                if flag & ERR_FLAGS:
                    sys.exit('Error occurred to the channel')

                if flag & READ_FLAGS:
                    self.recv()

                if flag & WRITE_FLAGS:
                    while self.window_is_open():
                        self.send()
