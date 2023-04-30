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

        message_metadata = {**self.opts, **metadata}
        message = {"type": type, "payload": payload, "metadata": message_metadata}
        return json.dumps(message).encode("utf-8")

    def send(self, sock, packet, seq_num):
        """Adds metadata to header and sends packet to UDP socket."""
        self.sent_packets += 1
        metadata = {"packet_num": seq_num, "total_message": self.total_message}
        message = self.encode_message("message", packet, metadata)
        self.send_packet(sock, message)

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
                ## @TODO WINDOW NEXT SEQ NUM IS BASED ON SENT MESSAGES e.g. we just sent 2 messages thus seq num is 0->1->2
                is_seq_within_buffer = window_offset < len(self.buffer)

                if is_within_window and is_seq_within_buffer:
                    next_packet = self.buffer[window_offset]
                    pack_num = self.window_next_seq_num
                    self.send(sock, next_packet, pack_num)
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
            # self.acked_packets += 1
            logger.info(f"ACK{pack_num} received, window moves to {self.window_base}")
            # if self.partial_message == total_message:
            #     total_packets = (
            #         self.dropped_packets + self.acked_packets + self.sent_packets
            #     )
            #     logger.info(
            #         f"[Summary] {self.dropped_packets}/{total_packets} packets discarded, loss rate = {self.dropped_packets/total_packets}%",
            #     )
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
        ack_message = self.encode_message("ack", None, ack_metadata)

        # increase incoming seq num
        self.incoming_seq_num += 1
        logger.info(f"ACK{pack_num} sent, expecting packet{self.incoming_seq_num}")
        self.acked_packets += 1

        sock.sendto(ack_message, (sender_ip, client_port))

        if self.partial_message == total_message:
            total_packets = self.dropped_packets + self.acked_packets
            stats_message = f"[Summary] {self.dropped_packets}/{total_packets} packets discarded, loss rate = {self.dropped_packets/total_packets}%"
            logger.info(stats_message)
            stats_message = self.encode_message("stats", stats_message)
            sock.sendto(stats_message, (sender_ip, client_port))

    def sender_timer(self, stop_event):
        """Attaches to last sent message and if no ACK is recv'ed within interval resends & resets."""

        # Init sock for resending timed out messages
        sock = self.create_sock()
        while True:
            # Listen for kill events
            if stop_event.is_set():
                logger.info("stopping listener")
                break

            with self.buffer_lock:
                # do nothing if nothing outbound is being awaited
                if len(self.buffer) == 0:
                    continue

            old_base = self.window_base

            time.sleep(self.timer_sleep_interval)

            # First message was recv'ed; we're ok
            if self.window_base > old_base:
                continue

            # handle resend logic (send whats remaining in window)
            logger.info(f"packet{self.window_base} timeout")
            with self.buffer_lock:
                messages_to_send = self.buffer[
                    0 : self.window_next_seq_num - self.window_base
                ]

                packet_seq_num = self.window_base
                for packet in messages_to_send:
                    self.send(sock, packet, packet_seq_num)
                    logger.info(f"packet{packet_seq_num} {packet} sent")
                    packet_seq_num += 1

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
            # start outbound send timer listener
            timer_thread = Thread(target=self.sender_timer, args=(self.stop_event,))
            timer_thread.start()
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
    if mode == "-d" and not mode_value.isdigit():
        raise InvalidArgException(f"<value> must be a valid digit")
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
