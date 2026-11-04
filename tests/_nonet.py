"""Guard that makes tests fail instead of reaching the real network.

Patch ``socket.socket.connect`` with :func:`no_sockets`. Loopback stays allowed:
on Windows asyncio builds its internal ``socketpair`` with a connect to
127.0.0.1, so blocking every connect would stop an event loop from starting.
"""
import socket

_real_connect = socket.socket.connect
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


def no_sockets(self, address, *args, **kwargs):
    host = address[0] if isinstance(address, tuple) and address else None
    if host in _LOOPBACK:
        return _real_connect(self, address, *args, **kwargs)
    raise AssertionError("real network access attempted")
