import sys
from operator import itemgetter
import time
import json
from threading import Thread, Event, Lock
from log import logger
import signal
import socket
import select

from messages import parse_help_message, cn_help_message
from utils import (
    InvalidArgException,
    valid_port,
    SocketClient,
    handles_signal,
    deadloop,
)
from gbnnode import GBNode, GenericGBNode
from dvnode import DVNode

LOSS_RATE_PRINT_INTERVAL = 1


class LinkError(Exception):
    """Thrown when Link errors during regular operation."""

    pass


class CNLink:
    def __init__(self, port, recv_neighbors, send_neighbors):
        self.port = port
        self.recv_neighbors = recv_neighbors
        self.send_neighbors = send_neighbors

        self.stop_event = Event()

        # wipe loss for recv_neighbors since we start with zero loss.
        empty_neighbors = [{"port": n["port"], "loss": 0} for n in recv_neighbors]
        # include self in neighbors
        empty_neighbors.append({"port": port, "loss": 0})
        self.dv_node = DVNode(port, empty_neighbors, self.demux_incoming_dv_message)

        self.sending_probes_lock = Lock()
        self.sending_probes = {}

        self.loss_rates_lock = Lock()
        self.loss_rates = {}

        self.recv_gbnodes = {}
        for recv in recv_neighbors:
            recv_port, loss = itemgetter("port", "loss")(recv)
            self.start_gbn_listener(recv_port, loss)

    def start_gbn_listener(self, recv_port, loss):
        gbnode = GenericGBNode(
            self.port,
            recv_port,
            5,
            "-p",
            loss,
            self.stop_event,
            self.on_send_gbn,
        )
        self.recv_gbnodes[recv_port] = gbnode

    def on_send_gbn(self, message, peer_port):
        print(f"should send {message} to {peer_port}")

    def create_probe_message(self, type):
        message_metadata = {"port": self.port}
        return {"type": type, "metadata": message_metadata}

    def link_cost(self, sender_port):
        """Calculates link cost for a given neighbor link."""
        ## @TODO DO THIS
        # default to zero when no probes have been sent
        # cost=dropped/sent

    @deadloop
    def print_loss_rate(self):
        """Prints loss rate for neighbors every 1s."""
        with self.loss_rates_lock:
            for port, data in self.loss_rates.items():
                sent, lost, rate = itemgetter("sent", "lost", "rate")(data)
                logger.info(f"Link to {port}: {sent} sent, {lost} lost, loss {rate}")
        time.sleep(LOSS_RATE_PRINT_INTERVAL)

    def on_stats(self, message, metadata):
        with self.sending_probes_lock:
            self.sending_probes[metadata.get("port")] = False
        print(f"got stats for {metadata}")

    @deadloop
    def handle_send_probes(self):
        """Sends probes at specified interval to proper neighbors. Also starts 1s loss rate prints."""
        # send probes to each send neighbor
        for send_neighbor_port in self.send_neighbors:
            with self.sending_probes_lock:
                if not self.sending_probes.get(send_neighbor_port, False):
                    ## @TODO SEND PROBE HERE via GBNode
                    gb_node = GenericGBNode(
                        self.port,
                        send_neighbor_port,
                        5,
                        "-p",
                        0,
                        self.stop_event,
                        self.dv_node.send,
                        self.on_stats,
                    )
                    gb_node.handle_command("send probe")

    def demux_incoming_dv_message(self, _payload):
        """Callback when DV recv'es message."""
        # Kickoff probe sender and loss rate printer when initial DV is sent
        with self.sending_probes_lock:
            if self.sending_probes == {}:
                Thread(target=self.handle_send_probes).start()
                Thread(target=self.print_loss_rate).start()

    @handles_signal
    def listen(self, should_start):
        """Listens for incoming neighbor vectors. If `should_start` kicks off cascading DV updates."""
        self.dv_node.listen(should_start)


def parse_args(args):
    """Validates local port and neighbor options."""
    local_port, neighbor_args = args[0], args[1:]

    # check local port
    if not valid_port(local_port):
        raise InvalidArgException(
            f"Invalid <local-port>: {local_port}; Must be within 1024-65535"
        )

    is_last = args[-1] == "last"
    neighbor_args = neighbor_args[:-1] if is_last else neighbor_args

    if len(neighbor_args) == 0:
        raise InvalidArgException(
            "Specify at least one group of options: `receive <neighbor#-port> <loss-rate-#> send <neighbor#-port>`"
        )

    if not ("receive" in neighbor_args):
        raise InvalidArgException(
            "Must specify keyword `receive` before neighbors even if none defined"
        )
    if not ("send" in neighbor_args):
        raise InvalidArgException(
            "Must specify keyword `send` before neighbors even if none defined"
        )

    send_idx = neighbor_args.index("send")
    receive_args = neighbor_args[1:send_idx]
    send_args = neighbor_args[send_idx + 1 :]

    if len(receive_args) % 2 != 0:
        raise InvalidArgException(
            "receive options must be in pairs of 2: `<neighbor#-port> <loss-rate-#>`"
        )

    recv_neighbors = []
    for idx, neighbor_arg in enumerate(receive_args):
        # 1st is port, 2nd is loss
        if idx % 2 == 0:
            if not valid_port(neighbor_arg):
                raise InvalidArgException(
                    f"Invalid <neighbor#-port>: {neighbor_arg}; Must be within 1024-65535"
                )
            recv_neighbors.append({"port": int(neighbor_arg)})
        else:
            if not float(neighbor_arg):
                raise InvalidArgException(
                    f"Invalid <loss-rate-#>: {neighbor_arg}; Must be a valid floating number"
                )
            recv_neighbors[max(idx - 2, 0)]["loss"] = float(neighbor_arg)

    send_neighbors = []
    for neighbor_arg in send_args:
        if not valid_port(neighbor_arg):
            raise InvalidArgException(
                f"Invalid send <neighbor#-port>: {neighbor_arg}; Must be within 1024-65535"
            )
        send_neighbors.append(neighbor_arg)

    return int(local_port), recv_neighbors, send_neighbors, is_last


def parse_mode_and_go():
    """Validate send/receive neighbor options and check for end flag."""
    args = parse_help_message(cn_help_message)
    # validate args
    port, recv_neighbors, send_neighbors, is_last = parse_args(args)
    link = CNLink(port, recv_neighbors, send_neighbors)
    link.listen(is_last)


if __name__ == "__main__":
    """Start link and handle root errors.

    Example usage:
    $ clear && python src/cnnode.py 1111 receive send 2222 3333
    $ clear && python src/cnnode.py 2222 receive 1111 .1 send 3333 4444
    $ clear && python src/cnnode.py 3333 receive 1111 .5 2222 .2 send 4444
    $ clear && python src/cnnode.py 4444 receive 2222 .8 3333 .5 send last
    """
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
