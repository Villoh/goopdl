import asyncio
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from gpdl.api import _extract_delivery_from_fields
from gpdl.download import (
    DownloadSpec,
    IntegrityError,
    _download_one,
    _select_digest,
    _write_manifest,
    make_progress,
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

        self.assertEqual((delivery.download_size, delivery.sha1, delivery.sha256), (4, base_sha1, base_sha256))
        self.assertEqual(
            (delivery.splits[0].size, delivery.splits[0].sha1, delivery.splits[0].sha256),
            (5, split_sha1, split_sha256),
        )

    def test_sha256_preferred_and_sha1_fallback(self):
        sha1 = digest(b"base", "sha1")
        sha256 = digest(b"base", "sha256")
        spec = DownloadSpec("url", Path("base.apk"), expected_size=4, sha1=sha1, sha256=sha256)
        self.assertEqual(_select_digest(spec)[:2], ("sha256", sha256))
        spec.sha256 = ""
        self.assertEqual(_select_digest(spec)[:2], ("sha1", sha1))

    def test_rejects_malformed_or_missing_digest(self):
        short = base64.urlsafe_b64encode(b"short").decode("ascii").rstrip("=")
        invalid = ["+" * 43, "A" * 42 + "=", short, ""]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(IntegrityError):
                _select_digest(DownloadSpec("url", Path("base.apk"), expected_size=4, sha256=value))

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
            _write_manifest(manifest, [spec])
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
            with self.assertRaisesRegex(IntegrityError, "download failed for base.apk") as raised:
                await _download_one(
                    spec,
                    Client(),
                    make_progress(),
                    asyncio.Semaphore(1),
                )
            self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
