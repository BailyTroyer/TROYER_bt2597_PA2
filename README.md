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

### GBN

#### Sending basic message with spaces deterministically

We send a message of `a b c` which drops every 2nd packet. We see in the output from both sides that the resulting summary is printed out and shows 3/8 packets were discarded which makes sense granted we initially sent 5 packets in `a{sp}b{sp}c` and the `a`,`b`,`c` packets were discarded resulting in a total of 8 packets being sent out including retrans.

Due to the rich data in the logs there aren't many separate test cases needed since we can see the following is working in the example:

1. Spaces are properly sent
2. Logs are printed on sender when messages are sent, packets timeout, acks are recv'ed and window is moved
3. Logs are printed on recv'er when packet is recv'ed, dropped (from drop mode p,d), discarded (when already recv'ed) and acks sent.
4. A summary is printed on both sides showing the packets discarded and the resulting loss rate (which matches closely to the deterministic value passed in)

**Client 1:**

```
$ python src/gbnnode.py 5000 5001 5 -d 2
node> send a b c
[793.2689189910889] [packet0 a sent]
[793.4410572052002] [packet1   sent]
[793.5888767242432] [packet2 b sent]
[793.7500476837158] [packet3   sent]
[793.8470840454102] [packet4 c sent]
[304.4459819793701] [packet0 timeout]
[304.7192096710205] [packet0 a sent]
[304.86083030700684] [packet1   sent]
[304.99792098999023] [packet2 b sent]
[305.09305000305176] [packet3   sent]
[305.1729202270508] [packet4 c sent]
[322.04699516296387] [ACK0 received, window moves to 1]
[322.1099376678467] [ACK1 received, window moves to 2]
[322.13616371154785] [ACK2 received, window moves to 3]
[322.1619129180908] [ACK3 received, window moves to 4]
[328.04203033447266] [ACK4 received, window moves to 5]
[333.9221477508545] [[Summary] 3/8 packets discarded, loss rate = 0.375%]
```

**Client 2:**

```
$ python src/gbnnode.py 5001 5000 5 -d 2
node> [798.6359596252441] [packet0 a discarded]
[798.7198829650879] [packet1   received]
[798.7399101257324] [packet1   dropped]
[798.7689971923828] [packet2 b discarded]
[805.264949798584] [packet3   received]
[805.3150177001953] [packet3   dropped]
[805.4900169372559] [packet4 c discarded]
[315.40513038635254] [packet0 a received]
[315.52696228027344] [ACK0 sent, expecting packet1]
[321.9258785247803] [packet1   received]
[321.96617126464844] [ACK1 sent, expecting packet2]
[322.0219612121582] [packet2 b received]
[322.04222679138184] [ACK2 sent, expecting packet3]
[322.0810890197754] [packet3   received]
[322.0980167388916] [ACK3 sent, expecting packet4]
[322.145938873291] [packet4 c received]
[322.1628665924072] [ACK4 sent, expecting packet5]
[322.2830295562744] [[Summary] 3/8 packets discarded, loss rate = 0.375%]
```

#### Sending basic message probabilistically with message greater than window

Similar to the previous deterministic case we can try with -p for 20% drops.

The logs are quite verbose but we see a few things in this test case:

1. When a packet is discarded the subsequent packets are recv'ed but then dropped until the proper resends are triggered on the sending side. Here `packet0` is discarded and its only until `packet0` times out on the sender that packets are properly recv'ed again at recv'er time `680.1950931549072` which logs `[packet0 1 received]`
2. Timeouts are working, which we see when the first packets are sent and `packet0` times out since it was originally dropped
3. Only the window is sent out initially (packet0-4) and its not until proper acks are rec'ved before the next packet4+ are sent.

**Client 1:**

```
$ python src/gbnnode.py 5000 5001 5 -d 2
node> send 1234567890
node> send 1234567890
[153.55706214904785] [packet0 1 sent]
[153.7189483642578] [packet1 2 sent]
[153.97405624389648] [packet2 3 sent]
[154.14905548095703] [packet3 4 sent]
[154.20222282409668] [packet4 5 sent]
[664.7369861602783] [packet0 timeout]
[677.2429943084717] [packet0 1 sent]
[677.4101257324219] [packet1 2 sent]
[677.5679588317871] [packet2 3 sent]
[677.699089050293] [packet3 4 sent]
[677.8008937835693] [packet4 5 sent]
[690.0429725646973] [ACK0 received, window moves to 1]
[690.2010440826416] [packet5 6 sent]
node> [697.3178386688232] [packet1 timeout]
[697.5140571594238] [packet1 2 sent]
[697.5569725036621] [packet2 3 sent]
[697.5910663604736] [packet3 4 sent]
[697.6261138916016] [packet4 5 sent]
[697.6580619812012] [packet5 6 sent]
[710.3989124298096] [ACK1 received, window moves to 2]
[710.564136505127] [packet6 7 sent]
[735.1529598236084] [ACK2 received, window moves to 3]
[735.3310585021973] [packet7 8 sent]
[747.556209564209] [ACK3 received, window moves to 4]
[747.877836227417] [packet8 9 sent]
[778.6140441894531] [ACK4 received, window moves to 5]
[778.8228988647461] [packet9 0 sent]
[785.1121425628662] [ACK5 received, window moves to 6]
[791.2800312042236] [ACK6 received, window moves to 7]
[719.6600437164307] [packet7 timeout]
[719.8450565338135] [packet7 8 sent]
[719.9411392211914] [packet8 9 sent]
[720.0849056243896] [packet9 0 sent]
[746.4759349822998] [ACK7 received, window moves to 8]
[765.6099796295166] [ACK8 received, window moves to 9]
[784.0499877929688] [ACK9 received, window moves to 10]
[784.2109203338623] [[Summary] 4/14 packets discarded, loss rate = 0.2857142857142857%]
```

**Client 2:**

```
$ python src/gbnnode.py 5001 5000 5 -d 2
node> [172.25003242492676] [packet0 1 discarded]
[185.03284454345703] [packet1 2 received]
[185.09793281555176] [packet1 2 dropped]
[185.2109432220459] [packet2 3 received]
[185.2710247039795] [packet2 3 dropped]
[190.89508056640625] [packet3 4 received]
[190.9470558166504] [packet3 4 dropped]
[191.08819961547852] [packet4 5 discarded]
[680.1950931549072] [packet0 1 received]
[680.2759170532227] [ACK0 sent, expecting packet1]
[718.2309627532959] [packet1 2 discarded]
[718.4090614318848] [packet2 3 received]
[718.4460163116455] [packet2 3 dropped]
[718.5070514678955] [packet3 4 received]
[718.5299396514893] [packet3 4 dropped]
[718.5730934143066] [packet4 5 received]
[718.6539173126221] [packet4 5 dropped]
[718.7259197235107] [packet5 6 received]
[718.7449932098389] [packet5 6 dropped]
[703.3100128173828] [packet1 2 received]
[703.4120559692383] [ACK1 sent, expecting packet2]
[722.3501205444336] [packet2 3 received]
[722.4230766296387] [ACK2 sent, expecting packet3]
[747.1470832824707] [packet3 4 received]
[747.2269535064697] [ACK3 sent, expecting packet4]
[766.2270069122314] [packet4 5 received]
[766.3090229034424] [ACK4 sent, expecting packet5]
[778.7649631500244] [packet5 6 received]
[778.8219451904297] [ACK5 sent, expecting packet6]
[785.4199409484863] [packet6 7 received]
[785.4859828948975] [ACK6 sent, expecting packet7]
[785.6450080871582] [packet7 8 discarded]
[785.693883895874] [packet8 9 received]
[785.7110500335693] [packet8 9 dropped]
[785.7420444488525] [packet9 0 received]
[785.7558727264404] [packet9 0 dropped]
[727.647066116333] [packet7 8 received]
[727.7309894561768] [ACK7 sent, expecting packet8]
[733.8199615478516] [packet8 9 received]
[733.8690757751465] [ACK8 sent, expecting packet9]
[734.1041564941406] [packet9 0 received]
[734.1790199279785] [ACK9 sent, expecting packet10]
[740.2219772338867] [[Summary] 4/14 packets discarded, loss rate = 0.2857142857142857%]
```

### DV

@TODO

### CN

## Callouts

### GBN

For deterministically based dropping we run a modulus to determine when a packet gets dropped. So if you specify -d 3 then the 0th, 3rd, 6th, etc will get dropped.
