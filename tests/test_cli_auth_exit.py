import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from goopdl.cli import (
    AUTH_UNAVAILABLE_EXIT_CODE,
    RATE_LIMIT_EXIT_CODE,
    VERSION_UNAVAILABLE_EXIT_CODE,
    RateLimitedError,
    VersionUnavailableError,
    app,
)


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
