from log import logger
import select
import socket
import json
from threading import Thread, Event
import signal

import sys


class ClientError(Exception):
    """Thrown when Client errors during regular operation."""

    pass


class Sender:
    def __init__(self, mode, opts):
        self.mode = mode
        self.opts = opts

    def send_packet(self, sock, packet):
        """Sends a single packet onto UDP socket."""
        client_port, client_ip = self.mode["peer_port"], "0.0.0.0"

        try:
            client_destination = (client_ip, client_port)
            sock.sendto(packet, client_destination)
        except socket.error as e:
            raise ClientError(f"UDP socket error: {e}")

    def send_message(self, sock, message):
        """Sends a string chunked into seprate char packets."""
        packets = list(message)
        print("SENDING ... ", packets)

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

    def server_listen(self, stop_event):
        """Listens on specified `client_port` for messages from server."""
        sock = self.create_sock()
        sock.bind(("", self.opts["client_port"]))

        sent_initial_register = False

        while True:
            # Listen for kill events
            if stop_event.is_set():
                logger.info("stopping peer-port listener")
                break

            readables, writables, errors = select.select([sock], [], [], 1)
            for read_socket in readables:
                # logger.info("listening")
                data, (sender_ip, sender_port) = read_socket.recvfrom(4096)
                message = self.decode_message(data)
                # self.handle_request(read_socket, sender_ip, message)

    def start(self):
        """Start both the user input listener and peer event listener."""
        try:
            # Handle signal events (e.g. `^C`)
            self.stop_event = Event()
            signal.signal(signal.SIGINT, self.signal_handler)
            # start server listener
            server_thread = Thread(target=self.server_listen, args=(self.stop_event,))
            server_thread.start()
            # Init sock for outbound messages
            sock = self.create_sock()
            # Deadloop input 'till client ends
            while server_thread.is_alive() and not self.stop_event.is_set():
                server_thread.join(1)
                user_input = input(f">>> ")
                self.send_message(sock, user_input)
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

    return {"self_port": self_port, "peer_port": peer_port, "window_size": window_size}


def parse_mode(args):
    """Validate `-p` or `-d` flag and arg."""

    if len(args) != 2:
        raise InvalidArgException("-p,-d only accepts <value>")
    mode, mode_value = args
    if mode != "-p" and mode != "-d":
        raise InvalidArgException(f"{mode} is not a valid mode")
    if not float(mode_value):
        raise InvalidArgException(f"{mode} <value> must be a valid digit")

    return {"mode": mode, "mode_value": mode_value}


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
    sender = Sender({**mode_opts, **opts}, opts)
    sender.start()


if __name__ == "__main__":
    """ """
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
