# bt2597 PA2

## Code Architecture

### File Structure

We have the main code inside `src` with docs + supporting tooling inside the root.

```
.
├── README.md
└── src
    ├── __init__.py
    ├── cnnode.py
    ├── dvnode.py
    ├── gbnnode.py
    └── log.py

3 directories, 7 files
```

While the 3 parts are separate (except for p3 combining 1 and 2), there's similar abstractions in the codebase similar to PA1 that we leverage:

1. CLI input validation & parsing
2. Network & Input Communication (Links)
3. Threading

### 1. CLI Input Validation

In [dvnode.py](./src/dvnode.py), [gbnnode.py](./src/gbnnode.py) and [cnnode.py](./src/cnnode.py) we handle the root `parse_mode_and_go` method which handles input validation along with starting the respective logic which listens and creates the required UDP sockets.

A custom exception `InvalidArgException` is used to handle different error states such as invalid argument types, incomplete args and a default message simulating regular terminal CLIs (e.g `kubectl`). Since there was a mandate against public packages I used `sys.argv` instead of a fancier arg parser such as [click](https://click.palletsprojects.com/en/8.1.x/).

### 2. Network & Input Communication

**For all of them:**

1. Kickoff function
2. Signal handling
3. Threaded events (timer, inbound UDP messages)

We have different kickoff functions specified below which handle the initial setup of listener threads (for timers, UDP messages) along with input from user (for GBN specifically).

This is where we also handle joining threads and if other threads are spawned (which spawn other threads) then Ctrl-C will properly get handled by the signal handling below which closes threads.

The signal handling allows us to gracefully exit when the user attempts to force close the running program. However to cancel all threads properly with one SIGINT, we have 2 signal listeners. One for the first call, and another for spamming `^C` to prevent the threading from throwing errors when closing.

**For GBN:**

In [gnnode.py](./src/gnnode.py) we initialize the sender and call `start` which handles:
1. Listening to signals (e.g. `^C`)
2. Starting the ack listen thread (UDP socket)
3. Listening to the user input via `input`

Then we start a separate thread for `server_listen` which endlessly loops (unless a stop event is triggered) for data coming inbound from the server/client.

In the main thread we listen for input and call `handle_command` which handles commmand validation and pattern matching (via regex) to call the necessary utility method corresponding to the command. In this case its only one message type, but could support more in the future.

### 3. Threading

To keep things consistent we have a structure of `{_var_name}_lock` and `{_var_name}` for each variable that needs locking when being shared in threads.

**For GBN:**

We have a lock on the buffer which is written into on the main `server_listen` thread and read in either a timer thread (for GBN timeouts) or in a separate buffer sender thread `send_buffer` that checks if the window is within range to send more data from the buffer to the UDP listener.

**For DV:**

We have a lock on the distance vector object which gets read from two threads:

1. The main kickoff thread in `listen` when the `end` flag is entered in the CLI which reads from the initial DV and sends to the neighbors
2. The listener thread in `handle_incoming_message` that handles incoming messages and updates the DV object based on the result of the BF equation with incoming DVs from neighbors

**For CN:**

@TODO

## Usage

You can get the main structure of the CLI with no args for all three parts:

```sh
$ python src/gbnode.py
GbNode allows you to send chars to a client with defined loss.

Flags:
    -d      Drop packets in a deterministic way
    -p      Drop packets with defined probability

Options:
    <self-port>: Sender port
    <peer-port>: Reciever port
    <window-size>: Size of GBN window

Usage:
    GbNode [flags] [options]
```

```sh
$ python src/dvnode.py
Dvnode constructs a bellman-ford baneighbor_ distance vector for all nodes in the network.

Flags:
    last:   Last node information in network.

Options:
    <local-port>: Listening port
    <neighbor#-port>: Neighbor's listening port
    <loss-rate-#>: link distance to neighbor

Usage:
    Dvnode [...options] [flags]
```

```sh
$ python src/cnnode.py
Cnnode leverages GBN and Bellman-Ford to synchronize loss rates between links.

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
    Cnnode [...options] [flags]
```

### GBN Input Validation

The following example starts a node on self-port 5000, peer-port 5001, a window size of 5 and a deterministic mode of 2 (every 2nd is dropped).

```sh
$ python src/gbnnode.py 5000 5001 5 -d 2
```

If validation fails it will print an "Invalid" message:

```sh
# Missing Mode
$ python src/gbnnode.py 5000 5001 5
-p,-d only accepts <value>

# Invalid mode
$ python src/gbnnode.py 5001 5000 5 -f fdsa
-f is not a valid mode

# Invalid self port format
$ python src/gbnnode.py fdsa 5001 5 -d 2
Invalid <self-port>: fdsa; Must be within 1024-65535

# Invalid peer port format
$ python src/gbnnode.py 5000 fdsa 5 -d 2
Invalid <peer-port>: fdsa; Must be within 1024-65535
```

### DV Input Validation

The following example starts a link on local-port 1027 with a neighbor on port 1024 and a loss rate of 0.05.

```sh
$ python src/dvnode.py 1027 1024 0.05
```

If validation fails it will print an "Invalid" message:

```sh
# Missing Loss Rate
$ python src/dvnode.py 1027 1024
options must be in pairs of 2: `<neighbor#-port> <loss-rate-#>`

# Missing Neighbor port
$ python src/dvnode.py 1027
options must be in pairs of 2: `<neighbor#-port> <loss-rate-#>`

# Invalid local port
$ python src/dvnode.py fdsa 1024 0.05
Invalid <local-port>: fdsa; Must be within 1024-65535
```

### CN Input Validation

The following example starts a link on local-port 222 with a receiver neighbor at 1111 and loss rate 0.1 with a sender neighbor at 3333 and 4444.

```sh
$ python src/cnnode.py 2222 receive 1111 .1 send 3333 4444
```

If validation fails it will print an "Invalid" message:

```sh
# Missing send
$ python src/cnnode.py 2222 receive 1111
Must specify keyword `send` before neighbors even if none defined

# Missing receive
$ python src/cnnode.py 2222
Specify at least one group of options: `receive <neighbor#-port> <loss-rate-#> send <neighbor#-port>`

# Invalid recieve pairs
$ python src/cnnode.py 2222 receive 1111 send
receive options must be in pairs of 2: `<neighbor#-port> <loss-rate-#>`

# Invalid send port
$ python src/cnnode.py 2222 receive 1111 0.01 send fdsa
Invalid send <neighbor#-port>: fdsa; Must be within 1024-65535
```

## Testing
