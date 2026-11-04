"""Final polish regressions: CLI error paths, logs, toc, secrets, redirects."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from goopdl import auth
from goopdl.cli import AUTH_UNAVAILABLE_EXIT_CODE, app

PIXEL = "Google Pixel 9a"
BUSY = "Token pool is busy (another goopdl process holds the lock); retry later."


def _flat(output: str) -> str:
    return " ".join(output.split())


@patch.dict(os.environ, {}, clear=True)
class PoolBusyCliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.out = tmp / "out"
        for patcher in (
            patch.object(auth, "_CONFIG_DIR", tmp / "config", create=True),
            patch.object(auth, "_POOL_LOCK_TIMEOUT", 0.05),
            patch("httpx.post", side_effect=AssertionError("network")),
            patch("httpx.get", side_effect=AssertionError("network")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _invoke_busy(self, *args: str):
        # Separate open handles conflict even inside one process.
        with auth._pool_lock("arm64", None, PIXEL) as acquired:
            self.assertTrue(acquired)
            return CliRunner().invoke(app, [*args, "com.example.pkg", "--profile", PIXEL])

    def test_busy_pool_exits_cleanly_with_gold_exit_codes(self) -> None:
        for command, code in (
            (("fast-download", "-o", "OUT"), 1),
            (("download", "-o", "OUT"), AUTH_UNAVAILABLE_EXIT_CODE),
            (("latest",), 1),
        ):
            with self.subTest(command=command[0]):
                args = [str(self.out) if a == "OUT" else a for a in command]
                result = self._invoke_busy(*args)
                self.assertEqual(result.exit_code, code, result.output)
                self.assertIsInstance(result.exception, SystemExit)
                self.assertIn(BUSY, _flat(result.output))
                self.assertNotIn("Traceback", result.output)


@patch.dict(os.environ, {}, clear=True)
@patch("goopdl.auth.time.sleep")
class DispenserRejectionMessageTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(
            auth, "_CONFIG_DIR", Path(tempfile.mkdtemp()), create=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth(self, **post):
        with patch("goopdl.auth.httpx.post", **post):
            return CliRunner().invoke(app, ["auth"])

    def test_answering_dispenser_is_not_reported_as_down(self, _sleep) -> None:
        for response, last in (
            (httpx.Response(200, json={}), "no authToken"),
            (httpx.Response(400), "HTTP 400"),
        ):
            with self.subTest(last=last):
                result = self._auth(return_value=response)
                self.assertEqual(result.exit_code, AUTH_UNAVAILABLE_EXIT_CODE)
                text = _flat(result.output)
                self.assertIn(
                    "Authentication failed — all profiles rejected "
                    f"(last dispenser response: {last}).",
                    text,
                )
                self.assertNotIn("may be down", text)

    def test_outages_keep_the_unavailable_message(self, _sleep) -> None:
        for post, expected in (
            ({"return_value": httpx.Response(503)}, "(HTTP 503). It may be down"),
            ({"side_effect": httpx.ConnectError("x")}, "unavailable. It may be down"),
        ):
            with self.subTest(expected=expected):
                result = self._auth(**post)
                self.assertEqual(result.exit_code, AUTH_UNAVAILABLE_EXIT_CODE)
                self.assertIn(expected, _flat(result.output))
                self.assertNotIn("all profiles rejected", result.output)


@patch.dict(os.environ, {}, clear=True)
@patch("goopdl.auth.time.sleep")
class DispenserLogDeviceTest(unittest.TestCase):
    def _messages(self, **post) -> list[str]:
        with (
            self.assertLogs("goopdl.auth", level="WARNING") as logs,
            patch("goopdl.auth.httpx.post", **post),
        ):
            auth.fetch_token(profile=PIXEL, proxy="http://user:pass@proxy:1")
        return [record.getMessage() for record in logs.records]

    def test_server_and_transport_failures_name_the_device(self, _sleep) -> None:
        for post, expected in (
            ({"return_value": httpx.Response(503)}, f"Dispenser returned HTTP 503 for {PIXEL}"),
            (
                {"side_effect": httpx.ConnectError("user:pass")},
                f"Dispenser transport failed for {PIXEL}: ConnectError",
            ),
        ):
            with self.subTest(expected=expected):
                messages = self._messages(**post)
                self.assertIn(expected, messages)
                self.assertNotIn("pass", " ".join(messages))


def _direct_responses():
    checkin = httpx.Response(
        200, content=auth._proto_varint(7, 1234) + auth._proto_bytes(12, "consistency")
    )
    upload = httpx.Response(
        200,
        content=auth._proto_bytes(
            1, auth._proto_bytes(28, auth._proto_bytes(1, "config"))
        ),
    )
    return checkin, upload, httpx.Response(200, text="Auth=bearer-secret\n")


@patch.dict(os.environ, {}, clear=True)
class DirectTocRateLimitTest(unittest.TestCase):
    def _direct_auth(self):
        return auth._direct_auth(
            "email@example.test", "aas_et/aas-secret", "arm64", None, None, "Pv"
        )

    def test_toc_429_keeps_the_bundle_without_dfe_cookie(self) -> None:
        limited = httpx.Response(429, headers={"Retry-After": "60"})
        with (
            patch("goopdl.auth.httpx.post", side_effect=_direct_responses()),
            patch("goopdl.auth.httpx.get", return_value=limited),
            self.assertLogs("goopdl.auth", level="WARNING") as logs,
        ):
            bundle = self._direct_auth()
        assert bundle is not None
        self.assertEqual(bundle["authToken"], "bearer-secret")
        self.assertEqual(bundle["dfeCookie"], "")
        text = " ".join(logs.output)
        self.assertIn("toc", text)
        for secret in ("bearer-secret", "aas-secret", "email@"):
            self.assertNotIn(secret, text)

    def test_checkin_429_still_aborts_with_retry_after(self) -> None:
        limited = httpx.Response(429, headers={"Retry-After": "60"})
        with (
            patch("goopdl.auth.httpx.post", return_value=limited),
            patch("goopdl.auth.httpx.get", side_effect=AssertionError("toc")),
            self.assertRaises(auth.DirectAuthError) as raised,
        ):
            self._direct_auth()
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.stage, "checkin")
        self.assertEqual(raised.exception.retry_after, 60.0)


if __name__ == "__main__":
    unittest.main()
