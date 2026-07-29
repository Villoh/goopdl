import unittest

from goopdl.cli import _adb_install_command


class AdbInstallTipTest(unittest.TestCase):
    def test_scopes_apks_to_package_and_version(self):
        self.assertEqual(
            _adb_install_command("com.example.app", 123),
            "adb install-multiple com.example.app-123*.apk",
        )

    def test_appends_dex_metadata_when_downloaded(self):
        self.assertEqual(
            _adb_install_command("com.example.app", 123, dm=True),
            "adb install-multiple com.example.app-123*.apk com.example.app-123.dm",
        )


if __name__ == "__main__":
    unittest.main()
