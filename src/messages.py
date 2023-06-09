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


dv_help_message = """Dvnode constructs a bellman-ford based distance vector for all nodes in the network.

Flags:
    last:   Last node information in network.

Options:
    <local-port>: Listening port
    <neighbor#-port>: Neighbor's listening port
    <loss-rate-#>: link distance to neighbor

Usage:
    Dvnode [...options] [flags]"""


cn_help_message = """Cnnode leverages GBN and Bellman-Ford to synchronize loss rates between links.

Flags:
    last:   Last node information in network.

Options:
    <local-port>: Listening port
    receive: Current node is probe receiver for subsequent neighbors
    <neighbor#-port>: Neighbor's listening port
    <loss-rate-#>: link distance to neighbor
    send: Current node is probe sender for subsequent neighbors
    <neighbor-port>: Neighbor's listening port (receiver for probe)
    
Usage:
    Cnnode [...options] [flags]"""


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
