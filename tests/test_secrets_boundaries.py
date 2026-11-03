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
                      httpx.ConnectError(OAUTH)):
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


if __name__ == "__main__":
    unittest.main()
