"""In-memory backoff after a dispenser HTTP 429 (per process, never on disk).

Hermetic: sockets are blocked, goopdl._http.post/get are fakes, the config dir and
HOME are temporary and time.monotonic is a fake clock (no real sleep).
"""
import json
import logging
import os
import socket
import tempfile
import threading
import time
import unittest
from email.utils import formatdate
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from _envutil import isolated_env
from _nonet import no_sockets
from goopdl import Client, auth, cli
from goopdl.errors import DispenserRateLimitedError, RateLimitedError
from test_client import PKG, FakePlay

PROXY = "http://user:proxy-pass-sentinel@proxy.invalid:8080"
OTHER = "https://other-dispenser.invalid/api/auth"


def limited(retry_after=None):
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(429, headers=headers)


class BackoffTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cache = self.root / "cache"
        self.assertTrue(str(self.root).startswith(tempfile.gettempdir()))
        self.fake = FakePlay()
        self.now = 1000.0
        for patcher in (
            isolated_env({"GOOPDL_CONFIG_DIR": str(self.cache), "HOME": str(self.root)}),
            patch.object(socket.socket, "connect", no_sockets),
            patch("goopdl._http.post", side_effect=lambda *a, **k: self.fake.post(*a, **k)),
            patch("goopdl._http.get", side_effect=lambda *a, **k: self.fake.get(*a, **k)),
            patch("time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        auth._reset_dispenser_backoff()
        self.addCleanup(auth._reset_dispenser_backoff)
        self.assertTrue(str(auth.get_config_dir()).startswith(tempfile.gettempdir()))

    def clock(self):
        """Drive time.monotonic from self.now."""
        patcher = patch.object(auth.time, "monotonic", side_effect=lambda: self.now)
        patcher.start()
        self.addCleanup(patcher.stop)

    def posts(self):
        return len(self.fake.bearers("dispenser"))

    def fail_next(self, *responses):
        self.fake.faults["dispenser"] = list(responses)

    def seed(self, valid):
        now = time.time()
        tokens = [{"authToken": f"T{i}", "gsfId": f"{i:x}", "_cached_at": now - 120}
                  for i in range(valid)]
        auth._save_pool(tokens, "arm64", None)

    def assert_limited(self, retry_after, **kwargs):
        before = self.posts()
        with self.assertRaises(DispenserRateLimitedError) as raised:
            auth.fetch_token(raise_on_error=True, **kwargs)
        self.assertEqual(retry_after, raised.exception.retry_after)
        self.assertEqual(before, self.posts(), "no request inside the window")

    # ── window length ─────────────────────────────────────────────────────────

    def test_retry_after_window_answers_without_a_request(self):
        self.clock()
        self.fail_next(limited("120"))
        with self.assertRaises(DispenserRateLimitedError) as raised:
            auth.fetch_token(raise_on_error=True)
        self.assertEqual(120.0, raised.exception.retry_after)  # the server's value
        self.assertEqual(1, self.posts())
        self.now += 30.4
        self.assert_limited(90.0)  # time left, rounded up
        with self.assertLogs("goopdl.auth", "WARNING") as logs:
            self.assertIsNone(auth.fetch_token())  # legacy callers: None, as a 429
        self.assertIn("backing off for 90s", logs.output[0])
        self.assertEqual(1, self.posts())

    def test_without_retry_after_the_default_is_60s_then_it_retries(self):
        self.clock()
        self.fail_next(limited())
        self.assertIsNone(auth.fetch_token())
        self.assert_limited(60.0)
        self.now += 59.5
        self.assert_limited(1.0)  # never 0 while the window lasts
        self.now += 0.5  # window over: the next call asks again
        self.assertTrue((auth.fetch_token(raise_on_error=True) or {})["authToken"])
        self.assertEqual(2, self.posts())

    def test_huge_retry_after_is_capped_at_900s(self):
        self.clock()
        self.fail_next(limited("86400"))
        self.assertIsNone(auth.fetch_token())
        self.assert_limited(900.0)
        self.now += 900
        self.assertTrue(auth.fetch_token())

    def test_zero_or_invalid_retry_after_uses_the_default(self):
        for value in ("0", "soon"):
            with self.subTest(value=value):
                auth._reset_dispenser_backoff()
                self.clock()
                self.fail_next(limited(value))
                self.assertIsNone(auth.fetch_token())
                self.assert_limited(60.0)

    def test_http_date_retry_after(self):
        self.clock()
        self.fail_next(limited(formatdate(time.time() + 300, usegmt=True)))
        self.assertIsNone(auth.fetch_token())
        with self.assertRaises(DispenserRateLimitedError) as raised:
            auth.fetch_token(raise_on_error=True)
        self.assertTrue(290 <= (raised.exception.retry_after or 0) <= 300, raised.exception)
        self.assertEqual(1, self.posts())

    def test_success_after_the_window_clears_the_entry(self):
        self.clock()
        self.fail_next(limited("120"))
        auth.fetch_token()
        self.assertEqual(1, len(auth._dispenser_backoff))
        self.now += 120
        self.assertTrue(auth.fetch_token())
        self.assertEqual({}, auth._dispenser_backoff)
        # Also a 200 racing a window another thread opened meanwhile.
        key = auth._dispenser_backoff_key(auth.DEFAULT_DISPENSER_URL, None)
        real_post = self.fake.post

        def post(*args, **kwargs):
            auth._dispenser_backoff_start(key, 120)
            return real_post(*args, **kwargs)

        with patch("goopdl._http.post", side_effect=post):
            self.assertTrue(auth.fetch_token())
        self.assertEqual({}, auth._dispenser_backoff)

    # ── what callers see ──────────────────────────────────────────────────────

    def test_partial_pool_50_calls_make_one_request(self):
        for retry_after in ("120", None):
            with self.subTest(retry_after=retry_after):
                auth._reset_dispenser_backoff()
                self.fake.requests.clear()
                self.seed(3)
                self.fail_next(*[limited(retry_after)] * 50)
                client = Client(config_dir=self.cache)
                for _ in range(50):
                    self.assertEqual(4242, client.details(PKG).version_code)
                self.assertEqual(1, self.posts())

    def test_empty_pool_raises_with_the_time_left_and_no_request(self):
        self.clock()
        self.fail_next(*[limited("120")] * 10)
        client = Client(config_dir=self.cache)
        with self.assertRaises(RateLimitedError) as raised:
            client.details(PKG)
        self.assertEqual(120.0, raised.exception.retry_after)
        for elapsed in (10, 50):
            self.now += elapsed
            with self.assertRaises(RateLimitedError) as raised:
                client.details(PKG)
            self.assertIsInstance(raised.exception.__cause__, DispenserRateLimitedError)
        self.assertEqual(60.0, raised.exception.retry_after)
        with self.assertRaises(DispenserRateLimitedError):
            auth.pick_pool_token(raise_on_error=True)
        with self.assertRaises(DispenserRateLimitedError):
            auth.replace_pool_token({"authToken": "x"}, raise_on_error=True)
        self.assertEqual(1, self.posts())

    def test_replacement_inside_the_window_returns_a_survivor(self):
        self.seed(3)
        self.fail_next(*[limited("120")] * 5)
        pool = auth.ensure_pool(raise_on_error=True)  # opens the window
        token = auth.replace_pool_token(pool[0], raise_on_error=True)
        self.assertIn(token, pool[1:])
        self.assertEqual(1, self.posts())

    def test_32_threads_with_a_cold_window_make_one_request(self):
        self.seed(3)
        self.fail_next(*[limited("120")] * 32)
        client = Client(config_dir=self.cache)
        start, results = threading.Barrier(32), []

        def call():
            start.wait(timeout=10)
            results.append(client.details(PKG).version_code)

        threads = [threading.Thread(target=call) for _ in range(32)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual([4242] * 32, results)
        self.assertEqual(1, self.posts())

    # ── the key: dispenser URL + proxy ────────────────────────────────────────

    def test_one_dispenser_or_proxy_does_not_block_another(self):
        self.fail_next(limited("120"))
        self.assertIsNone(auth.fetch_token(dispenser_url=OTHER))
        self.assertTrue(auth.fetch_token())  # default dispenser: still asked
        self.assertTrue(auth.fetch_token(dispenser_url=OTHER, proxy=PROXY))
        self.assertEqual(3, self.posts())
        self.assert_limited(120.0, dispenser_url=OTHER)
        self.fail_next(limited("60"))
        self.assertIsNone(auth.fetch_token(proxy=PROXY))
        self.assertTrue(auth.fetch_token())
        self.assert_limited(60.0, proxy=PROXY)
        self.assertEqual(5, self.posts())

    def test_outages_do_not_open_a_window(self):
        ok = httpx.Response(200, json={"authToken": "T"})
        for failure in (httpx.Response(503), httpx.ConnectError("down")):
            with self.subTest(failure=repr(failure)), \
                    patch("goopdl._http.post", side_effect=[failure, failure, ok]) as post:
                self.assertIsNone(auth.fetch_token())
                self.assertEqual({}, auth._dispenser_backoff)
                self.assertTrue(auth.fetch_token())  # asks again at once
                self.assertEqual(3, post.call_count)

    def test_account_mode_ignores_the_window(self):
        auth._dispenser_backoff_start(
            auth._dispenser_backoff_key(auth.DEFAULT_DISPENSER_URL, None), 120
        )
        throttled = auth.DirectAuthError("auth (HTTP 429)", stage="auth", status=429)
        with isolated_env({"GOOPDL_CONFIG_DIR": str(self.cache), "HOME": str(self.root),
                           "GOOPDL_ACCOUNT_EMAIL": "a@b.c",
                           "GOOPDL_AAS_TOKEN": "aas_et/x"}):
            with patch.object(auth, "_direct_auth", return_value={"authToken": "A"}):
                self.assertEqual("A", (auth.fetch_token() or {})["authToken"])
            with patch.object(auth, "_direct_auth", side_effect=throttled):
                self.assertIsNone(auth.fetch_token())
        self.assertEqual(1, len(auth._dispenser_backoff))  # untouched
        self.assertEqual(0, self.posts())

    # ── CLI, clearing, secrets, fork ──────────────────────────────────────────

    def test_cli_exits_5_inside_the_window_without_a_request(self):
        self.fail_next(limited("120"))
        auth.fetch_token()
        for command in (["download", PKG, "-o", str(self.root / "out")],
                        ["latest", PKG], ["auth", "token"]):
            with self.subTest(command=command[0]):
                result = CliRunner().invoke(cli.app, command)
                self.assertEqual(cli.RATE_LIMIT_EXIT_CODE, result.exit_code, result.output)
                self.assertIn("rate limited", result.output)
        self.assertEqual(1, self.posts())

    def test_cli_inside_the_window_matches_a_fresh_429(self):
        for command in (["info", PKG], ["search", "x"], ["list-splits", PKG]):
            with self.subTest(command=command[0]):
                auth._reset_dispenser_backoff()
                self.fake.requests.clear()
                self.fail_next(limited("120"))
                fresh = CliRunner().invoke(cli.app, command)
                again = CliRunner().invoke(cli.app, command)
                self.assertEqual(fresh.exit_code, again.exit_code, again.output)
                self.assertEqual(1, self.posts())

    def test_clear_auth_and_clear_cache_forget_the_window(self):
        for clear in (auth.clear_auth, Client(config_dir=self.cache).clear_cache,
                      lambda: CliRunner().invoke(cli.app, ["auth", "logout"])):
            self.fail_next(limited("120"))
            self.assertIsNone(auth.fetch_token())
            clear()
            self.assertEqual({}, auth._dispenser_backoff)
        self.assertTrue(auth.fetch_token())
        self.assertEqual(4, self.posts())

    def test_proxy_never_appears_in_logs_messages_or_keys(self):
        self.fail_next(limited("120"))
        errors = []
        with self.assertLogs("goopdl", logging.DEBUG) as logs:
            self.assertIsNone(auth.fetch_token(proxy=PROXY))
            for _ in range(2):
                try:
                    auth.fetch_token(proxy=PROXY, raise_on_error=True)
                except DispenserRateLimitedError as exc:
                    errors.append(exc)
            result = CliRunner().invoke(cli.app, ["download", PKG, "-p", PROXY,
                                                  "-o", str(self.root / "out")])
        self.assertEqual(cli.RATE_LIMIT_EXIT_CODE, result.exit_code, result.output)
        self.assertEqual(1, self.posts())
        self.assertEqual(2, len(errors))
        error = errors[-1]
        text = " ".join([*logs.output, str(error), repr(error), repr(error.args),
                         json.dumps(error.__dict__, default=str), result.output,
                         repr(auth._dispenser_backoff)])
        for secret in ("proxy-pass-sentinel", "proxy.invalid", "user:"):
            self.assertNotIn(secret, text)

    @unittest.skipUnless(hasattr(os, "fork"), "needs os.fork")
    def test_fork_resets_the_window_and_its_lock(self):
        self.fail_next(limited("120"))
        auth.fetch_token()
        lock = auth._dispenser_backoff_lock
        lock.acquire()  # as if another thread were consulting it during fork
        try:
            pid = os.fork()
            if pid == 0:  # child: no window, a usable lock, asks the dispenser
                code = 1
                try:
                    if not auth._dispenser_backoff and auth._dispenser_backoff_lock is not lock:
                        code = 0 if auth.fetch_token() else 2
                finally:
                    os._exit(code)
            _, status = os.waitpid(pid, 0)
        finally:
            lock.release()
        self.assertEqual(0, os.waitstatus_to_exitcode(status))
        self.assertEqual(1, len(auth._dispenser_backoff))  # the parent keeps its own


if __name__ == "__main__":
    unittest.main()
