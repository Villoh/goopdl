"""Importing goopdl as a library must not touch disk, HOME or stdout."""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
