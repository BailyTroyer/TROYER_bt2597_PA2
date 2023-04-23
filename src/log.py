import logging
from queue import Queue
from logging.handlers import QueueHandler, QueueListener

que = Queue(-1)  # no limit on size
queue_handler = QueueHandler(que)
handler = logging.StreamHandler()
# handler.terminator = "\r"
listener = QueueListener(que, handler)
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)
logger.addHandler(queue_handler)
# uncomment if debugging thread sources in logs
# formatter = logging.Formatter("%(threadName)s: >>> [%(message)s]")
formatter = logging.Formatter("[%(msecs)s] [%(message)s]")
handler.setFormatter(formatter)
listener.start()
