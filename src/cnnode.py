import sys


help_message = """Cnnode leverages GBN and Bellman-Ford to synchronize loss rates between links.

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
    args = sys.argv[1:]
    if len(args) == 0:
        raise InvalidArgException(help_message)

    # validate args
    local_port, recv_neighbors, send_neighbors, is_last = parse_args(args)
    print(local_port, recv_neighbors, send_neighbors, is_last)


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
