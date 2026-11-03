"""Credential boundaries: inspect every library frame, including hidden chains."""
import asyncio
import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, Mock, patch

import httpx
import typer

from goopdl import aastoken, api, auth, browser_oauth, cli, download


EMAIL = "EMAIL_SENTINEL@example.test"
OAUTH = "oauth2_4/OAUTH_SENTINEL"
SIGNED = "https://play.googleapis.com/apk?sig=SIGNED_SENTINEL"
COOKIE = "COOKIE_SENTINEL"
PROXY = "http://USER_SENTINEL:PASS_SENTINEL@localhost:8080"
NEEDLES = ("EMAIL_SENTINEL", "OAUTH_SENTINEL", "SIGNED_SENTINEL",
           "COOKIE_SENTINEL", "USER_SENTINEL", "PASS_SENTINEL")


def caught(call) -> Any:
    try:
        call()
    except BaseException as exc:
        return exc
    raise AssertionError("expected failure")


class BoundaryCase(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def assert_sanitized(self, error):
        pending = [error]
        seen = set()
        while pending:
            exc = pending.pop()
            if exc is None or id(exc) in seen:
                continue
            seen.add(id(exc))
            for needle in NEEDLES:
                self.assertNotIn(needle, str(exc))
                self.assertNotIn(needle, repr(exc))
                self.assertNotIn(needle, repr(vars(exc)))
            tb = exc.__traceback__
            while tb:
                frame = tb.tb_frame
                if frame.f_globals.get("__name__", "").startswith("goopdl."):
                    for name, value in frame.f_locals.items():
                        for needle in NEEDLES:
                            self.assertNotIn(needle, repr(value), (frame.f_code.co_name, name))
                tb = tb.tb_next
            pending.extend((exc.__cause__, exc.__context__))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)


class AccountBoundariesTest(BoundaryCase):
    def test_google_error_allowlist_and_secret_cleanup(self):
        for reason in ("BadAuthentication", "NeedsBrowser", "TokenExpired",
                       "ServiceUnavailable", "OAUTH_SENTINEL", "arbitrary-code", ""):
            with self.subTest(reason=reason), patch.object(
                httpx, "post", return_value=httpx.Response(
                    200, text=f"Error={reason}\nEmail={EMAIL}\n",
                    request=httpx.Request("POST", aastoken.AUTH_URL),
                )
            ) as post:
                error = caught(lambda: aastoken.fetch_aas_token(EMAIL, OAUTH))
                self.assertIsInstance(error, aastoken.AASTokenError)
                expected = reason if reason in {"BadAuthentication", "NeedsBrowser",
                           "TokenExpired", "ServiceUnavailable"} else (
                               "NoAASToken" if not reason else "unknown error")
                self.assertEqual(str(error), expected)
                self.assertEqual(post.call_count, 1)
                self.assert_sanitized(error)

    def test_aas_transport_status_and_password_errors_are_sanitized(self):
        for mode in ("transport", "status", "password"):
            with self.subTest(mode=mode):
                def post(*args, mode=mode, **kwargs):
                    request = httpx.Request("POST", aastoken.AUTH_URL, data=kwargs["data"])
                    if mode == "transport":
                        raise httpx.ConnectError(OAUTH, request=request)
                    return httpx.Response(503, text=OAUTH, request=request)
                with patch.object(httpx, "post", side_effect=post):
                    error = caught(lambda mode=mode: aastoken.fetch_aas_token(
                        EMAIL, "p" * 200 if mode == "password" else OAUTH
                    ))
                self.assertIsInstance(error, ValueError if mode == "password" else httpx.HTTPError)
                if mode == "transport":
                    self.assertIsInstance(error, httpx.ConnectError)
                if mode == "status":
                    self.assertIsInstance(error, httpx.HTTPStatusError)
                    self.assertEqual(error.response.status_code, 503)
                    self.assertNotIn(OAUTH, error.response.text)
                self.assert_sanitized(error)

    def test_cli_auth_failures_do_not_echo_or_chain_secrets(self):
        for error in (aastoken.AASTokenError("OAUTH_SENTINEL"),
                      httpx.ConnectError(OAUTH), ValueError("invalid credentials")):
            with self.subTest(error=type(error)), patch.object(
                cli, "fetch_aas_token", side_effect=error
            ), patch.object(cli.err, "print") as output:
                failure = caught(lambda: cli.aastoken(EMAIL, OAUTH, oauth=False, browser=False))
                self.assertIsInstance(failure, typer.Exit)
                self.assertEqual(failure.exit_code, 1)
                self.assertNotIn("OAUTH_SENTINEL", repr(output.call_args_list))
                self.assert_sanitized(failure)

    def test_cli_browser_failure_is_generic_and_unchained(self):
        with patch.object(cli, "capture_oauth_credentials", side_effect=(
            browser_oauth.BrowserOAuthError(OAUTH)
        )), patch.object(cli.err, "print") as output, patch.object(cli.console, "print"):
            error = caught(lambda: cli.aastoken(EMAIL, None, oauth=False, browser=True))
        self.assertEqual(error.exit_code, 1)
        self.assertNotIn("OAUTH_SENTINEL", repr(output.call_args_list))
        self.assert_sanitized(error)

    def test_cli_validation_clears_captured_credentials(self):
        with patch.object(cli, "capture_oauth_credentials", return_value=(EMAIL, OAUTH)), \
                patch.object(cli.err, "print"), patch.object(cli.console, "print"):
            error = caught(lambda: cli.aastoken("other@example.test", None, oauth=False, browser=True))
        self.assertEqual(error.exit_code, 2)
        self.assert_sanitized(error)

    def test_browser_cdp_error_after_cookie_capture(self):
        websocket = Mock()
        websocket.recv.side_effect = [
            json.dumps({"id": 2, "result": {"cookies": [{
                "name": "oauth_token", "domain": ".accounts.google.com", "value": OAUTH
            }]}}),
            json.dumps({"id": 3, "error": {"message": EMAIL + OAUTH}}),
        ]
        process = Mock()
        process.poll.return_value = None
        with patch.object(browser_oauth, "create_connection", return_value=websocket), \
                patch.object(browser_oauth, "_attach_page", return_value=(1, "session")):
            error = caught(lambda: browser_oauth._wait_for_oauth_credentials(
                "ws://127.0.0.1", "http://local", process, 1
            ))
        self.assertIsInstance(error, browser_oauth.BrowserOAuthError)
        self.assertEqual(str(error), "Browser rejected account email access.")
        self.assert_sanitized(error)
        websocket.close.assert_called_once()

    def test_browser_transport_error_after_cookie_capture(self):
        responses = [(2, {"result": {"cookies": [{
            "name": "oauth_token", "domain": ".accounts.google.com", "value": OAUTH
        }]}}), browser_oauth.WebSocketException(EMAIL + OAUTH)]
        process = Mock()
        process.poll.return_value = None
        with patch.object(browser_oauth, "create_connection", return_value=Mock()), \
                patch.object(browser_oauth, "_attach_page", return_value=(1, "session")), \
                patch.object(browser_oauth, "_cdp_request", side_effect=responses):
            error = caught(lambda: browser_oauth._wait_for_oauth_credentials(
                "ws://127.0.0.1", "http://local", process, 1
            ))
        self.assertIsInstance(error, browser_oauth.BrowserOAuthError)
        self.assert_sanitized(error)

    def test_direct_cdp_error_does_not_retain_remote_response(self):
        error = caught(lambda: browser_oauth._raise_cdp_error(
            {"error": {"message": EMAIL + OAUTH}}, "cookie access"
        ))
        self.assertEqual(str(error), "Browser rejected cookie access.")
        self.assert_sanitized(error)

    def test_details_raw_drops_proxy_on_failure(self):
        def transport(*args, **kwargs):
            raise httpx.ConnectError(PROXY)
        with patch.object(httpx, "get", side_effect=transport):
            error = caught(lambda: api.get_details_raw(
                "com.example.app", {"authToken": "bearer", "gsfId": "123"}, proxy=PROXY
            ))
        self.assertIsInstance(error, api.NetworkError)
        self.assert_sanitized(error)


class LockBoundariesTest(BoundaryCase):
    def test_new_lock_is_private_even_with_permissive_umask(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions")
        with tempfile.TemporaryDirectory() as tmp, patch.object(auth, "_CONFIG_DIR", Path(tmp)):
            previous = os.umask(0)
            try:
                with auth._pool_lock("arm64", None, "Pv") as acquired:
                    self.assertTrue(acquired)
                lock = auth._pool_path("arm64", None, "Pv").with_suffix(".lock")
                self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
            finally:
                os.umask(previous)

    def test_existing_lock_permissions_are_tightened(self):
        if os.name == "nt":
            self.skipTest("POSIX permissions")
        with tempfile.TemporaryDirectory() as tmp, patch.object(auth, "_CONFIG_DIR", Path(tmp)):
            lock = auth._pool_path("arm64", None, "Pv").with_suffix(".lock")
            lock.touch(mode=0o644)
            with auth._pool_lock("arm64", None, "Pv") as acquired:
                self.assertTrue(acquired)
            self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_symlink_lock_does_not_write_to_external_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(auth, "_CONFIG_DIR", Path(tmp) / "config"):
            auth._CONFIG_DIR.mkdir()
            target = Path(tmp) / "external"
            target.touch()
            lock = auth._pool_path("arm64", None, "Pv").with_suffix(".lock")
            try:
                lock.symlink_to(target)
            except OSError:
                self.skipTest("symlinks unavailable")
            with auth._pool_lock("arm64", None, "Pv") as acquired:
                self.assertFalse(acquired)
            self.assertEqual(target.read_bytes(), b"")
            self.assertTrue(lock.is_symlink())


class DownloadBoundariesTest(BoundaryCase):
    def test_integrity_preflight_stream_and_filesystem_errors_are_sanitized(self):
        for mode in ("metadata", "digest", "short", "long", "filesystem", "callback", "concurrency"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                async def run(mode=mode, tmp=tmp):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda request: httpx.Response(200, content=b"apk")
                    )) as client:
                        spec = download.DownloadSpec(
                            SIGNED, Path(tmp) / ("missing/base.apk" if mode == "filesystem" else "base.apk"),
                            cookies=[{"name": "auth", "value": COOKIE}],
                            integrity_required=mode == "metadata",
                            expected_size={"short": 4, "long": 2, "digest": 3}.get(mode, 0),
                            sha256=base64.urlsafe_b64encode(hashlib.sha256(b"bad").digest()).decode().rstrip("=")
                            if mode == "digest" else "",
                        )
                        def callback(*args):
                            try:
                                raise ValueError(SIGNED)
                            except ValueError as cause:
                                raise RuntimeError("caller error") from cause
                        try:
                            await download.download_files(
                                [spec], require_integrity=True, client=client,
                                max_concurrent=0 if mode == "concurrency" else 1,
                                on_progress=callback if mode == "callback" else None,
                            )
                        except Exception as exc:
                            return exc
                        self.fail("expected failure")
                error = asyncio.run(run())
                self.assert_sanitized(error)
                self.assertFalse(list(Path(tmp).glob("*.part")))

    def test_sync_integrity_failure_is_sanitized(self):
        real = httpx.AsyncClient
        def client(**kwargs):
            return real(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"apk")
            ), **kwargs)
        with tempfile.TemporaryDirectory() as tmp, patch.object(download.httpx, "AsyncClient", client), \
                patch.object(download, "make_progress", return_value=MagicMock()):
            error = caught(lambda: download.download_batch([
                download.DownloadSpec(SIGNED, Path(tmp) / "a.apk", expected_size=1,
                                      cookies=[{"name": "auth", "value": COOKIE}])
            ]))
        self.assertIsInstance(error, download.IntegrityError)
        self.assert_sanitized(error)

    def test_redirect_returning_to_initial_host_does_not_restore_credentials(self):
        requests = []
        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={"Location": "https://files.googleusercontent.com/b"})
            if len(requests) == 2:
                return httpx.Response(302, headers={"Location": "https://play.googleapis.com/c"})
            return httpx.Response(200, content=b"apk")
        async def run(dest):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), cookies={"auth": COOKIE},
                auth=("user", "password"), headers={"Proxy-Authorization": "proxy"}
            ) as client:
                await download.download_files([download.DownloadSpec(SIGNED, dest)], client=client)
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(run(Path(tmp) / "a.apk"))
        self.assertEqual(len(requests), 3)
        self.assertIn("cookie", requests[0].headers)
        for request in requests[1:]:
            for header in ("cookie", "authorization", "proxy-authorization"):
                self.assertNotIn(header, request.headers)

    def test_cdn_url_port_validation_preserves_domain_allowlist(self):
        for url in ("https://play.googleapis.com:8443/a", "https://play.googleapis.com:bad/a",
                    "http://play.googleapis.com/a", "https://play.googleapis.com:0/a",
                    "https://[invalid/a"):
            with self.subTest(url=url):
                self.assertEqual(api._safe_cdn_url(url), "")
        for url in ("https://play.googleapis.com/a", "https://play.googleapis.com:443/a",
                    "https://files.googleusercontent.com/a", "https://accounts.google.com/a"):
            self.assertEqual(api._safe_cdn_url(url), url)

    def test_redirect_nonstandard_https_port_is_rejected_before_request(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://play.googleapis.com:8443/a"})
        async def run(dest):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await download.download_files([download.DownloadSpec(SIGNED, dest)], client=client)
        with tempfile.TemporaryDirectory() as tmp:
            error = caught(lambda: asyncio.run(run(Path(tmp) / "a.apk")))
        self.assertIsInstance(error, download.DownloadError)
        self.assertEqual(len(requests), 1)
        self.assert_sanitized(error)


if __name__ == "__main__":
    unittest.main()
