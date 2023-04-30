# bt2597 PA2

# Packet
- ACK packet has no data
- Reg packet has data @ 1 byte MAX each

# Buffer
- sending DATA ONLY packets put in buffer before send & removed when recieved
- size of buffer relative to window size
- if buffer full SENDER WAITS UNTIL SPACE AVAILBLE

# Window
- moves w/ buffer. Wrap when end reached.

# Timer
- starts after 1st packet in window sent; Stops when ACK for 1st recv'd
- when window moves, if 1st packet sent timer restarts otherwise stop & wait for 1st to get sent
- timeout is 500ms & all packets in window resent AFTER TIMEOUT


## Workflow

1. Basic send,recv HAPPY CASE
2. Send w/ det/prob fails

1. Take input and print incoming messages (2 threads)
2. Create buffer logic w/ lock
3. Timer logic impl w/ buffer

Logic

1. thread1 listen STDIN -> push to buffer
2. thread2 listen buffer -> if >0 AND within window size send to client
3. thread3 timeout 500ms and check window_base was ACK'ED
4. thread4 listen INCOMING sock handling ACK AND drop logic (deter/rand)
