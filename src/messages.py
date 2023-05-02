import sys
from utils import InvalidArgException


gbn_help_message = """GbNode allows you to send chars to a client with defined loss.

Flags:
    -d      Drop packets in a deterministic way
    -p      Drop packets with defined probability

Options:
    <self-port>: Sender port
    <peer-port>: Reciever port
    <window-size>: Size of GBN window

Usage:
    GbNode [flags] [options]"""


def parse_help_message(message):
    """Shows message when only 1 arg is specified."""

    args = sys.argv[1:]
    if len(args) == 0:
        raise InvalidArgException(message)

    return args
