import socket
import json
import select
from functools import wraps
from log import logger
from threading import Lock


class InvalidArgException(Exception):
    """Thrown when CLI input arguments don't match expected type/structure/order."""

    pass


def valid_port(value):
    """Validate port matches expected range 1024-65535."""
    if value.isdigit():
        val = int(value)
        return val >= 1024 and val <= 65535
    return False


class SocketClientError(Exception):
    """Thrown when Client errors during regular operation."""

    pass


def deadloop(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        while True:
            if self.stop_event.is_set():
                logger.info("stopping deadloop")
                break

            method(self, *args, **kwargs)

        return

    return wrapper


def decode(message):
    """Convert bytes to deserialized JSON."""
    return json.loads(message.decode("utf-8"))


def encode(message):
    """Convert dict to serialized JSON."""
    return json.dumps(message).encode("utf-8")


class SocketClient:
    def __init__(self, listen_port, stop_event, on_message_fn):
        self.sock = self._create_sock()
        self.sock_lock = Lock()
        self.stop_event = stop_event
        self.on_message_fn = on_message_fn

        self.sock.bind(("", listen_port))

    def _create_sock(self):
        """Create a socket."""
        try:
            return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except socket.error as e:
            raise SocketClientError(f"UDP client error when creating socket: {e}")

    @deadloop
    def listen(self):
        """Listens for messages."""
        readables, _, _ = select.select([self.sock], [], [], 1)
        for read_socket in readables:
            data, (sender_ip, _) = read_socket.recvfrom(4096)
            message = decode(data)
            self.on_message_fn(read_socket, sender_ip, message)

    def send(self, message, port, ip="0.0.0.0"):
        """Sends a single packet onto UDP socket."""
        try:
            # logger.info(f"sending {message} to {port} @ {ip}")
            packet = encode(message)
            with self.sock_lock:
                self.sock.sendto(packet, (ip, port))
        except socket.error as e:
            raise SocketClientError(f"UDP socket error: {e}")
