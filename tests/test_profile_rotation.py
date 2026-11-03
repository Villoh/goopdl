"""Profile rejection is not a dispenser transport/server outage."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from goopdl import api, auth, cli


class ProfileRotationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for patcher in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(auth, "_CONFIG_DIR", Path(self.directory.name), create=True),
            patch.object(
                httpx, "get", side_effect=AssertionError("unexpected network")
            ),
            patch.object(
                httpx, "post", side_effect=AssertionError("unexpected network")
            ),
            patch.object(cli.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def rejected(status):
        return httpx.Response(status, json={"error": "profile rejected"})

    def download(self):
        return CliRunner().invoke(
            cli.app,
            [
                "download",
                "com.example.pkg",
                "-a",
                "arm64",
                "-o",
                self.directory.name,
            ],
        )

    def test_exhausted_profile_rejections_preserve_original_error(self):
        for original in (api.AppNotSupportedError, api.AppNotAvailableError):
            for status in (200, 400, 403, 404):
                with (
                    self.subTest(error=original.__name__, status=status),
                    patch.object(
                        cli, "pick_pool_token", return_value={"authToken": "old"}
                    ),
                    patch.object(
                        cli,
                        "get_details",
                        return_value=api.AppDetails("com.example.pkg", version_code=7),
                    ),
                    patch.object(cli, "purchase", return_value=""),
                    patch.object(
                        cli, "get_delivery", side_effect=original("original rejection")
                    ),
                    patch.object(
                        cli,
                        "get_discovery_profiles",
                        return_value=cli.get_compat_profiles("arm64")[:6],
                    ),
                    patch.object(
                        httpx, "post", return_value=self.rejected(status)
                    ) as post,
                ):
                    result = self.download()
                    self.assertEqual(result.exit_code, 4, result.output)
                    self.assertIn("original rejection", result.output)
                    self.assertEqual(post.call_count, 6)

    def test_exhausted_rotation_preserves_exception_identity(self):
        for kind in (api.AppNotSupportedError, api.AppNotAvailableError):
            original = kind("original rejection")
            with (
                self.subTest(error=kind.__name__),
                patch.object(cli, "get_details", side_effect=original),
                patch.object(
                    cli,
                    "get_discovery_profiles",
                    return_value=cli.get_compat_profiles("arm64")[:6],
                ),
                patch.object(httpx, "post", return_value=self.rejected(200)) as post,
            ):
                with self.assertRaises(kind) as raised:
                    cli._acquire_delivery(
                        "com.example.pkg", 7, "arm64", {}, None, None, None, None
                    )
                self.assertIs(raised.exception, original)
                self.assertEqual(post.call_count, 6)

    def test_third_profile_is_used_and_rotation_continues_if_delivery_rejects_it(self):
        for delivered in (False, True):
            seen = []

            def delivery(
                _package,
                _version,
                token,
                *,
                _seen=seen,
                _delivered=delivered,
                **_kwargs,
            ):
                _seen.append(token["authToken"])
                if _delivered and token["authToken"] == "third":
                    return api.DeliveryResult(
                        download_url="https://example.test/base.apk"
                    )
                raise api.AppNotSupportedError("original rejection")

            with (
                self.subTest(delivered=delivered),
                patch.object(cli, "pick_pool_token", return_value={"authToken": "old"}),
                patch.object(
                    cli,
                    "get_details",
                    return_value=api.AppDetails("com.example.pkg", version_code=7),
                ),
                patch.object(cli, "purchase", return_value=""),
                patch.object(cli, "get_delivery", side_effect=delivery),
                patch.object(
                    cli,
                    "download_batch",
                    side_effect=lambda specs, **_kwargs: [
                        spec.dest.write_bytes(b"apk") for spec in specs
                    ],
                ) as download,
                patch.object(
                    httpx,
                    "post",
                    side_effect=[
                        self.rejected(200),
                        self.rejected(200),
                        httpx.Response(200, json={"authToken": "third", "gsfId": "1"}),
                        self.rejected(200),
                        self.rejected(200),
                        self.rejected(200),
                    ],
                ) as post,
            ):
                result = self.download()
                self.assertEqual(result.exit_code, 0 if delivered else 4, result.output)
                self.assertEqual(post.call_count, 3 if delivered else 6)
                self.assertEqual(seen, ["old", "third"])
                self.assertEqual(download.call_count, int(delivered))

    def test_anonymous_fetch_rotates_all_profiles_for_rejections(self):
        count = len(auth.get_priority_profiles("arm64"))
        self.assertEqual(count, 19)
        for status in (200, 400, 403, 404):
            for raise_on_error in (False, True):
                with (
                    self.subTest(status=status, raising=raise_on_error),
                    patch.object(
                        httpx,
                        "post",
                        return_value=self.rejected(status),
                    ) as post,
                ):
                    if raise_on_error:
                        with self.assertRaises(
                            auth.DispenserUnavailableError
                        ) as raised:
                            auth.fetch_token(raise_on_error=True)
                        self.assertEqual(raised.exception.reason, "profile_rejected")
                        self.assertEqual(raised.exception.status, status)
                    else:
                        self.assertIsNone(auth.fetch_token())
                    self.assertEqual(post.call_count, count)

    def test_anonymous_fetch_finds_third_profile(self):
        with patch.object(
            httpx,
            "post",
            side_effect=[
                self.rejected(200),
                self.rejected(404),
                httpx.Response(200, json={"authToken": "third"}),
            ],
        ) as post:
            token = auth.fetch_token(raise_on_error=True)
            assert token is not None
            self.assertEqual(token["authToken"], "third")
            self.assertEqual(post.call_count, 3)

    def test_anonymous_endpoint_failures_stop_rotation(self):
        for failure, count, expected, reason in (
            (
                httpx.Response(429, headers={"Retry-After": "120"}),
                1,
                auth.DispenserRateLimitedError,
                None,
            ),
            (httpx.Response(503), 1, auth.DispenserUnavailableError, "server"),
            (
                httpx.ConnectError("down"),
                2,
                auth.DispenserUnavailableError,
                "transport",
            ),
            (httpx.ReadTimeout("late"), 2, auth.DispenserUnavailableError, "transport"),
        ):
            with (
                self.subTest(failure=repr(failure)),
                patch.object(
                    httpx,
                    "post",
                    side_effect=[failure] * 19,
                ) as post,
            ):
                with self.assertRaises(expected) as raised:
                    auth.fetch_token(raise_on_error=True)
                self.assertEqual(post.call_count, count)
                if reason:
                    assert isinstance(raised.exception, auth.DispenserUnavailableError)
                    self.assertEqual(raised.exception.reason, reason)
                else:
                    self.assertEqual(raised.exception.retry_after, 120)

    def test_single_profile_acquisition_does_not_repeat_unavailable_requests(self):
        for status in (200, 400, 403, 404, 503):
            with (
                self.subTest(status=status),
                patch.object(
                    httpx,
                    "post",
                    return_value=self.rejected(status),
                ) as post,
            ):
                with self.assertRaises(auth.DispenserUnavailableError):
                    api._acquire_single_token("arm64", None, None, None, "Pv")
                self.assertEqual(post.call_count, 1)

    def test_profile_rotation_stops_on_endpoint_failure(self):
        for failure, code in (
            (httpx.Response(429, headers={"Retry-After": "120"}), 5),
            (httpx.Response(503), 1),
            (httpx.ConnectError("down"), 1),
            (httpx.ReadTimeout("late"), 1),
        ):
            with (
                self.subTest(failure=repr(failure)),
                patch.object(cli, "pick_pool_token", return_value={"authToken": "old"}),
                patch.object(
                    cli, "get_details", side_effect=api.AppNotSupportedError("original")
                ),
                patch.object(httpx, "post", side_effect=[failure] * 19) as post,
            ):
                result = self.download()
                self.assertEqual(result.exit_code, code, result.output)
                self.assertEqual(post.call_count, 1)

    def test_version_resolution_retries_profile_rejections(self):
        for status in (200, 400, 403, 404):
            with (
                self.subTest(status=status),
                patch.object(
                    httpx,
                    "post",
                    side_effect=[
                        self.rejected(status),
                        self.rejected(status),
                        httpx.Response(200, json={"authToken": "third"}),
                    ],
                ) as post,
                patch.object(
                    cli,
                    "get_details",
                    return_value=api.AppDetails(
                        "com.example.pkg", version_code=7, version_string="1.2"
                    ),
                ),
            ):
                version, token = cli._resolve_version_string(
                    "com.example.pkg", "1.2", "arm64", None, None, None, "Pv"
                )
                self.assertEqual(version, 7)
                self.assertEqual(token["authToken"], "third")
                self.assertEqual(post.call_count, 3)

    def test_version_resolution_stops_on_server_failure(self):
        with patch.object(httpx, "post", return_value=httpx.Response(503)) as post:
            with self.assertRaises(api.NetworkError):
                cli._resolve_version_string(
                    "com.example.pkg", "1.2", "arm64", None, None, None, "Pv"
                )
            self.assertEqual(post.call_count, 1)

    def test_latest_skips_rejected_token_replacements(self):
        for status in (200, 400, 403, 404):
            auth.clear_auth()
            with (
                self.subTest(status=status),
                patch.object(
                    cli, "ensure_pool", return_value=[{"authToken": "expired"}] * 3
                ),
                patch.object(
                    cli,
                    "get_details",
                    side_effect=[
                        api.AuthExpiredError("expired"),
                        api.AuthExpiredError("expired"),
                        api.AuthExpiredError("expired"),
                        api.AppDetails(
                            "com.example.pkg", version_code=7, version_string="1.2"
                        ),
                    ],
                ),
                patch.object(
                    httpx,
                    "post",
                    side_effect=[
                        self.rejected(status),
                        self.rejected(status),
                        httpx.Response(200, json={"authToken": "third"}),
                    ],
                ) as post,
            ):
                result = CliRunner().invoke(cli.app, ["latest", "com.example.pkg"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(post.call_count, 3)

    def test_latest_rejected_initial_profile_is_not_an_endpoint_outage(self):
        with patch.object(httpx, "post", return_value=self.rejected(200)) as post:
            result = CliRunner().invoke(cli.app, ["latest", "com.example.pkg"])
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertNotIn("may be down", result.output)
            self.assertEqual(post.call_count, 1)

    def test_latest_stops_on_server_failure(self):
        with patch.object(httpx, "post", return_value=httpx.Response(503)) as post:
            result = CliRunner().invoke(cli.app, ["latest", "com.example.pkg"])
            self.assertEqual(result.exit_code, 3, result.output)
            self.assertEqual(post.call_count, 1)

    def test_unavailable_legacy_constructor_classifies_status(self):
        for status, reason in (
            (None, "transport"),
            (503, "server"),
            (400, "profile_rejected"),
            (200, "profile_rejected"),
        ):
            error = auth.DispenserUnavailableError(status)
            self.assertEqual(error.status, status)
            self.assertEqual(error.reason, reason)


if __name__ == "__main__":
    unittest.main()
