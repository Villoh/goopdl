"""The CLI must not crash when the console encoding (e.g. cp1252) cannot show a character."""
import io
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from _envutil import isolated_env
from goopdl import cli
from goopdl.api import AppDetails


def strict_cp1252():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


class ConsoleEncodingTest(unittest.TestCase):
    def test_resolve_version_string_prints_only_cp1252_glyphs(self):
        stdout, stderr = strict_cp1252(), strict_cp1252()
        details = AppDetails("com.example", version_code=42, version_string="1.2.3")
        with (
            patch("sys.stdout", stdout),
            patch("sys.stderr", stderr),
            patch("goopdl.cli._fetch_probe_token", return_value={"authToken": "t"}),
            patch("goopdl.cli.get_details", return_value=details),
        ):
            code, _ = cli._resolve_version_string(
                "com.example", "1.2.3", "arm64", None, None, None, None
            )
            stdout.flush()
        self.assertEqual(42, code)
        self.assertIn("->", stdout.buffer.getvalue().decode("cp1252"))

    @isolated_env()
    def test_remote_text_is_replaced_instead_of_crashing(self):
        results = [{"title": "日本語 😀", "package": "com.example"}]
        with (
            patch("goopdl.cli._require_auth", return_value={"authToken": "t"}),
            patch("goopdl.cli.search_apps", return_value=results),
        ):
            # CliRunner's stdout is a strict cp1252 stream here.
            result = CliRunner(charset="cp1252").invoke(cli.app, ["search", "x"])
        self.assertEqual(0, result.exit_code, result.exception)
        self.assertIn("??? ?", result.output)
        self.assertIn("com.example", result.output)


if __name__ == "__main__":
    unittest.main()
