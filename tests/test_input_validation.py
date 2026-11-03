import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from goopdl import cli
from goopdl.api import (
    InvalidPackageError,
    VersionUnavailableError,
    download_app,
    fetch_app_item,
    get_delivery,
    get_details,
    get_details_raw,
    list_splits,
    purchase,
    search_apps,
    validate_package,
)
from typer.testing import CliRunner

BAD_PACKAGES = [
    "../etc/passwd",
    "com.example/../x",
    "com.example.app&dtok=x",
    "com.example.app#x",
    "com.example.app\n",
    "noDots",
    "1com.example",
    "com..example",
    "",
]
AUTH = {"authToken": "t"}


def _recording_get(sent: list[httpx.URL], status: int = 200):
    """Fake httpx.get that builds the real request URL, as httpx would send it."""

    def fake_get(url, *, params=None, **_kwargs):
        sent.append(httpx.Request("GET", url, params=params).url)
        return httpx.Response(status)

    return fake_get


class QueryEncodingTest(unittest.TestCase):
    def test_search_query_special_chars_are_encoded(self):
        sent: list[httpx.URL] = []
        with patch("goopdl.api.httpx.get", side_effect=_recording_get(sent)):
            search_apps("a&dtok=x#frag", {"authToken": "t"}, country="us")

        query = sent[0].query.decode()
        self.assertNotIn("&dtok=", query)
        self.assertNotIn("#", query)
        self.assertEqual(
            parse_qs(query), {"q": ["a&dtok=x#frag"], "c": ["3"], "gl": ["US"]}
        )

    def test_delivery_token_is_encoded(self):
        sent: list[httpx.URL] = []
        with (
            patch("goopdl.api.httpx.get", side_effect=_recording_get(sent, 404)),
            self.assertRaises(VersionUnavailableError),
        ):
            get_delivery(
                "com.example.app", 1, {"authToken": "t"}, delivery_token="a&gl=XX"
            )

        params = parse_qs(sent[0].query.decode())
        self.assertEqual(params["dtok"], ["a&gl=XX"])
        self.assertNotIn("gl", params)

    @patch("goopdl.api.httpx.post")
    def test_purchase_body_is_form_encoded(self, post):
        post.return_value = httpx.Response(204)
        purchase("com.example.app", 7, {"authToken": "t"})

        body = post.call_args.kwargs["content"]
        self.assertEqual(
            parse_qs(body), {"doc": ["com.example.app"], "ot": ["1"], "vc": ["7"]}
        )


class PackageValidationTest(unittest.TestCase):
    def test_accepts_real_package_names(self):
        for package in ("com.whatsapp", "com.google.android.youtube", "a.b_C9"):
            self.assertEqual(validate_package(package), package)

    def test_error_is_a_value_error(self):
        self.assertTrue(issubclass(InvalidPackageError, ValueError))

    @patch("goopdl.api.pick_pool_token")
    @patch("goopdl.api.httpx.post")
    @patch("goopdl.api.httpx.get")
    def test_public_api_rejects_bad_packages_before_any_request(
        self, get, post, pick
    ):
        calls = [
            lambda p: get_details(p, AUTH),
            lambda p: get_details_raw(p, AUTH),
            lambda p: purchase(p, 1, AUTH),
            lambda p: get_delivery(p, 1, AUTH, delivery_token="d"),
            lambda p: list_splits(p, AUTH),
            lambda p: fetch_app_item(p),
            lambda p: download_app(p, Path("unused")),
        ]
        for package in BAD_PACKAGES:
            for call in calls:
                with self.subTest(package=package), self.assertRaises(
                    InvalidPackageError
                ):
                    call(package)
        get.assert_not_called()
        post.assert_not_called()
        pick.assert_not_called()

    def test_cli_rejects_bad_package(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in ("download", "fast-download", "info", "list-splits"):
                with patch("goopdl.cli.pick_pool_token") as pick:
                    result = CliRunner().invoke(
                        cli.app, [command, "../../tmp/x", "-o", directory]
                        if "download" in command
                        else [command, "../../tmp/x"],
                    )
                self.assertEqual(result.exit_code, 2, result.output)
                pick.assert_not_called()


if __name__ == "__main__":
    unittest.main()
