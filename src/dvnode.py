import sys
from operator import itemgetter
import time
import json
from threading import Thread, Event, Lock
from log import logger

from messages import parse_help_message, dv_help_message
from utils import (
    InvalidArgException,
    valid_port,
    SocketClient,
    handles_signal,
)


class DVNode:
    def __init__(self, port, neighbors):
        # CLI args
        self.port = port
        self.neighbors = neighbors

        self.ip = "0.0.0.0"
        self.distance_vector_lock = Lock()
        # { local_port: {loss, hops} } for each links local_port
        self.distance_vector = self.create_distance_vector(neighbors)

        self.stop_event = Event()
        self.client = SocketClient(port, self.stop_event, self.demux_incoming_message)

    def create_dv_message(self, type, payload=None):
        """Convert plaintext user input to serialized message 'packet'."""
        message_metadata = {"port": self.port, "neighbors": self.neighbors}
        return {"type": type, "payload": payload, "metadata": message_metadata}

    def sync_distance_vector(self, incoming_port, incoming, existing):
        """Updates distance vector based on updated neighbor vectors."""
        # This really helped solidify my understanding:
        # https://www.youtube.com/watch?v=00AAnwgl2DI&ab_channel=Udacity

        incoming_port_loss = existing[incoming_port]["loss"]

        for port, port_data in incoming.items():
            loss_rate, hops = port_data.get("loss"), port_data.get("hops")
            port_ = int(port)

            if port_ == int(self.port):
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
                    existing[port_]["hops"] = [incoming_port]

        # check if port exists in current (if not, compute sum of the source and its weight)
        # e.g. if we have { 1025: 0.05, 1027: 0.03 } with incoming { 1024: 0.05, 1026: 0.03 } & source
        return existing

    def print_updated_vector(self, vec):
        """Prints the updated distance vector."""
        logger.info(f"[{time.time()}] Node {self.port} Routing Table")
        for k, v in vec.items():
            loss, hops = itemgetter("loss", "hops")(v)
            hops_messages = " ; ".join([f"Next hop -> {hop}" for hop in hops])
            combined_hops_message = f"; {hops_messages}" if hops else ""
            logger.info(f"- ({loss}) -> Node {k}{combined_hops_message}")

    def create_distance_vector(self, neighbors):
        """Creates first distance vector with starting neighbors."""
        vector = {}
        for neighbor in neighbors:
            port, loss = itemgetter("port", "loss")(neighbor)
            # hops are empty since its from current node on init
            vector[port] = {"loss": loss, "hops": []}
        self.print_updated_vector(vector)
        return vector

    def handle_incoming_dv(self, metadata, message):
        """Handles incoming neighbors distance vector."""
        incoming_dv, incoming_port = message.get("vector"), metadata.get("port")
        logger.info(f"Message received at Node { self.port} from Node {incoming_port}")

        with self.distance_vector_lock:
            new_distance_vector = self.sync_distance_vector(
                incoming_port, incoming_dv, self.distance_vector.copy()
            )
            # we print regardless if it results in new dispatch
            self.print_updated_vector(new_distance_vector)
            # If changed update and dispatch
            if self.distance_vector != new_distance_vector:
                self.distance_vector = new_distance_vector
                self.dispatch_dv(new_distance_vector)

    def demux_incoming_message(self, _sock, _sender_ip, payload):
        """Sends ACK based on configured drop rate."""
        metadata, message, type = itemgetter("metadata", "payload", "type")(payload)

        if type != "dv":
            logger.info(f"Received invalid message type: {type}. Expecting ONLY `dv`")
        else:
            self.handle_incoming_dv(metadata, message)

    def dispatch_dv(self, dv):
        """Sends distance vector to neighbors in bulk."""
        for neighbor_port, _loss in dv.items():
            if neighbor_port == self.port:
                continue

            logger.info(f"Message sent from Node {self.port} to Node {neighbor_port}")
            dv_message = self.create_dv_message("dv", {"vector": dv})
            self.client.send(dv_message, neighbor_port, self.ip)

    @handles_signal
    def listen(self, should_start):
        """Listens for incoming neighbor vectors. If `should_start` sends initial distance vector to neighbors."""
        # Listens for incoming UDP messages
        client_listen = Thread(target=self.client.listen)
        client_listen.start()

        # send kickoff if CLI specified `last`
        if should_start:
            with self.distance_vector_lock:
                self.dispatch_dv(self.distance_vector)

        client_listen.join(1)


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
            "Specify at least one group of options: `<neighbor#-port> <loss-rate-#>`"
        )

    if len(neighbor_args) % 2 != 0:
        raise InvalidArgException(
            "options must be in pairs of 2: `<neighbor#-port> <loss-rate-#>`"
        )

    # Sanitize & validate neighbor port,loss list
    neighbors = []
    for idx, neighbor_arg in enumerate(neighbor_args):
        # 1st is port, 2nd is loss
        if idx % 2 == 0:
            if not valid_port(neighbor_arg):
                raise InvalidArgException(
                    f"Invalid <neighbor#-port>: {neighbor_arg}; Must be within 1024-65535"
                )
            neighbors.append({"port": int(neighbor_arg)})
        else:
            if not float(neighbor_arg):
                raise InvalidArgException(
                    f"Invalid <loss-rate-#>: {neighbor_arg}; Must be a valid floating number"
                )
            neighbors[max(idx - 2, 0)]["loss"] = float(neighbor_arg)

    return int(local_port), neighbors, is_last


def parse_mode_and_go():
    """Validate neighbor options and check for end flag."""
    args = parse_help_message(dv_help_message)
    # validate args
    local_port, neighbors, is_last = parse_args(args)
    # Create link and start if last flag was pasneighbor_ in CLI
    link = DVNode(local_port, neighbors)
    link.listen(is_last)


if __name__ == "__main__":
    """Start link and handle root errors.

    Example usage:
    $ clear && python src/dvnode.py 1024 1025 0.01 1027 0.05
    $ clear && python src/dvnode.py 1025 1024 0.01 1026 0.05
    $ clear && python src/dvnode.py 1026 1025 0.05 1027 0.03
    $ clear && python src/dvnode.py 1027 1024 0.05 1026 0.03 last
    """
    try:
        parse_mode_and_go()
    except InvalidArgException as e:
        print(e)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Quitting.")
        sys.exit(1)
