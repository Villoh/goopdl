"""Legacy imports and exception handlers remain usable without eager Rich imports."""

import os
import pickle
import subprocess
import sys
import unittest
import warnings
from pathlib import Path
from typing import get_type_hints
from unittest.mock import patch

import httpx

from _envutil import isolated_env
from goopdl import auth, download
from goopdl.api import NetworkError, PlayAPIError


class CompatShimsTest(unittest.TestCase):
    def setUp(self):
        environment = isolated_env()
        environment.start()
        self.addCleanup(environment.stop)

    def run_python(self, source):
        result = subprocess.run(
            [sys.executable, "-c", source],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=15,
            # The child must see the parent's dependencies (Nix passes them via PYTHONPATH).
            env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_network_error_keeps_legacy_handlers_and_message(self):
        error = NetworkError("message only")
        self.assertEqual(str(error), "message only")
        self.assertEqual(error.args, ("message only",))
        self.assertIsNone(error.status_code)
        for handler in (
            httpx.HTTPError, httpx.RequestError, httpx.TransportError, PlayAPIError,
        ):
            with self.subTest(handler=handler), self.assertRaises(handler):
                raise error

    def test_network_error_request_is_optional_and_settable(self):
        error = NetworkError("message only")
        self.assertIsNone(error.request)
        request = httpx.Request("GET", "https://example.invalid/")
        error.request = request
        self.assertIs(error.request, request)

    def test_network_error_pickle_round_trip(self):
        for status_code in (None, 503):
            with self.subTest(status_code=status_code):
                original = NetworkError("message", status_code=status_code)
                # Trusted bytes created here, never user input.
                error = pickle.loads(pickle.dumps(original))  # noqa: S301
                self.assertIsInstance(error, NetworkError)
                self.assertIsInstance(error, httpx.HTTPError)
                self.assertIsInstance(error, PlayAPIError)
                self.assertEqual(str(error), "message")
                self.assertEqual(error.status_code, status_code)
                self.assertIsNone(error.request)

    def test_auth_rich_names_warn_and_console_is_cached_and_patchable(self):
        from rich.console import Console

        with self.assertWarns(DeprecationWarning):
            self.assertIs(auth.Console, Console)
        with self.assertWarns(DeprecationWarning):
            console = auth.console
        self.assertIsInstance(console, Console)
        with self.assertWarns(DeprecationWarning):
            self.assertIs(auth.console, console)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with patch.object(auth, "console") as mocked:
                self.assertIs(auth.console, mocked)
            self.assertIs(auth.console, console)

    def test_download_rich_names_warn_on_every_access(self):
        from rich import progress

        for name in (
            "BarColumn", "DownloadColumn", "Progress", "SpinnerColumn", "TextColumn",
            "TimeRemainingColumn", "TransferSpeedColumn",
        ):
            for _ in range(2):
                with self.subTest(name=name), self.assertWarns(DeprecationWarning) as caught:
                    self.assertIs(getattr(download, name), getattr(progress, name))
                self.assertEqual(caught.filename, __file__)

    def test_unknown_compat_names_raise_attribute_error(self):
        for module in (auth, download):
            with self.subTest(module=module), self.assertRaises(AttributeError):
                _ = module.no_such_compat_name

    def test_imports_do_not_load_rich(self):
        self.run_python(
            # httpx imports its command line client (rich, click) when they are installed.
            "import sys; sys.modules['httpx._main'] = None; "
            "import goopdl.auth, goopdl.download; "
            "assert 'rich' not in sys.modules"
        )

    def test_make_progress_type_hints_resolve_in_fresh_process(self):
        self.run_python(
            "from typing import get_type_hints; import goopdl.download as download; "
            "hints = get_type_hints(download.make_progress); "
            "from rich.progress import Progress; assert hints['return'] is Progress"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            from rich.progress import Progress
            self.assertIs(get_type_hints(download.make_progress)["return"], Progress)

    def test_profiles_star_import_in_fresh_process(self):
        self.run_python(
            "from goopdl.profiles import *; "
            "assert ARM64_PROFILES and ARMV7_PROFILES and TV_PROFILES; "
            "assert PROFILES_DIR.is_dir(); assert FALLBACK_PROFILE; "
            "assert ABI_TOKENS and VALID_ARCHS and COUNTRY_MCC; "
            "assert all(callable(f) for f in (patch_profile_country, is_tv_profile, "
            "load_all_profiles, find_profile, get_latest_probe_profiles, "
            "get_priority_profiles, get_compat_profiles, get_discovery_profiles))"
        )


if __name__ == "__main__":
    unittest.main()
