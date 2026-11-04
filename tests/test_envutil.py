import os
import unittest
from unittest.mock import patch

from _envutil import isolated_env

ESSENTIALS = {
    "SystemRoot": r"C:\Windows", "windir": r"C:\Windows", "USERPROFILE": r"C:\Users\u",
    "HOMEDRIVE": "C:", "HOMEPATH": r"\Users\u", "TEMP": r"C:\t", "TMP": r"C:\t",
    "ComSpec": r"C:\Windows\cmd.exe", "PATHEXT": ".EXE", "APPDATA": r"C:\a",
    "LOCALAPPDATA": r"C:\l", "ProgramData": r"C:\ProgramData",
}
NOISE = {"GOOPDL_ACCOUNT_EMAIL": "a@b.c", "GOOPDL_AAS_TOKEN": "x", "PYTHONPATH": "p",
         "HOME": "/h", "PATH": "/bin"}


class IsolatedEnvTest(unittest.TestCase):
    def isolated(self, os_name, extra=None):
        environ = {**ESSENTIALS, **NOISE}
        with patch.object(os, "name", os_name), patch.object(os, "environ", environ):
            with isolated_env(extra):
                inside = dict(environ)
            self.assertEqual({**ESSENTIALS, **NOISE}, environ)  # restored
        return inside

    def test_windows_keeps_os_essentials_only(self):
        self.assertEqual(ESSENTIALS, self.isolated("nt"))
        self.assertEqual({**ESSENTIALS, "GOOPDL_AAS_TOKEN": "y"},
                         self.isolated("nt", {"GOOPDL_AAS_TOKEN": "y"}))

    def test_posix_is_empty_plus_extra(self):
        self.assertEqual({}, self.isolated("posix"))
        self.assertEqual({"HOME": "/h2"}, self.isolated("posix", {"HOME": "/h2"}))

    def test_decorator_and_start_stop(self):
        @isolated_env({"ONLY": "1"})
        def inside():
            return dict(os.environ)

        self.assertEqual("1", inside()["ONLY"])
        patcher = isolated_env()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertNotIn("GOOPDL_AAS_TOKEN", os.environ)


if __name__ == "__main__":
    unittest.main()
