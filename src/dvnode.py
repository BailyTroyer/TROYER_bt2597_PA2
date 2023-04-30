import sys
import time
import json
from threading import Thread, Event, Lock
from log import logger
import signal
import socket
import select

## port > 1024
## Max nodes 16
## Static Link/node
## Non negative links
## NON-DIRECTIONAL


## Dest is listening port, source is arbitrary UDP
## **Data Field: sending UDP listen port, most recent routing table**

### Lost/Out of Order: timestamp/sequence to each packet


class LinkError(Exception):
    """Thrown when Link errors during regular operation."""

    pass


class Link:
    def __init__(self, opts):
        # CLI args
        print("------")
        self.opts = opts
        self.ip = "0.0.0.0"
        self.distance_vector_lock = Lock()
        # { local_port: loss } for each links local_port
        self.distance_vector = self.create_distance_vector(self.opts["neighbors"])

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

    def sync_distance_vector(
        self, incoming_port, incoming_distance_vector, old_distance_vector
    ):
        """Updates distance vector based on updated neighbor vectors."""
        # This really helped solidify my understanding:
        # https://www.youtube.com/watch?v=00AAnwgl2DI&ab_channel=Udacity

        incoming_port_loss = old_distance_vector[incoming_port]

        for port, loss_rate in incoming_distance_vector.items():
            port_ = int(port)
            # ignore entry where its current local_port @TODO RIGHT??
            if port_ == int(self.opts["local_port"]):
                continue

            incoming_port_loss_rate = round(
                float(incoming_port_loss) + float(loss_rate), 2
            )

            existing_port_loss_rate = old_distance_vector.get(port_)
            # add to dict summing the incoming port's loss in current vector
            if not existing_port_loss_rate:
                old_distance_vector[port_] = incoming_port_loss_rate
            else:
                if incoming_port_loss_rate < existing_port_loss_rate:
                    old_distance_vector[port_] = incoming_port_loss_rate

        # check if port exists in current (if not, compute sum of the source and its weight)
        # e.g. if we have { 1025: 0.05, 1027: 0.03 } with incoming { 1024: 0.05, 1026: 0.03 } & source

        return old_distance_vector

    def print_updated_vector(self, vec):
        """Prints the updated distance vector."""
        ## @TODO next hop in prints
        logger.info(f"[{time.time()}] Node {self.opts['local_port']} Routing Table")
        for k, v in vec.items():
            logger.info(f"- ({v}) -> Node {k}")

    def create_distance_vector(self, neighbors):
        """Creates first distance vector with starting neighbors."""
        vector = {}
        for neighbor in neighbors:
            port, loss = neighbor.get("port"), neighbor.get("loss")
            vector[port] = loss
        self.print_updated_vector(vector)
        return vector

    def handle_incoming_message(self, sock, sender_ip, payload):
        """Handles incoming neighbors distance vector."""
        metadata, message, type = (
            payload.get("metadata"),
            payload.get("payload"),
            payload.get("type"),
        )

        if type != "dv":
            logger.info(f"Received invalid message type: {type}. Expecting ONLY `dv`")
            return

        incoming_distance_vector = message.get("vector")
        incoming_port = metadata.get("local_port")
        # logger.info(
        #     f"Message {incoming_distance_vector} received at Node {self.opts['local_port']} from Node {incoming_port}"
        # )

        logger.info(
            f"Message received at Node {self.opts['local_port']} from Node {incoming_port}"
        )

        ## @TODO UPDATE DISTANCE VECTOR HERE AND HANDLE BELLMAN-FORD + SENDING UPDATES TO NEIGHBORS IF CHANGED
        with self.distance_vector_lock:
            new_distance_vector = self.sync_distance_vector(
                incoming_port, incoming_distance_vector, self.distance_vector.copy()
            )

            self.print_updated_vector(new_distance_vector)

            # If changed update and dispatch
            if self.distance_vector != new_distance_vector:
                # print("ITS DIFFERENT... DISPATCHING")
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
                self.handle_incoming_message(read_socket, sender_ip, message)

    def listen(self, should_start):
        """Listens for incoming neighbor vectors. If `should_start` sends initial distance vector to neighbors."""
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
                with self.distance_vector_lock:
                    self.dispatch_dv(sock, self.distance_vector)
            neighbor_thread.join()
        except LinkError:
            # Prevent exceptions when quickly spamming `^C`
            signal.signal(signal.SIGINT, lambda s, f: None)

    def send_dv(self, sock, port, dv):
        """Sends current distance vector to neighbor."""
        logger.info(f"Message sent from Node {self.opts['local_port']} to Node {port}")
        dv_message = self.encode_message("dv", {"vector": dv})
        sock.sendto(dv_message, (self.ip, port))

    def dispatch_dv(self, sock, dv):
        """Sends distance vector to neighbors in bulk."""
        for neighbor_port, _loss in dv.items():
            if neighbor_port != self.opts["local_port"]:
                self.send_dv(sock, neighbor_port, dv)


help_message = """Dvnode constructs a bellman-ford baneighbor_ distance vector for all nodes in the network.

Flags:
    last:   Last node information in network.

Options:
    <local-port>: Listening port
    <neighbor#-port>: Neighbor's listening port
    <loss-rate-#>: link distance to neighbor

Usage:
    Dvnode [...options] [flags]"""


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
    args = sys.argv[1:]
    if len(args) == 0:
        raise InvalidArgException(help_message)

    # validate args
    local_port, neighbors, is_last = parse_args(args)
    # Create link and start if last flag was pasneighbor_ in CLI
    link = Link({"local_port": local_port, "neighbors": neighbors})
    link.listen(is_last)


if __name__ == "__main__":
    """Start link and handle root errors."""
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
