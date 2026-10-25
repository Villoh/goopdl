import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
from typer.testing import CliRunner

from goopdl import cli
from goopdl.api import (
    AdditionalFile,
    AppDetails,
    DeliveryResult,
    SplitInfo,
    _extract_delivery_from_fields,
)
from goopdl.download import (
    DownloadSpec,
    IntegrityError,
    _download_one,
    _select_digest,
    download_batch,
    make_progress,
    write_manifest,
)


def field(number, wire_type, value):
    return (number, wire_type, value)


def varint(value):
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def encoded_field(number, wire_type, value):
    output = varint((number << 3) | wire_type)
    if wire_type == 0:
        return output + varint(value)
    return output + varint(len(value)) + value


def digest(content, algorithm):
    value = hashlib.new(algorithm, content).digest()
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


class DeliveryIntegrityTest(unittest.TestCase):
    def test_parses_base_and_split_integrity_fields(self):
        base_sha1 = digest(b"base", "sha1")
        base_sha256 = digest(b"base", "sha256")
        split_sha1 = digest(b"split", "sha1")
        split_sha256 = digest(b"split", "sha256")
        split = b"".join(
            [
                encoded_field(1, 2, b"config.arm64_v8a"),
                encoded_field(2, 0, 5),
                encoded_field(4, 2, split_sha1.encode("ascii")),
                encoded_field(5, 2, b"https://play.googleapis.com/download/split"),
                encoded_field(9, 2, split_sha256.encode("ascii")),
            ]
        )
        delivery = _extract_delivery_from_fields(
            [
                field(1, 0, 4),
                field(2, 2, base_sha1.encode("ascii")),
                field(3, 2, b"https://play.googleapis.com/download/base"),
                field(15, 2, split),
                field(19, 2, base_sha256.encode("ascii")),
            ]
        )

        self.assertEqual(
            (delivery.download_size, delivery.sha1, delivery.sha256),
            (4, base_sha1, base_sha256),
        )
        self.assertEqual(
            (
                delivery.splits[0].size,
                delivery.splits[0].sha1,
                delivery.splits[0].sha256,
            ),
            (5, split_sha1, split_sha256),
        )

    def test_sha256_preferred_and_sha1_fallback(self):
        sha1 = digest(b"base", "sha1")
        sha256 = digest(b"base", "sha256")
        spec = DownloadSpec(
            "url", Path("base.apk"), expected_size=4, sha1=sha1, sha256=sha256
        )
        self.assertEqual(_select_digest(spec)[:2], ("sha256", sha256))
        spec.sha256 = ""
        self.assertEqual(_select_digest(spec)[:2], ("sha1", sha1))

    def test_rejects_malformed_or_missing_digest(self):
        short = base64.urlsafe_b64encode(b"short").decode("ascii").rstrip("=")
        invalid = ["+" * 43, "A" * 42 + "=", short, ""]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(IntegrityError):
                _select_digest(
                    DownloadSpec("url", Path("base.apk"), expected_size=4, sha256=value)
                )

    def test_manifest_contains_no_url_or_cookies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sha1 = digest(b"base", "sha1")
            encoded = digest(b"base", "sha256")
            spec = DownloadSpec(
                "https://secret.invalid/token",
                root / "base.apk",
                cookies=[{"name": "auth", "value": "secret"}],
                expected_size=4,
                sha1=sha1,
                sha256=encoded,
            )
            manifest = root / "manifest.json"
            write_manifest(manifest, [spec])
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(
                data,
                {
                    "version": 1,
                    "files": [
                        {
                            "path": "base.apk",
                            "size": 4,
                            "algorithm": "sha256",
                            "digest": encoded,
                            "google_sha1": sha1,
                            "google_sha256": encoded,
                        }
                    ],
                },
            )
            self.assertNotIn("secret", manifest.read_text(encoding="utf-8"))


class DownloadErrorRedactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_http_error_does_not_expose_url(self):
        class FailingStream:
            async def __aenter__(self):
                raise httpx.ConnectError("https://example.invalid/?token=secret")

            async def __aexit__(self, *_args):
                return False

        class Client:
            def stream(self, *_args, **_kwargs):
                return FailingStream()

        with tempfile.TemporaryDirectory() as directory:
            spec = DownloadSpec(
                "https://example.invalid/?token=secret",
                Path(directory) / "base.apk",
                label="base.apk",
            )
            with self.assertRaisesRegex(
                IntegrityError, "download failed for base.apk"
            ) as raised:
                await _download_one(
                    spec,
                    cast(httpx.AsyncClient, Client()),
                    make_progress(),
                    asyncio.Semaphore(1),
                )
            self.assertNotIn("secret", str(raised.exception))


class IncrementalDownloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_skips_existing_verified_file(self):
        class Client:
            def stream(self, *_args, **_kwargs):
                raise AssertionError("network must not be used")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "base.apk"
            path.write_bytes(b"base")
            spec = DownloadSpec(
                "https://example.invalid/base",
                path,
                expected_size=4,
                sha256=digest(b"base", "sha256"),
            )

            result = await _download_one(
                spec,
                cast(httpx.AsyncClient, Client()),
                make_progress(),
                asyncio.Semaphore(1),
            )

            self.assertEqual(result, path)
            self.assertEqual(path.read_bytes(), b"base")

    async def test_replaces_existing_file_when_digest_does_not_match(self):
        class Stream:
            headers = {"Content-Length": "4"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            async def aiter_bytes(self, chunk_size):
                yield b"good"

        class Client:
            def stream(self, *_args, **_kwargs):
                return Stream()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "base.apk"
            path.write_bytes(b"bad!")
            spec = DownloadSpec(
                "https://example.invalid/base",
                path,
                expected_size=4,
                sha256=digest(b"good", "sha256"),
            )

            await _download_one(
                spec,
                cast(httpx.AsyncClient, Client()),
                make_progress(),
                asyncio.Semaphore(1),
            )

            self.assertEqual(path.read_bytes(), b"good")


class RequireIntegrityTest(unittest.TestCase):
    @patch("goopdl.download._run_downloads", new_callable=AsyncMock)
    def test_skips_files_google_sends_without_digest(self, run):
        apk = DownloadSpec(
            url="https://play.googleapis.com/base",
            dest=Path("base.apk"),
            integrity_required=True,
        )
        obb = DownloadSpec(url="https://play.googleapis.com/obb", dest=Path("x.obb"))

        download_batch([obb], require_integrity=True)
        run.assert_awaited_once()
        with self.assertRaises(IntegrityError):
            download_batch([apk, obb], require_integrity=True)


class MultiArchManifestTest(unittest.TestCase):
    def test_manifest_covers_every_architecture(self):
        def delivery(split):
            return DeliveryResult(
                download_url="https://play.googleapis.com/base",
                download_size=4,
                sha256=digest(b"base", "sha256"),
                splits=[
                    SplitInfo(
                        split,
                        url=f"https://play.googleapis.com/{split}",
                        size=5,
                        sha256=digest(split.encode(), "sha256"),
                    )
                ],
            )

        deliveries = {
            "arm64": delivery("config.arm64_v8a"),
            "armv7": delivery("config.armeabi_v7a"),
        }
        # Google sends no digest for OBB files; they must not block the manifest.
        deliveries["arm64"].additional_files.append(
            AdditionalFile(size=3, url="https://play.googleapis.com/obb")
        )

        def fake_batch(specs, require_integrity=False):
            self.assertTrue(require_integrity)
            for spec in specs:
                spec.dest.write_bytes(b"x")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            with (
                patch("goopdl.cli.pick_pool_token", return_value={"authToken": "t"}),
                patch(
                    "goopdl.cli._acquire_delivery",
                    side_effect=lambda package, vc, arch, *rest: (
                        AppDetails(package, version_string="1.0"),
                        7,
                        deliveries[arch],
                    ),
                ),
                patch("goopdl.cli.download_batch", side_effect=fake_batch),
            ):
                result = CliRunner().invoke(
                    cli.app,
                    [
                        "download", "pkg", "-a", "arm64,armv7", "--splits", "--extras",
                        "-o", str(root), "--integrity-manifest", str(manifest),
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            paths = [
                entry["path"]
                for entry in json.loads(manifest.read_text(encoding="utf-8"))["files"]
            ]
            self.assertEqual(
                paths,
                [
                    "pkg-7.apk",
                    "pkg-7-config.arm64_v8a.apk",
                    "pkg-7-config.armeabi_v7a.apk",
                ],
            )


if __name__ == "__main__":
    unittest.main()
