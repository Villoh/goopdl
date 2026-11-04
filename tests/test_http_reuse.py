"""goopdl._http reuses keep-alive connections across synchronous requests.

Hermetic: only loopback servers in this process; every other connect is blocked.
"""
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import httpx

from _nonet import no_sockets
from goopdl import _http, api

SENTINEL = "SENTINEL_USER:SENTINEL_PASS"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 -- base signature
        pass

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.cookies.append(self.headers.get("Cookie"))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.send_header("Set-Cookie", "NID=tracking; Path=/")
        self.end_headers()
        self.wfile.write(b"ok")

    do_GET = do_POST = _reply


class AnswerOnce(Handler):
    """Answer one request, then close the kept-alive connection."""

    def handle(self):
        self.handle_one_request()


class DropSecondRequest(Handler):
    """Answer one request, then close as soon as the next one arrives."""

    def handle(self):
        self.handle_one_request()
        self.rfile.readline()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64  # 16 threads may connect at once

    def __init__(self, handler=Handler):
        super().__init__(("127.0.0.1", 0), handler)
        self.accepted = 0
        self.cookies = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def get_request(self):
        request = super().get_request()
        # Like real servers: http.server writes headers and body separately.
        request[0].setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.accepted += 1
        return request

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}/"

    def stop(self):
        self.shutdown()
        self.server_close()


class HttpReuseTest(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket.connect", no_sockets)
        guard.start()
        self.addCleanup(guard.stop)
        _http.close_all()
        self.addCleanup(_http.close_all)  # before stopping servers (LIFO)

    def server(self, handler=Handler):
        server = Server(handler)
        self.addCleanup(server.stop)
        return server

    def test_sequential_requests_share_one_connection(self):
        reused, fresh = self.server(), self.server()
        for _ in range(10):
            self.assertEqual(_http.get(reused.url, timeout=30).text, "ok")
            self.assertEqual(_http.post(reused.url, content=b"body", timeout=30).text, "ok")
            httpx.get(fresh.url, timeout=30)
        self.assertEqual(reused.accepted, 1)
        self.assertEqual(fresh.accepted, 10)

    def test_concurrent_threads_reuse_the_pool(self):
        server = self.server()
        errors = []

        def work():
            try:
                for _ in range(5):
                    _http.get(server.url, timeout=30)
            except Exception as exc:  # noqa: BLE001 -- reported below
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(server.accepted, 16)  # 80 with httpx.get
        self.assertLessEqual(server.accepted, _http._LIMITS.max_keepalive_connections)

    def test_cookies_are_not_carried_between_requests(self):
        server = self.server()
        _http.get(server.url, timeout=30)
        _http.get(server.url, timeout=30)
        self.assertEqual(server.cookies, [None, None])

    def test_proxies_get_separate_clients_and_never_leak(self):
        proxy_server = self.server()
        port = proxy_server.server_address[1]
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Capture(logging.DEBUG)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            proxies = (
                f"http://{SENTINEL}1@127.0.0.1:{port}",
                httpx.Proxy(f"http://127.0.0.1:{port}", auth=("SENTINEL_USER", "SENTINEL_PASS2")),
            )
            for proxy in proxies:
                response = _http.get("http://example.test/", proxy=proxy, timeout=30)
                self.assertEqual(response.text, "ok")
            _http.get(proxy_server.url, timeout=30)
            closed = socket.socket()
            closed.bind(("127.0.0.1", 0))
            dead = f"http://{SENTINEL}@127.0.0.1:{closed.getsockname()[1]}"
            closed.close()
            with self.assertRaises(httpx.ConnectError) as raised:
                _http.get("http://example.test/", proxy=dead, timeout=30)
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        self.assertEqual(len(_http._clients), 4)  # two proxies, no proxy, dead proxy
        self.assertEqual(proxy_server.accepted, 3)
        texts = [repr(_http._clients), str(raised.exception), repr(raised.exception)]
        texts += [record.getMessage() for record in records]
        for client in _http._clients.values():
            texts.append(repr(client))
        for text in texts:
            self.assertNotIn("SENTINEL", text)

    def test_ninth_proxy_evicts_and_closes_the_oldest(self):
        clients = [_http._client(f"http://127.0.0.1:{9000 + n}") for n in range(9)]
        self.assertEqual(len(_http._clients), _http._MAX_CLIENTS)
        self.assertTrue(clients[0].is_closed)
        self.assertNotIn(clients[0], _http._clients.values())
        self.assertFalse(any(client.is_closed for client in clients[1:]))
        self.assertIs(_http._client(f"http://127.0.0.1:{9008}"), clients[8])

    def test_evicted_client_is_replaced_before_sending(self):
        server = self.server()
        evicted = [_http._client(None)]
        _http.close_all()  # as if another thread evicted it right after the lookup
        real = _http._client
        lookup = lambda proxy: evicted.pop() if evicted else real(proxy)  # noqa: E731
        with patch.object(_http, "_client", side_effect=lookup):
            self.assertEqual(_http.get(server.url, timeout=30).text, "ok")
        self.assertEqual(server.accepted, 1)

    def test_close_all_closes_every_client(self):
        clients = [_http._client(None), _http._client("http://127.0.0.1:9000")]
        _http.close_all()
        self.assertEqual(_http._clients, {})
        self.assertTrue(all(client.is_closed for client in clients))

    def test_atexit_closes_clients(self):
        code = (
            "import httpx\n"
            "from goopdl import _http\n"
            "close = httpx.Client.close\n"
            "def traced(self):\n"
            "    print('closed', flush=True)\n"
            "    close(self)\n"
            "httpx.Client.close = traced\n"
            "_http._client(None)\n"
        )
        root = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["closed"])

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX only")
    def test_fork_child_starts_with_no_clients_and_a_free_lock(self):
        _http._client(None)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # multi-threaded fork
            with _http._lock:
                pid = os.fork()
                if pid == 0:
                    ok = not _http._clients and _http._lock.acquire(blocking=False)
                    os._exit(0 if ok else 1)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertEqual(len(_http._clients), 1)

    def test_connection_closed_while_idle_is_replaced(self):
        server = self.server(AnswerOnce)
        for _ in range(3):
            # httpcore sees the server's FIN on the idle socket and opens a new
            # connection: even a POST (never retried) goes through.
            self.assertEqual(_http.post(server.url, content=b"body", timeout=30).text, "ok")
            time.sleep(0.2)
        self.assertEqual(server.accepted, 3)

    def test_stale_reused_connection_retries_only_safe_requests(self):
        server = self.server(DropSecondRequest)
        self.assertEqual(_http.get(server.url, timeout=30).text, "ok")
        # The server drops the kept-alive connection only when the next
        # request arrives: deterministic stale reuse. GET retries once.
        self.assertEqual(_http.get(server.url, timeout=30).text, "ok")
        self.assertEqual(server.accepted, 2)
        # A POST whose body went out is not repeated: same error as httpx.
        with self.assertRaises(httpx.TransportError):
            _http.post(server.url, content=b"body", timeout=30)
        self.assertEqual(server.accepted, 2)
        with httpx.Client() as client:  # what the retry protects against
            client.get(server.url)
            with self.assertRaises(httpx.TransportError):
                client.get(server.url)

    def test_fresh_connection_failures_are_not_retried(self):
        server = self.server(DropSecondRequest)
        # Close every connection, fresh ones included, without answering.
        drop = type("Drop", (Handler,), {"handle": lambda h: h.rfile.readline()})
        server.RequestHandlerClass = drop
        with self.assertRaises(httpx.TransportError):
            _http.get(server.url, timeout=30)
        self.assertEqual(server.accepted, 1)

    def test_per_call_timeout_and_errors_match_httpx(self):
        silent = socket.socket()
        silent.bind(("127.0.0.1", 0))
        silent.listen(4)
        self.addCleanup(silent.close)
        silent_url = f"http://127.0.0.1:{silent.getsockname()[1]}/"
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_url = f"http://127.0.0.1:{closed.getsockname()[1]}/"
        closed.close()
        # A refused loopback connect takes ~2 s on Windows: its timeout is larger.
        for url, timeout, error, message in (
            (silent_url, 0.2, httpx.ReadTimeout, "Google Play request timed out."),
            (closed_url, 15, httpx.ConnectError, "Google Play request failed (ConnectError)."),
        ):
            for request in (httpx.get, _http.get):
                with self.subTest(error=error.__name__, request=request.__module__):
                    start = time.monotonic()
                    with self.assertRaises(error):
                        request(url, timeout=timeout)
                    # 0.2 s, not the client's 5 s default timeout.
                    self.assertLess(time.monotonic() - start, 2 if timeout < 1 else 10)
            with self.assertRaises(api.NetworkError) as raised:
                api._send(_http.get, url, timeout=timeout)
            self.assertEqual(str(raised.exception), message)
            self.assertIsNone(raised.exception.__context__)


if __name__ == "__main__":
    unittest.main()
