import subprocess
import sys
import unittest

import goopdl
from goopdl import errors
from goopdl.aastoken import AASTokenError
from goopdl.api import AuthExpiredError, PlayAPIError
from goopdl.auth import DirectAuthConfigurationError, DirectAuthError
from goopdl.browser_oauth import BrowserOAuthError
from goopdl.download import IntegrityError


class HierarchyTest(unittest.TestCase):
    def test_every_error_derives_from_goopdl_error_and_keeps_builtin_base(self):
        cases = [
            (PlayAPIError, Exception),
            (AuthExpiredError, PlayAPIError),
            (IntegrityError, RuntimeError),
            (DirectAuthError, RuntimeError),
            (DirectAuthConfigurationError, ValueError),
            (AASTokenError, ValueError),
            (BrowserOAuthError, RuntimeError),
        ]
        for cls, base in cases:
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, errors.GoopdlError))
                self.assertTrue(issubclass(cls, base))

    def test_reexports_are_the_same_objects(self):
        for name in errors.__all__:
            with self.subTest(name=name):
                self.assertIs(getattr(goopdl, name), getattr(errors, name))
        self.assertIs(errors.PlayAPIError, PlayAPIError)
        self.assertIs(errors.IntegrityError, IntegrityError)
        self.assertIn("__version__", goopdl.__all__)

    def test_import_goopdl_does_not_load_heavy_modules(self):
        code = (
            "import sys, goopdl, goopdl.errors; "
            "print([m for m in ('httpx', 'rich', 'websocket', 'goopdl.api') "
            "if m in sys.modules])"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
