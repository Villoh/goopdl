import json
import tempfile
import unittest
from pathlib import Path
from subprocess import TimeoutExpired
from unittest.mock import Mock, call, patch

import typer
from typer.testing import CliRunner

from _envutil import isolated_env
from goopdl import cli
from goopdl.aastoken import AASTokenError, fetch_aas_token
from goopdl.browser_oauth import (
    EMBEDDED_SETUP_URL,
    BrowserOAuthError,
    WebSocketException,
    _browser_candidates,
    _oauth_token,
    _profile_email,
    _wait_for_oauth_credentials,
)
from goopdl.cli import app


class AASTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name).resolve()
        environment = isolated_env({"GOOPDL_CONFIG_DIR": str(self.config)})
        environment.start()
        self.addCleanup(environment.stop)

    @patch("goopdl.aastoken.httpx.post")
    def test_exchanges_embedded_setup_oauth_token(self, post: Mock) -> None:
        post.return_value = Mock(text="Token=aas_et/persistent\nAuth=g.a000temporary\n")

        token = fetch_aas_token("user@example.com", "oauth2_4/one-time-token")

        self.assertEqual("aas_et/persistent", token)
        request = post.call_args.kwargs
        self.assertEqual("oauth2_4/one-time-token", request["data"]["Token"])
        self.assertEqual("1", request["data"]["ACCESS_TOKEN"])
        self.assertEqual("ac2dm", request["data"]["service"])
        self.assertEqual("com.google.android.gms", request["headers"]["app"])
        self.assertNotIn("EncryptedPasswd", request["data"])

    @patch("goopdl.aastoken.httpx.post")
    def test_rejects_anything_but_an_oauth_token_without_a_request(
        self, post: Mock
    ) -> None:
        for value in ("app-password-secret", "aas_et/secret", "", None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError) as caught:
                    fetch_aas_token("user@example.com", value)  # type: ignore[arg-type]
                self.assertEqual("Invalid Google account credentials.", str(caught.exception))
                self.assertNotIsInstance(caught.exception, AASTokenError)
        post.assert_not_called()

    @patch("goopdl.aastoken.httpx.post")
    def test_reports_google_error_code(self, post: Mock) -> None:
        post.return_value = Mock(text="Error=BadAuthentication\n")

        with self.assertRaisesRegex(AASTokenError, "BadAuthentication"):
            fetch_aas_token("user@example.com", "oauth2_4/expired")

    def _login(self, *args: str, token: str = "aas_et/token", **capture: object):
        capture.setdefault("return_value", ("user@example.com", "oauth2_4/one-time-token"))
        with (
            patch("goopdl.cli.fetch_aas_token", return_value=token) as fetch,
            patch("goopdl.cli.capture_oauth_credentials", **capture) as browser,
            patch("webbrowser.open") as opened,
            patch.object(cli.console, "_width", 80),
            patch.object(cli.err, "_width", 80),
        ):
            result = CliRunner().invoke(app, ["auth", "login", *args])
        return result, fetch, browser, opened

    def test_print_writes_only_the_token_on_one_line(self) -> None:
        token = "aas_et/" + "A" * 300
        result, fetch, browser, opened = self._login("--print", token=token)

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(token + "\n", result.stdout)
        self.assertIn("I agree", result.stderr)
        self.assertNotIn("one-time-token", result.output)
        browser.assert_called_once_with()
        opened.assert_not_called()
        fetch.assert_called_once_with("user@example.com", "oauth2_4/one-time-token")

    def test_browser_failure_suggests_manual_mode(self) -> None:
        result, fetch, _, _ = self._login(
            "--print", side_effect=BrowserOAuthError("Chrome was not found")
        )

        self.assertEqual(1, result.exit_code)
        self.assertEqual("", result.stdout)
        self.assertIn("goopdl auth login --manual", " ".join(result.stderr.split()))
        fetch.assert_not_called()

    def test_manual_mode_opens_browser_and_hides_the_oauth_token(self) -> None:
        # A real hidden prompt writes to the terminal; CliRunner's would hit stdout.
        with (
            patch("typer.prompt", return_value=" oauth2_4/one-time-token ") as prompt,
            patch("goopdl.cli.fetch_aas_token", return_value="aas_et/token") as fetch,
            patch("goopdl.cli.capture_oauth_credentials") as browser,
            patch("webbrowser.open") as opened,
        ):
            result = CliRunner().invoke(
                app, ["auth", "login", "--manual", "--print"], input="user@example.com\n"
            )

        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual("aas_et/token\n", result.stdout)
        self.assertNotIn("one-time-token", result.output)
        self.assertIn("F12", result.stderr)
        opened.assert_called_once_with(EMBEDDED_SETUP_URL)
        browser.assert_not_called()
        fetch.assert_called_once_with("user@example.com", "oauth2_4/one-time-token")
        prompt.assert_called_once()
        self.assertTrue(prompt.call_args.kwargs["hide_input"])
        self.assertTrue(prompt.call_args.kwargs["err"])

    def test_manual_mode_rejects_a_token_without_prefix(self) -> None:
        with (
            patch("typer.prompt", return_value="secret-password"),
            patch("goopdl.cli.fetch_aas_token") as fetch,
            patch("webbrowser.open"),
        ):
            result = CliRunner().invoke(
                app, ["auth", "login", "--manual", "--print"], input="user@example.com\n"
            )

        self.assertEqual(2, result.exit_code)
        self.assertIn("oauth2_4/", result.stderr)
        self.assertNotIn("secret-password", result.output)
        self.assertEqual("", result.stdout)
        fetch.assert_not_called()

    @patch("goopdl.cli.fetch_aas_token", side_effect=AASTokenError("BadAuthentication"))
    @patch(
        "goopdl.cli.capture_oauth_credentials",
        return_value=("user@example.com", "oauth2_4/one-time-token"),
    )
    def test_bad_authentication_suggests_signing_in_again(
        self, capture: Mock, fetch: Mock
    ) -> None:
        result = CliRunner().invoke(app, ["auth", "login", "--print"])

        self.assertEqual(1, result.exit_code)
        self.assertIn("BadAuthentication", result.stderr)
        self.assertIn("goopdl auth login", result.stderr)
        self.assertNotIn("one-time-token", result.output)

    def test_aastoken_is_hidden_deprecated_and_delegates(self) -> None:
        self.assertNotIn("aastoken", CliRunner().invoke(app, ["--help"]).output)
        for args, manual in (([], False), (["--browser"], False), (["--oauth"], True)):
            with self.subTest(args=args), patch(
                "goopdl.cli._run_login", side_effect=lambda *_: typer.echo("aas_et/t")
            ) as run:
                result = CliRunner().invoke(app, ["aastoken", *args])
                self.assertEqual(0, result.exit_code, result.output)
                self.assertEqual("aas_et/t\n", result.stdout)
                self.assertIn("goopdl auth login --print", result.stderr)
                self.assertEqual((True, manual, False), run.call_args.args)

    def test_aastoken_rejects_email_and_password(self) -> None:
        for args in (["user@example.com", "secret-password"], ["user@example.com", "--browser"],
                     ["--oauth", "--browser"]):
            with self.subTest(args=args), patch("goopdl.cli._run_login") as run:
                result = CliRunner().invoke(app, ["aastoken", *args])
                self.assertEqual(2, result.exit_code)
                self.assertIn("goopdl auth login --print", result.stderr)
                self.assertNotIn("secret-password", result.output)
                run.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
