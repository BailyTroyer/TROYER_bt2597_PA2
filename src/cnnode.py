import sys
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
)


class LinkError(Exception):
    """Thrown when Link errors during regular operation."""

    pass


class CNLink:
    def __init__(self, opts):
        # CLI args
        print("------")
        self.opts = opts
        self.ip = "0.0.0.0"
        self.distance_vector_lock = Lock()
        # { local_port: {loss, hops} } for each links local_port
        self.distance_vector = self.create_distance_vector(
            self.opts["recv_neighbors"], self.opts["send_neighbors"]
        )
        self.is_sending_probes_lock = Lock()
        self.is_sending_probes = False
        self.loss_rate_print_interval = 1  # 1s
        self.loss_rates_lock = Lock()
        # { port: {sent,lost,rate} }
        self.loss_rates = {}

    def print_updated_vector(self, vec):
        """Prints the updated distance vector."""
        logger.info(f"[{time.time()}] Node {self.opts['local_port']} Routing Table")
        for k, v in vec.items():
            loss, hops = v.get("loss"), v.get("hops")
            hops_messages = " ; ".join([f"Next hop -> {hop}" for hop in hops])
            combined_hops_message = f"; {hops_messages}" if hops else ""
            logger.info(f"- ({loss}) -> Node {k}{combined_hops_message}")

    def create_distance_vector(self, recv_neighbors, send_neighbors):
        """Creates first distance vector with recv,send neighbors."""
        # NOTE: recv_neighbors are [{ port, loss }] whereas send_neighbors are [port]
        vector = {}
        for neighbor in recv_neighbors:
            port = neighbor.get("port")
            # hops are empty since its from current node on init
            vector[int(port)] = {"loss": 0, "hops": []}
        for neighbor_port in send_neighbors:
            vector[int(neighbor_port)] = {"loss": 0, "hops": []}
        self.print_updated_vector(vector)
        return vector

    def create_sock(self):
        """Create a socket."""
        try:
            return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except socket.error as e:
            raise LinkError(f"UDP link error when creating socket: {e}")

    def signal_handler(self, signum, _frame):
        """Custom wrapper that throws error when exit signal received."""
        print()  # this adds a nice newline when `^C` is entered
        self.stop_event.set()
        raise LinkError(f"Link aborted... {signum}")

    def decode_message(self, message):
        """Convert bytes to deserialized JSON."""
        return json.loads(message.decode("utf-8"))

    def encode_message(self, type, payload=None):
        """Convert plaintext user input to serialized message 'packet'."""
        message_metadata = {**self.opts}
        message = {"type": type, "payload": payload, "metadata": message_metadata}
        return json.dumps(message).encode("utf-8")

    def sync_distance_vector(self, incoming_port, incoming, existing):
        """Updates distance vector based on updated neighbor vectors."""
        # This really helped solidify my understanding:
        # https://www.youtube.com/watch?v=00AAnwgl2DI&ab_channel=Udacity

        incoming_port_loss = existing[incoming_port]["loss"]

        for port, port_data in incoming.items():
            loss_rate, hops = port_data.get("loss"), port_data.get("hops")
            port_ = int(port)

            if port_ == int(self.opts["local_port"]):
                continue

            incoming_port_loss_rate = round(
                float(incoming_port_loss) + float(loss_rate), 2
            )

            existing_port_loss_rate = existing.get(port_)
            # add to dict summing the incoming port's loss in current vector
            if not existing_port_loss_rate:
                existing[port_] = {
                    "loss": incoming_port_loss_rate,
                    "hops": [incoming_port],
                }
            else:
                if incoming_port_loss_rate < existing_port_loss_rate.get("loss"):
                    existing[port_]["loss"] = incoming_port_loss_rate
                    ## @TODO UPDATE HOPS FOR n hops, not just one
                    existing[port_]["hops"] = [incoming_port]

        # check if port exists in current (if not, compute sum of the source and its weight)
        # e.g. if we have { 1025: 0.05, 1027: 0.03 } with incoming { 1024: 0.05, 1026: 0.03 } & source

        return existing

    def handle_incoming_message(self, sock, payload):
        """Handles incoming neighbors message."""
        metadata, message, type = (
            payload.get("metadata"),
            payload.get("payload"),
            payload.get("type"),
        )

        print("got message")

        if type != "dv":
            logger.info(f"Received invalid message type: {type}. Expecting ONLY `dv`")
            return

        with self.is_sending_probes_lock:
            if not self.is_sending_probes:
                self.is_sending_probes = True
                probe_thread = Thread(
                    target=self.handle_send_probes, args=(self.stop_event,)
                )
                probe_thread.start()

        incoming_dv, incoming_port = message.get("vector"), metadata.get("local_port")
        local_port = self.opts["local_port"]

        logger.info(f"Message received at Node {local_port} from Node {incoming_port}")

        with self.distance_vector_lock:
            new_distance_vector = self.sync_distance_vector(
                incoming_port, incoming_dv, self.distance_vector.copy()
            )

            # we print regardless if it results in new dispatch
            self.print_updated_vector(new_distance_vector)

            # If changed update and dispatch
            if self.distance_vector != new_distance_vector:
                self.distance_vector = new_distance_vector
                self.dispatch_dv(sock, new_distance_vector)

    def neighbor_listen(self, stop_event):
        """Listens on specified `local_port` for messages from other links."""
        sock = self.create_sock()
        sock.bind(("", self.opts["local_port"]))

        while True:
            # Listen for kill events
            if stop_event.is_set():
                logger.info("stopping listener")
                break

            readables, writables, errors = select.select([sock], [], [], 1)
            for read_socket in readables:
                data, (sender_ip, sender_port) = read_socket.recvfrom(4096)
                message = self.decode_message(data)
                self.handle_incoming_message(read_socket, message)

    def link_cost(self, sender_port):
        """Calculates link cost for a given neighbor link."""
        ## @TODO DO THIS
        # default to zero when no probes have been sent
        # cost=dropped/sent

    def send_dv(self, sock, port, dv):
        """Sends current distance vector to neighbor."""
        logger.info(f"Message sent from Node {self.opts['local_port']} to Node {port}")
        dv_message = self.encode_message("dv", {"vector": dv})
        sock.sendto(dv_message, (self.ip, int(port)))

    def dispatch_dv(self, sock, dv):
        """Sends distance vector to neighbors in bulk."""
        for neighbor_port, _loss in dv.items():
            if neighbor_port != self.opts["local_port"]:
                self.send_dv(sock, neighbor_port, dv)

    def print_loss_rate(self, stop_event):
        """Prints loss rate for neighbors every 1s."""

        print("loss rate printer ticking...")

        while True:
            # Listen for kill events
            if stop_event.is_set():
                logger.info("stopping listener")
                break

            with self.loss_rates_lock:
                for port, data in self.loss_rates.items():
                    sent, lost, rate = (
                        data.get("sent"),
                        data.get("lost"),
                        data.get("rate"),
                    )
                    logger.info(
                        f"Link to {port}: {sent} packets sent, {lost} packets lost, loss rate {rate}"
                    )

            time.sleep(self.loss_rate_print_interval)

    def handle_send_probes(self, stop_event):
        """Sends probes at specified interval to proper neighbors. Also starts 1s loss rate prints."""

        sock = self.create_sock()

        print("handle send probes ... ")

        # loss rate printer
        loss_rate_thread = Thread(target=self.print_loss_rate, args=(self.stop_event,))
        loss_rate_thread.start()
        loss_rate_thread.join()

        while True:
            # Listen for kill events
            if stop_event.is_set():
                logger.info("stopping listener")
                break

            # send probes to each send neighbor
            for send_neighbor_port in self.opts["send_neighbors"]:
                cost = self.link_cost(send_neighbor_port)
                ## @TODO WHAT DOES COST HAVE TO DO HERE SINCE SEND NEIGHBORS DON'T CONFIGURE THAT
                probe_message = self.encode_message("probe", {"cost": cost})
                sock.sendto(probe_message, (self.ip, send_neighbor_port))

    def listen(self, should_start):
        """Listens for incoming neighbor vectors. If `should_start` kicks off cascading DV updates."""
        try:
            # Handle signal events (e.g. `^C`)
            self.stop_event = Event()
            signal.signal(signal.SIGINT, self.signal_handler)
            # start server listener
            neighbor_thread = Thread(
                target=self.neighbor_listen, args=(self.stop_event,)
            )
            neighbor_thread.start()
            # send kickoff if CLI specified `last`
            if should_start:
                sock = self.create_sock()

                ## DV update messages sent to neighbors
                ## triggers neighbors to send DV messages
                ## 1st time getting DV message network is ready AND SEND PROBES

                ## DV updates every 5s if changes
                ## DV updates when update recv from neighbor

                ### updates sent to neighbor nodes + probe sender/recv'er
                ### probe recv'er ONLY get calc'ed dist of links through updates sent by probe senders

                #####
                ### 1. Probe packets ONLY to send list
                ### 2. DV to both send/recv list
                ### 3. Packet los rate every 1s (link to <>: <> packets sent, <> packets lost, loss rate <>)

                ## TODO:
                # 0. link_cost (0 init, dropped/sent)
                # 1. send probes (func and logic)
                # 2. DV logic from p2, GBN from p1
                #####

                ## @TODO DEFINE KICKOFF HERE
                with self.distance_vector_lock:
                    self.dispatch_dv(sock, self.distance_vector)

            neighbor_thread.join()
        except LinkError:
            # Prevent exceptions when quickly spamming `^C`
            signal.signal(signal.SIGINT, lambda s, f: None)


def valid_port(value):
    """Validate port matches expected range 1024-65535."""
    if value.isdigit():
        val = int(value)
        return val >= 1024 and val <= 65535
    return False


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
    local_port, recv_neighbors, send_neighbors, is_last = parse_args(args)

    opts = {
        "local_port": local_port,
        "recv_neighbors": recv_neighbors,
        "send_neighbors": send_neighbors,
    }

    link = CNLink(opts)
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
