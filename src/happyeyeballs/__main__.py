import logging
import socket
from . import connect_host

LOG = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.DEBUG)
    LOG.info("Starting")

    with connect_host("localhost", 80, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP, timeout=0):
        LOG.info("Connected")

main()
