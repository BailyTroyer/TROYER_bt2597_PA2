from log import logger
import random
import time
import re
import select
import socket
import json
from threading import Thread, Event, Lock
import signal

import sys
from messages import parse_help_message, gbn_help_message

from utils import (
    deadloop,
    InvalidArgException,
    valid_port,
    SocketClient,
    decode,
    encode,
    handles_signal,
    SignalError,
)


def decision(probability):
    return random.random() < probability


class ClientError(Exception):
    """Thrown when Client errors during regular operation."""

    pass


class Sender:
    def __init__(self, opts):
        # CLI args
        self.opts = opts

        # Basic buffer array + lock
        self.buffer_lock = Lock()
        ## @TODO MAX THIS BUFFER SIZE
        self.buffer = []
        self.window_size = opts["window_size"]

        # the last ack'ed message from receiver
        self.window_base = 0
        # the next index in window to send
        self.window_next_seq_num = 0

        # increased on succesful ACKs
        self.incoming_seq_num = 0
        # used to compare in the timer against current inbox state
        self.last_incoming_seq_num = 0
        self.timer_sleep_interval = 500 / 1000  # 500ms (500ms/1000ms = 0.5s)

        self.dropped_packet_numbers = []
        # The message being sent (added to packet metadata)
        self.total_message = ""
        self.dropped_packets = 0
        self.acked_packets = 0
        self.sent_packets = 0
        self.partial_message = ""

        self.stop_event = Event()
        self.client = SocketClient(
            self.opts["self_port"], self.stop_event, self.handle_incoming_message
        )

    def handle_command(self, user_input):
        """Parses user plaintext and sends to proper destination."""
        # Pattern match inputs to command methods
        if re.match("send (.*)", user_input):
            # Push to queue
            message = " ".join(user_input.split(" ")[1:])
            self.total_message = message
            packets = list(message)
            ## @TODO DEADLOOP WHILE BUFFER DOESN'T HAVE SPACE TO FIT EVERYTHIGN
            ## @TODO USE A SIMPLE FOR LOOP TO INSERT INSTEAD OF EXTEND WITH MAX BUFFER SIZE
            with self.buffer_lock:
                # logger.info(f"appending to buffer: {packets}")
                self.buffer.extend(packets)
        else:
            logger.info(f"Unknown command `{user_input}`.")

    def encode_message(self, type, payload=None, metadata={}):
        """Convert plaintext user input to serialized message 'packet'."""
        message_metadata = {**self.opts, **metadata}
        return {"type": type, "payload": payload, "metadata": message_metadata}

    def send(self, packet, seq_num):
        """Adds metadata to header and sends packet to UDP socket."""
        self.sent_packets += 1
        metadata = {"packet_num": seq_num, "total_message": self.total_message}
        message = self.encode_message("message", packet, metadata)
        self.client.send(message, self.opts["peer_port"])

    @deadloop
    def send_buffer(self):
        """Sends outbound buffer messages when available."""
        with self.buffer_lock:
            # check if we can send more messages from buffer
            # buffer must be gt zero AND next_seq_num within window range

            if len(self.buffer) == 0:
                return

            # Can keep sending if next sequence number - window base <= window size
            window_offset = self.window_next_seq_num - self.window_base
            is_within_window = window_offset < self.window_size
            # Prevent sending sequence number thats gt buffer
            ## @TODO WINDOW NEXT SEQ NUM IS BASED ON SENT MESSAGES e.g. we just sent 2 messages thus seq num is 0->1->2
            is_seq_within_buffer = window_offset < len(self.buffer)

            if is_within_window and is_seq_within_buffer:
                next_packet = self.buffer[window_offset]
                pack_num = self.window_next_seq_num
                self.send(next_packet, pack_num)

                logger.info(f"packet{pack_num} {next_packet} sent")
                self.window_next_seq_num += 1

    def handle_incoming_message(self, sock, sender_ip, payload):
        """Sends ACK based on configured drop rate."""
        metadata, message, type = (
            payload.get("metadata"),
            payload.get("payload"),
            payload.get("type"),
        )
        total_message = metadata.get("total_message")
        pack_num = metadata.get("packet_num")

        if type == "stats":
            logger.info(message)
            return

        # if ACK handle increasing sender window params and removing original message from buffer
        if type == "ack":
            # base should ONLY increase if pack_num matches sender base next seq num
            if pack_num != self.window_base:
                logger.info(f"ACK{pack_num} discarded")
                # self.dropped_packets += 1
                return

            with self.buffer_lock:
                ### @TODO IS THIS RIGHT OFFSETTING WITH WINDOW BASE
                ack_message = self.buffer.pop(pack_num - self.window_base)
                # self.partial_message += ack_message

            self.window_base += 1
            logger.info(f"ACK{pack_num} received, window moves to {self.window_base}")
            return

        ## Handle DROPS based on mode resolution
        mode, mode_value = self.opts["mode"], self.opts["mode_value"]
        if mode == "-p":
            # drop when the prob matches probabilistic
            should_drop = decision(mode_value)
        else:
            # drop when the incoming index matches deterministic mode value
            should_drop = (
                pack_num % mode_value == 0
                and pack_num not in self.dropped_packet_numbers
            )
        # don't ack if should drop resolves TRUTHY
        if should_drop:
            self.dropped_packets += 1
            self.dropped_packet_numbers.append(pack_num)
            ##### @TODO SHOULD RESENDS COUNT AGAINST DROPPED THINGS?
            logger.info(f"packet{pack_num} {message} discarded")
            return

        logger.info(f"packet{pack_num} {message} received")

        ### Handle ACK ONLY if incoming message matches incoming seq num
        # Drop invalid incoming messages that don't match incoming seq number
        if pack_num > self.incoming_seq_num:
            # the sender will eventually timeout and send new message
            logger.info(f"packet{pack_num} {message} dropped")
            return

        self.partial_message += message

        # send ACK to recv'er
        client_port = metadata.get("self_port")
        ack_metadata = {"packet_num": pack_num, "total_message": total_message}
        ack_message = encode(self.encode_message("ack", None, ack_metadata))

        # increase incoming seq num
        self.incoming_seq_num += 1
        logger.info(f"ACK{pack_num} sent, expecting packet{self.incoming_seq_num}")
        self.acked_packets += 1

        sock.sendto(ack_message, (sender_ip, client_port))

        if self.partial_message == total_message:
            total_packets = self.dropped_packets + self.acked_packets
            stats_message = f"[Summary] {self.dropped_packets}/{total_packets} packets discarded, loss rate = {self.dropped_packets/total_packets}%"
            logger.info(stats_message)
            stats_message = encode(self.encode_message("stats", stats_message))
            sock.sendto(stats_message, (sender_ip, client_port))

    @deadloop
    def sender_timer(self):
        """Attaches to last sent message and if no ACK is recv'ed within interval resends & resets."""
        with self.buffer_lock:
            # do nothing if nothing outbound is being awaited
            if len(self.buffer) == 0:
                return

        old_base = self.window_base

        time.sleep(self.timer_sleep_interval)

        # First message was recv'ed; we're ok
        if self.window_base > old_base:
            return

        # handle resend logic (send whats remaining in window)
        logger.info(f"packet{self.window_base} timeout")
        with self.buffer_lock:
            messages_to_send = self.buffer[
                0 : self.window_next_seq_num - self.window_base
            ]

            packet_seq_num = self.window_base
            for packet in messages_to_send:
                self.send(packet, packet_seq_num)
                logger.info(f"packet{packet_seq_num} {packet} sent")
                packet_seq_num += 1

    def start_gbn_threads(self):
        """Starts listener, buffer sender and timeout sender threads."""
        # Listens for incoming UDP messages
        Thread(target=self.client.listen).start()
        # Deadloops and dumps buffer to UDP socket when free
        Thread(target=self.send_buffer).start()
        # start outbound send timer listener
        Thread(target=self.sender_timer).start()

    @handles_signal
    def start(self):
        """Start threads, and listen for input."""
        self.start_gbn_threads()
        # User input parsing (root of GBN sending)
        while not self.stop_event.is_set():
            user_input = input(f"node> ")
            self.handle_command(user_input)


def parse_args(args):
    """Validate flags `<self-port>`, `<peer-port>`, `<window-size>`"""
    if len(args) != 3:
        raise InvalidArgException(
            "only accepts <self-port>, <peer-port> and <window-size>"
        )
    self_port, peer_port, window_size = args
    if not valid_port(self_port):
        raise InvalidArgException(
            f"Invalid <self-port>: {self_port}; Must be within 1024-65535"
        )
    if not valid_port(peer_port):
        raise InvalidArgException(
            f"Invalid <peer-port>: {peer_port}; Must be within 1024-65535"
        )
    if int(window_size) < 0:
        raise InvalidArgException(
            f"Invalid <window-size>: {window_size}; Must be greater than zero"
        )
    return {
        "self_port": int(self_port),
        "peer_port": int(peer_port),
        "window_size": int(window_size),
    }


def parse_mode(args):
    """Validate `-p` or `-d` flag and arg."""
    if len(args) != 2:
        raise InvalidArgException("-p,-d only accepts <value>")
    mode, mode_value = args
    if mode != "-p" and mode != "-d":
        raise InvalidArgException(f"{mode} is not a valid mode")
    if mode == "-d" and not mode_value.isdigit():
        raise InvalidArgException(f"<value> must be a valid digit")
    if not float(mode_value):
        raise InvalidArgException(f"{mode} <value> must be a valid digit")
    return {"mode": mode, "mode_value": float(mode_value)}


def parse_mode_and_go():
    """Validate root mode args: `-d` or `-p`."""
    args = parse_help_message(gbn_help_message)

    # validate common base args
    opts = parse_args(args[:3])
    # valid deterministic or probabilistic args
    mode_opts = parse_mode(args[3:])

    # Construct main GBN sender class
    sender = Sender({**mode_opts, **opts})
    sender.start()


if __name__ == "__main__":
    """Start client and handle root errors."""
    try:
        parse_mode_and_go()
    except InvalidArgException as e:
        print(e)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Quitting.")
        sys.exit(1)
