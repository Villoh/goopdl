import base64
import contextlib
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from goopdl.download import (
    DownloadError,
    DownloadSpec,
    IntegrityError,
    download_batch,
    download_files,
)


def sha256(content):
    value = hashlib.sha256(content).digest()
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class DownloadFilesTest(unittest.IsolatedAsyncioTestCase):
    async def test_runs_in_event_loop_without_stdout_and_reports_progress(self):
        events = []
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "base.apk"
            spec = DownloadSpec(
                "https://cdn.invalid/base",
                dest,
                label="base.apk",
                expected_size=4,
                sha256=sha256(b"good"),
            )
            async with mock_client(lambda _r: httpx.Response(200, content=b"good")) as client:
                with contextlib.redirect_stdout(stdout):
                    await download_files(
                        [spec],
                        require_integrity=True,
                        on_progress=lambda *event: events.append(event),
                        client=client,
                    )
            self.assertEqual(dest.read_bytes(), b"good")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(events[0], ("base.apk", 0, 4))
        self.assertEqual(events[-1], ("base.apk", 4, 4))

    async def test_cdn_status_errors_are_download_errors(self):
        for status, retryable in ((403, False), (429, True), (503, True)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                spec = DownloadSpec(
                    "https://cdn.invalid/base?token=secret",
                    Path(directory) / "base.apk",
                    label="base.apk",
                )
                async with mock_client(lambda _r, s=status: httpx.Response(s)) as client:
                    with self.assertRaises(DownloadError) as raised:
                        await download_files([spec], client=client)
                # Compat: pre-existing "except IntegrityError" still catches it.
                self.assertIsInstance(raised.exception, IntegrityError)
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertNotIn("secret", str(raised.exception))
                self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_timeout_is_retryable_download_error(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out", request=request)

        with tempfile.TemporaryDirectory() as directory:
            spec = DownloadSpec("https://cdn.invalid/b", Path(directory) / "b.apk")
            async with mock_client(handler) as client:
                with self.assertRaises(DownloadError) as raised:
                    await download_files([spec], client=client)
        self.assertIsNone(raised.exception.status_code)
        self.assertTrue(raised.exception.retryable)

    async def test_digest_mismatch_stays_integrity_error(self):
        with tempfile.TemporaryDirectory() as directory:
            spec = DownloadSpec(
                "https://cdn.invalid/base",
                Path(directory) / "base.apk",
                label="base.apk",
                expected_size=4,
                sha256=sha256(b"good"),
            )
            async with mock_client(lambda _r: httpx.Response(200, content=b"bad!")) as client:
                with self.assertRaisesRegex(IntegrityError, "digest mismatch") as raised:
                    await download_files([spec], client=client)
            self.assertNotIsInstance(raised.exception, DownloadError)

    async def test_download_batch_in_running_loop_points_to_download_files(self):
        with self.assertRaisesRegex(RuntimeError, "download_files"):
            download_batch([])


class SyncDownloadBatchTest(unittest.TestCase):
    def test_download_batch_still_works_without_a_loop(self):
        def handler(_request):
            return httpx.Response(200, content=b"good")

        real_client = httpx.AsyncClient

        def fake_client(**kwargs):
            return real_client(transport=httpx.MockTransport(handler))

        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "base.apk"
            spec = DownloadSpec("https://cdn.invalid/base", dest, label="base.apk")
            with patch("goopdl.download.httpx.AsyncClient", fake_client):
                download_batch([spec])
            self.assertEqual(dest.read_bytes(), b"good")


if __name__ == "__main__":
    unittest.main()
