import base64
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from goopdl import cli
from goopdl.api import (
    AdditionalFile,
    AppDetails,
    DeliveryResult,
    DexMetadata,
    SplitInfo,
)
from goopdl.download import DownloadSpec, IntegrityError


class AdbInstallTipTest(unittest.TestCase):
    def test_quotes_exact_selected_paths_and_excludes_obb(self) -> None:
        output = Path("APK files [bold] & $HOME; user's `cache` (1)")
        names = [
            "com.example.app-123.apk",
            "com.example.app-123-config.en.apk",
            "com.example.app-123-asset.apk",
            "com.example.app-123.dm",
            "main.123.com.example.app.obb",
            "notes.txt",
        ]
        specs = [DownloadSpec(url="", dest=output / name) for name in names]
        with patch.object(cli, "os", SimpleNamespace(name="posix"), create=True):
            command = cli._adb_install_command(specs)
        self.assertEqual(
            shlex.split(command),
            ["adb", "install-multiple", *(str(output / name) for name in names[:4])],
        )
        self.assertNotIn("*", command)

    def test_windows_paths_use_literal_powershell_quoting(self) -> None:
        # Patch the CLI's module reference, never pathlib's shared os.name.
        specs = [
            DownloadSpec(
                url="",
                dest=Path(r"C:\APK files [bold] & $HOME; user's %PATH% `x` (1)^\pkg-123.apk"),
            ),
            DownloadSpec(url="", dest=Path(r"C:\APK files\pkg-123-config.en.apk")),
            DownloadSpec(url="", dest=Path(r"C:\APK files\pkg-123.dm")),
        ]
        with patch.object(cli, "os", SimpleNamespace(name="nt"), create=True):
            command = cli._adb_install_command(specs)
        self.assertEqual(
            command,
            "adb install-multiple "
            r"'C:\APK files [bold] & $HOME; user''s %PATH% `x` (1)^\pkg-123.apk' "
            r"'C:\APK files\pkg-123-config.en.apk' "
            r"'C:\APK files\pkg-123.dm'",
        )

    def test_powershell_escapes_all_recognized_single_quotes(self) -> None:
        path = Path("John’s ‘APKs’ ‚cache‛; Write-Output PWN; #") / "pkg-123.apk"
        with patch.object(cli, "os", SimpleNamespace(name="nt")):
            command = cli._adb_install_command([DownloadSpec(url="", dest=path)])
        self.assertIn("John’’s", command)
        self.assertIn("‘‘APKs’’", command)
        self.assertIn("‚‚cache‛‛", command)

    def test_powershell_parser_preserves_literal_arguments(self) -> None:
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if powershell is None:
            if os.name == "nt":
                self.fail("PowerShell is required for the Windows quoting regression")
            self.skipTest("PowerShell is not installed")
        paths = [
            Path("John’s ‘APKs’ ‚cache‛; Write-Output PWN; #") / "pkg-123.apk",
            Path("user's [bold] & $HOME %PATH% `cache`") / "pkg-123-config.en.apk",
        ]
        with patch.object(cli, "os", SimpleNamespace(name="nt")):
            command = cli._adb_install_command([DownloadSpec(url="", dest=p) for p in paths])
        # Stub adb inside the real parser; never execute or require Android tools.
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "function adb { ConvertTo-Json -Compress -InputObject @($args) }; "
            + command
        )
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout), ["install-multiple", *(str(p) for p in paths)]
        )

    def test_download_tip_uses_only_successful_selected_specs(self) -> None:
        for dm, extras, splits, metadata, platform in (
            (False, False, True, True, "posix"),
            (True, False, True, True, "posix"),
            (True, True, True, True, "posix"),
            (True, True, True, False, "posix"),
            (True, True, False, True, "posix"),
            (True, True, True, True, "nt"),
        ):
            with self.subTest(dm=dm, extras=extras, splits=splits, metadata=metadata, platform=platform):
                with tempfile.TemporaryDirectory() as directory:
                    output = Path(directory) / "APK files [bold] & user's $cache [/missing]"
                    output.mkdir(parents=True)
                    stale_names = [
                        "com.example.app-1234.apk",
                        "com.example.app-1234-config.en.apk",
                        "com.example.app-123-config.fr.apk",
                        "com.example.app-123-asset.apk",
                        "com.example.app-123.dm",
                        "com.example.app-1234.dm",
                        "other-123.apk",
                    ]
                    for name in stale_names:
                        (output / name).write_bytes(b"stale")
                    delivery = DeliveryResult(
                        download_url="https://example.com/base",
                        splits=[SplitInfo(name="config.en", url="https://example.com/en")],
                        dex_metadata=DexMetadata(url="https://example.com/dm") if metadata else None,
                        additional_files=[
                            AdditionalFile(file_type=0, version_code=123),
                            AdditionalFile(file_type=1, version_code=123),
                            AdditionalFile(file_type=2),
                        ],
                    )
                    downloaded: list[Path] = []

                    def download_batch(specs: list[DownloadSpec], **_kwargs) -> None:
                        for spec in specs:
                            downloaded.append(spec.dest)
                            spec.dest.write_bytes(b"downloaded")

                    with (
                        patch("goopdl.cli.direct_auth_enabled", return_value=False),
                        patch("goopdl.cli.pick_pool_token", return_value={"authToken": "token"}),
                        patch(
                            "goopdl.cli._acquire_delivery",
                            return_value=(AppDetails(package="com.example.app"), 123, delivery),
                        ),
                        patch("goopdl.cli.download_batch", side_effect=download_batch),
                        patch.object(cli, "os", SimpleNamespace(name=platform), create=True),
                    ):
                        args = ["download", "com.example.app", "--output", str(output)]
                        if splits:
                            args.append("--splits")
                        if dm:
                            args.append("--dm")
                        if extras:
                            args.append("--extras")
                        result = CliRunner().invoke(cli.app, args)

                    self.assertEqual(result.exit_code, 0, result.output)
                    expected_names = ["com.example.app-123.apk"]
                    if splits:
                        expected_names.append("com.example.app-123-config.en.apk")
                    if dm and metadata:
                        expected_names.append("com.example.app-123.dm")
                    if extras:
                        expected_names.extend([
                            "main.123.com.example.app.obb",
                            "patch.123.com.example.app.obb",
                            "com.example.app-123-asset.apk",
                        ])
                    self.assertEqual(downloaded, [output / name for name in expected_names])
                    if splits:
                        command = next(
                            line.strip() for line in result.output.splitlines()
                            if line.strip().startswith("adb install-multiple ")
                        )
                        paths = [str(output / name) for name in expected_names if not name.endswith(".obb")]
                        if platform == "nt":
                            self.assertIn("PowerShell", result.output)
                            expected = "adb install-multiple " + " ".join(
                                "'" + path.replace("'", "''") + "'" for path in paths
                            )
                            self.assertEqual(command, expected)
                        else:
                            self.assertEqual(shlex.split(command), ["adb", "install-multiple", *paths])
                    else:
                        self.assertNotIn("adb install-multiple", result.output)

    def test_failed_download_never_prints_install_tip(self) -> None:
        with (
            tempfile.TemporaryDirectory() as output,
            patch("goopdl.cli.direct_auth_enabled", return_value=False),
            patch("goopdl.cli.pick_pool_token", return_value={"authToken": "token"}),
            patch(
                "goopdl.cli._acquire_delivery",
                return_value=(
                    AppDetails(package="com.example.app"),
                    123,
                    DeliveryResult(splits=[SplitInfo(name="config.en")]),
                ),
            ),
            patch("goopdl.cli.download_batch", side_effect=IntegrityError("bad digest")),
        ):
            result = CliRunner().invoke(
                cli.app, ["download", "com.example.app", "--splits", "--output", output]
            )
        self.assertEqual(result.exit_code, 1)
        self.assertNotIn("adb install-multiple", result.output)


if __name__ == "__main__":
    unittest.main()
