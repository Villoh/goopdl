import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import Mock, call, patch

from typer.testing import CliRunner

from goopdl.aastoken import AASTokenError, encrypt_password, fetch_aas_token
from goopdl.browser_oauth import (
    BrowserOAuthError,
    WebSocketException,
    _browser_candidates,
    _oauth_token,
    _profile_email,
    _wait_for_oauth_credentials,
)
from goopdl.cli import app


class AASTokenTest(unittest.TestCase):
    def test_encrypted_password_has_google_header_and_rsa_payload(self) -> None:
        encrypted = base64.urlsafe_b64decode(
            encrypt_password("user@example.com", "secret")
        )
        self.assertEqual(133, len(encrypted))
        self.assertEqual(0, encrypted[0])

    @patch("goopdl.aastoken.httpx.post")
    def test_fetches_token_without_logging_or_files(self, post: Mock) -> None:
        post.return_value = Mock(text="SID=ignored\nToken=aas-token\n")

        self.assertEqual("aas-token", fetch_aas_token("user@example.com", "secret"))
        request = post.call_args.kwargs
        self.assertEqual("GoogleAuth/1.4", request["headers"]["User-Agent"])
        self.assertEqual("240913000", request["data"]["google_play_services_version"])
        self.assertEqual("ac2dm", request["data"]["service"])

    @patch("goopdl.aastoken.httpx.post")
    def test_exchanges_embedded_setup_oauth_token(self, post: Mock) -> None:
        post.return_value = Mock(text="Token=aas_et/persistent\nAuth=g.a000temporary\n")

        token = fetch_aas_token("user@example.com", "oauth2_4/one-time-token")

        self.assertEqual("aas_et/persistent", token)
        request = post.call_args.kwargs
        self.assertEqual("oauth2_4/one-time-token", request["data"]["Token"])
        self.assertEqual("1", request["data"]["ACCESS_TOKEN"])
        self.assertEqual("com.google.android.gms", request["headers"]["app"])
        self.assertNotIn("EncryptedPasswd", request["data"])

    @patch("goopdl.aastoken.httpx.post")
    def test_reports_google_error_code(self, post: Mock) -> None:
        post.return_value = Mock(text="Error=BadAuthentication\n")

        with self.assertRaisesRegex(AASTokenError, "BadAuthentication"):
            fetch_aas_token("user@example.com", "wrong")

    @patch("goopdl.cli.fetch_aas_token", return_value="aas-token")
    def test_argument_mode(self, fetch: Mock) -> None:
        result = CliRunner().invoke(app, ["aastoken", "user@example.com", "secret"])

        self.assertEqual(0, result.exit_code)
        self.assertIn("AASToken: aas-token", result.output)
        fetch.assert_called_once_with("user@example.com", "secret")

    @patch("goopdl.cli.fetch_aas_token", side_effect=AASTokenError("BadAuthentication"))
    def test_bad_authentication_explains_app_password(self, fetch: Mock) -> None:
        result = CliRunner().invoke(app, ["aastoken", "user@example.com", "wrong"])

        self.assertEqual(1, result.exit_code)
        self.assertIn("BadAuthentication", result.output)
        self.assertIn("--oauth", result.output)
        self.assertNotIn("wrong", result.output)

    @patch("goopdl.cli.fetch_aas_token", return_value="aas-token")
    def test_oauth_mode_hides_one_time_token(self, fetch: Mock) -> None:
        result = CliRunner().invoke(
            app,
            ["aastoken", "--oauth"],
            input="user@example.com\noauth2_4/one-time-token\n",
        )

        self.assertEqual(0, result.exit_code)
        self.assertIn("AASToken: aas-token", result.output)
        self.assertNotIn("one-time-token", result.output)
        fetch.assert_called_once_with("user@example.com", "oauth2_4/one-time-token")

    @patch("goopdl.cli.fetch_aas_token", return_value="aas-token")
    @patch(
        "goopdl.cli.capture_oauth_credentials",
        return_value=("user@example.com", "oauth2_4/one-time-token"),
    )
    def test_browser_mode_captures_email_and_oauth_token(
        self, capture: Mock, fetch: Mock
    ) -> None:
        result = CliRunner().invoke(app, ["aastoken", "--browser"])

        self.assertEqual(0, result.exit_code)
        self.assertIn("AASToken: aas-token", result.output)
        self.assertNotIn("one-time-token", result.output)
        capture.assert_called_once_with()
        fetch.assert_called_once_with("user@example.com", "oauth2_4/one-time-token")

    @patch("goopdl.cli.fetch_aas_token")
    @patch(
        "goopdl.cli.capture_oauth_credentials",
        return_value=("other@example.com", "oauth2_4/one-time-token"),
    )
    def test_browser_mode_rejects_explicit_email_mismatch(
        self, capture: Mock, fetch: Mock
    ) -> None:
        result = CliRunner().invoke(app, ["aastoken", "user@example.com", "--browser"])

        self.assertEqual(2, result.exit_code)
        self.assertIn("other@example.com", result.output)
        fetch.assert_not_called()
        capture.assert_called_once_with()

    @patch("goopdl.browser_oauth.shutil.which")
    def test_browser_candidates_include_brave_executable(self, which: Mock) -> None:
        brave = "/opt/brave/brave"
        which.side_effect = lambda name: brave if name == "brave" else None

        self.assertIn(Path(brave), _browser_candidates())

    @patch("goopdl.browser_oauth.create_connection")
    def test_browser_closes_before_launcher_wait_and_socket_close(
        self, connect: Mock
    ) -> None:
        lifecycle = Mock()
        websocket = connect.return_value = lifecycle.websocket
        process = lifecycle.launcher
        process.poll.return_value = None
        websocket.recv.side_effect = [
            json.dumps(
                {
                    "id": 1,
                    "result": {"targetInfos": [{"type": "page", "targetId": "page"}]},
                }
            ),
            json.dumps({"id": 2, "result": {"sessionId": "session"}}),
            json.dumps(
                {
                    "id": 3,
                    "result": {
                        "cookies": [
                            {
                                "name": "oauth_token",
                                "value": "oauth2_4/test",
                                "domain": "accounts.google.com",
                            }
                        ]
                    },
                }
            ),
            json.dumps({"id": 4, "result": {"result": {"value": "user@example.com"}}}),
        ]

        self.assertEqual(
            ("user@example.com", "oauth2_4/test"),
            _wait_for_oauth_credentials("ws://isolated", "http://local", process, 1),
        )
        self.assertEqual(4, websocket.recv.call_count)
        self.assertEqual(
            [
                call.websocket.send(json.dumps({"id": 5, "method": "Browser.close"})),
                call.launcher.wait(timeout=5),
                call.websocket.close(),
            ],
            lifecycle.mock_calls[-3:],
        )

    def test_browser_timeout_survives_cleanup_failures(self) -> None:
        for send_error, wait_error, close_error in (
            (None, None, None),
            (None, TimeoutExpired("launcher", 5), None),
            (OSError("disconnected"), None, None),
            (WebSocketException("disconnected"), None, None),
            (None, OSError("wait failed"), None),
            (None, None, WebSocketException("already closed")),
            (None, None, OSError("already closed")),
        ):
            with self.subTest(send=send_error, wait=wait_error, close=close_error):
                lifecycle = Mock()
                websocket = lifecycle.websocket
                websocket.send.side_effect = send_error
                websocket.close.side_effect = close_error
                lifecycle.launcher.wait.side_effect = wait_error
                with (
                    patch(
                        "goopdl.browser_oauth.create_connection", return_value=websocket
                    ),
                    patch(
                        "goopdl.browser_oauth._attach_page", return_value=(2, "session")
                    ),
                    patch("goopdl.browser_oauth.time.monotonic", side_effect=[0, 1]),
                ):
                    with self.assertRaisesRegex(
                        BrowserOAuthError, "Google sign-in timed out"
                    ):
                        _wait_for_oauth_credentials(
                            "ws://isolated", "http://local", lifecycle.launcher, 1
                        )
                expected = [
                    call.websocket.send(
                        json.dumps({"id": 3, "method": "Browser.close"})
                    )
                ]
                if send_error is None:
                    expected.append(call.launcher.wait(timeout=5))
                expected.append(call.websocket.close())
                self.assertEqual(expected, lifecycle.mock_calls)

    def test_browser_cookie_filter_ignores_unrelated_cookies(self) -> None:
        cookies = [
            {"name": "oauth_token", "value": "oauth2_4/wrong", "domain": "example.com"},
            {"name": "SID", "value": "secret", "domain": ".accounts.google.com"},
            {
                "name": "oauth_token",
                "value": "oauth2_4/right",
                "domain": ".accounts.google.com",
            },
        ]

        self.assertEqual("oauth2_4/right", _oauth_token(cookies))

    def test_browser_profile_email_reads_runtime_value(self) -> None:
        response = {
            "result": {"result": {"type": "string", "value": "user@example.com"}}
        }

        self.assertEqual("user@example.com", _profile_email(response))

    @patch("goopdl.cli.fetch_aas_token", return_value="aas-token")
    def test_interactive_mode_hides_password_and_creates_no_files(
        self, fetch: Mock
    ) -> None:
        runner = CliRunner()
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            try:
                os.chdir(tmp)
                result = runner.invoke(
                    app, ["aastoken"], input="user@example.com\nsecret\n"
                )

                self.assertEqual(0, result.exit_code)
                self.assertNotIn("secret", result.output)
                self.assertIn("AASToken: aas-token", result.output)
                self.assertFalse(Path("credentials.txt").exists())
                self.assertFalse(Path("token.txt").exists())
            finally:
                os.chdir(original_cwd)
        fetch.assert_called_once_with("user@example.com", "secret")


if __name__ == "__main__":
    unittest.main()
