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


def get_stats_message(dropped_packets, total_packets):
    """Prints stats message on both ends based on GBN loss data."""
    loss = dropped_packets / total_packets
    return f"[Summary] {dropped_packets}/{total_packets} packets discarded, loss rate = {loss}%"
