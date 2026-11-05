"""`goopdl auth login/logout/status` and the account.json only the CLI reads."""
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from _envutil import isolated_env
from _fakekeyring import LockedKeyring, MemoryKeyring, NoKeyring, NullKeyring
from _fakekeyring import use as use_keyring
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
IN_KEYRING = {"v": 1, "email": EMAIL, "storage": "keyring"}
IN_FILE = {"v": 1, "email": EMAIL, "aas_token": AAS}
ROOT = Path(__file__).resolve().parent.parent


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
        self.keyring = MemoryKeyring()
        self.use(self.keyring)

    def env(self, config: Path, **extra: str) -> None:
        # The fail backend only matters if a test forgot use(): never the real store.
        environment = isolated_env({
            "GOOPDL_CONFIG_DIR": str(config),
            "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
            **extra,
        })
        environment.start()
        self.addCleanup(environment.stop)

    def use(self, backend: object) -> None:
        patcher = use_keyring(backend)
        patcher.start()
        self.addCleanup(patcher.stop)

    def untouched_keyring(self) -> Mock:
        """A backend that records calls; tests assert it saw none."""
        backend = Mock(spec=["get_password", "set_password", "delete_password"])
        self.use(backend)
        return backend

    def store(self, email: str = EMAIL, aas_token: str = AAS) -> None:
        self.keyring.set_password("goopdl", email, aas_token)

    def save(self, data: object = None, mode: int = 0o600) -> None:
        self.config.mkdir(mode=0o700, exist_ok=True)
        if data is None:
            data = IN_FILE
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

    def assert_private(self) -> None:
        if os.name != "nt":
            self.assertEqual(0o600, stat.S_IMODE(self.account.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(self.config.stat().st_mode))
        self.assertEqual(["account.json"], [p.name for p in self.config.iterdir()])

    def test_login_keeps_the_token_in_the_keyring_and_prints_none(self) -> None:
        result = self.login()
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn(f"Logged in as {EMAIL}", result.stdout)
        self.assertIn("system keyring (memory Keyring)", result.stdout)
        self.assertIn("goopdl auth status", result.stdout)
        self.assertIn("goopdl auth logout", result.stdout)
        self.assertEqual("", result.stderr.split("I agree.")[-1].strip())
        self.assertEqual(IN_KEYRING, json.loads(self.account.read_text()))
        self.assertEqual({("goopdl", EMAIL): AAS}, self.keyring.items)
        self.assert_private()

    def test_manual_login_saves_too(self) -> None:
        result = self.login("--manual", input=f"{EMAIL}\n")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(IN_KEYRING, json.loads(self.account.read_text()))
        self.assertEqual({("goopdl", EMAIL): AAS}, self.keyring.items)

    def test_print_does_not_save_or_touch_the_keyring(self) -> None:
        backend = self.untouched_keyring()
        result = self.login("--print")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertFalse(self.config.exists())
        self.assertEqual([], backend.mock_calls)

    def test_insecure_storage_writes_the_file_without_the_keyring(self) -> None:
        backend = self.untouched_keyring()
        result = self.login("--insecure-storage")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(IN_FILE, json.loads(self.account.read_text()))
        self.assertIn(f"Token saved in {self.account}", result.stdout)
        self.assertNotIn("keyring", result.output)
        self.assertEqual([], backend.mock_calls)
        self.assert_private()

    def test_without_a_usable_keyring_the_file_is_used(self) -> None:
        for backend, reason in ((NoKeyring(), "NoKeyringError"),
                                (NullKeyring(), "backend returned nothing"),
                                (LockedKeyring(), "RuntimeError")):
            with self.subTest(backend=backend.name):
                self.use(backend)
                result = self.login()
                self.assertEqual(0, result.exit_code, result.output)
                self.assertEqual(IN_FILE, json.loads(self.account.read_text()))
                self.assertIn(f"Could not use the system keyring ({reason})",
                              " ".join(result.stderr.split()))
                self.assertIn(f"Token saved in {self.account}", result.stdout)
                self.assertNotIn("no backend for", result.output)
                self.assert_private()

    def test_a_null_backend_that_keeps_the_token_is_cleaned_up(self) -> None:
        class Forgetful(MemoryKeyring):
            name = "forgetful Keyring"

            def get_password(self, service, username):
                return None

        backend = Forgetful()
        self.use(backend)
        self.assertEqual(0, self.login().exit_code)
        self.assertEqual(IN_FILE, json.loads(self.account.read_text()))
        self.assertEqual({}, backend.items)

    def test_a_new_account_removes_the_old_keyring_token(self) -> None:
        self.save({**IN_KEYRING, "email": "old@example.test"})
        self.store("old@example.test", "aas_et/old")
        self.store("other@example.test", "aas_et/other")  # not goopdl's to delete
        self.assertEqual(0, self.login().exit_code)
        self.assertEqual(IN_KEYRING, json.loads(self.account.read_text()))
        self.assertEqual({("goopdl", EMAIL): AAS, ("goopdl", "other@example.test"):
                          "aas_et/other"}, self.keyring.items)

    def test_moving_to_the_file_removes_the_keyring_token(self) -> None:
        # The second login writes the token, then fails to read it back.
        for args, failure in ((["--insecure-storage"], None), ([], RuntimeError)):
            with self.subTest(args=args), patch.object(
                    self.keyring, "get_password", side_effect=failure,
                    wraps=self.keyring.get_password):
                self.save(IN_KEYRING)
                self.store()
                result = self.login(*args)
                self.assertEqual(0, result.exit_code, result.output)
                self.assertEqual(IN_FILE, json.loads(self.account.read_text()))
                self.assertEqual({}, self.keyring.items)

    def test_login_again_keeps_the_same_keyring_entry(self) -> None:
        self.save(IN_KEYRING)
        self.store(EMAIL, "aas_et/older")
        self.assertEqual(0, self.login().exit_code)
        self.assertEqual({("goopdl", EMAIL): AAS}, self.keyring.items)

    def test_login_replaces_a_permissive_file_with_a_private_one(self) -> None:
        self.save({"v": 1, "email": "old@example.test", "aas_token": "aas_et/old"}, 0o644)
        self.assertEqual(0, self.login().exit_code)
        self.assertEqual(IN_KEYRING, json.loads(self.account.read_text()))
        self.assert_private()

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
        self.assertEqual({}, self.keyring.items)


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

    def test_logout_removes_the_keyring_token(self) -> None:
        self.save(IN_KEYRING)
        self.store()
        self.store("other@example.test")
        result = self.invoke("auth", "logout")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("Removed the token from the system keyring", result.stdout)
        self.assertFalse(self.account.exists())
        self.assertEqual([("goopdl", "other@example.test")], list(self.keyring.items))

    def test_logout_without_the_keyring_token_still_removes_the_file(self) -> None:
        for backend, warning in ((MemoryKeyring(), None), (NullKeyring(), None),
                                 (NoKeyring(), "NoKeyringError"),
                                 (LockedKeyring(), "RuntimeError")):
            with self.subTest(backend=backend.name):
                self.save(IN_KEYRING)
                self.use(backend)
                result = self.invoke("auth", "logout")
                self.assertEqual(0, result.exit_code, result.output)
                self.assertFalse(self.account.exists())
                self.assertIn("All cached tokens removed", result.stdout)
                if warning is None:
                    self.assertEqual("", result.stderr)
                else:
                    self.assertIn(f"Could not remove the token from the system keyring "
                                  f"({warning})", " ".join(result.stderr.split()))
                    self.assertNotIn("no backend for", result.output)

    def test_logout_of_a_file_account_leaves_the_keyring_alone(self) -> None:
        self.save()
        backend = self.untouched_keyring()
        self.assertEqual(0, self.invoke("auth", "logout").exit_code)
        self.assertFalse(self.account.exists())
        self.assertEqual([], backend.mock_calls)

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
        self.save(IN_KEYRING)
        self.store()
        auth.clear_auth()
        Client(config_dir=self.config).clear_cache()
        self.assertEqual({("goopdl", EMAIL): AAS}, self.keyring.items)


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
        self.assertIn("Storage : file", result.stdout)

    def test_file_account_status_leaves_the_keyring_alone(self) -> None:
        self.save()
        backend = self.untouched_keyring()
        result = self.invoke("auth", "status")
        self.assertIn("Storage : file", result.stdout)
        self.assertEqual([], backend.mock_calls)

    def test_keyring_account(self) -> None:
        self.save(IN_KEYRING)
        self.store()
        result = self.invoke("auth", "status")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("Mode    : saved account\n", result.stdout)
        self.assertIn(EMAIL, result.stdout)
        self.assertIn("Storage : system keyring (memory Keyring)", result.stdout)
        self.assertEqual("", result.stderr)

    def test_keyring_account_without_its_token_is_unusable(self) -> None:
        for backend, problem in ((MemoryKeyring(), "not in the system keyring"),
                                 (NullKeyring(), "not in the system keyring"),
                                 (NoKeyring(), "(NoKeyringError)")):
            with self.subTest(backend=backend.name):
                self.save(IN_KEYRING)
                self.use(backend)
                result = self.invoke("auth", "status")
                self.assertEqual(0, result.exit_code, result.output)
                self.assertIn("saved account (unusable)", result.stdout)
                self.assertIn(f"system keyring ({backend.name})", result.stdout)
                self.assertIn(problem, " ".join(result.stderr.split()))
                self.assertNotIn("no backend for", result.output)

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

    def test_environment_keeps_status_away_from_the_keyring(self) -> None:
        self.save(IN_KEYRING)
        self.env(self.config, **ENV)
        backend = self.untouched_keyring()
        result = self.invoke("auth", "status")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("environment", result.stdout)
        self.assertIn("Storage : system keyring\n", result.stdout)
        self.assertEqual([], backend.mock_calls)

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

    def test_commands_use_the_keyring_token(self) -> None:
        self.save(IN_KEYRING)
        self.store(aas_token=f" {AAS}\n")
        result, seen = self.credentials_seen_by("auth", "token")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual([(EMAIL, AAS)], seen)

    def test_missing_or_unreadable_keyring_token_exits_2(self) -> None:
        for backend, store, problem in (
            (MemoryKeyring(), None, "is not in the system keyring"),
            (MemoryKeyring(), "", "is not in the system keyring"),
            (MemoryKeyring(), OAUTH, "in the system keyring is invalid"),
            (NullKeyring(), None, "is not in the system keyring"),
            (NoKeyring(), None, "from the system keyring (NoKeyringError)"),
            (LockedKeyring(), None, "from the system keyring (RuntimeError)"),
        ):
            with self.subTest(backend=backend.name, store=store):
                self.save(IN_KEYRING)
                if store is not None:
                    backend.set_password("goopdl", EMAIL, store)
                self.use(backend)
                for command in (("auth", "token"), ("info", "com.example.app")):
                    result, seen = self.credentials_seen_by(*command)
                    self.assertEqual(2, result.exit_code)
                    message = " ".join(result.output.split())
                    self.assertIn(problem, message)
                    self.assertIn("goopdl auth login", message)
                    self.assertIn("GOOPDL_AAS_TOKEN", message)
                    self.assertNotIn("no backend for", message)
                    self.assertNotIn("Traceback", result.output)
                    self.assertEqual([], seen)

    def test_environment_wins_without_touching_the_keyring(self) -> None:
        self.save(IN_KEYRING)
        self.env(self.config, **ENV)
        backend = self.untouched_keyring()
        result, seen = self.credentials_seen_by("auth", "token")
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual([("env@example.test", ENV_AAS)], seen)
        self.assertEqual([], backend.mock_calls)

    def test_file_account_and_anonymous_commands_leave_the_keyring_alone(self) -> None:
        backend = self.untouched_keyring()
        for data in (None, IN_FILE):
            if data is not None:
                self.save(data)
            result, _ = self.credentials_seen_by("auth", "token")
            self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual([], backend.mock_calls)

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
        self.store()
        for mode, data in ((0o644, IN_FILE), (0o640, IN_FILE), (0o604, IN_FILE),
                           (0o644, IN_KEYRING)):
            with self.subTest(mode=oct(mode), data=list(data)):
                self.save(data, mode=mode)
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
                     {"v": 1, "email": EMAIL, "aas_token": ["aas_et/x"]},
                     {"v": 1, "email": EMAIL, "storage": "file"},
                     {"v": 1, "email": EMAIL, "storage": "keyring", "aas_token": AAS},
                     {"v": 1, "email": " ", "storage": "keyring"},
                     {"v": 1, "storage": "keyring"},
                     {"v": 2, "email": EMAIL, "storage": "keyring"}):
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


class ImportBoundaryTest(unittest.TestCase):
    def run_isolated(self, code: str) -> None:
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith("GOOPDL_")}
        environment.update(PYTHONPATH=os.pathsep.join(p for p in sys.path if p),
                           PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=environment,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_library_imports_neither_keyring_nor_the_account_module(self) -> None:
        self.run_isolated(
            "import sys\n"
            "import goopdl, goopdl.client, goopdl.api, goopdl.auth, goopdl.aastoken\n"
            "import goopdl.download, goopdl.browser_oauth\n"
            "loaded = [m for m in sys.modules\n"
            "          if m.split('.')[0] == 'keyring' or m == 'goopdl._account']\n"
            "assert not loaded, loaded\n"
        )

    def test_commands_without_a_keyring_account_do_not_import_keyring(self) -> None:
        with tempfile.TemporaryDirectory(dir=tempfile.gettempdir()) as directory:
            config = str(Path(directory).resolve() / "config")
            self.run_isolated(
                "import json, os, sys\n"
                "from typer.testing import CliRunner\n"
                "from goopdl import cli\n"
                f"os.environ['GOOPDL_CONFIG_DIR'] = {config!r}\n"
                "def run(*args):\n"
                "    result = CliRunner().invoke(cli.app, list(args))\n"
                "    assert result.exit_code == 0, result.output\n"
                "run('auth', 'status')\n"
                "run('auth', 'logout')\n"
                "run('profiles')\n"
                "os.makedirs(os.environ['GOOPDL_CONFIG_DIR'], mode=0o700)\n"
                "path = os.path.join(os.environ['GOOPDL_CONFIG_DIR'], 'account.json')\n"
                "with open(path, 'w') as f:\n"
                f"    json.dump({IN_FILE!r}, f)\n"
                "os.chmod(path, 0o600)\n"
                "run('auth', 'status')\n"
                "run('auth', 'logout')\n"
                "assert 'keyring' not in sys.modules\n"
            )


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
        self.assertIn("--insecure-storage", self.invoke("auth", "login", "--help").output)


if __name__ == "__main__":
    unittest.main()
