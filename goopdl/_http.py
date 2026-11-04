"""Shared httpx clients for goopdl's synchronous requests.

``get``/``post`` take the same keyword arguments goopdl passes to
``httpx.get``/``httpx.post`` and return an ``httpx.Response``, but reuse one
``httpx.Client`` (and its keep-alive connections) per proxy instead of opening
a new TCP+TLS connection for every request.
"""

from __future__ import annotations

import atexit
import hashlib
import os
import threading
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any

import httpx

# ponytail: at most 8 clients (one per distinct proxy); the 9th evicts the
# oldest, so dispenser region rotation over more proxies reconnects. Raise the
# cap (or key by proxy host) if a backend rotates many proxies and wants reuse.
_MAX_CLIENTS = 8
# Servers usually keep idle HTTP/1.1 connections longer (nginx defaults to
# 75 s): expiring ours first avoids reusing one the server is closing, and 20 s
# still spans a command's auth handshake chain and the 1-3 s delivery backoffs.
# httpcore also drops idle connections the server already closed.
_LIMITS = httpx.Limits(max_keepalive_connections=20, keepalive_expiry=20.0)
# httpx.get used a throwaway client, so Set-Cookie never reached a later request.
_NO_COOKIES = DefaultCookiePolicy(allowed_domains=[])

_clients: dict[str, httpx.Client] = {}  # sha256(proxy) -> client; never the proxy
_lock = threading.Lock()

ProxyArg = str | httpx.URL | httpx.Proxy | None


def _key(proxy: ProxyArg) -> str:
    if proxy is None:
        return ""
    p: httpx.Proxy = proxy if isinstance(proxy, httpx.Proxy) else httpx.Proxy(proxy)
    material = repr((str(p.url), p.raw_auth, p.headers.raw))
    return hashlib.sha256(material.encode()).hexdigest()


def _client(proxy: ProxyArg) -> httpx.Client:
    key = _key(proxy)
    with _lock:
        client = _clients.get(key)
    if client is not None:
        return client
    # Built outside the lock (loading CA certificates takes a few ms).
    new: httpx.Client | None = httpx.Client(
        proxy=proxy, cookies=CookieJar(_NO_COOKIES), limits=_LIMITS
    )
    evicted = None
    with _lock:
        client = _clients.get(key)
        if client is None:
            client = _clients[key] = new
            new = None
            if len(_clients) > _MAX_CLIENTS:
                evicted = _clients.pop(next(iter(_clients)))
    for unused in (new, evicted):
        if unused is not None:
            unused.close()
    return client


def _request(method: str, url: Any, *, proxy: ProxyArg = None, **kwargs: Any) -> httpx.Response:
    events: list[str] = []
    kwargs["extensions"] = {"trace": lambda name, info: events.append(name)}
    client = _client(proxy)
    try:
        return client.request(method, url, **kwargs)
    except RuntimeError:
        # Evicted and closed by another thread before sending: nothing went out.
        if not client.is_closed:
            raise
    except (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError):
        # Retry once only a reused keep-alive connection the server dropped
        # before answering (closed while we were sending), and only if the
        # request is safe to repeat: a GET, or a POST whose body never went out.
        stale = not any(e.endswith(("connect_tcp.started", "receive_response_headers.complete"))
                        for e in events)
        if not stale or (method != "GET" and "http11.send_request_body.started" in events):
            raise
    # Outside the except: the first error (its .request has the headers) must
    # not be chained to a second failure.
    events.clear()
    return _client(proxy).request(method, url, **kwargs)


def get(url: Any, **kwargs: Any) -> httpx.Response:
    """``httpx.get`` over a shared keep-alive client."""
    return _request("GET", url, **kwargs)


def post(url: Any, **kwargs: Any) -> httpx.Response:
    """``httpx.post`` over a shared keep-alive client."""
    return _request("POST", url, **kwargs)


def close_all() -> None:
    """Close and forget every shared client (atexit, tests)."""
    with _lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        client.close()


def _reset_after_fork() -> None:
    # The child must not inherit a held lock nor share the parent's sockets.
    global _lock
    _clients.clear()
    _lock = threading.Lock()


atexit.register(close_all)
if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)
