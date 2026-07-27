import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from gplaydl.cli import (
    AUTH_UNAVAILABLE_EXIT_CODE,
    VERSION_UNAVAILABLE_EXIT_CODE,
    VersionUnavailableError,
    app,
)


class AuthExitCodeTest(unittest.TestCase):
    def test_unavailable_auth_has_dedicated_exit_code(self) -> None:
        with patch("gplaydl.cli.fetch_token", return_value=None):
            result = CliRunner().invoke(app, ["auth", "--arch", "arm64"])
        self.assertEqual(AUTH_UNAVAILABLE_EXIT_CODE, result.exit_code)

    def test_unavailable_version_has_dedicated_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "gplaydl.cli.pick_pool_token", return_value={"authToken": "token"}
        ), patch(
            "gplaydl.cli._resolve_version_string",
            side_effect=VersionUnavailableError("version unavailable"),
        ):
            result = CliRunner().invoke(
                app,
                ["download", "com.example.app", "--version", "1.2.3", "--output", str(Path(directory) / "output")],
            )
        self.assertEqual(VERSION_UNAVAILABLE_EXIT_CODE, result.exit_code)


if __name__ == "__main__":
    unittest.main()
