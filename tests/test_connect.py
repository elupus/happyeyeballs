import errno
import socket
import threading
import time
from typing import Any

import pytest

from happyeyeballs import (
    AddressInfoTuple,
    AddressTuple,
    FailedToConnect,
    connect_addresses,
    connect_host,
    connect_hosts,
)

ADDR_1 = ("192.0.2.1", 8009)
ADDR_2 = ("2001:db8::1", 8009, 0, 0)
ADDR_3 = ("192.0.2.3", 8009)


def info(address: AddressTuple) -> AddressInfoTuple:
    family = socket.AF_INET6 if len(address) == 4 else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", address)


class FakeSocket(socket.socket):
    """Socket backed by a socketpair, emulating connect per a behaviour table"""

    def __init__(self, behaviours: dict[Any, str], default: str) -> None:
        a, b = socket.socketpair()
        super().__init__(fileno=a.detach())
        self.peer = b
        self.behaviours = behaviours
        self.default = default
        self.so_error = 0
        self.address: Any = None
        self.timer: threading.Timer | None = None

    def connect(self, address: Any) -> None:
        self.address = address
        behaviour = self.behaviours.get(address, self.default)
        if behaviour == "ok_now":
            return
        if behaviour == "refused_now":
            raise ConnectionRefusedError(errno.ECONNREFUSED, "refused")
        if behaviour == "pending_ok":
            raise BlockingIOError(errno.EINPROGRESS, "in progress")
        if behaviour == "pending_refused":
            self.so_error = errno.ECONNREFUSED
            raise BlockingIOError(errno.EINPROGRESS, "in progress")
        if behaviour == "stall":
            self._fill()
            raise BlockingIOError(errno.EINPROGRESS, "in progress")
        if behaviour == "slow_refused":
            # stall, then become writable with an error later
            self._fill()
            self.so_error = errno.ECONNREFUSED
            self.timer = threading.Timer(0.2, self._drain)
            self.timer.start()
            raise BlockingIOError(errno.EINPROGRESS, "in progress")
        raise AssertionError(f"Unknown behaviour {behaviour}")

    def _fill(self) -> None:
        # fill send buffer so socket is not writable
        try:
            while True:
                self.send(b"x" * 1024)
        except BlockingIOError:
            pass

    def _drain(self) -> None:
        self.peer.setblocking(False)
        try:
            while self.peer.recv(65536):
                pass
        except OSError:
            pass

    def getsockopt(self, level: int, optname: int, *args: Any) -> Any:
        if level == socket.SOL_SOCKET and optname == socket.SO_ERROR:
            return self.so_error
        return super().getsockopt(level, optname, *args)

    def close(self) -> None:
        if self.timer:
            self.timer.cancel()
        super().close()
        self.peer.close()


class Factory:
    def __init__(self, behaviours: dict[Any, str], default: str = "ok_now") -> None:
        self.behaviours = behaviours
        self.default = default
        self.sockets: list[FakeSocket] = []
        self.calls: list[tuple[int, int, int]] = []

    def __call__(self, family: int, type: int, proto: int) -> socket.socket:
        self.calls.append((family, type, proto))
        sock = FakeSocket(self.behaviours, self.default)
        self.sockets.append(sock)
        return sock

    def assert_others_closed(self, winner: socket.socket | None) -> None:
        for sock in self.sockets:
            if sock is not winner:
                assert sock.fileno() == -1


def connect(factory: Factory, *addresses: AddressTuple, **kwargs: Any) -> FakeSocket:
    sock = connect_addresses(
        [info(address) for address in addresses], socket_factory=factory, **kwargs
    )
    assert isinstance(sock, FakeSocket)
    return sock


def test_immediate_success() -> None:
    factory = Factory({ADDR_1: "ok_now"})
    with connect(factory, ADDR_1, ADDR_2) as sock:
        assert sock.address == ADDR_1
        assert sock.getblocking()
        assert sock.gettimeout() is None
    assert len(factory.sockets) == 1


def test_refused_then_success() -> None:
    factory = Factory({ADDR_1: "refused_now", ADDR_2: "pending_ok"})
    start = time.monotonic()
    with connect(factory, ADDR_1, ADDR_2, delay=5) as sock:
        assert sock.address == ADDR_2
        factory.assert_others_closed(sock)
    assert time.monotonic() - start < 1


def test_pending_refused_starts_next_directly() -> None:
    factory = Factory({ADDR_1: "pending_refused", ADDR_2: "pending_ok"})
    start = time.monotonic()
    with connect(factory, ADDR_1, ADDR_2, delay=5) as sock:
        assert sock.address == ADDR_2
        factory.assert_others_closed(sock)
    assert time.monotonic() - start < 1


def test_stall_then_second_wins_after_delay() -> None:
    factory = Factory({ADDR_1: "stall", ADDR_2: "pending_ok"})
    start = time.monotonic()
    with connect(factory, ADDR_1, ADDR_2, ADDR_3, delay=0.1) as sock:
        elapsed = time.monotonic() - start
        assert sock.address == ADDR_2
        factory.assert_others_closed(sock)
    assert 0.1 <= elapsed < 1
    assert len(factory.sockets) == 2


def test_all_stall_timeout() -> None:
    factory = Factory({}, default="stall")
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        connect(factory, ADDR_1, ADDR_2, delay=0.05, timeout=0.3)
    assert 0.3 <= time.monotonic() - start < 1
    assert len(factory.sockets) == 2
    factory.assert_others_closed(None)


def test_timeout_limits_attempts() -> None:
    factory = Factory({}, default="stall")
    with pytest.raises(TimeoutError):
        connect(factory, ADDR_1, ADDR_2, ADDR_3, delay=0.2, timeout=0.1)
    assert len(factory.sockets) == 1
    factory.assert_others_closed(None)


def test_timeout_all_errors() -> None:
    factory = Factory({ADDR_1: "refused_now"}, default="stall")
    with pytest.raises(FailedToConnect) as exc_info:
        connect(factory, ADDR_1, ADDR_2, timeout=0.1, all_errors=True)
    assert [type(exc) for exc in exc_info.value.exceptions] == [
        ConnectionRefusedError,
        TimeoutError,
    ]


def test_all_fail_raises_first() -> None:
    factory = Factory({ADDR_1: "pending_refused", ADDR_2: "refused_now"})
    with pytest.raises(ConnectionRefusedError) as exc_info:
        connect(factory, ADDR_1, ADDR_2)
    assert f"Address: {ADDR_1}" in exc_info.value.__notes__
    factory.assert_others_closed(None)


def test_errors_in_address_order() -> None:
    factory = Factory({ADDR_1: "slow_refused", ADDR_2: "refused_now"})
    with pytest.raises(ConnectionRefusedError) as exc_info:
        connect(factory, ADDR_1, ADDR_2, delay=0.05)
    assert f"Address: {ADDR_1}" in exc_info.value.__notes__

    factory = Factory({ADDR_1: "slow_refused", ADDR_2: "refused_now"})
    with pytest.raises(FailedToConnect) as group_info:
        connect(factory, ADDR_1, ADDR_2, delay=0.05, all_errors=True)
    assert [exc.__notes__[0] for exc in group_info.value.exceptions] == [
        f"Address: {ADDR_1}",
        f"Address: {ADDR_2}",
    ]


def test_all_fail_all_errors() -> None:
    factory = Factory({ADDR_1: "pending_refused", ADDR_2: "refused_now"})
    with pytest.raises(FailedToConnect) as exc_info:
        connect(factory, ADDR_1, ADDR_2, all_errors=True)
    assert len(exc_info.value.exceptions) == 2
    assert all(
        isinstance(exc, ConnectionRefusedError) for exc in exc_info.value.exceptions
    )


def test_empty() -> None:
    with pytest.raises(ValueError):
        connect_addresses([])
    with pytest.raises(ValueError):
        connect_hosts([], 8009)


@pytest.mark.parametrize("timeout", [0, -1])
def test_invalid_timeout(timeout: float) -> None:
    with pytest.raises(ValueError):
        connect(Factory({}), ADDR_1, timeout=timeout)


def test_keyboard_interrupt_propagates() -> None:
    factory = Factory({ADDR_1: "stall"})

    def interrupting_factory(family: int, type: int, proto: int) -> socket.socket:
        if factory.sockets:
            raise KeyboardInterrupt
        return factory(family, type, proto)

    with pytest.raises(KeyboardInterrupt):
        connect_addresses(
            [info(ADDR_1), info(ADDR_2)],
            delay=0.01,
            socket_factory=interrupting_factory,
        )
    factory.assert_others_closed(None)


def test_connect_host_stream_only() -> None:
    factory = Factory({}, default="refused_now")
    with pytest.raises(ConnectionRefusedError) as exc_info:
        connect_host("localhost", 8009, socket_factory=factory)
    assert factory.calls
    assert all(type == socket.SOCK_STREAM for _, type, _ in factory.calls)
    assert "Host: 'localhost', Port: 8009" in exc_info.value.__notes__


def test_connect_hosts_skips_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host == "bad.example":
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    factory = Factory({})
    with connect_hosts(
        ["bad.example", "192.0.2.1"], 8009, socket_factory=factory
    ) as sock:
        assert isinstance(sock, FakeSocket)
        assert sock.address == ("192.0.2.1", 8009)

    with pytest.raises(socket.gaierror):
        connect_hosts(["bad.example"], 8009, socket_factory=factory)

    with pytest.raises(FailedToConnect):
        connect_hosts(["bad.example"], 8009, socket_factory=factory, all_errors=True)


def test_connect_hosts_interleaves() -> None:
    factory = Factory({}, default="refused_now")
    with pytest.raises(FailedToConnect):
        connect_hosts(
            ["192.0.2.1", "192.0.2.2", "2001:db8::1"],
            8009,
            socket_factory=factory,
            all_errors=True,
        )
    assert [sock.address[0] for sock in factory.sockets] == [
        "192.0.2.1",
        "2001:db8::1",
        "192.0.2.2",
    ]


def test_scoped_link_local() -> None:
    index, name = socket.if_nameindex()[0]
    factory = Factory({})
    with connect_host(f"fe80::1%{name}", 8009, socket_factory=factory) as sock:
        assert isinstance(sock, FakeSocket)
        assert sock.address == ("fe80::1", 8009, 0, index)


def test_real_localhost() -> None:
    try:
        server = socket.create_server(("127.0.0.1", 0))
    except PermissionError:
        pytest.skip("Not allowed to bind ports")
    with server:
        port = server.getsockname()[1]
        with connect_host("127.0.0.1", port, timeout=5) as sock:
            assert sock.getpeername() == ("127.0.0.1", port)
            assert sock.getblocking()
