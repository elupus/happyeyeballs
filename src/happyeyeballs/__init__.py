import socket
import selectors
from typing import cast, Callable, Iterable, Iterator, NoReturn
import logging
import os
import time
from contextlib import contextmanager
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

DEFAULT_DELAY = 0.25


class FailedToConnect(ExceptionGroup[OSError]):
    """Raised with all collected errors when called with all_errors=True"""


def interleave_family(infos: Iterable[AddressInfoTuple]) -> Iterator[AddressInfoTuple]:
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
    family: socket.AddressFamily | int,
    type: socket.SocketKind | int,
    proto: int,
) -> socket.socket:
    """Default socket provider"""
    return socket.socket(family=family, type=type, proto=proto)


def connect_host(
    host: bytes | str | None,
    port: bytes | str | int | None,
    *,
    family: socket.AddressFamily | int = 0,
    type: socket.SocketKind | int = socket.SOCK_STREAM,
    proto: int = 0,
    flags: int = 0,
    delay: float = DEFAULT_DELAY,
    timeout: float | None = None,
    all_errors: bool = False,
    socket_factory: SocketFactory = default_socket_factory,
) -> socket.socket:
    """Connect to given host and port, using happy eyeball algorithm

    See connect_hosts for details.
    """

    try:
        return connect_hosts(
            [host],
            port,
            family=family,
            type=type,
            proto=proto,
            flags=flags,
            delay=delay,
            timeout=timeout,
            all_errors=all_errors,
            socket_factory=socket_factory,
        )
    except (OSError, FailedToConnect) as exc:
        exc.add_note(f"Host: {host!r}, Port: {port!r}")
        raise


def connect_hosts(
    hosts: Iterable[bytes | str | None],
    port: bytes | str | int | None,
    *,
    family: socket.AddressFamily | int = 0,
    type: socket.SocketKind | int = socket.SOCK_STREAM,
    proto: int = 0,
    flags: int = 0,
    delay: float = DEFAULT_DELAY,
    timeout: float | None = None,
    all_errors: bool = False,
    socket_factory: SocketFactory = default_socket_factory,
) -> socket.socket:
    """Connect to any of the given hosts on port, using happy eyeball algorithm

    Each host is resolved using getaddrinfo, hosts that fail to resolve are
    skipped. The resolved addresses are interleaved by family and connected
    to as described in connect_addresses.

    The timeout only bounds the connection attempts, name resolution is
    not bounded by it.

    If no host resolves, the first resolution error is raised, or a
    FailedToConnect with all of them if all_errors is set.
    """

    infos: list[AddressInfoTuple] = []
    unresolved: list[OSError] = []
    for host in hosts:
        try:
            infos.extend(
                socket.getaddrinfo(
                    host, port, family=family, type=type, proto=proto, flags=flags
                )
            )
        except socket.gaierror as exc:
            exc.add_note(f"Failed to resolve host: {host!r}")
            unresolved.append(exc)

    if not infos:
        if not unresolved:
            raise ValueError("No hosts to connect to")
        _raise_errors("Failed to resolve any host", unresolved, all_errors)

    try:
        return connect_addresses(
            interleave_family(infos),
            delay=delay,
            timeout=timeout,
            all_errors=all_errors,
            socket_factory=socket_factory,
        )
    except (OSError, FailedToConnect) as exc:
        for error in unresolved:
            exc.add_note(f"Skipped unresolved host: {error!r}")
        raise


def connect_addresses(
    addresses: Iterable[AddressInfoTuple],
    *,
    delay: float = DEFAULT_DELAY,
    timeout: float | None = None,
    all_errors: bool = False,
    socket_factory: SocketFactory = default_socket_factory,
) -> socket.socket:
    """Connect to given list of addresses, using happy eyeball algorithm

    Addresses are attempted in the given order. A new attempt is started
    when the previous one fails, or after delay seconds while it is still
    pending. The first connected socket is returned, in blocking mode with
    no timeout set, call settimeout() on it as needed.

    timeout is the overall time limit for connecting, None means no limit.

    If all attempts fail, the error of the earliest address in the given
    order is raised, regardless of which attempt failed first. When the
    timeout expires a TimeoutError is raised. The other errors are attached
    as notes. With all_errors set, a FailedToConnect with all errors in
    address order is raised instead, including any TimeoutError.

    Raises ValueError if there are no addresses to attempt.
    """

    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be positive or None")

    # errors keyed by address index, since attempts
    # can fail in a different order than started
    exceptions: dict[int, OSError] = {}
    pending = enumerate(addresses)
    exhausted = False

    starting = time.monotonic()
    deadline = None if timeout is None else starting + timeout

    with _pending_sockets() as selector:
        while True:
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                timeout_error = TimeoutError(
                    f"Timed out connecting after {timeout} seconds"
                )
                _raise_errors(
                    "Failed to connect",
                    [*_ordered(exceptions), timeout_error],
                    all_errors,
                    primary=timeout_error,
                )

            # see if we have a new address to work with
            # starting of the connection process
            if not exhausted:
                if (item := next(pending, None)) is None:
                    exhausted = True
                else:
                    index, info = item
                    LOG.debug(
                        "Adding potential %s after %s seconds", info, now - starting
                    )

                    started = _start_connect(index, info, socket_factory, exceptions)
                    if started is None:
                        # since this socket failed directly
                        # attempt to add a new socket directly
                        continue

                    sock, connected = started
                    if connected:
                        return sock
                    selector.register(sock, selectors.EVENT_WRITE, (index, info[4]))

            # if there are no pending sockets,
            # there is nothing more to check, so
            # exit loop and raise exceptions
            if not selector.get_map():
                break

            # wait for any socket being writable, or until
            # it's time for the next attempt. Writable
            # indicates either error or connected socket.
            wait = _wait_time(None if exhausted else delay, deadline)
            for key, _ in selector.select(wait):
                sock = cast(socket.socket, key.fileobj)
                index, address = key.data
                selector.unregister(sock)

                with _collect_error(exceptions, index, address):
                    _finish_connect(sock)
                    return sock

    if not exceptions:
        raise ValueError("No addresses to connect to")

    _raise_errors("Failed to connect", _ordered(exceptions), all_errors)


@contextmanager
def _pending_sockets() -> Iterator[selectors.BaseSelector]:
    """Selector for pending sockets, closing any still pending on exit"""
    selector = selectors.DefaultSelector()
    try:
        yield selector
    finally:
        for key in selector.get_map().values():
            cast(socket.socket, key.fileobj).close()
        selector.close()


@contextmanager
def _close_on_error(sock: socket.socket) -> Iterator[None]:
    """Close socket on any error"""
    try:
        yield
    except BaseException:
        sock.close()
        raise


@contextmanager
def _collect_error(
    exceptions: dict[int, OSError], index: int, address: AddressTuple
) -> Iterator[None]:
    """Collect socket errors for the address at index"""
    try:
        yield
    except OSError as exc:
        exc.add_note(f"Address: {address}")
        exceptions[index] = exc


def _start_connect(
    index: int,
    info: AddressInfoTuple,
    socket_factory: SocketFactory,
    exceptions: dict[int, OSError],
) -> tuple[socket.socket, bool] | None:
    """Start a non-blocking connect

    Returns the socket and whether it's already connected,
    or None if the attempt failed and the error was collected.
    """
    family, sock_type, proto, _, address = info
    with _collect_error(exceptions, index, address):
        sock = socket_factory(family, sock_type, proto)
        with _close_on_error(sock):
            sock.setblocking(False)
            try:
                sock.connect(address)
            except BlockingIOError:
                return sock, False
            sock.setblocking(True)
        return sock, True
    return None


def _finish_connect(sock: socket.socket) -> None:
    """Check result of a pending connect on a writable socket"""
    with _close_on_error(sock):
        if error := sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
            raise OSError(error, os.strerror(error))
        sock.setblocking(True)


def _wait_time(delay: float | None, deadline: float | None) -> float | None:
    """Time to wait, limited by the deadline"""
    if deadline is None:
        return delay
    remain = max(0.0, deadline - time.monotonic())
    return remain if delay is None else min(delay, remain)


def _ordered(exceptions: dict[int, OSError]) -> list[OSError]:
    return [exc for _, exc in sorted(exceptions.items())]


def _raise_errors(
    message: str,
    errors: list[OSError],
    all_errors: bool,
    primary: OSError | None = None,
) -> NoReturn:
    if all_errors:
        raise FailedToConnect(message, errors)

    # default to raising the error of the most preferred address
    if primary is None:
        primary = errors[0]
    for other in errors:
        if other is not primary:
            primary.add_note(f"Also failed: {other!r}")
    raise primary
