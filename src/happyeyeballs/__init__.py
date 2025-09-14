import socket
import selectors
from typing import cast
import time
import logging
import errno


type AddressTuple = tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes]

type AddressInfoTuple = tuple[
    socket.AddressFamily,
    socket.SocketKind,
    int,
    str,
    AddressTuple,
]

LOG = logging.getLogger(__name__)

class FailedToConnect(ExceptionGroup):
    pass

def connect_host(
    host: str,
    port: int,
    family: socket.AddressFamily | int = 0,
    type: socket.SocketKind | int = 0,
    proto: int = 0,
    flags: int = 0,
) -> socket.socket:
    addresses = socket.getaddrinfo(
        host, port, family=family, type=type, proto=proto, flags=flags
    )
    return connect_addresses(addresses)


def connect_addresses(
    addresses: list[AddressInfoTuple],
) -> socket.socket:
    selector = selectors.DefaultSelector()

    exceptions: list[BaseException] = []

    try:
        stage = 0
        delay = 0.3

        while True:
            if stage < len(addresses):
                info = addresses[stage]
                address = info[4]
                stage += 1

                LOG.debug("Adding potential %s", info)

                try:
                    fd = socket.socket(info[0], info[1], info[2])
                except BaseException as exc:
                    exceptions.append(exc)
                else:
                    try:
                        fd.setblocking(False)
                        fd.connect(address)
                        return fd
                    except BlockingIOError:
                        selector.register(fd, selectors.EVENT_WRITE, address)
                    except BaseException as exc:
                        exc.add_note(f"Address: {address}")
                        exceptions.append(exc)
                        fd.close()

            if len(selector.get_map()) == 0:
                break

            for key, event in selector.select(delay):
                if not (event & selectors.EVENT_WRITE):
                    continue

                fd: socket.socket = key.fileobj
                address: AddressTuple = key.data
                selector.unregister(fd)

                try:
                    error = fd.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if error:
                        raise OSError(error, errno.errorcode.get(error, "Unknown"))
                    return fd
                except BaseException as exc:
                    exc.add_note(f"Address: {address}")
                    exceptions.append(exc)
                    fd.close()
    finally:
        for key in selector.get_map().values():
            cast(socket.socket, key.fileobj).close()
        selector.close()

    raise FailedToConnect("Failed to connect to target", exceptions)
