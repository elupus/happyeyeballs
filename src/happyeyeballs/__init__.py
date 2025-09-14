import socket
import selectors
from typing import cast, Iterator, Iterable, Callable
import logging
import errno
import contextlib
import time
from collections import defaultdict, deque

type AddressTuple = tuple[str, int] | tuple[str, int, int, int] | tuple[int, bytes]

type AddressInfoTuple = tuple[
    socket.AddressFamily,
    socket.SocketKind,
    int,
    str,
    AddressTuple,
]

type SocketFactory = Callable[
    [socket.AddressFamily | int, socket.SocketKind | int, int], socket.socket
]

LOG = logging.getLogger(__name__)


class FailedToConnect(ExceptionGroup):
    pass


def interleave_family(infos: list[AddressInfoTuple]):
    """Interleave the address families of the given info while retaining order"""

    grouped: dict[int, deque[AddressInfoTuple]] = defaultdict(deque)
    for info in infos:
        grouped[info[0]].append(info)

    while True:
        exhausted: list[int] = []
        for family, values in grouped.items():
            if values:
                yield values.popleft()
            else:
                exhausted.append(family)

        for family in exhausted:
            grouped.pop(family)

        if not grouped:
            break


def default_socket_factory(
    family: socket.AddressFamily | int = -1,
    type: socket.SocketKind | int = -1,
    proto: int = -1,
) -> socket.socket:
    """Default socket provider"""
    return socket.socket(family=family, type=type, proto=proto)


def connect_host(
    host: bytes | str | None,
    port:  bytes | str | int | None,
    *,
    family: socket.AddressFamily | int = 0,
    type: socket.SocketKind | int = 0,
    proto: int = 0,
    flags: int = 0,
    delay: float = 0.3,
    timeout: float = 0.0,
    socket_factory: SocketFactory = default_socket_factory,
) -> socket.socket:
    """Connect to given host and port, using happy eyeball algorithm"""

    addresses = socket.getaddrinfo(
        host, port, family=family, type=type, proto=proto, flags=flags
    )
    try:
        return connect_addresses(
            interleave_family(addresses),
            timeout=timeout,
            delay=delay,
            socket_factory=socket_factory,
        )
    except Exception as exc:
        exc.add_note(f"Host: {host}, Port: {port}")
        raise exc


def connect_addresses(
    addresses: Iterator[AddressInfoTuple] | Iterable[AddressInfoTuple],
    *,
    delay: float = 0.3,
    timeout: float = 0.0,
    socket_factory: SocketFactory = default_socket_factory,
) -> socket.socket:
    """Connect to given list of addresses, using happy eyeball algorithm"""

    selector = selectors.DefaultSelector()

    exceptions: list[BaseException] = []
    addresses = iter(addresses)

    starting = time.time()

    try:
        while True:
            now = time.time()
            # see if we have a new address to work with
            # starting of the connection process
            if info := next(addresses, None):
                address = info[4]

                LOG.debug("Adding potential %s after %s seconds", info, now - starting)

                try:
                    fd = socket_factory(info[0], info[1], info[2])
                except BaseException as exc:
                    exceptions.append(exc)
                else:
                    try:
                        fd.setblocking(False)
                        fd.connect(address)
                        fd.setblocking(True)
                        return fd
                    except BlockingIOError:
                        selector.register(fd, selectors.EVENT_WRITE, address)
                    except BaseException as exc:
                        exc.add_note(f"Address: {address}")
                        exceptions.append(exc)
                        with contextlib.suppress(OSError):
                            fd.close()

                        # since this socket failed directly
                        # attempt to add a new socket directly
                        continue

            # if there are no pending sockets,
            # there is nothing more to check, so
            # exit loop and raise exceptions
            if len(selector.get_map()) == 0:
                break

            if timeout:
                remain = max(0, timeout - (now - starting))
                delay = min(delay, remain)

            # wait for any socket being writable
            # this will indicate either error or
            # connectable socket. Since this will
            # remove the socket from pending sockets
            # we will just add a new one directly
            # if this was a failure.
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
                    fd.setblocking(True)
                    return fd
                except BaseException as exc:
                    exc.add_note(f"Address: {address}")
                    exceptions.append(exc)
                    with contextlib.suppress(OSError):
                        fd.close()

            # if delay is now zero, we have timeout condition
            if delay == 0:
                exc = TimeoutError("Timeout on connection")
                exc.add_note(f"Timeout: {timeout}")
                raise exc

    finally:
        # clean up all pending sockets
        for key in selector.get_map().values():
            cast(socket.socket, key.fileobj).close()
        selector.close()

    raise FailedToConnect("Failed to connect to target", exceptions)
