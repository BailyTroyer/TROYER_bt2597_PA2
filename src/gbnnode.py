from log import logger
import time
import re
import select
import socket
import json
from threading import Thread, Event, Lock
import signal

import sys


class ClientError(Exception):
    """Thrown when Client errors during regular operation."""

    pass


class Sender:
    def __init__(self, opts):
        self.opts = opts
        self.buffer_lock = Lock()
        self.buffer = []
        self.window_size = opts["window_size"]
        # the last ack'ed message from receiver
        self.window_base = 0
        # the next index in window to send
        self.window_next_seq_num = 0

        self.incoming_seq_num = 0
        self.timer_sleep_interval = 500  # 500ms

    def send_packet(self, sock, packet):
        """Sends a single packet onto UDP socket."""
        client_port, client_ip = self.opts["peer_port"], "0.0.0.0"
        try:
            client_destination = (client_ip, client_port)
            sock.sendto(packet, client_destination)
        except socket.error as e:
            raise ClientError(f"UDP socket error: {e}")

    def handle_command(self, user_input):
        """Parses user plaintext and sends to proper destination."""
        # Pattern match inputs to command methods
        if re.match("send (.*)", user_input):
            # Push to queue
            message = user_input.split(" ")[1]
            packets = list(message)
            with self.buffer_lock:
                # logger.info(f"appending to buffer: {packets}")
                self.buffer.extend(packets)
        else:
            logger.info(f"Unknown command `{user_input}`.")

    def signal_handler(self, signum, _frame):
        """Custom wrapper that throws error when exit signal received."""
        print()  # this adds a nice newline when `^C` is entered
        self.stop_event.set()
        raise ClientError(f"Client aborted... {signum}")

    def create_sock(self):
        """Create a socket."""
        try:
            return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except socket.error as e:
            raise ClientError(f"UDP client error when creating socket: {e}")

    def decode_message(self, message):
        """Convert bytes to deserialized JSON."""
        return json.loads(message.decode("utf-8"))

    def encode_message(self, type, payload=None, metadata={}):
        """Convert plaintext user input to serialized message 'packet'."""
        packet_num = self.window_next_seq_num
        message_metadata = {**self.opts, **metadata}
        message = {"type": type, "payload": payload, "metadata": message_metadata}
        return json.dumps(message).encode("utf-8"), packet_num

    def send(self, sock, packet, seq_num):
        """Adds metadata to header and sends packet to UDP socket."""
        metadata = {"packet_num": seq_num}
        message, packet_num = self.encode_message("message", packet, metadata)
        self.send_packet(sock, message)
        return packet_num

    def send_buffer(self, stop_event):
        """Sends outbound buffer messages when available."""

        # Init sock for outbound messages
        sock = self.create_sock()
        while True:
            # Listen for kill events
            if stop_event.is_set():
                logger.info("stopping buffer sender listener")
                break

            with self.buffer_lock:
                # check if we can send more messages from buffer
                # buffer must be gt zero AND next_seq_num within window range

                if len(self.buffer) == 0:
                    continue

                # Can keep sending if next sequence number - window base <= window size
                window_offset = self.window_next_seq_num - self.window_base
                is_within_window = window_offset < self.window_size
                # Prevent sending sequence number thats gt buffer
                is_seq_within_buffer = self.window_next_seq_num < len(self.buffer)

                if is_within_window and is_seq_within_buffer:
                    next_packet = self.buffer[self.window_next_seq_num]
                    packet_num = self.send(sock, next_packet, self.window_next_seq_num)
                    self.window_next_seq_num += 1

                    logger.info(f"packet{packet_num} {next_packet} sent")

    def handle_incoming_message(self, sock, sender_ip, payload):
        """Sends ACK based on configured drop rate."""
        metadata, message, type = (
            payload.get("metadata"),
            payload.get("payload"),
            payload.get("type"),
        )
        pack_num = metadata.get("packet_num")

        # if ACK handle increasing sender window params
        if type == "ack":
            # base should ONLY increase if pack_num matches sender base next seq num
            if pack_num != self.window_base:
                logger.info(f"ACK{pack_num} discarded")
                return

            self.window_base += 1
            logger.info(f"ACK{pack_num} received, window moves to {self.window_base}")
            return

        logger.info(f"packet{pack_num} {message} received")

        #### HANDLE DROPS HERE ####
        ###                     ###
        #### HANDLE DROPS HERE ####

        # handle ACK ONLY if incoming message matches incoming seq num

        # Drop invalid incoming messages that don't match incoming seq number
        if pack_num > self.incoming_seq_num:
            # the sender will eventually timeout and send new message
            return

        # send ACK to recv'er
        client_port = metadata.get("self_port")
        ack_metadata = {"packet_num": pack_num}
        ack_message, ack_pack_num = self.encode_message("ack", None, ack_metadata)

        # increase incoming seq num
        self.incoming_seq_num += 1
        logger.info(f"ACK{ack_pack_num} sent, expecting packet{self.incoming_seq_num}")
        # print("SENDER IP: ", sender_ip, client_port, ack_message)
        sock.sendto(ack_message, (sender_ip, client_port))

    def sender_timer(self, stop_event):
        """Attaches to last sent message and if no ACK is recv'ed within interval resends & resets."""

        # Init sock for resending timed out messages
        sock = self.create_sock()
        ## @TODO create decorator for wrapping thread w/ stop event
        while True:
            # Listen for kill events
            if stop_event.is_set():
                logger.info("stopping listener")
                break

            time.sleep(self.timer_sleep_interval)

            if self.window_base == self.window_next_seq_num:
                return

            # if window base != next_seq_num we ARE NOT in parity with recv'er

            # handle resend logic
            logger.info(f"packet{self.window_base} timeout")

            ## @TODO IS THIS OK USING TWICE IN 2 THREADS?
            with self.buffer_lock:
                timed_out_pack = self.buffer[self.window_base]
                # packet_num = self.send(sock, timed_out_pack)
                # logger.info(f"packet{packet_num} {timed_out_pack} sent")

    def server_listen(self, stop_event):
        """Listens on specified `client_port` for messages from server."""
        sock = self.create_sock()
        sock.bind(("", self.opts["self_port"]))

        while True:
            # Listen for kill events
            if stop_event.is_set():
                logger.info("stopping listener")
                break

            readables, writables, errors = select.select([sock], [], [], 1)
            for read_socket in readables:
                # logger.info("listening")
                data, (sender_ip, sender_port) = read_socket.recvfrom(4096)
                message = self.decode_message(data)
                self.handle_incoming_message(read_socket, sender_ip, message)

    def start(self):
        """Start both the user input listener and peer event listener."""
        try:
            # Handle signal events (e.g. `^C`)
            self.stop_event = Event()
            signal.signal(signal.SIGINT, self.signal_handler)
            # start server listener
            server_thread = Thread(target=self.server_listen, args=(self.stop_event,))
            server_thread.start()
            # start sending buffer listener
            send_buf_thread = Thread(target=self.send_buffer, args=(self.stop_event,))
            send_buf_thread.start()

            # Deadloop input 'till client ends
            while server_thread.is_alive() and not self.stop_event.is_set():
                server_thread.join(1)
                user_input = input(f"node> ")
                self.handle_command(user_input)
        except ClientError:
            # Prevent exceptions when quickly spamming `^C`
            signal.signal(signal.SIGINT, lambda s, f: None)


help_message = """GbNode allows you to send chars to a client with defined loss.

Flags:
    -d      Drop packets in a deterministic way
    -p      Drop packets with defined probability

Options:
    <self-port>: Sender port
    <peer-port>: Reciever port
    <window-size>: Size of GBN window

Usage:
    GbNode [flags] [options]"""


class InvalidArgException(Exception):
    """Thrown when CLI input arguments don't match expected type/structure/order."""

    pass


def valid_port(value):
    """Validate port matches expected range 1024-65535."""
    if value.isdigit():
        val = int(value)
        return val >= 1024 and val <= 65535
    return False


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
    if not float(mode_value):
        raise InvalidArgException(f"{mode} <value> must be a valid digit")

    return {"mode": mode, "mode_value": float(mode_value)}


def parse_mode_and_go():
    """Validate root mode args: `-d` or `-p`."""
    args = sys.argv[1:]
    if len(args) == 0:
        raise InvalidArgException(help_message)

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
    except Exception as e:
        print(f"Unknown error {e}.")
        sys.exit(1)
