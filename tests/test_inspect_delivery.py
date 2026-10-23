import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from goopdl import cli
from goopdl.api import (
    AppDetails,
    DeliveryResult,
    RateLimitedError,
    SplitInfo,
    VersionUnavailableError,
)


class InspectDeliveryTest(unittest.TestCase):
    def test_writes_delivery_json_without_downloading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "delivery.json"
            with (
                patch("goopdl.cli._require_auth", return_value={}),
                patch(
                    "goopdl.cli._acquire_delivery",
                    return_value=(
                        AppDetails(
                            "com.google.android.youtube",
                            version_string="21.04.223",
                            version_code=1561052632,
                        ),
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

    def test_only_reports_version_name_for_matching_details(self) -> None:
        for requested_code, listed_name, expected_name in (
            (2463, "6.99.4", None),
            (2470, "6.99.4", None),
            (2468, "6.99.4", "6.99.4"),
            (2468, "", None),
        ):
            for mode in ("text", "json", "file"):
                with self.subTest(
                    requested_code=requested_code, listed_name=listed_name, mode=mode
                ), tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "delivery.json"
                    args = [
                        "inspect-delivery",
                        "com.duolingo",
                        "--arch", "arm64",
                        "--version", str(requested_code),
                    ]
                    if mode == "json":
                        args.append("--json")
                    elif mode == "file":
                        args.extend(["--json", "--output", str(output)])
                    with (
                        patch("goopdl.cli.direct_auth_enabled", return_value=False),
                        patch("goopdl.cli._require_auth", return_value={}),
                        patch(
                            "goopdl.cli.get_details",
                            return_value=AppDetails(
                                "com.duolingo",
                                version_code=2468,
                                version_string=listed_name,
                            ),
                        ),
                        patch("goopdl.cli.purchase", return_value="delivery-token"),
                        patch(
                            "goopdl.cli.get_delivery",
                            return_value=DeliveryResult(
                                splits=[SplitInfo(name="config.arm64_v8a")]
                            ),
                        ) as get_delivery,
                    ):
                        result = CliRunner().invoke(cli.app, args)

                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertEqual(get_delivery.call_args.args[1], requested_code)
                    if mode == "text":
                        self.assertIn(
                            f"com.duolingo {expected_name or 'version name unknown'} "
                            f"({requested_code})",
                            result.stdout,
                        )
                        self.assertIn("- base", result.stdout)
                        self.assertIn("- config.arm64_v8a", result.stdout)
                    else:
                        payload = json.loads(
                            output.read_text(encoding="utf-8")
                            if mode == "file"
                            else result.stdout
                        )
                        self.assertEqual(
                            payload["version"],
                            {"code": requested_code, "name": expected_name},
                        )
                        self.assertEqual(
                            payload["splits"], ["base", "config.arm64_v8a"]
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
