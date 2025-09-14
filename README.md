# Happy Eyeballs Connector

A basic happy eyeballs connector.

## What is Happy Eyeballs?

The Happy Eyeballs algorithm (RFC 8305) is designed to improve user experience when connecting to hosts that support both IPv4 and IPv6. It attempts connections to both address families in parallel, reducing connection delays caused by unreachable addresses. The algorithm interleaves connection attempts and uses non-blocking sockets to quickly establish a connection with the first available address.

## Usage

Install the package and import the connector:

```python
import socket
from happyeyeballs import connect_host

# Connect to a host and port using the happy eyeballs algorithm
with connect_host(
    host="example.com",
    port=80,
    type=socket.SOCK_STREAM,
    proto=socket.IPPROTO_TCP,
    timeout=4.0
) as sock:
    # Use the socket as needed
    sock.send(b"GET / HTTP/1.0\r\nHost: example.com\r\n\r\n")
    response = sock.recv(4096)
```

## Features

- Interleaves IPv4 and IPv6 addresses for connection attempts.
- Uses non-blocking sockets and selectors for efficient connection handling.
- Returns the first successfully connected socket or raises an exception if all attempts fail.
