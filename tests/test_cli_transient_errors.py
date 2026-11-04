import tempfile
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from _envutil import isolated_env
from goopdl.api import AppNotSupportedError, NetworkError, RateLimitedError
from goopdl.cli import (
    RATE_LIMIT_EXIT_CODE,
    _acquire_delivery,
    _resolve_version_string,
    app,
)

TOKEN = {"authToken": "token", "gsfId": "1234567890"}
PKG = "com.example.pkg"


@isolated_env()
@patch("goopdl.cli.time.sleep")
class TransientErrorsStopRetriesTest(unittest.TestCase):
    """Network failures and throttling must not rotate to another profile/token."""

    def test_network_error_does_not_try_the_next_profile(self, _sleep) -> None:
        with (
            patch(
                "goopdl.cli.get_details",
                side_effect=[AppNotSupportedError("no"), NetworkError("down")],
            ),
            patch(
                "goopdl.cli.get_compat_profiles",
                return_value=[("first", {}), ("second", {})],
            ),
            patch("goopdl.cli.fetch_token", return_value=TOKEN) as fetch,
            self.assertRaises(NetworkError),
        ):
            _acquire_delivery(PKG, None, "arm64", TOKEN, None, None, None, None)
        self.assertEqual(
            [call.kwargs["profile"] for call in fetch.call_args_list], ["first"]
        )

    def test_resolve_version_stops_on_first_transient_error(self, _sleep) -> None:
        for error in (NetworkError("down"), RateLimitedError("slow")):
            with (
                self.subTest(type(error).__name__),
                patch("goopdl.cli.get_details", side_effect=error),
                patch("goopdl.cli.fetch_token", return_value=TOKEN) as fetch,
            ):
                with self.assertRaises(type(error)):
                    _resolve_version_string(
                        PKG, "1.2.3", "arm64", None, None, None, None
                    )
                self.assertEqual(fetch.call_count, 1)

    def test_download_version_string_rate_limit_exits_with_code_5(self, _sleep) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("goopdl.cli.pick_pool_token", return_value=TOKEN),
            patch("goopdl.cli.fetch_token", return_value=TOKEN),
            patch("goopdl.cli.get_details", side_effect=RateLimitedError("slow")),
        ):
            result = CliRunner().invoke(
                app,
                ["download", PKG, "--version", "1.2.3", "--output", directory],
            )
        self.assertEqual(RATE_LIMIT_EXIT_CODE, result.exit_code)
        self.assertIn("rate limited", result.output.lower())

    def test_latest_stops_probing_on_transient_errors(self, _sleep) -> None:
        for error, code in ((NetworkError("down"), 1), (RateLimitedError("slow"), 5)):
            with (
                self.subTest(type(error).__name__),
                patch("goopdl.cli.ensure_pool", return_value=[TOKEN, TOKEN, TOKEN]),
                patch("goopdl.cli.get_details", side_effect=error) as details,
            ):
                result = CliRunner().invoke(app, ["latest", PKG])
                self.assertEqual(code, result.exit_code, result.output)
                self.assertEqual(details.call_count, 1)


if __name__ == "__main__":
    unittest.main()
