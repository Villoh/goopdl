import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from goopdl import cli
from goopdl.api import RateLimitedError, VersionUnavailableError


class InspectDeliveryTest(unittest.TestCase):
    def test_writes_delivery_json_without_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "delivery.json"
            with (
                patch("goopdl.cli._require_auth", return_value={}),
                patch(
                    "goopdl.cli._acquire_delivery",
                    return_value=(
                        SimpleNamespace(version_string="21.04.223"),
                        1561052632,
                        SimpleNamespace(
                            splits=[
                                SimpleNamespace(name="config.armeabi_v7a"),
                                SimpleNamespace(name="config.en"),
                            ]
                        ),
                    ),
                ),
            ):
                cli.inspect_delivery_cmd(
                    package="com.google.android.youtube",
                    version=1561052632,
                    arch="armv7",
                    dispenser=None,
                    country=None,
                    proxy=None,
                    profile=None,
                    json_output=True,
                    output=output,
                )
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                {
                    "architecture": "armv7",
                    "package": "com.google.android.youtube",
                    "splits": ["base", "config.armeabi_v7a", "config.en"],
                    "version": {"code": 1561052632, "name": "21.04.223"},
                },
            )

    def test_rate_limit_uses_dedicated_exit_code(self) -> None:
        with (
            patch("goopdl.cli._require_auth", return_value={}),
            patch(
                "goopdl.cli._acquire_delivery",
                side_effect=RateLimitedError("rate limited"),
            ),
        ):
            try:
                cli.inspect_delivery_cmd(
                    package="com.google.android.youtube",
                    version=1,
                    arch="arm64",
                    dispenser=None,
                    country=None,
                    proxy=None,
                    profile=None,
                    json_output=True,
                    output=None,
                )
            except cli.typer.Exit as raised:
                exit_code = raised.exit_code
            else:
                self.fail("inspect-delivery did not exit")

        self.assertEqual(cli.RATE_LIMIT_EXIT_CODE, exit_code)

    def test_unavailable_version_keeps_version_exit_code(self) -> None:
        with (
            patch("goopdl.cli._require_auth", return_value={}),
            patch(
                "goopdl.cli._acquire_delivery",
                side_effect=VersionUnavailableError("version unavailable"),
            ),
        ):
            try:
                cli.inspect_delivery_cmd(
                    package="com.google.android.youtube",
                    version=1,
                    arch="arm64",
                    dispenser=None,
                    country=None,
                    proxy=None,
                    profile=None,
                    json_output=True,
                    output=None,
                )
            except cli.typer.Exit as raised:
                exit_code = raised.exit_code
            else:
                self.fail("inspect-delivery did not exit")

        self.assertEqual(cli.VERSION_UNAVAILABLE_EXIT_CODE, exit_code)


if __name__ == "__main__":
    unittest.main()
