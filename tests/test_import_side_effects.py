"""Importing goopdl as a library must not touch disk, HOME or stdout."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from goopdl import auth

ROOT = Path(__file__).resolve().parent.parent


def run_isolated(code: str) -> subprocess.CompletedProcess:
    # Fresh interpreter: the modules under test must not be imported yet.
    return subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
        timeout=60,
    )


class ImportSideEffectsTest(unittest.TestCase):
    def test_profiles_are_loaded_on_first_use_not_on_import(self):
        result = run_isolated(
            "import pathlib\n"
            "real = pathlib.Path.glob\n"
            "def boom(*a, **k): raise AssertionError('profiles read at import')\n"
            "pathlib.Path.glob = boom\n"
            "import goopdl.profiles as p\n"
            "pathlib.Path.glob = real\n"
            "assert p.get_priority_profiles('arm64')\n"
            "assert p.ARM64_PROFILES and p.TV_PROFILES and p._ALL\n"
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_import_without_home_is_silent_and_writes_nothing(self):
        result = run_isolated(
            "import pathlib\n"
            "def boom(*a, **k): raise RuntimeError('HOME used at import')\n"
            "pathlib.Path.home = classmethod(boom)\n"
            "import goopdl, goopdl.auth, goopdl.api, goopdl.aastoken\n"
            "import goopdl.browser_oauth, goopdl.profiles\n"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)


class ConfigDirTest(unittest.TestCase):
    def test_resolution_order(self):
        with patch.dict(os.environ, {"GOOPDL_CONFIG_DIR": "/g",
                                     "XDG_CONFIG_HOME": "/x"}):
            self.assertEqual(Path("/g"), auth.get_config_dir())
        with patch.dict(os.environ, {"GOOPDL_CONFIG_DIR": "",
                                     "XDG_CONFIG_HOME": "/x"}):
            self.assertEqual(Path("/x/goopdl"), auth.get_config_dir())
            self.assertEqual(Path("/x/goopdl"), auth._CONFIG_DIR)
        with (patch.dict(os.environ, {"GOOPDL_CONFIG_DIR": "",
                                      "XDG_CONFIG_HOME": "relative"}),
              patch.object(Path, "home", return_value=Path("/h"))):
            self.assertEqual(Path("/h/.config/goopdl"), auth.get_config_dir())

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_save_auth_is_private_and_atomic(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "cfg"
            with patch.dict(os.environ, {"GOOPDL_CONFIG_DIR": str(directory)}):
                path = auth.save_auth({"authToken": "secret"}, "arm64", "US")
                cached = auth.load_cached_auth("arm64", "US")
            self.assertEqual("secret", (cached or {}).get("authToken"))
            self.assertEqual(directory, path.parent)
            self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual([path], list(directory.iterdir()))  # no temp leftovers


if __name__ == "__main__":
    unittest.main()
