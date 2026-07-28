import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from gpdl.aastoken import AASTokenError, encrypt_password, fetch_aas_token
from gpdl.cli import app


class AASTokenTest(unittest.TestCase):
    def test_encrypted_password_has_google_header_and_rsa_payload(self) -> None:
        encrypted = base64.urlsafe_b64decode(
            encrypt_password("user@example.com", "secret")
        )
        self.assertEqual(133, len(encrypted))
        self.assertEqual(0, encrypted[0])

    @patch("gpdl.aastoken.httpx.post")
    def test_fetches_token_without_logging_or_files(self, post: Mock) -> None:
        post.return_value = Mock(text="SID=ignored\nToken=aas-token\n")

        self.assertEqual("aas-token", fetch_aas_token("user@example.com", "secret"))
        request = post.call_args.kwargs
        self.assertEqual("GoogleAuth/1.4", request["headers"]["User-Agent"])
        self.assertEqual("240913000", request["data"]["google_play_services_version"])
        self.assertEqual("ac2dm", request["data"]["service"])

    @patch("gpdl.aastoken.httpx.post")
    def test_exchanges_embedded_setup_oauth_token(self, post: Mock) -> None:
        post.return_value = Mock(text="Token=aas_et/persistent\nAuth=g.a000temporary\n")

        token = fetch_aas_token("user@example.com", "oauth2_4/one-time-token")

        self.assertEqual("aas_et/persistent", token)
        request = post.call_args.kwargs
        self.assertEqual("oauth2_4/one-time-token", request["data"]["Token"])
        self.assertEqual("1", request["data"]["ACCESS_TOKEN"])
        self.assertEqual("com.google.android.gms", request["headers"]["app"])
        self.assertNotIn("EncryptedPasswd", request["data"])

    @patch("gpdl.aastoken.httpx.post")
    def test_reports_google_error_code(self, post: Mock) -> None:
        post.return_value = Mock(text="Error=BadAuthentication\n")

        with self.assertRaisesRegex(AASTokenError, "BadAuthentication"):
            fetch_aas_token("user@example.com", "wrong")

    @patch("gpdl.cli.fetch_aas_token", return_value="aas-token")
    def test_argument_mode(self, fetch: Mock) -> None:
        result = CliRunner().invoke(
            app, ["aastoken", "user@example.com", "secret"]
        )

        self.assertEqual(0, result.exit_code)
        self.assertIn("AASToken: aas-token", result.output)
        fetch.assert_called_once_with("user@example.com", "secret")

    @patch("gpdl.cli.fetch_aas_token", side_effect=AASTokenError("BadAuthentication"))
    def test_bad_authentication_explains_app_password(self, fetch: Mock) -> None:
        result = CliRunner().invoke(
            app, ["aastoken", "user@example.com", "wrong"]
        )

        self.assertEqual(1, result.exit_code)
        self.assertIn("BadAuthentication", result.output)
        self.assertIn("--oauth", result.output)
        self.assertNotIn("wrong", result.output)

    @patch("gpdl.cli.fetch_aas_token", return_value="aas-token")
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

    @patch("gpdl.cli.fetch_aas_token", return_value="aas-token")
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
