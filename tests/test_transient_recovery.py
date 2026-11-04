"""Hermetic HTTP-level regressions against the original main (c6e5f1c)."""
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from _envutil import isolated_env
from goopdl import api, auth, cli

PACKAGE = "com.example.pkg"


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result) + bytes([value])


def field(number, value):
    if isinstance(value, int):
        return varint(number << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(number << 3 | 2) + varint(len(value)) + value


DETAILS = field(1, field(2, field(4,
    field(1, PACKAGE) + field(5, "Example")
    + field(13, field(1, field(3, 7) + field(4, "1.2")))
)))
DELIVERY = field(1, field(21, field(1, 1) + field(2,
    field(3, "https://play.googleapis.com/base") + field(29, 7)
)))


class TransientRecoveryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "out"
        self.calls = Counter()
        self.failures = []
        self.expire_first = False
        self.details_status = 200
        for patcher in (
            isolated_env(),
            patch.object(auth, "_CONFIG_DIR", Path(directory.name) / "cache", create=True),
            patch.object(httpx, "post", side_effect=lambda *a, **k: self.request("POST", *a, **k)),
            patch.object(httpx, "get", side_effect=lambda *a, **k: self.request("GET", *a, **k)),
            patch.object(api, "download_batch", side_effect=self.download),
            patch.object(cli, "download_batch", side_effect=self.download),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeper = patch("time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def request(self, method, url, **kwargs):
        endpoint = str(url).rsplit("/", 1)[-1]
        if "android.clients.google.com" not in str(url):
            endpoint = "dispenser"
        self.calls.update([method, endpoint])
        if endpoint == "dispenser":
            if self.failures:
                failure = self.failures.pop(0)
                if isinstance(failure, Exception):
                    raise failure
                return failure
            return httpx.Response(200, json={
                "authToken": f"token-{self.calls[endpoint]}",
                "gsfId": str(self.calls[endpoint]),
            })
        if endpoint == "details":
            if self.expire_first and self.calls[endpoint] == 1:
                return httpx.Response(401)
            return httpx.Response(self.details_status, content=DETAILS)
        return httpx.Response(200, content=DELIVERY if endpoint == "delivery" else b"")

    def download(self, specs, **kwargs):
        for spec in specs:
            spec.dest.parent.mkdir(parents=True, exist_ok=True)
            spec.dest.write_bytes(b"apk")

    def invoke(self, command, *options):
        arguments = [command, PACKAGE, *options]
        if command in ("download", "fast-download"):
            arguments += ["-o", str(self.output)]
        return CliRunner().invoke(cli.app, arguments)

    def test_auth_recovers_on_next_profile_like_gold(self):
        self.failures = [httpx.Response(503)]
        result = CliRunner().invoke(cli.app, ["auth"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.calls["POST"], 2)
        self.assertEqual(self.calls["GET"], 0)
        self.sleep.assert_not_called()  # gold rotates profiles without sleeping

    def test_single_token_downloads_recover_like_gold(self):
        for path in ("fast-download", "api"):
            for failure in (httpx.ConnectError("down"), httpx.ReadTimeout("late"),
                            httpx.Response(502), httpx.Response(503)):
                with self.subTest(path=path, failure=repr(failure)):
                    auth.clear_auth()
                    self.calls.clear()
                    self.sleep.reset_mock()
                    self.failures = [failure]
                    if path == "api":
                        dest, version = api.download_app(PACKAGE, self.output, profile="Pv")
                        self.assertTrue(dest.exists())
                        self.assertEqual(version, "1.2")
                    else:
                        result = self.invoke(path, "--profile", "Pv")
                        self.assertEqual(result.exit_code, 0, result.output)
                    self.assertEqual(self.calls["POST"], 3)
                    self.assertEqual(self.calls["GET"], 2)
                    self.assertEqual(self.sleep.call_args_list, [((5,),)])

    def test_transport_and_server_share_two_failure_budget(self):
        for profile in (None, "Pv"):
            for failures in (
                [httpx.Response(503)] * 2,
                [httpx.ConnectError("down")] * 2,
                [httpx.Response(502), httpx.ReadTimeout("late")],
                [httpx.ConnectError("down"), httpx.Response(503)],
            ):
                with self.subTest(profile=profile, failures=repr(failures)):
                    self.calls.clear()
                    self.sleep.reset_mock()
                    self.failures = failures.copy()
                    with self.assertRaises(auth.DispenserUnavailableError) as raised:
                        auth.fetch_token(profile=profile, raise_on_error=True)
                    self.assertEqual(self.calls["POST"], 2)
                    self.assertEqual(raised.exception.status,
                                     failures[-1].status_code if isinstance(failures[-1], httpx.Response) else None)
                    self.assertEqual(self.sleep.call_args_list, [((5,),)] if profile else [])

    def test_fetch_item_uses_survivor_without_refilling(self):
        # Gold: 5 initial mints + 19 rejected replacements + 19 redundant
        # refill attempts, 2 GETs, dict. Keep its result, omit the refill.
        auth.ensure_pool()
        self.expire_first = True
        self.failures = [httpx.Response(400)] * 19
        item = api.fetch_app_item(PACKAGE)
        self.assertEqual(item["package_name"], PACKAGE)
        self.assertEqual(self.calls["POST"], 24)
        self.assertEqual(self.calls["GET"], 2)
        self.assertEqual(len(auth._load_pool("arm64", None)), 4)
        self.sleep.assert_not_called()

    def test_fetch_item_survives_endpoint_outage(self):
        auth.ensure_pool()
        self.expire_first = True
        self.failures = [httpx.Response(503)] * 2
        self.assertTrue(api.fetch_app_item(PACKAGE))
        self.assertEqual(self.calls["POST"], 7)
        self.assertEqual(self.calls["GET"], 2)
        self.sleep.assert_not_called()

    def test_latest_keeps_four_valid_probes_after_failed_replacement(self):
        for failure in (httpx.Response(503), httpx.ConnectError("down")):
            with self.subTest(failure=repr(failure)):
                auth.clear_auth()
                self.calls.clear()
                self.sleep.reset_mock()
                auth.ensure_pool(profile="Pv")
                self.failures = [failure] * 2
                self.expire_first = True
                result = self.invoke("latest", "--profile", "Pv")
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(self.calls["GET"], 5)  # gold: 401 + four survivors
                # Gold: 6 POST. Survivors exist, so no cooldown retry/sleep.
                self.assertEqual(self.calls["POST"], 6)
                self.sleep.assert_not_called()
                self.assertEqual(len(self.failures), 1)  # single attempt
                self.failures.clear()

    def test_replacement_without_survivors_keeps_cooldown_retry(self):
        auth.ensure_pool(profile="Pv", size=1)
        (failed,) = auth._load_pool("arm64", None, "Pv")
        self.calls.clear()
        self.failures = [httpx.Response(503)]
        token = auth.replace_pool_token(failed, profile="Pv")
        assert token is not None
        self.assertEqual(token["authToken"], "token-2")  # 503, then fresh mint
        self.assertEqual(self.calls["POST"], 2)
        self.assertEqual(self.sleep.call_args_list, [((5,),)])

    def test_latest_replacement_rate_limit_stops_immediately(self):
        auth.ensure_pool(profile="Pv")
        self.expire_first = True
        self.failures = [httpx.Response(429, headers={"Retry-After": "120"})]
        result = self.invoke("latest", "--profile", "Pv")
        self.assertEqual(result.exit_code, 5, result.output)
        self.assertEqual(self.calls["POST"], 6)
        self.assertEqual(self.calls["GET"], 1)
        self.sleep.assert_not_called()

    def test_partial_pool_does_not_hide_rate_limit(self):
        auth.ensure_pool(profile="Pv", size=1)
        self.failures = [httpx.Response(429, headers={"Retry-After": "120"})]
        with self.assertRaises(auth.DispenserRateLimitedError) as raised:
            auth.ensure_pool(profile="Pv", raise_on_error=True)
        self.assertEqual(raised.exception.retry_after, 120)
        self.assertEqual(self.calls["POST"], 2)
        self.sleep.assert_not_called()

    def test_version_resolution_recovers_with_cooldown(self):
        self.failures = [httpx.Response(503)]
        version, token = cli._resolve_version_string(
            PACKAGE, "1.2", "arm64", None, None, None, "Pv"
        )
        self.assertEqual(version, 7)
        self.assertEqual(token["authToken"], "token-2")
        self.assertEqual(self.calls["POST"], 2)
        self.assertEqual(self.calls["GET"], 1)
        self.assertEqual(self.sleep.call_args_list, [((5,),)])

    def test_info_reuses_survivor_without_refilling(self):
        auth.ensure_pool(profile="Pv")
        self.expire_first = True
        self.failures = [httpx.Response(400)] * 2
        result = self.invoke("info", "--profile", "Pv")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.calls["POST"], 6)
        self.assertEqual(self.calls["GET"], 2)
        self.assertEqual(len(self.failures), 1)  # no second mint to refill
        self.sleep.assert_not_called()

    def test_replacement_429_stops_download_and_info_paths(self):
        for path in ("download", "fast-download", "info", "raw", "api", "item"):
            with self.subTest(path=path):
                auth.clear_auth()
                self.calls.clear()
                self.sleep.reset_mock()
                auth.ensure_pool(profile="Pv")
                self.expire_first = True
                self.failures = [httpx.Response(429, headers={"Retry-After": "120"})]
                if path in ("api", "item"):
                    # fetch_app_item now matches download_app (RateLimitedError).
                    with self.assertRaises(api.RateLimitedError) as raised:
                        if path == "api":
                            api.download_app(PACKAGE, self.output, profile="Pv")
                        else:
                            api.fetch_app_item(PACKAGE, profile="Pv")
                    self.assertEqual(raised.exception.retry_after, 120)
                    self.assertIsInstance(
                        raised.exception.__cause__, auth.DispenserRateLimitedError
                    )
                elif path == "raw":
                    result = self.invoke("info", "--raw", "--profile", "Pv")
                    self.assertEqual(result.exit_code, 5, result.output)
                else:
                    result = self.invoke(path, "--profile", "Pv")
                    self.assertEqual(result.exit_code, 5, result.output)
                self.assertEqual(self.calls["POST"], 6)
                self.assertEqual(self.calls["GET"], 1)
                self.sleep.assert_not_called()

    def test_profile_response_resets_server_failure_streak(self):
        self.failures = [httpx.Response(503), httpx.Response(400), httpx.Response(502)]
        token = auth.fetch_token(raise_on_error=True)
        assert token is not None
        self.assertEqual(token["authToken"], "token-4")
        self.assertEqual(self.calls["POST"], 4)
        self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
