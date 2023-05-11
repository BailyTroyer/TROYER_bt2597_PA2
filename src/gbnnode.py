from log import logger
from operator import itemgetter
import random
import time
import re
from threading import Thread, Event, Lock
import sys

from messages import parse_help_message, gbn_help_message, get_stats_message
from utils import (
    deadloop,
    InvalidArgException,
    valid_port,
    SocketClient,
    encode,
    handles_signal,
)


# 500ms (500ms/1000ms = 0.5s)
TIMER_SLEEP_INTERVAL = 500 / 1000


def decision(probability):
    return random.random() < probability


class ClientError(Exception):
    """Thrown when Client errors during regular operation."""

    pass


class GenericGBNode:
    def __init__(
        self,
        port,
        peer_port,
        window_size,
        mode,
        mode_value,
        stop_event,
        on_send,
        on_stats=None,
    ):
        # Main Params
        self.port = port
        self.peer_port = peer_port
        self.window_size = window_size
        self.mode = mode
        self.mode_value = mode_value
        # GBN Logic
        self.init_gbn_state()
        self.buffer_lock = Lock()
        self.on_send = on_send
        self.stop_event = stop_event
        self.on_stats = on_stats
        self.last_acked = 0

    def init_gbn_state(self):
        """Initialize instance vars that depend on each GBN send."""
        self.buffer = []
        # the last ack'ed message from receiver
        self.window_base = 0
        # the next index in window to send
        self.next_seq_num = 0
        # increased on succesful ACKs
        self.incoming_seq_num = 0
        # used to compare in the timer against current inbox state
        self.last_incoming_seq_num = 0
        self.dropped_packet_numbers = []
        # The message being sent (added to packet metadata)
        self.total_message = ""
        self.dropped_packets = 0
        self.acked_packets = 0
        self.sent_packets = 0
        self.partial_message = ""

    def create_gbn_message(self, type, payload=None, metadata={}):
        """Convert plaintext user input to serialized message 'packet'."""
        message_metadata = {"port": self.port, **metadata}
        return {"type": type, "payload": payload, "metadata": message_metadata}

    def send(self, packet, seq_num):
        """Adds metadata to header and sends packet to UDP socket."""
        self.sent_packets += 1
        metadata = {"packet_num": seq_num, "total_message": self.total_message}
        message = self.create_gbn_message("message", packet, metadata)
        self.on_send(message, self.peer_port)

    @deadloop
    def send_buffer(self):
        """Sends outbound buffer messages when available."""
        with self.buffer_lock:
            # check if we can send more messages from buffer
            # buffer must be gt zero AND next_seq_num within window range
            if len(self.buffer) == 0:
                return
            # Can keep sending if next sequence number - window base <= window size
            window_offset = self.next_seq_num - self.window_base
            is_within_window = window_offset < self.window_size
            # Prevent sending sequence number thats gt buffer
            is_seq_within_buffer = window_offset < len(self.buffer)
            # only dump buffer when seq-base is within window index range
            if is_within_window and is_seq_within_buffer:
                # fetch from buffer and send, increasing next seq num
                next_packet = self.buffer[window_offset]
                pack_num = self.next_seq_num
                self.send(next_packet, pack_num)
                logger.info(f"packet{pack_num} {next_packet} sent")
                self.next_seq_num += 1

    def handle_incoming_stats(self, message, metadata):
        """Handles incoming `stats` message type."""
        self.init_gbn_state()
        if self.on_stats:
            self.on_stats(message, metadata)

    def handle_incoming_ack(self, sender_ip, sock, metadata):
        """Handle incoming `ack` message type."""
        pack_num = itemgetter("packet_num")(metadata)

        # Handle DROPS based on mode resolution
        if self.should_drop(pack_num):
            self.dropped_packets += 1
            self.dropped_packet_numbers.append(pack_num)
            logger.info(f"ACK{pack_num} discarded")
            return

        # base should ONLY increase if pack_num matches sender base next seq num
        if pack_num != self.window_base:
            logger.info(f"ACK{pack_num} dropped, at base {self.window_base}")
            return
        # remove original message from buffer
        with self.buffer_lock:
            self.buffer.pop(pack_num - self.window_base)
        # increase window base from removed message in buffer
        self.window_base += 1
        logger.info(f"ACK{pack_num} received, window moves to {self.window_base}")

    def should_drop(self, pack_num):
        """Determines whether current packet is dropped based on config."""
        if self.mode == "-p":
            # drop when the prob matches probabilistic
            return decision(self.mode_value)
        else:
            # drop when the incoming index matches deterministic mode value
            is_drop_index = pack_num % self.mode_value == 0 and pack_num != 0
            already_dropped_pack_num = pack_num in self.dropped_packet_numbers
            return is_drop_index and not already_dropped_pack_num

    def handle_incoming_message(self, sender_ip, sock, payload, metadata):
        """Handle incoming `message` message type."""
        metadata, message = itemgetter("metadata", "payload")(payload)
        total_message, pack_num = itemgetter("total_message", "packet_num")(metadata)

        logger.info(f"packet{pack_num} {message} received")

        # Handle DROPS based on mode resolution
        if self.should_drop(pack_num):
            self.dropped_packets += 1
            self.dropped_packet_numbers.append(pack_num)
            logger.info(f"packet{pack_num} {message} discarded")
            return

        # Handle ACK ONLY if incoming message matches incoming seq num
        if pack_num > self.incoming_seq_num:
            logger.info(f"packet{pack_num} {message} dropped")
            return

        if pack_num < self.incoming_seq_num:
            logger.info(
                f"dup ACK{pack_num} sent, expecting packet{self.incoming_seq_num}"
            )
            client_port = itemgetter("port")(metadata)
            ack_metadata = {
                "packet_num": pack_num,
                "total_message": total_message,
            }
            ack_message = encode(self.create_gbn_message("ack", None, ack_metadata))
            sock.sendto(ack_message, (sender_ip, client_port))
            return

        # increase incoming seq num
        self.incoming_seq_num += 1
        logger.info(f"ACK{pack_num} sent, expecting packet{self.incoming_seq_num}")
        self.acked_packets += 1

        if pack_num == 0 and self.last_acked == 0:
            self.partial_message = message
        elif pack_num > self.last_acked:
            self.partial_message += message
            self.last_acked = pack_num

        # send ACK to recv'er
        client_port = itemgetter("port")(metadata)
        ack_metadata = {"packet_num": pack_num, "total_message": total_message}
        ack_message = encode(self.create_gbn_message("ack", None, ack_metadata))
        sock.sendto(ack_message, (sender_ip, client_port))

        # Check if we've hit end
        if self.partial_message == total_message:
            total_packets = self.dropped_packets + self.acked_packets
            stats_data = {
                "dropped_packets": self.dropped_packets,
                "total_packets": total_packets,
            }
            logger.info(get_stats_message(**stats_data))
            stats_message = encode(self.create_gbn_message("stats", stats_data))
            sock.sendto(stats_message, (sender_ip, client_port))
            self.init_gbn_state()

    def demux_incoming_message(self, sock, sender_ip, payload):
        """Sends ACK based on configured drop rate."""
        metadata, message, type = itemgetter("metadata", "payload", "type")(payload)
        if type == "stats":
            self.handle_incoming_stats(message, metadata)
            return
        elif type == "ack":
            self.handle_incoming_ack(sender_ip, sock, metadata)
            return
        elif type == "message":
            self.handle_incoming_message(sender_ip, sock, payload, metadata)

    @deadloop
    def sender_timer(self):
        """Attaches to last sent message and if no ACK is recv'ed within interval resends & resets."""
        # do nothing if nothing outbound is being awaited
        with self.buffer_lock:
            if len(self.buffer) == 0:
                return

        # First message was recv'ed; we're ok
        pre_timer_base = self.window_base
        time.sleep(TIMER_SLEEP_INTERVAL)
        if self.window_base > pre_timer_base:
            return

        # handle resend logic (send whats remaining in window)
        logger.info(f"packet{self.window_base} timeout")
        with self.buffer_lock:
            # window_base and next_seq_num are constantly inc thus we get relative position
            messages_to_send = self.buffer[0 : self.next_seq_num - self.window_base]
            packet_seq_num = self.window_base
            for packet in messages_to_send:
                self.send(packet, packet_seq_num)
                logger.info(f"packet{packet_seq_num} {packet} sent")
                packet_seq_num += 1

    def handle_command(self, user_input):
        """Parses user plaintext and sends to proper destination."""

        # Pattern match inputs to command methods
        if re.match("send (.*)", user_input):
            # Push to queue
            message = " ".join(user_input.split(" ")[1:])
            self.total_message = message
            packets = list(message)
            with self.buffer_lock:
                self.buffer.extend(packets)
        else:
            logger.info(f"Unknown command `{user_input}`.")


class GBNode:
    def __init__(self, port, peer_port, window_size, mode, mode_value):
        self.stop_event = Event()
        self.node = GenericGBNode(
            port,
            peer_port,
            window_size,
            mode,
            mode_value,
            self.stop_event,
            self.on_send,
            self.on_stats,
        )

        self.client = SocketClient(
            port, self.stop_event, self.node.demux_incoming_message
        )

    def on_stats(self, message, metadata):
        logger.info(get_stats_message(**message))

    def on_send(self, message, peer_port):
        """Wraps generic GBN for sending."""
        self.client.send(message, peer_port)

    def start_gbn_threads(self):
        """Starts listener, buffer sender and timeout sender threads."""
        # Listens for incoming UDP messages
        Thread(target=self.client.listen).start()
        # Deadloops and dumps buffer to UDP socket when free
        Thread(target=self.node.send_buffer).start()
        # start outbound send timer listener
        Thread(target=self.node.sender_timer).start()

    @handles_signal
    def start(self):
        """Start threads, and listen for input."""
        self.start_gbn_threads()
        # User input parsing (root of GBN sending)
        while not self.stop_event.is_set():
            user_input = input(f"node> ")
            self.node.handle_command(user_input)


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
    return int(self_port), int(peer_port), int(window_size)


def parse_mode(args):
    """Validate `-p` or `-d` flag and arg."""
    if len(args) != 2:
        raise InvalidArgException("-p,-d only accepts <value>")
    mode, mode_value = args
    if mode != "-p" and mode != "-d":
        raise InvalidArgException(f"{mode} is not a valid mode")
    if mode == "-d" and not mode_value.isdigit():
        raise InvalidArgException(f"<value> must be a valid digit")
    mode_val_float = float(mode_value)
    if mode_val_float != 0 and not mode_val_float:
        raise InvalidArgException(f"{mode} <value> must be a valid digit")
    return mode, float(mode_value)


def parse_mode_and_go():
    """Validate root mode args: `-d` or `-p`."""
    args = parse_help_message(gbn_help_message)
    # validate common base args
    self_port, peer_port, window_size = parse_args(args[:3])
    # valid deterministic or probabilistic args
    mode, mode_value = parse_mode(args[3:])
    # Construct main GBN sender class
    sender = GBNode(self_port, peer_port, window_size, mode, mode_value)
    # Listen for input and send to peer
    sender.start()


if __name__ == "__main__":
    """Start client and handle root errors.

    Example usage:
    $ clear && python src/gbnnode.py 5000 5001 1 -p 0.5
    $ clear && python src/gbnnode.py 5001 5000 1 -p 0.5
    """
    try:
        parse_mode_and_go()
    except InvalidArgException as e:
        print(e)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Quitting.")
        sys.exit(1)
