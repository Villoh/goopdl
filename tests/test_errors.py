import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import httpx

import goopdl
from goopdl import errors
from goopdl.aastoken import AASTokenError
from goopdl.api import (
    AuthExpiredError,
    InvalidPackageError,
    NetworkError,
    PlayAPIError,
    get_delivery,
    get_details,
    list_splits,
    purchase,
    search_apps,
)
from goopdl.auth import DirectAuthConfigurationError, DirectAuthError
from goopdl.browser_oauth import BrowserOAuthError
from goopdl.download import IntegrityError

AUTH = {"authToken": "token"}
DTOK = "signed-dtok-value"
# Field 1, length-delimited, declares 5 bytes but carries only 1.
TRUNCATED = b"\x0a\x05x"


class HierarchyTest(unittest.TestCase):
    def test_every_exported_error_derives_from_goopdl_error(self):
        self.assertEqual(set(errors.__all__) - {"GoopdlError"}, set(errors._LAZY))
        self.assertLessEqual(set(errors.__all__), set(goopdl.__all__))
        for name in errors.__all__:
            with self.subTest(name=name):
                self.assertTrue(issubclass(getattr(errors, name), errors.GoopdlError))

    def test_errors_keep_builtin_base(self):
        cases = [
            (InvalidPackageError, ValueError),
            (PlayAPIError, Exception),
            (AuthExpiredError, PlayAPIError),
            (NetworkError, PlayAPIError),
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
            [sys.executable, "-c", code], capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)},
        )
        self.assertEqual(out.stdout.strip(), "[]")


class TransportWrappingTest(unittest.TestCase):
    calls = [
        ("get", lambda: get_details("com.example.pkg", AUTH)),
        ("get", lambda: get_delivery("com.example.pkg", 1, AUTH, delivery_token=DTOK)),
        ("get", lambda: list_splits("com.example.pkg", AUTH)),
        ("get", lambda: search_apps("q", AUTH)),
        ("post", lambda: purchase("com.example.pkg", 1, AUTH)),
    ]

    def test_httpx_errors_become_network_error(self):
        failures = [
            httpx.ConnectTimeout("timed out"),
            httpx.ConnectError("refused"),
            httpx.RemoteProtocolError("broken"),
        ]
        for method, call in self.calls:
            for failure in failures:
                with self.subTest(method=method, failure=type(failure).__name__):
                    with (
                        patch(f"goopdl.api.httpx.{method}", side_effect=failure),
                        self.assertRaises(NetworkError) as raised,
                    ):
                        call()
                    self.assertIs(raised.exception.__cause__, failure)
                    self.assertNotIn(DTOK, str(raised.exception))


class MalformedProtoTest(unittest.TestCase):
    @staticmethod
    def _wrap(payload):
        # ResponseWrapper(1) whose nested payload is truncated.
        return b"\x0a" + bytes([len(payload)]) + payload

    def test_decoder_errors_become_play_api_error(self):
        details = self._wrap(b"\x12\x03" + b"\x22\x05x")  # 1 -> 2 -> 4 truncated
        delivery = self._wrap(b"\xaa\x01\x03" + b"\x12\x05x")  # 1 -> 21 -> 2 truncated
        cases = [
            ("get", details, lambda: get_details("com.example.pkg", AUTH)),
            ("get", details, lambda: list_splits("com.example.pkg", AUTH)),
            ("get", delivery, lambda: get_delivery("com.example.pkg", 1, AUTH)),
            ("post", TRUNCATED, lambda: purchase("com.example.pkg", 1, AUTH)),
        ]
        for method, content, call in cases:
            with self.subTest(content=content, method=method):
                response = httpx.Response(200, content=content)
                with (
                    patch(f"goopdl.api.httpx.{method}", return_value=response),
                    self.assertRaises(PlayAPIError) as raised,
                ):
                    call()
                self.assertIsInstance(
                    raised.exception.__cause__, (ValueError, EOFError)
                )


if __name__ == "__main__":
    unittest.main()
