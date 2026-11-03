"""Hermetic regressions for CDN streaming and delivery extras."""

import asyncio
import base64
import contextlib
import gzip
import hashlib
import os
import tempfile
import unittest
import zlib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import httpx

from goopdl.api import NetworkError
from goopdl.download import (
    DownloadError,
    DownloadSpec,
    IntegrityError,
    _download_one,
    download_batch,
    download_files,
)


def digest(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


class IsolatedEnvironment(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def assert_no_files(self):
        self.assertEqual(list(self.root.iterdir()), [])


class DownloadHardeningTest(IsolatedEnvironment, unittest.IsolatedAsyncioTestCase):
    async def test_progress_http_errors_propagate_unchanged_and_clean_partial(self):
        for error_type in (NetworkError, httpx.ConnectError, zlib.error):
            for after_write in (False, True):
                with self.subTest(error=error_type.__name__, after_write=after_write):
                    error = error_type("caller callback failed")

                    def on_progress(_label, done, _total, error=error, after_write=after_write):
                        if bool(done) == after_write:
                            raise error

                    async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda _request: httpx.Response(200, content=b"payload")
                    )) as client:
                        with patch("goopdl.download.CHUNK_SIZE", 2):
                            try:
                                await download_files([
                                    DownloadSpec("https://play.googleapis.com/apk", self.root / "base.apk")
                                ], client=client, on_progress=on_progress)
                            except Exception as caught:
                                self.assertIs(caught, error)
                                self.assertNotIsInstance(caught, DownloadError)
                            else:
                                self.fail("expected caller callback exception")
                    self.assert_no_files()

    async def test_rejects_unsafe_redirect_before_request_with_injected_client(self):
        for location in (
            "http://127.0.0.1:8080/private",
            "https://attacker.invalid/private?token=SIGNED_MARK",
            "http://play.googleapis.com/private",
            "https://play.googleapis.com.attacker.invalid/private",
        ):
            with self.subTest(location=location):
                requests = []

                def handler(request, requests=requests, location=location):
                    requests.append(request)
                    if len(requests) == 1:
                        return httpx.Response(302, headers={"Location": location})
                    return httpx.Response(200, content=b"private")

                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler), follow_redirects=True
                ) as client:
                    with self.assertRaises(DownloadError) as raised:
                        await download_files([
                            DownloadSpec("https://play.googleapis.com/apk", self.root / "base.apk")
                        ], client=client)
                self.assertFalse(raised.exception.retryable)
                self.assertIsNone(raised.exception.status_code)
                self.assertNotIn(location, str(raised.exception))
                self.assertEqual(len(requests), 1)
                self.assert_no_files()

    async def test_safe_relative_redirect_and_cross_host_strip_credentials(self):
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={"Location": "/next"})
            if len(requests) == 2:
                return httpx.Response(307, headers={"Location": "https://files.googleusercontent.com/final"})
            if len(requests) == 3:
                return httpx.Response(302, headers={"Location": "/final-again"})
            return httpx.Response(200, content=b"good")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True,
            auth=("user", "password"),
            headers={"Proxy-Authorization": "secret-proxy"},
            cookies={"client-auth": "client-cookie-secret"},
        ) as client:
            await download_files([
                DownloadSpec("https://play.googleapis.com/apk", self.root / "base.apk",
                             cookies=[{"name": "auth", "value": "cookie-secret"}])
            ], client=client)
        self.assertEqual((self.root / "base.apk").read_bytes(), b"good")
        self.assertIn("authorization", requests[0].headers)
        self.assertEqual(len(requests), 4)
        for request in requests[2:]:
            for header in ("authorization", "proxy-authorization", "cookie"):
                self.assertNotIn(header, request.headers)

    async def test_redirect_limit_is_non_retryable(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "/again"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as client:
            with self.assertRaises(DownloadError) as raised:
                await download_files([
                    DownloadSpec("https://play.googleapis.com/apk", self.root / "base.apk")
                ], client=client)
        self.assertFalse(raised.exception.retryable)
        self.assertLessEqual(len(requests), 6)
        self.assert_no_files()

    async def test_error_has_no_secret_context_cause_or_library_traceback_locals(self):
        for failure in (403, httpx.ReadError, "gzip"):
            for direct in (False, True):
                with self.subTest(failure=failure, direct=direct):
                    def handler(request, failure=failure):
                        if isinstance(failure, type):
                            raise failure("SIGNED_MARK", request=request)
                        if failure == "gzip":
                            return httpx.Response(200, content=b"not gzip")
                        return httpx.Response(failure)

                    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                        spec = DownloadSpec(
                            "https://play.googleapis.com/apk?token=SIGNED_MARK",
                            self.root / "base.apk", gzipped=failure == "gzip",
                        )
                        try:
                            if direct:
                                await _download_one(spec, client, None, asyncio.Semaphore(1))
                            else:
                                await download_files([spec], client=client)
                        except DownloadError as caught:
                            error = caught
                        else:
                            self.fail("expected DownloadError")
                    self.assertIsNone(error.__context__)
                    self.assertIsNone(error.__cause__)
                    self.assertNotIn("SIGNED_MARK", str(error))
                    self.assertNotIn("SIGNED_MARK", repr(error))
                    tb = error.__traceback__
                    while tb:
                        # Caller-owned frames necessarily retain their own input spec.
                        if tb.tb_frame.f_globals.get("__name__", "").startswith("goopdl."):
                            self.assertNotIn("SIGNED_MARK", repr(tb.tb_frame.f_locals))
                        tb = tb.tb_next
                    self.assert_no_files()

    async def test_truncated_gzip_is_retryable_and_never_published(self):
        compressed = gzip.compress(b"payload-" * 50)
        for body in (compressed[:-10], compressed[:len(compressed) // 2]):
            with self.subTest(length=len(body)):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _request, body=body: httpx.Response(200, content=body)
                )) as client:
                    with self.assertRaisesRegex(DownloadError, "truncated gzip stream") as raised:
                        await download_files([
                            DownloadSpec("https://play.googleapis.com/obb", self.root / "main.obb", gzipped=True)
                        ], client=client)
                self.assertTrue(raised.exception.retryable)
                self.assert_no_files()

    async def test_aborts_size_overrun_before_writing_excess_or_inflating_bomb(self):
        for gzipped in (False, True):
            with self.subTest(gzipped=gzipped):
                writes = []
                budgets = []
                original_open = Path.open
                original_decompressobj = zlib.decompressobj

                @contextlib.contextmanager
                def tracked_open(path, *args, _open=original_open, **kwargs):
                    with _open(path, *args, **kwargs) as stream:
                        class Writer:
                            def write(self, data):
                                writes.append(len(data))
                                return stream.write(data)
                        yield Writer()

                class Decompressor:
                    def __init__(self, *args, _factory=original_decompressobj):
                        self.inner = _factory(*args)

                    def decompress(self, data, max_length=0):
                        budgets.append(max_length)
                        return self.inner.decompress(data, max_length)

                    def __getattr__(self, name):
                        return getattr(self.inner, name)

                body = b"x" * 1_000_000
                if gzipped:
                    body = gzip.compress(body)
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _request, body=body: httpx.Response(200, content=body)
                )) as client:
                    with (
                        patch.object(Path, "open", tracked_open),
                        patch("goopdl.download.zlib.decompressobj", Decompressor),
                        self.assertRaisesRegex(IntegrityError, "size mismatch"),
                    ):
                        await download_files([
                            DownloadSpec("https://play.googleapis.com/apk", self.root / "base.apk",
                                         gzipped=gzipped, expected_size=1, sha256=digest(b"x"))
                        ], client=client)
                self.assertLessEqual(sum(writes), 1)
                if gzipped:
                    self.assertTrue(budgets)
                    self.assertTrue(all(0 < limit <= 2 for limit in budgets), budgets)
                self.assert_no_files()

    async def test_valid_gzip_size_and_digest_across_chunks(self):
        body = bytes(range(256)) * 100
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=gzip.compress(body))
        )) as client:
            with patch("goopdl.download.CHUNK_SIZE", 11):
                await download_files([
                    DownloadSpec("https://play.googleapis.com/apk", self.root / "base.apk",
                                 gzipped=True, expected_size=len(body), sha256=digest(body))
                ], client=client)
        self.assertEqual((self.root / "base.apk").read_bytes(), body)

    async def test_retry_after_from_cdn_status(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=60), usegmt=True)
        for status in (429, 503):
            for value in ("12.5", future, "invalid", "-1", "nan", "inf", None):
                with self.subTest(status=status, retry_after=value):
                    headers = {"Retry-After": value} if value is not None else {}
                    async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda _request, status=status, headers=headers: httpx.Response(status, headers=headers)
                    )) as client:
                        with self.assertRaises(DownloadError) as raised:
                            await download_files([
                                DownloadSpec("https://play.googleapis.com/apk", self.root / "base.apk")
                            ], client=client)
                    error = raised.exception
                    self.assertTrue(error.retryable)
                    self.assertEqual(error.status_code, status)
                    if value == "12.5":
                        self.assertEqual(error.retry_after, 12.5)
                    elif value == future:
                        assert error.retry_after is not None
                        self.assertGreater(error.retry_after, 40)
                        self.assertLessEqual(error.retry_after, 60)
                    else:
                        self.assertIsNone(error.retry_after)
                    self.assert_no_files()

    async def test_size_only_extra_without_digest_validates_actual_bytes(self):
        for size, succeeds in ((3, True), (12, False)):
            with self.subTest(size=size):
                dest = self.root / "main.obb"
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, content=b"obb")
                )) as client:
                    spec = DownloadSpec("https://play.googleapis.com/obb", dest, expected_size=size)
                    if succeeds:
                        await download_files([spec], require_integrity=True, client=client)
                        self.assertEqual(dest.read_bytes(), b"obb")
                        dest.unlink()
                    else:
                        with self.assertRaisesRegex(IntegrityError, "size mismatch"):
                            await download_files([spec], require_integrity=True, client=client)
                self.assert_no_files()


class SyncDownloadHardeningTest(IsolatedEnvironment):
    def test_batch_error_does_not_retain_signed_url_in_library_frames(self):
        real_client = httpx.AsyncClient

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(
                lambda _request: httpx.Response(403)
            ), **kwargs)

        with patch("goopdl.download.httpx.AsyncClient", client):
            try:
                download_batch([
                    DownloadSpec("https://play.googleapis.com/apk?token=SIGNED_MARK", self.root / "base.apk")
                ])
            except DownloadError as caught:
                error = caught
            else:
                self.fail("expected DownloadError")
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        tb = error.__traceback__
        while tb:
            if tb.tb_frame.f_globals.get("__name__", "").startswith("goopdl."):
                self.assertNotIn("SIGNED_MARK", repr(tb.tb_frame.f_locals))
            tb = tb.tb_next
        self.assert_no_files()

    def test_batch_checks_extra_size_without_requiring_digest(self):
        real_client = httpx.AsyncClient

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=b"obb")
            ), **kwargs)

        with patch("goopdl.download.httpx.AsyncClient", client):
            with self.assertRaisesRegex(IntegrityError, "size mismatch"):
                download_batch([
                    DownloadSpec("https://play.googleapis.com/obb", self.root / "main.obb", expected_size=12)
                ], require_integrity=True)
            download_batch([
                DownloadSpec("https://play.googleapis.com/obb", self.root / "main.obb", expected_size=3)
            ], require_integrity=True)
        self.assertEqual((self.root / "main.obb").read_bytes(), b"obb")


if __name__ == "__main__":
    unittest.main()
