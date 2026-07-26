import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from gplaydl.cli import AUTH_UNAVAILABLE_EXIT_CODE, app


class AuthExitCodeTest(unittest.TestCase):
    def test_unavailable_auth_has_dedicated_exit_code(self) -> None:
        with patch("gplaydl.cli.fetch_token", return_value=None):
            result = CliRunner().invoke(app, ["auth", "--arch", "arm64"])
        self.assertEqual(AUTH_UNAVAILABLE_EXIT_CODE, result.exit_code)


if __name__ == "__main__":
    unittest.main()
