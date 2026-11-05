"""`goopdl auth login/logout/status` and the account.json only the CLI reads."""
import json
import os
import socket
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from _envutil import isolated_env
from _nonet import no_sockets
from goopdl import auth, cli
from goopdl.api import PlayAPIError
from goopdl.client import Client, Config, Credentials, _run

EMAIL = "saved@example.test"
AAS = "aas_et/SAVED_AAS_SENTINEL"
OAUTH = "oauth2_4/ONE_TIME_SENTINEL"
ENV_AAS = "aas_et/ENV_AAS_SENTINEL"
SECRETS = ("SAVED_AAS_SENTINEL", "ONE_TIME_SENTINEL", "ENV_AAS_SENTINEL")
ENV = {"GOOPDL_ACCOUNT_EMAIL": "env@example.test", "GOOPDL_AAS_TOKEN": ENV_AAS}


class AccountCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(dir=tempfile.gettempdir())
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name).resolve() / "config"
        self.account = self.config / "account.json"
        self.env(self.config)
        network = patch.object(socket.socket, "connect", no_sockets)
        network.start()
        self.addCleanup(network.stop)

    def env(self, config: Path, **extra: str) -> None:
        environment = isolated_env({"GOOPDL_CONFIG_DIR": str(config), **extra})
        environment.start()
        self.addCleanup(environment.stop)

    def save(self, data: object = None, mode: int = 0o600) -> None:
        self.config.mkdir(mode=0o700, exist_ok=True)
        if data is None:
            data = {"v": 1, "email": EMAIL, "aas_token": AAS}
        self.account.write_text(data if isinstance(data, str) else json.dumps(data))
        self.account.chmod(mode)

    def invoke(self, *args: str, **kwargs):
        result = CliRunner().invoke(cli.app, list(args), **kwargs)
        printed = result.output
        if "--print" in args:
            self.assertEqual(AAS + "\n", result.stdout)
            printed = result.stderr
        for secret in SECRETS:
            self.assertNotIn(secret, printed)
        return result

    def credentials_seen_by(self, *args: str):
        """Run a CLI command; return (result, credentials fetch_token saw)."""
        seen = []

        def fetch_token(**_):
            seen.append(auth._direct_credentials())
            return {"authToken": "bearer", "gsfId": "1"}

        with patch.object(cli, "fetch_token", side_effect=fetch_token), \
                patch.object(cli, "save_auth", return_value=self.config / "auth.json"):
            result = self.invoke(*args)
        return result, seen


class LoginTest(AccountCase):
    def login(self, *args: str, **kwargs):
        with patch.object(cli, "capture_oauth_credentials", return_value=(EMAIL, OAUTH)), \
                patch.object(cli, "fetch_aas_token", return_value=AAS) as fetch, \
                patch("webbrowser.open"), patch("typer.prompt", return_value=OAUTH):
            result = self.invoke("auth", "login", *args, **kwargs)
        fetch.assert_called_once_with(EMAIL, OAUTH)
        return result

    def test_login_saves_the_account_privately_and_prints_no_token(self) -> None:
        result = self.login()
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn(f"Logged in as {EMAIL}", result.stdout)
        self.assertIn("goopdl auth status", result.stdout)
        self.assertIn("goopdl auth logout", result.stdout)
        self.assertEqual({"v": 1, "email": EMAIL, "aas_token": AAS},
                         json.loads(self.account.read_text()))
        if os.name != "nt":
            self.assertEqual(0o600, stat.S_IMODE(self.account.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(self.config.stat().st_mode))
        self.assertEqual(["account.json"], [p.name for p in self.config.iterdir()])

    def test_manual_login_saves_too(self) -> None:
        result = self.login("--manual", input=f"{EMAIL}\n")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(AAS, json.loads(self.account.read_text())["aas_token"])

    def test_print_does_not_save(self) -> None:
        result = self.login("--print")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertFalse(self.config.exists())

    def test_login_replaces_a_permissive_file_with_a_private_one(self) -> None:
        self.save({"v": 1, "email": "old@example.test", "aas_token": "aas_et/old"}, 0o644)
        self.assertEqual(0, self.login().exit_code)
        self.assertEqual(EMAIL, json.loads(self.account.read_text())["email"])
        if os.name != "nt":
            self.assertEqual(0o600, stat.S_IMODE(self.account.stat().st_mode))

    def test_login_warns_when_the_environment_wins(self) -> None:
        self.env(self.config, **ENV)
        result = self.login()
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("take precedence", result.stderr)

    def test_unexpected_token_is_not_saved(self) -> None:
        with patch.object(cli, "capture_oauth_credentials", return_value=(EMAIL, OAUTH)), \
                patch.object(cli, "fetch_aas_token", return_value="oauth2_4/ONE_TIME_SENTINEL"):
            result = self.invoke("auth", "login")
        self.assertEqual(1, result.exit_code)
        self.assertFalse(self.account.exists())


class LogoutTest(AccountCase):
    def test_logout_removes_account_and_cache_only(self) -> None:
        self.save()
        cached = ("auth-arm64.json", "token-pool-arm64.json", "pool-index-arm64.json",
                  "direct-device-abc-arm64-profile-Pv.json")
        for name in cached:
            (self.config / name).write_text("{}")
        (self.config / "token-pool-arm64.lock").write_bytes(b"\0")
        result = self.invoke("auth", "logout")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(["token-pool-arm64.lock"], [p.name for p in self.config.iterdir()])
        self.assertNotIn("still set", result.output)

    def test_logout_without_account_and_with_environment_warns(self) -> None:
        self.env(self.config, **ENV)
        result = self.invoke("auth", "logout")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("No saved account", result.stdout)
        self.assertIn("still set", result.stderr)

    def test_library_clearing_keeps_the_saved_account(self) -> None:
        self.save()
        (self.config / "auth-arm64.json").write_text("{}")
        auth.clear_auth()
        Client().clear_cache()
        Client(config_dir=self.config).clear_cache()
        Client(config_dir=self.config,
               credentials=Credentials(EMAIL, "aas_et/x")).clear_cache()
        self.assertEqual(["account.json"], [p.name for p in self.config.iterdir()])


class StatusTest(AccountCase):
    def test_anonymous(self) -> None:
        result = self.invoke("auth", "status")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("anonymous", result.stdout)
        self.assertNotIn("Account", result.stdout)

    def test_saved_account_and_cache_summary(self) -> None:
        self.save()
        (self.config / "token-pool-arm64.json").write_text("[]")
        (self.config / "direct-device-abc-arm64-profile-Pv.json").write_text("{}")
        result = self.invoke("auth", "status")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("saved account", result.stdout)
        self.assertIn(EMAIL, result.stdout)
        self.assertIn("1 token file", result.stdout)
        self.assertIn("1 device registration", result.stdout)

    def test_long_lines_are_not_wrapped(self) -> None:
        # A long config path (Windows runners) must not split "1 token file".
        self.save()
        (self.config / "token-pool-arm64.json").write_text("[]")
        narrow = cli.Console(width=30, color_system=None, highlight=False)
        with patch.object(cli, "console", narrow):
            result = self.invoke("auth", "status")
        self.assertIn("1 token file", result.stdout)
        self.assertIn(str(self.config), result.stdout)

    def test_environment_shadows_the_saved_account(self) -> None:
        self.save()
        self.env(self.config, **ENV)
        result = self.invoke("auth", "status")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("environment", result.stdout)
        self.assertIn("env@example.test", result.stdout)
        self.assertNotIn(EMAIL, result.stdout)
        self.assertIn("ignored", result.stdout)

    def test_incomplete_environment_exits_2(self) -> None:
        self.save()
        self.env(self.config, GOOPDL_AAS_TOKEN=ENV_AAS)
        result = self.invoke("auth", "status")
        self.assertEqual(2, result.exit_code)
        self.assertIn("GOOPDL_ACCOUNT_EMAIL", result.output)

    def test_unusable_file_is_reported(self) -> None:
        self.save("{not json " + AAS)
        result = self.invoke("auth", "status")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("unusable", result.stdout)
        self.assertIn("invalid", result.stderr)


class SavedAccountUseTest(AccountCase):
    def test_commands_use_the_saved_account(self) -> None:
        self.save()
        result, seen = self.credentials_seen_by("auth", "token")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual([(EMAIL, AAS)], seen)
        self.assertFalse(auth.direct_auth_enabled())  # nothing left behind

    def test_environment_wins_over_the_file(self) -> None:
        self.save()
        self.env(self.config, **ENV)
        result, seen = self.credentials_seen_by("auth", "token")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual([("env@example.test", ENV_AAS)], seen)

    def test_one_variable_is_an_error_not_mixed_with_the_file(self) -> None:
        self.save()
        for name, value in ENV.items():
            with self.subTest(name=name):
                self.env(self.config, **{name: value})
                result, seen = self.credentials_seen_by("auth", "token")
                self.assertEqual(2, result.exit_code)
                self.assertIn("Missing required environment variable", result.output)
                self.assertEqual([], seen)

    def test_without_a_file_commands_stay_anonymous(self) -> None:
        result, seen = self.credentials_seen_by("auth", "token")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual([None], seen)

    @unittest.skipIf(sys.platform == "win32", "POSIX permissions")
    def test_readable_by_others_is_refused(self) -> None:
        for mode in (0o644, 0o640, 0o604):
            with self.subTest(mode=oct(mode)):
                self.save(mode=mode)
                result, seen = self.credentials_seen_by("auth", "token")
                self.assertEqual(2, result.exit_code)
                self.assertIn("chmod 600", " ".join(result.output.split()))
                self.assertEqual([], seen)

    @unittest.skipIf(sys.platform == "win32", "POSIX file types")
    def test_symlink_and_directory_are_refused(self) -> None:
        self.save()
        target = self.config / "real.json"
        self.account.rename(target)
        self.account.symlink_to(target)
        result, seen = self.credentials_seen_by("auth", "token")
        self.assertEqual(2, result.exit_code)
        self.assertEqual([], seen)
        self.account.unlink()
        self.account.mkdir()
        result, seen = self.credentials_seen_by("auth", "token")
        self.assertEqual(2, result.exit_code)
        self.assertEqual([], seen)

    def test_corrupt_or_invalid_files_are_refused(self) -> None:
        for data in ("{", "[]", '"x"', b"\xff\xfe".decode("latin-1"),
                     {"v": 2, "email": EMAIL, "aas_token": AAS},
                     {"v": 1, "email": EMAIL},
                     {"v": 1, "email": "", "aas_token": AAS},
                     {"v": 1, "email": EMAIL, "aas_token": OAUTH},
                     {"v": 1, "email": EMAIL, "aas_token": ["aas_et/x"]}):
            with self.subTest(data=data):
                self.save(data)
                for command in (("auth", "token"), ("info", "com.example.app")):
                    result, seen = self.credentials_seen_by(*command)
                    self.assertEqual(2, result.exit_code)
                    self.assertIn("is invalid", " ".join(result.output.split()))
                    self.assertEqual([], seen)

    def test_library_never_reads_the_file(self) -> None:
        self.save()
        with patch.object(Path, "read_text", side_effect=AssertionError), \
                patch.object(os, "open", side_effect=AssertionError):
            self.assertFalse(auth.direct_auth_enabled())
            self.assertIsNone(Credentials.from_env())
            self.assertIsNone(_run(Config(), lambda cfg: auth._direct_credentials()))
            self.assertIsNone(_run(Config(config_dir=self.config),
                                   lambda cfg: auth._direct_credentials()))
        seen = []

        def pick_pool_token(**_):
            seen.append(auth._direct_credentials())

        for client in (Client(), Client(config_dir=self.config)):
            with patch.object(auth, "pick_pool_token", side_effect=pick_pool_token), \
                    self.assertRaises(PlayAPIError):
                client.details("com.example.app")
        self.assertEqual([None, None], seen)


class HelpTest(AccountCase):
    def test_auth_without_subcommand_shows_help(self) -> None:
        result = self.invoke("auth")
        self.assertIn("Usage:", result.output)
        for command in ("login", "logout", "status", "token"):
            self.assertIn(command, result.output)

    def test_auth_help_lists_the_subcommands(self) -> None:
        result = self.invoke("auth", "--help")
        self.assertEqual(0, result.exit_code)
        for command in ("login", "logout", "status", "token"):
            self.assertIn(command, result.output)
        self.assertNotIn("--clear", self.invoke("auth", "token", "--help").output)


if __name__ == "__main__":
    unittest.main()
