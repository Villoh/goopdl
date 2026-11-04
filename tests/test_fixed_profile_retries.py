"""Hermetic retry budgets of the original main (c6e5f1c) for fixed profiles."""
import os
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from _envutil import isolated_env
from goopdl import _http, api, auth, cli

PACKAGE = "com.example.pkg"
DIRECT_ENV = {"GOOPDL_ACCOUNT_EMAIL": "a@b.c", "GOOPDL_AAS_TOKEN": "aas_et/xxx"}


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result) + bytes([value])


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


DETAILS = field(1, field(2, field(4,
    field(1, PACKAGE) + field(5, "Example")
    + field(13, field(1, field(3, 7) + field(4, "1.2")))
)))
DELIVERY = field(1, field(21, field(1, 1) + field(2,
    field(3, "https://play.googleapis.com/base") + field(29, 7)
)))
GOOGLE = {
    "details": DETAILS,
    "delivery": DELIVERY,
    "checkin": field(7, 123) + field(12, "consistency"),
    "uploadDeviceConfig": field(1, field(28, field(1, "config"))),
    "auth": b"Auth=TOKEN\n",
    "toc": field(1, field(6, field(22, "cookie"))),
}
REJECTIONS = {
    "noauth": lambda: httpx.Response(200, json={"error": "no token"}),
    "400": lambda: httpx.Response(400),
    "401": lambda: httpx.Response(401),
    "403": lambda: httpx.Response(403),
    "404": lambda: httpx.Response(404),
    "json": lambda: httpx.Response(200, content=b"{bad-json"),
    "proto": lambda: httpx.Response(200, content=b"\x80"),
}


class HttpCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "out"
        self.calls = Counter()
        self.failures = {}  # endpoint -> responses/exceptions served first
        for patcher in (
            isolated_env(),
            patch.object(auth, "_CONFIG_DIR", Path(directory.name) / "cache", create=True),
            patch.object(_http, "post", side_effect=lambda *a, **k: self.request("POST", *a, **k)),
            patch.object(_http, "get", side_effect=lambda *a, **k: self.request("GET", *a, **k)),
            patch.object(api, "download_batch", side_effect=self.download),
            patch.object(cli, "download_batch", side_effect=self.download),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeper = patch("time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.addCleanup(auth._reset_dispenser_backoff)  # in-memory 429 window

    def request(self, method, url, **kwargs):
        endpoint = str(url).split("?")[0].rsplit("/", 1)[-1]
        if "android.clients.google.com" not in str(url):
            endpoint = "dispenser"
        self.calls.update([method, endpoint])
        pending = self.failures.get(endpoint)
        if pending:
            failure = pending.pop(0) if isinstance(pending, list) else pending
            failure = failure() if callable(failure) else failure
            if isinstance(failure, Exception):
                raise failure
            return failure
        if endpoint == "dispenser":
            return httpx.Response(200, json={
                "authToken": f"token-{self.calls[endpoint]}",
                "gsfId": str(self.calls[endpoint]),
            })
        return httpx.Response(200, content=GOOGLE.get(endpoint, b""))

    def download(self, specs, **kwargs):
        for spec in specs:
            spec.dest.parent.mkdir(parents=True, exist_ok=True)
            spec.dest.write_bytes(b"apk")

    def reset(self, failures):
        auth.clear_auth()
        self.calls.clear()
        self.sleep.reset_mock()
        self.failures = failures

    def sleeps(self):
        return [c.args[0] for c in self.sleep.call_args_list]

    def run_path(self, path):
        """Return the exit code of fast-download or api.download_app (0/1)."""
        if path == "fast-download":
            result = CliRunner().invoke(
                cli.app, ["fast-download", PACKAGE, "-o", str(self.output)]
            )
            return result.exit_code
        try:
            api.download_app(PACKAGE, self.output)
        except api.PlayAPIError:
            return 1
        return 0


class FixedProfileRetryTest(HttpCase):

    def test_fixed_profile_rejection_retries_same_profile_like_gold(self):
        for path in ("fast-download", "api"):
            for mode, response in REJECTIONS.items():
                with self.subTest(path=path, mode=mode):
                    self.reset({"dispenser": [response]})
                    self.assertEqual(self.run_path(path), 0)
                    self.assertEqual(self.calls["dispenser"], 2)
                    self.assertEqual(self.calls["GET"], 2)
                    self.assertEqual(self.sleeps(), [5])

    def test_persistent_fixed_profile_rejection_is_bounded_like_gold(self):
        for path in ("fast-download", "api"):
            for mode, response in REJECTIONS.items():
                with self.subTest(path=path, mode=mode):
                    self.reset({"dispenser": response})
                    self.assertEqual(self.run_path(path), 1)
                    self.assertEqual(self.calls["dispenser"], 3)
                    self.assertEqual(self.calls["GET"], 0)
                    self.assertEqual(self.sleeps(), [5, 5])

    def test_persistent_rejection_raises_typed_dispenser_error(self):
        self.reset({"dispenser": REJECTIONS["400"]})
        with self.assertRaises(api.PlayAPIError) as caught:
            api.download_app(PACKAGE, self.output)
        cause = caught.exception.__cause__
        assert isinstance(cause, auth.DispenserUnavailableError), cause
        self.assertEqual(cause.reason, "profile_rejected")

    def test_dispenser_429_still_stops_at_once(self):
        self.reset({"dispenser": lambda: httpx.Response(429)})
        result = CliRunner().invoke(
            cli.app, ["fast-download", PACKAGE, "-o", str(self.output)]
        )
        self.assertEqual(result.exit_code, cli.RATE_LIMIT_EXIT_CODE, result.output)
        self.assertEqual(self.calls["dispenser"], 1)
        self.assertEqual(self.sleeps(), [])

    def resolve(self):
        return cli._resolve_version_string(
            PACKAGE, "1.2", "arm64", None, None, None, None
        )

    def test_resolve_version_recovers_from_transient_checkin_5xx_like_gold(self):
        os.environ.update(DIRECT_ENV)
        for failure in (httpx.Response(503), httpx.ConnectError("down")):
            with self.subTest(failure=repr(failure)):
                self.reset({"checkin": [failure]})
                version_code, token = self.resolve()
                self.assertEqual(version_code, 7)
                self.assertEqual(token["authToken"], "TOKEN")
                self.assertEqual(self.calls["checkin"], 2)
                self.assertEqual(self.calls["POST"], 4)
                self.assertEqual(self.calls["GET"], 2)
                self.assertEqual(self.sleeps(), [5])

    def test_resolve_version_bounds_persistent_checkin_5xx(self):
        os.environ.update(DIRECT_ENV)
        self.reset({"checkin": lambda: httpx.Response(503)})
        with self.assertRaises(api.PlayAPIError):
            self.resolve()
        self.assertEqual(self.calls["checkin"], api._TOKEN_FETCH_ATTEMPTS)
        self.assertEqual(self.sleeps(), [5, 5])

    def test_revoked_direct_credentials_are_not_retried(self):
        os.environ.update(DIRECT_ENV)
        for status in (400, 401, 403):
            with self.subTest(status=status):
                self.reset({"checkin": lambda status=status: httpx.Response(status)})
                with self.assertRaises(api.PlayAPIError):
                    self.resolve()
                self.assertEqual(self.calls["checkin"], 1)
                self.assertEqual(self.sleeps(), [])


class MalformedResponseTest(HttpCase):
    """A corrupt protobuf does not improve with another token: gold propagated."""

    def setUp(self):
        super().setUp()
        self.malformed = (
            lambda: httpx.Response(200, content=b"\x80"),
            lambda: httpx.Response(200, content=b"{bad-json"),
        )

    def test_error_type_stays_a_play_api_error(self):
        self.assertTrue(issubclass(api.MalformedResponseError, api.PlayAPIError))
        self.reset({"details": self.malformed[0]})
        with self.assertRaises(api.MalformedResponseError) as caught:
            api.get_details(PACKAGE, {"authToken": "T", "gsfId": "1"})
        self.assertIsInstance(caught.exception.__cause__, (ValueError, EOFError))

    def test_version_string_download_stops_like_gold(self):
        for response in self.malformed:
            with self.subTest(response=response().content):
                self.reset({"details": response})
                result = CliRunner().invoke(cli.app, [
                    "download", PACKAGE, "--version", "1.2", "-o", str(self.output),
                ])
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertEqual(self.calls["dispenser"], 6)  # pool of 5 + 1 probe
                self.assertEqual(self.calls["GET"], 1)
                self.assertEqual(self.sleeps(), [])

    def test_latest_stops_at_first_malformed_probe_like_gold(self):
        self.reset({"details": self.malformed[0]})
        result = CliRunner().invoke(cli.app, ["latest", PACKAGE])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(self.calls["GET"], 1)
        self.assertIn("Malformed protobuf", result.output)

    def test_compat_retry_stops_on_malformed_reply(self):
        self.reset({})
        with (
            patch.object(cli, "get_details", side_effect=[
                api.AppNotSupportedError("incompatible"),
                api.MalformedResponseError("corrupt"),
            ]) as details,
            self.assertRaises(api.MalformedResponseError),
        ):
            cli._acquire_delivery(
                PACKAGE, None, "arm64", {"authToken": "T"}, None, None, None, None
            )
        self.assertEqual(details.call_count, 2)
        self.assertEqual(self.calls["dispenser"], 1)


if __name__ == "__main__":
    unittest.main()
