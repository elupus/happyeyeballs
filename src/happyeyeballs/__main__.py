import logging
from . import connect_host

LOG = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.DEBUG)
    LOG.info("Starting")

    with connect_host("::1", 80):
        LOG.info("Connected")

    with connect_host("localhost", 80):
        LOG.info("Connected")


if __name__ == "__main__":
    main()
