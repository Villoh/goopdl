import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from goopdl import auth, cli
from goopdl.api import (
    AppDetails,
    DeliveryResult,
    InvalidPackageError,
    PlayAPIError,
    SplitInfo,
    VersionUnavailableError,
    _build_specs,
    _safe_dest,
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
from goopdl.auth import InvalidArchError
from goopdl.profiles import VALID_ARCHS
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


EVIL_SPLIT = "../../etc/x"


def _evil_delivery() -> DeliveryResult:
    return DeliveryResult(
        download_url="https://play.googleapis.com/base",
        splits=[SplitInfo(EVIL_SPLIT, url="https://play.googleapis.com/split")],
    )


class SplitFileNameTest(unittest.TestCase):
    def test_build_specs_keeps_split_inside_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            specs = _build_specs("com.example.app", 1, output, _evil_delivery(), False, True)

        split = specs[1]
        self.assertEqual(split.dest.parent, output)
        self.assertEqual(split.dest.name, "com.example.app-1-.._.._etc_x.apk")
        self.assertEqual(split.label, split.dest.name)

    def test_safe_dest_refuses_paths_that_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            output.mkdir()
            (output / "link").symlink_to(Path(directory))
            for name in ("..", ".", "link"):
                with self.subTest(name=name), self.assertRaises(PlayAPIError):
                    _safe_dest(output, name)

    def test_cli_download_keeps_split_inside_output(self):
        written: list[Path] = []

        def fake_batch(specs, require_integrity=False):
            for spec in specs:
                written.append(spec.dest)
                spec.dest.write_bytes(b"x")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            with (
                patch("goopdl.cli.pick_pool_token", return_value={"authToken": "t"}),
                patch(
                    "goopdl.cli._acquire_delivery",
                    return_value=(
                        AppDetails("com.example.app", version_string="1.0"),
                        1,
                        _evil_delivery(),
                    ),
                ),
                patch("goopdl.cli.download_batch", side_effect=fake_batch),
            ):
                result = CliRunner().invoke(
                    cli.app,
                    ["download", "com.example.app", "--splits", "-o", str(output)],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(len(written), 2)
            for dest in written:
                self.assertEqual(dest.resolve().parent, output.resolve())


if __name__ == "__main__":
    unittest.main()

class ArchValidationTest(unittest.TestCase):
    BAD_ARCHS = ("../x", "x/../../y", "", "ARM64 ", "x/../../outside/pwned")

    def test_cache_paths_reject_unknown_arch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "cfg"
            with patch("goopdl.auth._CONFIG_DIR", config):
                for arch in self.BAD_ARCHS:
                    with self.subTest(arch=arch):
                        with self.assertRaises(InvalidArchError):
                            auth.save_auth({"authToken": "t"}, arch=arch)
                        for build in (auth._pool_path, auth._index_path):
                            with self.assertRaises(InvalidArchError):
                                build(arch, None)
                        with self.assertRaises(InvalidArchError):
                            with auth._pool_lock(arch, None):
                                pass
            leaked = [p for p in Path(directory).rglob("*") if p != config]
            self.assertEqual(leaked, [])

    def test_valid_archs_build_paths_inside_config_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "cfg"
            with patch("goopdl.auth._CONFIG_DIR", config):
                for arch in VALID_ARCHS:
                    with self.subTest(arch=arch):
                        path = auth.save_auth({"authToken": "t"}, arch=arch)
                        self.assertEqual(path.parent, config)
                        self.assertEqual(auth.load_cached_auth(arch)["authToken"], "t")
                        self.assertEqual(auth._pool_path(arch, "us").parent, config)
                        self.assertEqual(auth._index_path(arch, None).parent, config)

    def test_error_is_a_value_error(self):
        self.assertTrue(issubclass(InvalidArchError, ValueError))

    def test_cli_rejects_bad_arch(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("goopdl.cli.fetch_token") as fetch,
        ):
            result = CliRunner().invoke(cli.app, ["auth", "--arch", "../x"])
        self.assertEqual(result.exit_code, 2, result.output)
        fetch.assert_not_called()
