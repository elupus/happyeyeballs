# Happy Eyeballs Connector

A basic happy eyeballs connector.

## What is Happy Eyeballs?

The Happy Eyeballs algorithm (RFC 8305) is designed to improve user experience when connecting to hosts that support both IPv4 and IPv6. It attempts connections to both address families in parallel, reducing connection delays caused by unreachable addresses. The algorithm interleaves connection attempts and uses non-blocking sockets to quickly establish a connection with the first available address.

## Usage

Install the package and import the connector:

```python
from happyeyeballs import connect_host

# Connect to a host and port using the happy eyeballs algorithm
with connect_host("example.com", 80, timeout=4.0) as sock:
    # The returned socket keeps its default timeout (normally blocking), set one as needed
    sock.settimeout(10.0)
    sock.send(b"GET / HTTP/1.0\r\nHost: example.com\r\n\r\n")
    response = sock.recv(4096)
```

To connect to any of several hosts, for example addresses of the same
device found via mDNS, use `connect_hosts`. Hosts that fail to resolve are
skipped, duplicate addresses are removed, and the addresses of the remaining
hosts are interleaved by family:

```python
from happyeyeballs import connect_hosts

sock = connect_hosts(["192.168.1.10", "fe80::1%en0", "2001:db8::10"], 8009, timeout=5.0)
```

Already resolved `getaddrinfo` results can be passed to `connect_addresses`,
which attempts them in the given order.

### Parameters

- `timeout`: overall time limit in seconds for the connection attempts, or
  `None` (the default) for no limit. Must be positive. Name resolution is not
  bounded by the timeout.
- `delay`: seconds to wait for a pending attempt before starting the next
  one (default 0.25, as recommended by RFC 8305, minimum 0.01). An attempt
  that fails starts the next one immediately.
- `type`: defaults to `socket.SOCK_STREAM`.
- `all_errors`: see below.

### Errors

When all attempts fail, the error of the earliest address in the given order
is raised, regardless of which attempt failed first. All errors are `OSError`
subclasses, so callers can use `except OSError`.
When the timeout expires, a `TimeoutError` is raised. The other errors are
attached as notes.

With `all_errors=True`, a `FailedToConnect` exception group containing all
errors in address order (including any `TimeoutError`) is raised instead.

Passing no hosts or addresses raises `ValueError`.

## Features

- Interleaves IPv4 and IPv6 addresses for connection attempts.
- Uses non-blocking sockets and selectors for efficient connection handling.
- Returns the first successfully connected socket and closes all others.
- Supports scoped link-local IPv6 addresses, like `fe80::1%en0`.
