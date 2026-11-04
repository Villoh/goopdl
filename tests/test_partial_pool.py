"""A partial anonymous pool serves callers while the dispenser rate limits.

Hermetic: sockets are blocked, httpx.get/post are fakes, and the config dir
and HOME are temporary.
"""
import socket
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from _envutil import isolated_env
from _nonet import no_sockets
from goopdl import Client, api, auth, cli
from goopdl import client as client_module
from goopdl.profiles import get_latest_probe_profiles
from test_transient_recovery import DELIVERY, DETAILS, PACKAGE

FAILURES = {
    "429": lambda: httpx.Response(429, headers={"Retry-After": "120"}),
    "503": lambda: httpx.Response(503),
    "ConnectError": lambda: httpx.ConnectError("down"),
}


class PartialPoolTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.output = root / "out"
        self.calls = Counter()
        self.failure = FAILURES["429"]
        self.expire_first = False
        cache = root / "cache"
        for patcher in (
            isolated_env({"GOOPDL_CONFIG_DIR": str(cache), "HOME": str(root)}),
            patch.object(socket.socket, "connect", no_sockets),
            patch.object(auth, "_CONFIG_DIR", cache, create=True),
            patch.object(httpx, "post", side_effect=self.request),
            patch.object(httpx, "get", side_effect=self.request),
            patch.object(api, "download_batch", side_effect=self.download),
            patch.object(cli, "download_batch", side_effect=self.download),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeper = patch("time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.assertTrue(str(auth.get_config_dir()).startswith(tempfile.gettempdir()))

    def request(self, url, **kwargs):
        endpoint = str(url).rsplit("/", 1)[-1]
        if "android.clients.google.com" not in str(url):
            self.calls["dispenser"] += 1
            failure = self.failure()
            if isinstance(failure, Exception):
                raise failure
            return failure
        self.calls[endpoint] += 1
        if endpoint == "details" and self.expire_first and self.calls[endpoint] == 1:
            return httpx.Response(401)
        content = {"details": DETAILS, "delivery": DELIVERY}.get(endpoint, b"")
        return httpx.Response(200, content=content)

    def download(self, specs, **kwargs):
        for spec in specs:
            spec.dest.parent.mkdir(parents=True, exist_ok=True)
            spec.dest.write_bytes(b"apk")

    def seed(self, valid, profile=None, expired=0):
        now = time.time()
        tokens = [{"authToken": f"OLD{i}", "gsfId": f"9{i}", "_cached_at": now - 7200}
                  for i in range(expired)]
        tokens += [{"authToken": f"T{i}", "gsfId": str(i), "_cached_at": now - 300}
                   for i in range(valid)]
        auth._save_pool(tokens, "arm64", None, profile)
        return tokens

    def invoke(self, *arguments):
        return CliRunner().invoke(cli.app, list(arguments))

    # ── refill (ensure_pool / pick_pool_token) ────────────────────────────────

    def test_download_uses_partial_pool_through_429(self):
        for valid, expired in ((3, 0), (4, 0), (3, 2)):
            with self.subTest(valid=valid, expired=expired):
                self.calls.clear()
                self.seed(valid, expired=expired)
                result = self.invoke("download", PACKAGE, "-o", str(self.output))
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertTrue((self.output / f"{PACKAGE}-7.apk").exists())
                self.assertIn(f"refill stopped; using {valid}/5 valid tokens", result.output)
                self.assertEqual(self.calls["dispenser"], 1)
                self.sleep.assert_not_called()

    def test_partial_pool_refill_outage_unchanged(self):
        for name in ("503", "ConnectError"):
            with self.subTest(failure=name):
                self.calls.clear()
                self.failure = FAILURES[name]
                self.seed(3)
                result = self.invoke("download", PACKAGE, "-o", str(self.output))
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(self.calls["dispenser"], 2)  # two-failure budget
                self.sleep.assert_not_called()

    def test_empty_pool_rate_limit_still_typed(self):
        result = self.invoke("download", PACKAGE, "-o", str(self.output))
        self.assertEqual(result.exit_code, cli.RATE_LIMIT_EXIT_CODE, result.output)
        self.assertIn("Token dispenser rate limited", result.output)
        self.assertEqual(self.calls["dispenser"], 1)
        self.seed(0, expired=3)  # expired tokens do not count
        with self.assertRaises(auth.DispenserRateLimitedError) as raised:
            auth.ensure_pool(raise_on_error=True)
        self.assertEqual(raised.exception.retry_after, 120)
        self.assertEqual(self.calls["dispenser"], 2)
        self.sleep.assert_not_called()

    def test_refill_429_is_not_retried_within_a_call(self):
        self.seed(3, "Pv")
        for attempt in (1, 2):  # each call makes one refill attempt
            pool = auth.ensure_pool(profile="Pv", raise_on_error=True)
            self.assertEqual([t["authToken"] for t in pool], ["T0", "T1", "T2"])
            self.assertEqual(self.calls["dispenser"], attempt)
        self.assertIsNotNone(auth.pick_pool_token(profile="Pv", raise_on_error=True))
        self.assertEqual(self.calls["dispenser"], 3)
        self.sleep.assert_not_called()

    def test_pinned_profile_partial_pool_skips_cooldown(self):
        self.failure = FAILURES["503"]
        self.seed(3, "Pv")
        self.assertEqual(len(auth.ensure_pool(profile="Pv", raise_on_error=True)), 3)
        self.assertEqual(self.calls["dispenser"], 1)
        self.sleep.assert_not_called()

    def test_latest_probes_partial_pool(self):
        self.seed(3, get_latest_probe_profiles("arm64", n=1)[0][0])
        result = self.invoke("latest", PACKAGE)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.calls["details"], 3)
        self.assertEqual(self.calls["dispenser"], 1)

    def test_client_uses_partial_pool(self):
        self.seed(3)
        for key, _ in get_latest_probe_profiles("arm64", n=2):
            self.seed(3, key)  # Client.download picks one of these
        client = Client()
        self.assertEqual(client.details(PACKAGE).version_code, 7)
        self.assertEqual(self.calls["dispenser"], 1)

        async def download_files(specs, **kwargs):
            self.download(specs)

        with patch.object(client_module, "download_files", download_files):
            self.assertTrue(client.download(PACKAGE, self.output).path.exists())
        self.assertEqual(self.calls["dispenser"], 1)  # size-1 pool: no refill

    def test_account_mode_ignores_anonymous_pool(self):
        self.seed(3)
        throttled = auth.DirectAuthError("auth (HTTP 429)", stage="auth", status=429)
        with (
            isolated_env({"GOOPDL_CONFIG_DIR": str(auth.get_config_dir()),
                          "GOOPDL_ACCOUNT_EMAIL": "a@b.c", "GOOPDL_AAS_TOKEN": "aas_et/x"}),
            patch.object(auth, "_direct_session", side_effect=throttled),
        ):
            result = self.invoke("download", PACKAGE, "-o", str(self.output))
        self.assertEqual(result.exit_code, cli.RATE_LIMIT_EXIT_CODE, result.output)
        self.assertIn("Direct Google authentication rate limited", result.output)
        self.assertEqual(self.calls["dispenser"], 0)


if __name__ == "__main__":
    unittest.main()
