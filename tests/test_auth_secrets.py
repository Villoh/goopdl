"""Hermetic regressions for credential boundaries, retries and pool contention."""

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from _envutil import isolated_env
from goopdl import api, auth, cli, errors


def capture_failure(call) -> Any:
    # The caller/test may own secrets; start at the library entry frame.
    try:
        call()
    except Exception as exc:  # noqa: BLE001 -- inspect the full failure graph
        tb = exc.__traceback__
        assert tb is not None and tb.tb_next is not None
        exc.__traceback__ = tb.tb_next.tb_next
        return exc
    raise AssertionError("Expected an exception")


class AuthSecretsTest(unittest.TestCase):
    def setUp(self):
        environment = isolated_env()
        environment.start()
        self.addCleanup(environment.stop)
        sleeper = patch("time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        # No test may accidentally contact a real endpoint.
        for method in ("get", "post"):
            network = patch.object(
                httpx, method, side_effect=AssertionError("real network")
            )
            network.start()
            self.addCleanup(network.stop)

    def assert_redacted(self, error, secrets, *, allow_caller_auth=False):
        seen = set()
        pending = [error]
        while pending:
            exc = pending.pop()
            if exc is None or id(exc) in seen:
                continue
            seen.add(id(exc))
            for text in (str(exc), repr(exc)):
                for secret in secrets:
                    self.assertNotIn(secret, text)
            tb = exc.__traceback__
            while tb:
                for name, value in tb.tb_frame.f_locals.items():
                    if allow_caller_auth and name == "auth":
                        continue  # incoming caller-owned dict is explicitly out of scope
                    for text in (str(value), repr(value)):
                        for secret in secrets:
                            self.assertNotIn(secret, text, tb.tb_frame.f_code.co_name)
                tb = tb.tb_next
            pending.extend((exc.__cause__, exc.__context__))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def direct_failure(self, response):
        with patch.object(httpx, "post", return_value=response):
            return capture_failure(
                lambda: auth._direct_auth(
                    "email-sentinel@example.test",
                    "aas_et/aas-sentinel",
                    "arm64",
                    None,
                    "http://user:proxy-sentinel@localhost:8080",
                    "Pv",
                )
            )

    def test_direct_entry_erases_credentials_and_proxy(self):
        error = self.direct_failure(httpx.Response(403))
        self.assertIsInstance(error, auth.DirectAuthError)
        self.assertEqual(str(error), "checkin (HTTP 403)")
        self.assert_redacted(
            error, ("email-sentinel", "aas-sentinel", "proxy-sentinel")
        )

    def test_direct_unexpected_exception_is_detached(self):
        with patch.object(
            httpx, "post", side_effect=httpx.ConnectError("aas-sentinel")
        ):
            error = capture_failure(
                lambda: auth._direct_auth(
                    "email-sentinel@example.test",
                    "aas_et/aas-sentinel",
                    "arm64",
                    None,
                    None,
                    "Pv",
                )
            )
        self.assertIsInstance(error, auth.DirectAuthError)
        self.assert_redacted(error, ("email-sentinel", "aas-sentinel"))

    def test_direct_later_stages_erase_intermediate_tokens(self):
        checkin = httpx.Response(
            200,
            content=auth._proto_varint(7, 1234)
            + auth._proto_bytes(12, "consistency-sentinel"),
        )
        upload = httpx.Response(
            200,
            content=auth._proto_bytes(
                1, auth._proto_bytes(28, auth._proto_bytes(1, "config-sentinel"))
            ),
        )
        exchange = httpx.Response(200, text="Auth=bearer-sentinel\n")
        for index, stage in enumerate(
            # toc is optional: its 429 keeps the token (test_final_polish).
            ("checkin", "device config upload", "token exchange")
        ):
            success = [checkin, upload, exchange]
            limited = httpx.Response(429, headers={"Retry-After": "120"})
            with (
                self.subTest(stage=stage),
                patch.object(httpx, "post", side_effect=success[:index] + [limited]),
                patch.object(
                    httpx,
                    "get",
                    return_value=limited,
                ),
            ):
                error = capture_failure(
                    lambda: auth._direct_auth(
                        "email-sentinel@example.test",
                        "aas_et/aas-sentinel",
                        "arm64",
                        None,
                        "http://user:proxy-sentinel@localhost:8080",
                        "Pv",
                    )
                )
                self.assertIsInstance(error, auth.DirectAuthError)
                self.assertEqual(error.status, 429)
                self.assertEqual(error.stage, stage)
                self.assertEqual(error.retry_after, 120.0)
                self.assert_redacted(
                    error,
                    (
                        "email-sentinel",
                        "aas-sentinel",
                        "proxy-sentinel",
                        "consistency-sentinel",
                        "config-sentinel",
                        "bearer-sentinel",
                    ),
                )

    def test_send_erases_request_arguments(self):
        with patch.object(
            httpx, "get", side_effect=httpx.ConnectError("proxy-sentinel")
        ):
            error = capture_failure(
                lambda: api._send(
                    httpx.get,
                    "https://example.test/?dtok=dtok-sentinel",
                    headers={"Authorization": "Bearer bearer-sentinel"},
                    proxy="http://user:proxy-sentinel@localhost:8080",
                )
            )
        self.assertIsInstance(error, api.NetworkError)
        self.assert_redacted(
            error, ("dtok-sentinel", "bearer-sentinel", "proxy-sentinel")
        )

    def test_api_callers_erase_derived_request_secrets(self):
        token = {"authToken": "bearer-sentinel"}
        dtok = "dtok-sentinel"
        calls = (
            lambda: api.get_details(
                "com.example.pkg",
                token,
                proxy="http://user:proxy-sentinel@localhost:8080",
            ),
            lambda: api.purchase(
                "com.example.pkg",
                1,
                token,
                proxy="http://user:proxy-sentinel@localhost:8080",
            ),
            lambda: api.get_delivery(
                "com.example.pkg",
                1,
                token,
                proxy="http://user:proxy-sentinel@localhost:8080",
                delivery_token=dtok,
            ),
            lambda: api.list_splits(
                "com.example.pkg",
                token,
                proxy="http://user:proxy-sentinel@localhost:8080",
            ),
            lambda: api.search_apps(
                "example", token, proxy="http://user:proxy-sentinel@localhost:8080"
            ),
        )
        with (
            patch.object(httpx, "get", side_effect=httpx.ConnectError("down")),
            patch.object(
                httpx,
                "post",
                side_effect=httpx.ConnectError("down"),
            ),
        ):
            for index, call in enumerate(calls):
                with self.subTest(function=index):
                    error = capture_failure(call)
                    self.assertIsInstance(error, api.NetworkError)
                    self.assert_redacted(
                        error,
                        ("bearer-sentinel", "dtok-sentinel", "proxy-sentinel"),
                        allow_caller_auth=True,
                    )
        self.assertEqual(token, {"authToken": "bearer-sentinel"})

    def test_existing_network_error_is_not_wrapped_or_retried(self):
        for entry in ("send", "fetch", "acquire", "profiles"):
            original = api.NetworkError("Endpoint unavailable.")
            with (
                self.subTest(entry=entry),
                patch.object(httpx, "get", side_effect=original),
                patch.object(httpx, "post", side_effect=original) as post,
                patch.object(api, "pick_pool_token", side_effect=original) as pick,
                patch.object(
                    cli, "get_details", side_effect=api.AppNotSupportedError("no")
                ),
                patch.object(api.time, "sleep") as sleep,
            ):
                if entry == "send":
                    error = capture_failure(
                        lambda: api._send(
                            httpx.get,
                            "https://example.test/?dtok=dtok-sentinel",
                            headers={"Authorization": "Bearer bearer-sentinel"},
                            proxy="http://user:proxy-sentinel@localhost:8080",
                        )
                    )
                elif entry == "fetch":
                    error = capture_failure(
                        lambda: auth.fetch_token(
                            proxy="http://user:proxy-sentinel@localhost:8080",
                            raise_on_error=True,
                        )
                    )
                    self.assertEqual(post.call_count, 1)
                elif entry == "acquire":
                    error = capture_failure(
                        lambda: api._acquire_single_token(
                            "arm64", None, None, None, "Pv"
                        )
                    )
                    self.assertEqual(pick.call_count, 1)
                else:
                    error = capture_failure(
                        lambda: cli._acquire_delivery(
                            "com.example.pkg", 1, "arm64", {}, None, None, None, None
                        )
                    )
                    self.assertEqual(post.call_count, 1)
                self.assertIs(error, original)
                sleep.assert_not_called()
                if entry in ("send", "fetch"):
                    self.assert_redacted(
                        error, ("dtok-sentinel", "bearer-sentinel", "proxy-sentinel")
                    )

    def test_fetch_erases_proxy_on_transport_failure(self):
        with patch.object(
            httpx, "post", side_effect=httpx.ConnectError("proxy-sentinel")
        ):
            error = capture_failure(
                lambda: auth.fetch_token(
                    proxy="http://user:proxy-sentinel@localhost:8080",
                    raise_on_error=True,
                )
            )
        self.assertIsInstance(error, auth.DispenserUnavailableError)
        self.assert_redacted(error, ("proxy-sentinel",))

    def test_direct_status_and_retry_after_survive_fetch(self):
        for status in (400, 401, 403, 429, 503):
            with (
                self.subTest(status=status),
                patch.dict(
                    os.environ,
                    {
                        "GOOPDL_ACCOUNT_EMAIL": "email-sentinel@example.test",
                        "GOOPDL_AAS_TOKEN": "aas_et/aas-sentinel",
                    },
                ),
                patch.object(
                    httpx,
                    "post",
                    return_value=httpx.Response(
                        status,
                        headers={"Retry-After": "120"},
                    ),
                ),
            ):
                error = capture_failure(lambda: auth.fetch_token(raise_on_error=True))
                self.assertEqual(error.status, status)
                self.assertEqual(error.stage, "checkin")
                self.assertEqual(error.retry_after, 120.0 if status == 429 else None)
                self.assert_redacted(error, ("email-sentinel", "aas-sentinel"))

    def test_direct_permanent_and_rate_limit_are_not_retried(self):
        with (
            patch.dict(
                os.environ,
                {
                    "GOOPDL_ACCOUNT_EMAIL": "email-sentinel@example.test",
                    "GOOPDL_AAS_TOKEN": "aas_et/aas-sentinel",
                },
            ),
            patch.object(api.time, "sleep") as sleep,
        ):
            for status in (400, 401, 403, 429):
                with (
                    self.subTest(status=status),
                    patch.object(
                        httpx,
                        "post",
                        return_value=httpx.Response(
                            status, headers={"Retry-After": "120"}
                        ),
                    ) as post,
                ):
                    error = capture_failure(
                        lambda: api.download_app(
                            "com.example.pkg", Path("unused"), profile="Pv"
                        )
                    )
                    self.assertEqual(post.call_count, 1)
                    if status == 429:
                        self.assertIsInstance(error, api.RateLimitedError)
                        self.assertEqual(error.retry_after, 120.0)
                    else:
                        self.assertIsInstance(error, api.PlayAPIError)
            sleep.assert_not_called()

    def test_direct_transient_failure_is_retried(self):
        with (
            patch.dict(
                os.environ,
                {
                    "GOOPDL_ACCOUNT_EMAIL": "email-sentinel@example.test",
                    "GOOPDL_AAS_TOKEN": "aas_et/aas-sentinel",
                },
            ),
            patch.object(api.time, "sleep") as sleep,
        ):
            for failure in (
                httpx.Response(503),
                httpx.ConnectError("down"),
                httpx.ReadTimeout("late"),
            ):
                with (
                    self.subTest(failure=type(failure).__name__),
                    patch.object(
                        httpx,
                        "post",
                        side_effect=[failure, failure, failure],
                    ) as post,
                ):
                    error = capture_failure(
                        lambda: api._acquire_single_token(
                            "arm64", None, None, None, "Pv"
                        )
                    )
                    self.assertIsInstance(error, auth.DirectAuthError)
                    self.assertEqual(post.call_count, 3)
            self.assertEqual(sleep.call_count, 6)

    def test_anonymous_transport_stops_after_two_failures(self):
        for failure in (httpx.ConnectError("down"), httpx.ReadTimeout("late")):
            with (
                self.subTest(failure=type(failure).__name__),
                patch.object(
                    httpx,
                    "post",
                    side_effect=failure,
                ) as post,
            ):
                error = capture_failure(lambda: auth.fetch_token(raise_on_error=True))
                self.assertIsInstance(error, auth.DispenserUnavailableError)
                self.assertLessEqual(post.call_count, 2)

    def test_transport_streak_resets_after_response(self):
        with patch.object(
            httpx,
            "post",
            side_effect=[
                httpx.ConnectError("down"),
                httpx.Response(400),
                httpx.ConnectError("down"),
                httpx.Response(200, json={"authToken": "ok"}),
            ],
        ) as post:
            token = auth.fetch_token(raise_on_error=True)
            assert token is not None
            self.assertEqual(token["authToken"], "ok")
            self.assertEqual(post.call_count, 4)

    def test_acquire_bounds_dispenser_transport_retry(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                auth,
                "_CONFIG_DIR",
                Path(directory),
                create=True,
            ),
            patch.object(httpx, "post", side_effect=httpx.ConnectError("down")) as post,
            patch.object(
                api.time,
                "sleep",
            ) as sleep,
        ):
            error = capture_failure(
                lambda: api._acquire_single_token("arm64", None, None, None, "Pv")
            )
            self.assertIsInstance(error, auth.DispenserUnavailableError)
            self.assertEqual(post.call_count, 2)
            sleep.assert_called_once_with(5)

    def test_profile_rotation_stops_on_dispenser_error(self):
        for response, code in (
            (httpx.Response(429, headers={"Retry-After": "120"}), 5),
            (httpx.ConnectError("down"), 1),
        ):
            with (
                self.subTest(code=code),
                patch.object(httpx, "post", side_effect=[response] * 6) as post,
                patch.object(
                    cli,
                    "_require_auth",
                    return_value={"authToken": "old"},
                ),
                patch.object(
                    cli, "get_details", side_effect=api.AppNotSupportedError("no")
                ),
            ):
                result = CliRunner().invoke(
                    cli.app, ["inspect-delivery", "com.example.pkg", "--version", "1"]
                )
                self.assertEqual(result.exit_code, code, result.output)
                self.assertEqual(post.call_count, 1 if code == 5 else 2)

    def test_version_resolution_stops_on_dispenser_error(self):
        for response, expected in (
            (httpx.Response(429, headers={"Retry-After": "120"}), api.RateLimitedError),
            (httpx.ConnectError("down"), api.NetworkError),
        ):
            with (
                self.subTest(error=expected.__name__),
                patch.object(httpx, "post", side_effect=[response] * 20) as post,
                patch.object(cli.time, "sleep"),
            ):
                error = capture_failure(
                    lambda: cli._resolve_version_string(
                        "com.example.pkg", "1.2", "arm64", None, None, None, "Pv"
                    )
                )
                self.assertIsInstance(error, expected)
                self.assertEqual(post.call_count, 1 if expected is api.RateLimitedError else 2)

    def test_latest_stops_on_dispenser_error(self):
        for failure, code in (
            (httpx.Response(429), 5),
            (httpx.ConnectError("down"), 3),
        ):
            with (
                self.subTest(code=code),
                patch.object(httpx, "post", side_effect=[failure] * 5) as post,
                tempfile.TemporaryDirectory() as directory,
                patch.object(
                    auth,
                    "_CONFIG_DIR",
                    Path(directory),
                    create=True,
                ),
            ):
                result = CliRunner().invoke(cli.app, ["latest", "com.example.pkg"])
                self.assertEqual(result.exit_code, code, result.output)
                self.assertEqual(post.call_count, 1 if code == 5 else 2)

    def test_latest_expired_tokens_stop_on_dispenser_error(self):
        for failure, code in (
            (httpx.Response(429), 5),
            (httpx.ConnectError("down"), 1),
        ):
            with (
                self.subTest(code=code),
                patch.object(httpx, "post", side_effect=[failure] * 6) as post,
                tempfile.TemporaryDirectory() as directory,
                patch.object(
                    auth,
                    "_CONFIG_DIR",
                    Path(directory),
                    create=True,
                ),
                patch.object(
                    cli, "ensure_pool", return_value=[{"authToken": "expired"}] * 3
                ),
                patch.object(
                    cli,
                    "get_details",
                    side_effect=api.AuthExpiredError("expired"),
                ),
            ):
                result = CliRunner().invoke(cli.app, ["latest", "com.example.pkg"])
                self.assertEqual(result.exit_code, code, result.output)
                self.assertEqual(post.call_count, 1 if code == 5 else 6)

    def test_direct_error_legacy_construction(self):
        error = auth.DirectAuthError("stage")
        self.assertIsNone(error.stage)
        self.assertIsNone(error.status)
        self.assertIsNone(error.retry_after)

    def test_fetch_configuration_error_erases_proxy_and_credentials(self):
        with patch.dict(
            os.environ, {"GOOPDL_ACCOUNT_EMAIL": "email-sentinel@example.test"}
        ):
            error = capture_failure(
                lambda: auth.fetch_token(
                    proxy="http://user:proxy-sentinel@localhost:8080",
                    raise_on_error=True,
                )
            )
        self.assertIsInstance(error, auth.DirectAuthConfigurationError)
        self.assert_redacted(error, ("email-sentinel", "proxy-sentinel"))

    def test_pool_contention_is_typed_and_not_retried(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                auth,
                "_CONFIG_DIR",
                Path(directory),
                create=True,
            ),
            patch.object(auth, "_POOL_LOCK_TIMEOUT", 0.025),
            auth._pool_lock("arm64", None, "Pv") as acquired,
            patch.object(
                api.time,
                "sleep",
                wraps=api.time.sleep,
            ) as sleep,
        ):
            self.assertTrue(acquired)
            error = capture_failure(
                lambda: api._acquire_single_token("arm64", None, None, None, "Pv")
            )
            self.assertEqual(type(error).__name__, "PoolBusyError")
            self.assertIsInstance(error, TimeoutError)
            self.assertIsInstance(error, errors.GoopdlError)
            self.assertIs(errors.PoolBusyError, type(error))
            self.assertFalse(any(call.args == (5,) for call in sleep.call_args_list))
            self.assertIsNone(auth.pick_pool_token(profile="Pv"))


if __name__ == "__main__":
    unittest.main()
