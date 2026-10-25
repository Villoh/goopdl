import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goopdl.api import AppDetails, DeliveryResult
from goopdl.cli import (
    AUTH_UNAVAILABLE_EXIT_CODE,
    RATE_LIMIT_EXIT_CODE,
    VERSION_UNAVAILABLE_EXIT_CODE,
    RateLimitedError,
    VersionUnavailableError,
    app,
)
from goopdl.download import IntegrityError


class AuthExitCodeTest(unittest.TestCase):
    def test_unavailable_auth_has_dedicated_exit_code(self) -> None:
        with patch("goopdl.cli.fetch_token", return_value=None):
            result = CliRunner().invoke(app, ["auth", "--arch", "arm64"])
        self.assertEqual(AUTH_UNAVAILABLE_EXIT_CODE, result.exit_code)

    def test_rate_limited_delivery_has_dedicated_exit_code(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("goopdl.cli.pick_pool_token", return_value={"authToken": "token"}),
            patch(
                "goopdl.cli._acquire_delivery",
                side_effect=RateLimitedError("rate limited"),
            ),
        ):
            result = CliRunner().invoke(
                app,
                [
                    "download",
                    "com.example.app",
                    "--output",
                    str(Path(directory) / "output"),
                ],
            )

        self.assertEqual(RATE_LIMIT_EXIT_CODE, result.exit_code)
        self.assertIn("HTTP 429", result.output)
        self.assertIn("rate limited", result.output.lower())
        self.assertNotIn("version unavailable", result.output.lower())

    def test_download_integrity_error_is_reported_without_success(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("goopdl.cli.pick_pool_token", return_value={"authToken": "token"}),
            patch(
                "goopdl.cli._acquire_delivery",
                return_value=(
                    AppDetails(package="pkg", title="Example", version_code=1),
                    1,
                    DeliveryResult(download_url="https://example.com/secret"),
                ),
            ),
            patch(
                "goopdl.cli.download_batch",
                side_effect=IntegrityError("digest mismatch for pkg-1.apk"),
            ) as download_batch,
        ):
            result = CliRunner().invoke(
                app, ["download", "pkg", "--output", directory]
            )

        download_batch.assert_called_once()
        self.assertEqual(1, result.exit_code)
        self.assertIn("digest mismatch for pkg-1.apk", result.output)
        self.assertIsInstance(result.exception, SystemExit)
        self.assertNotIn("IntegrityError", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertNotIn("secret", result.output)
        self.assertNotIn("Downloaded files", result.output)
        self.assertNotIn("Download complete!", result.output)

    def test_fast_download_integrity_error_is_reported_without_success(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "goopdl.cli.download_app",
                side_effect=IntegrityError("digest mismatch for pkg-1.apk"),
            ) as download_app,
        ):
            result = CliRunner().invoke(
                app, ["fast-download", "pkg", "--output", directory]
            )

        download_app.assert_called_once()
        self.assertEqual(1, result.exit_code)
        self.assertIn("digest mismatch for pkg-1.apk", result.output)
        self.assertIsInstance(result.exception, SystemExit)
        self.assertNotIn("IntegrityError", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertNotIn("Downloaded", result.output)

    def test_unavailable_version_has_dedicated_exit_code(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("goopdl.cli.pick_pool_token", return_value={"authToken": "token"}),
            patch(
                "goopdl.cli._resolve_version_string",
                side_effect=VersionUnavailableError("version unavailable"),
            ),
        ):
            result = CliRunner().invoke(
                app,
                [
                    "download",
                    "com.example.app",
                    "--version",
                    "1.2.3",
                    "--output",
                    str(Path(directory) / "output"),
                ],
            )
        self.assertEqual(VERSION_UNAVAILABLE_EXIT_CODE, result.exit_code)


if __name__ == "__main__":
    unittest.main()
