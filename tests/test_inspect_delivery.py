import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from goopdl import cli


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


if __name__ == "__main__":
    unittest.main()
