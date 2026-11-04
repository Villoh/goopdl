"""Final polish regressions: CLI error paths, logs, toc, secrets, redirects."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from goopdl import auth
from goopdl.cli import AUTH_UNAVAILABLE_EXIT_CODE, app

PIXEL = "Google Pixel 9a"
BUSY = "Token pool is busy (another goopdl process holds the lock); retry later."


def _flat(output: str) -> str:
    return " ".join(output.split())


@patch.dict(os.environ, {}, clear=True)
class PoolBusyCliTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.out = tmp / "out"
        for patcher in (
            patch.object(auth, "_CONFIG_DIR", tmp / "config", create=True),
            patch.object(auth, "_POOL_LOCK_TIMEOUT", 0.05),
            patch("httpx.post", side_effect=AssertionError("network")),
            patch("httpx.get", side_effect=AssertionError("network")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _invoke_busy(self, *args: str):
        # Separate open handles conflict even inside one process.
        with auth._pool_lock("arm64", None, PIXEL) as acquired:
            self.assertTrue(acquired)
            return CliRunner().invoke(app, [*args, "com.example.pkg", "--profile", PIXEL])

    def test_busy_pool_exits_cleanly_with_gold_exit_codes(self) -> None:
        for command, code in (
            (("fast-download", "-o", "OUT"), 1),
            (("download", "-o", "OUT"), AUTH_UNAVAILABLE_EXIT_CODE),
            (("latest",), 1),
        ):
            with self.subTest(command=command[0]):
                args = [str(self.out) if a == "OUT" else a for a in command]
                result = self._invoke_busy(*args)
                self.assertEqual(result.exit_code, code, result.output)
                self.assertIsInstance(result.exception, SystemExit)
                self.assertIn(BUSY, _flat(result.output))
                self.assertNotIn("Traceback", result.output)


@patch.dict(os.environ, {}, clear=True)
@patch("goopdl.auth.time.sleep")
class DispenserRejectionMessageTest(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(
            auth, "_CONFIG_DIR", Path(tempfile.mkdtemp()), create=True
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth(self, **post):
        with patch("goopdl.auth.httpx.post", **post):
            return CliRunner().invoke(app, ["auth"])

    def test_answering_dispenser_is_not_reported_as_down(self, _sleep) -> None:
        for response, last in (
            (httpx.Response(200, json={}), "no authToken"),
            (httpx.Response(400), "HTTP 400"),
        ):
            with self.subTest(last=last):
                result = self._auth(return_value=response)
                self.assertEqual(result.exit_code, AUTH_UNAVAILABLE_EXIT_CODE)
                text = _flat(result.output)
                self.assertIn(
                    "Authentication failed — all profiles rejected "
                    f"(last dispenser response: {last}).",
                    text,
                )
                self.assertNotIn("may be down", text)

    def test_outages_keep_the_unavailable_message(self, _sleep) -> None:
        for post, expected in (
            ({"return_value": httpx.Response(503)}, "(HTTP 503). It may be down"),
            ({"side_effect": httpx.ConnectError("x")}, "unavailable. It may be down"),
        ):
            with self.subTest(expected=expected):
                result = self._auth(**post)
                self.assertEqual(result.exit_code, AUTH_UNAVAILABLE_EXIT_CODE)
                self.assertIn(expected, _flat(result.output))
                self.assertNotIn("all profiles rejected", result.output)


@patch.dict(os.environ, {}, clear=True)
@patch("goopdl.auth.time.sleep")
class DispenserLogDeviceTest(unittest.TestCase):
    def _messages(self, **post) -> list[str]:
        with (
            self.assertLogs("goopdl.auth", level="WARNING") as logs,
            patch("goopdl.auth.httpx.post", **post),
        ):
            auth.fetch_token(profile=PIXEL, proxy="http://user:pass@proxy:1")
        return [record.getMessage() for record in logs.records]

    def test_server_and_transport_failures_name_the_device(self, _sleep) -> None:
        for post, expected in (
            ({"return_value": httpx.Response(503)}, f"Dispenser returned HTTP 503 for {PIXEL}"),
            (
                {"side_effect": httpx.ConnectError("user:pass")},
                f"Dispenser transport failed for {PIXEL}: ConnectError",
            ),
        ):
            with self.subTest(expected=expected):
                messages = self._messages(**post)
                self.assertIn(expected, messages)
                self.assertNotIn("pass", " ".join(messages))


def _direct_responses():
    checkin = httpx.Response(
        200, content=auth._proto_varint(7, 1234) + auth._proto_bytes(12, "consistency")
    )
    upload = httpx.Response(
        200,
        content=auth._proto_bytes(
            1, auth._proto_bytes(28, auth._proto_bytes(1, "config"))
        ),
    )
    return checkin, upload, httpx.Response(200, text="Auth=bearer-secret\n")


@patch.dict(os.environ, {}, clear=True)
class DirectTocRateLimitTest(unittest.TestCase):
    def _direct_auth(self):
        return auth._direct_auth(
            "email@example.test", "aas_et/aas-secret", "arm64", None, None, "Pv"
        )

    def test_toc_429_keeps_the_bundle_without_dfe_cookie(self) -> None:
        limited = httpx.Response(429, headers={"Retry-After": "60"})
        with (
            patch("goopdl.auth.httpx.post", side_effect=_direct_responses()),
            patch("goopdl.auth.httpx.get", return_value=limited),
            self.assertLogs("goopdl.auth", level="WARNING") as logs,
        ):
            bundle = self._direct_auth()
        assert bundle is not None
        self.assertEqual(bundle["authToken"], "bearer-secret")
        self.assertEqual(bundle["dfeCookie"], "")
        text = " ".join(logs.output)
        self.assertIn("toc", text)
        for secret in ("bearer-secret", "aas-secret", "email@"):
            self.assertNotIn(secret, text)

    def test_checkin_429_still_aborts_with_retry_after(self) -> None:
        limited = httpx.Response(429, headers={"Retry-After": "60"})
        with (
            patch("goopdl.auth.httpx.post", return_value=limited),
            patch("goopdl.auth.httpx.get", side_effect=AssertionError("toc")),
            self.assertRaises(auth.DirectAuthError) as raised,
        ):
            self._direct_auth()
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(raised.exception.stage, "checkin")
        self.assertEqual(raised.exception.retry_after, 60.0)


SECRETS = (
    "pool-sentinel", "dtok-sentinel", "sig-sentinel", "cookie-sentinel",
    "email-sentinel", "aas-sentinel", "proxy-sentinel",
)
PROXY = "http://user:proxy-sentinel@localhost:8080"
POOL_TOKEN = {"authToken": "pool-sentinel", "gsfId": "1", "_device_profile": PIXEL}


@patch.dict(os.environ, {}, clear=True)
@patch("time.sleep")
class PublicEntrySecretFramesTest(unittest.TestCase):
    def capture(self, call) -> BaseException:
        try:
            call()
        except Exception as exc:  # noqa: BLE001 -- inspect the failure graph
            return exc
        raise AssertionError("expected an exception")

    def assert_clean(self, error: BaseException, *, caller_auth: bool = False) -> None:
        seen: set[int] = set()
        pending: list[BaseException | None] = [error]
        while pending:
            exc = pending.pop()
            if exc is None or id(exc) in seen:
                continue
            seen.add(id(exc))
            for secret in SECRETS:
                self.assertNotIn(secret, f"{exc} {exc!r}")
            tb = exc.__traceback__
            while tb is not None:
                frame = tb.tb_frame
                if frame.f_code.co_name != "capture":
                    for name, value in frame.f_locals.items():
                        if caller_auth and name == "auth":
                            continue  # the caller's own dict is accepted
                        text = f"{value!s} {value!r}"
                        if isinstance(value, httpx.Response):
                            # repr hides it, but the request URL keeps the dtok.
                            text += f" {value.request.url} {value.request.headers}"
                        for secret in SECRETS:
                            self.assertNotIn(secret, text, (frame.f_code.co_name, name))
                tb = tb.tb_next
            pending.extend((exc.__cause__, exc.__context__))

    def test_download_app_failures(self, _sleep) -> None:
        from goopdl import api
        from goopdl.download import DownloadError

        delivery = api.DeliveryResult(
            version_code=1,
            download_url="https://play.googleapis.com/x?sig-sentinel",
            cookies=[{"name": "c", "value": "cookie-sentinel"}],
            sha256="0" * 64,
        )
        details = api.AppDetails(package="com.example.pkg", version_code=1)
        cases = {
            "cdn": {
                "fetch": patch.object(
                    api, "_fetch_latest_delivery", return_value=(details, delivery)
                ),
                "batch": patch.object(
                    api, "download_batch", side_effect=DownloadError("cdn failed")
                ),
            },
            "network": {
                "fetch": patch("httpx.get", side_effect=httpx.ConnectError("x")),
                "batch": patch.object(api, "download_batch"),
            },
        }
        out = Path(tempfile.mkdtemp())
        for name, mocks in cases.items():
            with (
                self.subTest(name),
                patch.object(api, "pick_pool_token", return_value=dict(POOL_TOKEN)),
                mocks["fetch"],
                mocks["batch"],
            ):
                error = self.capture(
                    lambda: api.download_app(
                        "com.example.pkg", out, proxy=PROXY, profile=PIXEL
                    )
                )
                self.assertIsInstance(error, api.PlayAPIError if name == "network" else DownloadError)
                self.assert_clean(error)

    def test_download_app_dispenser_429_keeps_a_clean_cause(self, _sleep) -> None:
        from goopdl import api

        def throttled(**kwargs):
            raise auth.DispenserRateLimitedError(60)

        with patch.object(api, "pick_pool_token", side_effect=throttled):
            error = self.capture(
                lambda: api.download_app("com.example.pkg", Path("x"), proxy=PROXY, profile=PIXEL)
            )
        assert isinstance(error, api.RateLimitedError)
        self.assertEqual(error.retry_after, 60)
        self.assertIsInstance(error.__cause__, auth.DispenserRateLimitedError)
        self.assert_clean(error)

    def test_download_app_lets_keyboard_interrupt_through(self, _sleep) -> None:
        from goopdl import api

        interrupt = KeyboardInterrupt()
        with patch.object(api, "pick_pool_token", side_effect=interrupt):
            try:
                api.download_app("com.example.pkg", Path("x"), profile=PIXEL)
            except KeyboardInterrupt as exc:
                raised = exc
            else:
                self.fail("KeyboardInterrupt was swallowed")
        self.assertIs(raised, interrupt)
        # Untouched: the traceback still reaches the raising mock.
        names = []
        tb = raised.__traceback__
        while tb is not None:
            names.append(tb.tb_frame.f_code.co_name)
            tb = tb.tb_next
        self.assertIn("_acquire_single_token", names)

    def test_fetch_app_item_failures(self, _sleep) -> None:
        from goopdl import api

        for name, get in (
            ("network", {"side_effect": httpx.ConnectError("x")}),
            ("server", {"return_value": httpx.Response(500)}),
        ):
            with (
                self.subTest(name),
                patch.object(api, "pick_pool_token", return_value=dict(POOL_TOKEN)),
                patch("httpx.get", **get),
            ):
                error = self.capture(
                    lambda: api.fetch_app_item("com.example.pkg", proxy=PROXY, profile=PIXEL)
                )
                self.assertIsInstance(error, api.PlayAPIError)
                self.assert_clean(error)

    def test_get_delivery_drops_the_dtok_response(self, _sleep) -> None:
        from goopdl import api

        def respond(status):
            def get(url, params=None, **kwargs):
                request = httpx.Request("GET", url, params=params)
                return httpx.Response(
                    status, headers={"Retry-After": "9999"}, request=request
                )
            return get

        for status in (401, 404, 429, 500, 200):
            with self.subTest(status=status), patch("httpx.get", side_effect=respond(status)):
                error = self.capture(
                    lambda: api.get_delivery(
                        "com.example.pkg", 1, dict(POOL_TOKEN), proxy=PROXY,
                        delivery_token="dtok-sentinel",
                    )
                )
                self.assertIsInstance(error, api.PlayAPIError)
                self.assert_clean(error, caller_auth=True)

    def test_malformed_direct_credentials(self, _sleep) -> None:
        for env in (
            {"GOOPDL_ACCOUNT_EMAIL": "email-sentinel@x", "GOOPDL_AAS_TOKEN": "g.aas-sentinel"},
            {"GOOPDL_AAS_TOKEN": "aas_et/aas-sentinel"},
            {"GOOPDL_ACCOUNT_EMAIL": "email-sentinel@x"},
        ):
            for entry in (auth.ensure_pool, auth.pick_pool_token):
                with self.subTest(env=sorted(env), entry=entry.__name__), patch.dict(os.environ, env):
                    error = self.capture(entry)
                    self.assertIsInstance(error, auth.DirectAuthConfigurationError)
                    self.assert_clean(error)


MALFORMED_LOCATIONS = (
    b"https:evil.com/x",
    b"data:text/plain,hi",
    "https://а́--.рф/x".encode(),  # Cyrillic IDN that fails IDNA
    b"https://xn--a.com/x",
    b"https://[::1/x",
)


def _redirecting_client(location: bytes):
    real_client = httpx.AsyncClient

    def handler(request):
        if request.url.path == "/apk":
            return httpx.Response(302, headers=[(b"Location", location)])
        return httpx.Response(200, content=b"x")

    def factory(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    return patch("goopdl.download.httpx.AsyncClient", side_effect=factory)


@patch.dict(os.environ, {}, clear=True)
class MalformedRedirectTest(unittest.TestCase):
    def test_library_raises_a_detached_non_retryable_download_error(self) -> None:
        import asyncio

        from goopdl.download import DownloadError, DownloadSpec, download_files

        out = Path(tempfile.mkdtemp())
        for location in MALFORMED_LOCATIONS:
            with self.subTest(location=location), _redirecting_client(location):
                try:
                    asyncio.run(download_files(
                        [DownloadSpec("https://play.googleapis.com/apk", out / "b.apk")]
                    ))
                except Exception as exc:  # noqa: BLE001
                    error = exc
                else:
                    self.fail("redirect was followed")
                self.assertIsInstance(error, DownloadError)
                assert isinstance(error, DownloadError)
                self.assertFalse(error.retryable)
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
                self.assertNotIn(location.decode(errors="replace")[8:], str(error))
                self.assertEqual(list(out.iterdir()), [])

    def test_cli_exits_with_a_clean_message(self) -> None:
        from goopdl import api

        delivery = api.DeliveryResult(
            version_code=1, download_url="https://play.googleapis.com/apk",
            download_size=1, sha256="0" * 43,
        )
        details = api.AppDetails(package="com.example.pkg", version_code=1)
        out = Path(tempfile.mkdtemp())
        for location in MALFORMED_LOCATIONS:
            with (
                self.subTest(location=location),
                _redirecting_client(location),
                patch.object(api, "pick_pool_token", return_value=dict(POOL_TOKEN)),
                patch.object(api, "_fetch_latest_delivery", return_value=(details, delivery)),
            ):
                result = CliRunner().invoke(
                    app, ["fast-download", "com.example.pkg", "--profile", PIXEL, "-o", str(out)]
                )
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertIsInstance(result.exception, SystemExit)
                self.assertIn("malformed CDN URL or redirect for", _flat(result.output))


@patch.dict(os.environ, {}, clear=True)
class FetchAppItemRateLimitTest(unittest.TestCase):
    def test_replacement_429_becomes_rate_limited_like_download_app(self) -> None:
        from goopdl import api

        with (
            patch.object(api, "pick_pool_token", return_value=dict(POOL_TOKEN)),
            patch.object(
                api, "replace_pool_token", side_effect=auth.DispenserRateLimitedError(30)
            ),
            patch("httpx.get", return_value=httpx.Response(401)),
            self.assertRaises(api.RateLimitedError) as raised,
        ):
            api.fetch_app_item("com.example.pkg", profile=PIXEL)
        self.assertEqual(raised.exception.retry_after, 30)
        self.assertIsInstance(raised.exception.__cause__, auth.DispenserRateLimitedError)


class PrettyExceptionLocalsTest(unittest.TestCase):
    def test_cli_never_relies_on_typers_show_locals_default(self) -> None:
        import runpy

        import typer

        from goopdl import cli

        class OldDefaultTyper(typer.Typer):
            """typer 0.16-0.19 showed locals (tokens, proxies) by default."""

            def __init__(self, *args, pretty_exceptions_show_locals=True, **kwargs):
                super().__init__(
                    *args, pretty_exceptions_show_locals=pretty_exceptions_show_locals,
                    **kwargs,
                )

        self.assertFalse(cli.app.pretty_exceptions_show_locals)
        with patch("typer.Typer", OldDefaultTyper):
            namespace = runpy.run_path(cli.__file__, run_name="goopdl_cli_probe")
        self.assertFalse(namespace["app"].pretty_exceptions_show_locals)


if __name__ == "__main__":
    unittest.main()
