import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from _envutil import isolated_env
from goopdl import api, auth
from goopdl.api import PlayAPIError, RateLimitedError, download_app
from goopdl.auth import (
    DirectAuthError,
    DispenserRateLimitedError,
    DispenserUnavailableError,
    fetch_token,
)
from goopdl.cli import AUTH_UNAVAILABLE_EXIT_CODE, RATE_LIMIT_EXIT_CODE, app


class DispenserFetchTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        for patcher in (
            isolated_env(),
            patch.object(auth, "logger"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(auth._reset_dispenser_backoff)  # in-memory 429 window

    @patch("goopdl._http.post")
    def test_429_returns_none_by_default_and_raises_when_asked(self, post) -> None:
        post.return_value = httpx.Response(429, headers={"Retry-After": "120"})
        self.assertIsNone(fetch_token())

        with self.assertRaises(DispenserRateLimitedError) as raised:
            fetch_token(raise_on_error=True)
        self.assertEqual(raised.exception.retry_after, 120.0)
        self.assertIsInstance(raised.exception, DirectAuthError)
        # 429 stops profile rotation; the second call is inside its window.
        self.assertEqual(post.call_count, 1)

    @patch("goopdl._http.post")
    def test_outage_raises_unavailable_with_last_status(self, post) -> None:
        post.return_value = httpx.Response(503)
        self.assertIsNone(fetch_token())

        with self.assertRaises(DispenserUnavailableError) as raised:
            fetch_token(raise_on_error=True)
        self.assertEqual(raised.exception.status, 503)
        self.assertNotIsInstance(raised.exception, DispenserRateLimitedError)

    @patch("goopdl._http.post", side_effect=httpx.ConnectError("down"))
    def test_unreachable_dispenser_raises_unavailable(self, _post) -> None:
        with self.assertRaises(DispenserUnavailableError) as raised:
            fetch_token(raise_on_error=True)
        self.assertIsNone(raised.exception.status)


@isolated_env()
@patch("goopdl.api.time.sleep")
class DownloadAppDispenserTest(unittest.TestCase):
    def test_dispenser_429_becomes_rate_limited(self, sleep) -> None:
        with patch.object(
            api, "pick_pool_token", side_effect=DispenserRateLimitedError(60)
        ) as pick:
            with self.assertRaises(RateLimitedError) as raised:
                download_app("com.example.pkg", Path("unused"), profile="Pv")
        self.assertEqual(raised.exception.retry_after, 60)
        self.assertEqual(pick.call_count, 1)  # no retries inside the 429 window
        sleep.assert_not_called()
        self.assertIsInstance(raised.exception.__cause__, DispenserRateLimitedError)

    def test_dispenser_outage_is_not_rate_limited(self, _sleep) -> None:
        with patch.object(
            api, "pick_pool_token", side_effect=DispenserUnavailableError(503)
        ):
            with self.assertRaises(PlayAPIError) as raised:
                download_app("com.example.pkg", Path("unused"), profile="Pv")
        self.assertNotIsInstance(raised.exception, RateLimitedError)
        self.assertEqual(raised.exception.status_code, 503)


class DownloadAppDirectAuthTest(unittest.TestCase):
    @isolated_env()
    @patch("goopdl.api.time.sleep")
    def test_direct_auth_failure_keeps_its_stage(self, _sleep) -> None:
        with (
            patch.object(
                api, "pick_pool_token", side_effect=DirectAuthError("checkin (HTTP 403)")
            ),
            self.assertRaises(PlayAPIError) as raised,
        ):
            download_app("com.example.pkg", Path("unused"), profile="Pv")
        self.assertEqual(
            "Direct Google authentication failed at checkin (HTTP 403).",
            str(raised.exception),
        )
        self.assertNotIsInstance(raised.exception, RateLimitedError)


@isolated_env()
class CliDispenserMessageTest(unittest.TestCase):
    def download(self, error: Exception):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("goopdl.cli.pick_pool_token", side_effect=error),
        ):
            return CliRunner().invoke(
                app, ["download", "com.example.pkg", "--output", directory]
            )

    def test_download_tells_rate_limit_from_outage(self) -> None:
        limited = self.download(DispenserRateLimitedError(None))
        self.assertEqual(RATE_LIMIT_EXIT_CODE, limited.exit_code)
        self.assertIn("dispenser rate limited", limited.output.lower())

        down = self.download(DispenserUnavailableError(503))
        self.assertEqual(AUTH_UNAVAILABLE_EXIT_CODE, down.exit_code)
        self.assertIn("unavailable (HTTP 503)", down.output)
        self.assertNotIn("rate limited", down.output.lower())

    def test_fast_download_reports_dispenser_rate_limit(self) -> None:
        error = RateLimitedError("Token dispenser rate limited (HTTP 429).")
        error.__cause__ = DispenserRateLimitedError(None)
        with patch("goopdl.cli.download_app", side_effect=error):
            result = CliRunner().invoke(app, ["fast-download", "com.example.pkg"])
        self.assertEqual(RATE_LIMIT_EXIT_CODE, result.exit_code)
        self.assertIn("dispenser rate limited", result.output.lower())
        self.assertNotIn("Google Play", result.output)

    def test_auth_command_rate_limit_exit_code(self) -> None:
        with patch(
            "goopdl.cli.fetch_token", side_effect=DispenserRateLimitedError(None)
        ):
            result = CliRunner().invoke(app, ["auth", "token"])
        self.assertEqual(RATE_LIMIT_EXIT_CODE, result.exit_code)


if __name__ == "__main__":
    unittest.main()
