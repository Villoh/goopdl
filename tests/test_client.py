"""goopdl.Client against a fake Google Play; sockets are disabled."""

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from _envutil import isolated_env
from _nonet import no_sockets
from goopdl import Client, Config, Credentials, DownloadResult, api, auth
from goopdl.errors import (
    AppNotAvailableError,
    DirectAuthConfigurationError,
    DispenserRateLimitedError,
    DownloadError,
    InvalidArchError,
    InvalidPackageError,
    PlayAPIError,
    PoolBusyError,
    RateLimitedError,
)

PKG = "com.example.app"
VC = 4242
REAL_ASYNC_CLIENT = httpx.AsyncClient
FILES = {"/dl/base": b"BASE" * 40000, "/dl/split": b"SPLIT" * 3000}
ENV_ACCOUNT = {"GOOPDL_ACCOUNT_EMAIL": "env@x.test", "GOOPDL_AAS_TOKEN": "aas_et/env"}
EMAIL, AAS = "email-sentinel@x.test", "aas_et/aas-sentinel"
SECRETS = ("email-sentinel", "aas-sentinel", "bearer-sentinel", "dtok-sentinel",
           "sig-sentinel", "proxy-sentinel")


def f(number, value):
    if isinstance(value, int):
        return auth._proto_varint(number, value)
    return auth._proto_bytes(number, value)


def sha256(data):
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


class FakePlay:
    """Fake dispenser, Google auth, FDFE and CDN; records every request."""

    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []  # (endpoint, bearer or email, config dir)
        self.faults = {}    # endpoint -> list of responses served first
        self.barrier = None  # threading.Barrier met by the first details calls
        self.hang = None     # asyncio.Event the CDN waits on
        self.tokens = 0

    def record(self, endpoint, who):
        with self.lock:
            self.requests.append((endpoint, who, auth.get_config_dir()))
            faults = self.faults.get(endpoint)
            return faults.pop(0) if faults else None

    def post(self, url, *, data=None, json=None, headers=None, content=None, **_):
        url = str(url)
        if "android.clients.google.com" not in url:  # dispenser
            fault = self.record("dispenser", url)
            if fault is not None:
                return fault
            with self.lock:
                self.tokens += 1
                n = self.tokens
            host = httpx.URL(url).host
            return httpx.Response(200, json={"authToken": f"anon-{host}-{n}", "gsfId": f"{n:x}"})
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "auth":
            fault = self.record("auth", data["Email"])
            return fault or httpx.Response(200, text=f"Auth=bearer-{data['Email']}\n")
        if endpoint == "purchase":
            fault = self.record("purchase", headers["Authorization"])
            vc = int(parse_qs(content)["vc"][0])
            return fault or httpx.Response(200, content=f(1, f(4, f(55, f"dtok-{vc}"))))
        fault = self.record(endpoint, "")
        if fault is not None:
            return fault
        if endpoint == "checkin":
            return httpx.Response(200, content=f(7, 1) + f(12, "consistency"))
        return httpx.Response(200, content=f(1, f(28, f(1, "device-config"))))

    def get(self, url, *, params=None, headers=None, **_):
        endpoint = str(url).rsplit("/", 1)[-1]
        bearer = headers["Authorization"].split(" ", 1)[1]
        fault = self.record(endpoint, bearer)
        if fault is not None:
            return fault
        if endpoint == "details":
            if self.barrier is not None:
                self.barrier.wait(timeout=10)
            app = f(3, VC) + f(4, "4.2") + f(17, f(9, "config.en"))
            doc = f(1, params["doc"]) + f(5, bearer) + f(6, "Dev") + f(13, f(1, app))
            return httpx.Response(200, content=f(1, f(2, f(4, doc))))
        if endpoint == "search":
            doc = f(1, "com.found.app") + f(5, bearer) + f(6, "Dev")
            return httpx.Response(200, content=f(1, f(2, f(1, doc))))
        if endpoint == "delivery":
            base, split = FILES["/dl/base"], FILES["/dl/split"]
            split_msg = (f(1, "config.en") + f(2, len(split))
                         + f(5, "https://play.googleapis.com/dl/split") + f(9, sha256(split)))
            data = (f(1, len(base)) + f(3, f"https://play.googleapis.com/dl/base?vc={params['vc']}")
                    + f(19, sha256(base)) + f(15, split_msg) + f(29, int(params["vc"])))
            return httpx.Response(200, content=f(1, f(21, f(1, 1) + f(2, data))))
        return httpx.Response(404)  # toc

    async def cdn(self, request):
        self.record("cdn", request.url.path)
        body = FILES[request.url.path]
        if self.hang is None:
            return httpx.Response(200, content=body)
        event = self.hang

        async def stream():
            yield body[:70000]  # more than one 64 KB progress chunk
            await event.wait()  # never set: the test cancels here
            yield body[70000:]

        return httpx.Response(200, content=stream())

    def transport(self):
        return httpx.MockTransport(self.cdn)

    def bearers(self, endpoint):
        return [who for e, who, _ in self.requests if e == endpoint]


class ClientTestCase(unittest.TestCase):
    env: dict = {}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = FakePlay()

        def async_client(*args, **kwargs):
            kwargs["transport"] = self.fake.transport()
            return REAL_ASYNC_CLIENT(*args, **kwargs)

        for patcher in (
            isolated_env({"GOOPDL_CONFIG_DIR": str(self.tmp / "env"), **self.env}),
            patch.object(socket.socket, "connect", no_sockets),
            patch("httpx.post", side_effect=lambda *a, **k: self.fake.post(*a, **k)),
            patch("httpx.get", side_effect=lambda *a, **k: self.fake.get(*a, **k)),
            patch("httpx.AsyncClient", side_effect=async_client),
            patch("time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_no_scope(self):
        self.assertIsNone(auth._scope.get())


class ConfigTest(unittest.TestCase):
    def test_config_dir_must_be_absolute(self):
        for value in ("relative", Path("rel/dir"), ""):
            with self.subTest(value=value), self.assertRaises(ValueError) as raised:
                Config(config_dir=value)
            self.assertIn("absolute", str(raised.exception))
        # Absolute on every OS: "/srv/g" has no drive letter, so it is not on Windows.
        base = Path(tempfile.gettempdir()).resolve()
        with isolated_env({"HOME": str(base / "u"), "USERPROFILE": str(base / "u")}):
            self.assertEqual(base / "u" / "g", Config(config_dir="~/g").config_dir)
        self.assertEqual(base / "srv", Config(config_dir=base / "srv").config_dir)

    def test_arch_and_types_are_validated(self):
        with self.assertRaises(InvalidArchError):
            Config(arch="mips")
        with self.assertRaises(InvalidArchError):
            Client(arch="mips")
        with self.assertRaises(InvalidArchError):
            Config().replace(arch="mips")
        with self.assertRaises(TypeError):
            Config(credentials=("a@b.c", "aas_et/x"))  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Client({"arch": "x86"})  # type: ignore[arg-type]

    def test_replace_and_overrides(self):
        base = Config(country="US")
        derived = base.replace(country="DE", arch="x86")
        self.assertEqual(("US", "arm64"), (base.country, base.arch))
        self.assertEqual(("DE", "x86"), (derived.country, derived.arch))
        self.assertEqual(Config(country="US", profile="Pv"), Client(base, profile="Pv").config)
        self.assertIs(base, Client(base).config)

    def test_credentials_validation_matches_the_environment(self):
        cases = {
            ("", "aas_et/x"): "Missing required environment variable: GOOPDL_ACCOUNT_EMAIL",
            ("a@b.c", " "): "Missing required environment variable: GOOPDL_AAS_TOKEN",
            ("a@b.c", "oauth2_4/x"): "GOOPDL_AAS_TOKEN must start with aas_et/",
            ("", ""): "Missing required environment variable: GOOPDL_ACCOUNT_EMAIL",
        }
        for (email, token), message in cases.items():
            with self.subTest(email=email, token=token):
                with self.assertRaises(DirectAuthConfigurationError) as raised:
                    Credentials(email, token)
                self.assertIn(message, str(raised.exception))
                self.assertNotIn("oauth2_4/x", str(raised.exception))
        creds = Credentials(" a@b.c ", " aas_et/x\n")
        self.assertEqual(("a@b.c", "aas_et/x"), (creds.email, creds.aas_token))

    def test_from_env(self):
        with isolated_env():
            self.assertIsNone(Credentials.from_env())
        with isolated_env(ENV_ACCOUNT):
            self.assertEqual(Credentials("env@x.test", "aas_et/env"), Credentials.from_env())
        with isolated_env({"GOOPDL_AAS_TOKEN": "aas_et/x"}), \
                self.assertRaises(DirectAuthConfigurationError):
            Credentials.from_env()

    def test_repr_has_no_secrets(self):
        creds = Credentials("someone.secret@example.com", "aas_et/aas-sentinel")
        config = Config(config_dir=Path(tempfile.gettempdir()).resolve() / "x", credentials=creds,
                        proxy="http://user:proxy-sentinel@h:1")
        for text in (repr(creds), str(creds), repr(config), str(config),
                     repr(Client(config))):
            with self.subTest(text=text):
                for secret in ("aas-sentinel", "someone.secret", "proxy-sentinel"):
                    self.assertNotIn(secret, text)
        self.assertEqual("Credentials(email=s***@example.com, aas_token=<hidden>)", repr(creds))


class MethodsTest(ClientTestCase):
    def client(self, **kw):
        return Client(Config(config_dir=self.tmp / "c", profile="Pv", **kw))

    def test_details_search_list_splits_item(self):
        client = self.client()
        details = client.details(PKG)
        self.assertEqual((PKG, VC, "4.2"), (details.package, details.version_code,
                                            details.version_string))
        self.assertTrue(details.title.startswith("anon-auroraoss.com-"))
        self.assertTrue(client.search("example")[0]["title"].startswith("anon-"))
        self.assertEqual(["config.en"], client.list_splits(PKG))
        self.assertEqual("4.2", client.item(PKG)["version"])
        self.assertTrue(list((self.tmp / "c").glob("token-pool-arm64-profile-Pv.json")))
        self.assertFalse((self.tmp / "env").exists())
        self.assert_no_scope()

    def test_delivery_uses_one_token_for_every_step(self):
        client = self.client()
        delivery = client.delivery(PKG)
        self.assertEqual(VC, delivery.version_code)
        endpoints = [e for e, _, _ in self.fake.requests if e != "dispenser"]
        self.assertEqual(["details", "purchase", "delivery"], endpoints)
        bearers = {who.split(" ")[-1] for e, who, _ in self.fake.requests if e != "dispenser"}
        self.assertEqual(1, len(bearers))

        self.fake.requests.clear()
        self.assertEqual(7, client.delivery(PKG, 7).version_code)
        self.assertEqual(["purchase", "delivery"],
                         [e for e, _, _ in self.fake.requests if e != "dispenser"])

    def test_per_call_overrides(self):
        client = self.client(country="US")
        client.details(PKG, country="DE", dispenser_url="https://other.invalid/api/auth")
        self.assertTrue(list((self.tmp / "c").glob("token-pool-arm64-DE-profile-Pv.json")))
        self.assertIn("anon-other.invalid-1", self.fake.bearers("details"))
        self.assertEqual("US", client.config.country)

    def test_expired_token_is_replaced_once(self):
        self.fake.faults["details"] = [httpx.Response(401)]
        self.assertEqual(VC, self.client().details(PKG).version_code)
        first, second = self.fake.bearers("details")
        self.assertNotEqual(first, second)

    def test_download_matches_download_app(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = self.client().download(PKG, self.tmp / "client", splits=True)
        self.assertEqual("", stdout.getvalue())  # no progress bars
        with contextlib.redirect_stdout(io.StringIO()):
            path, version = api.download_app(PKG, self.tmp / "api", profile="Pv",
                                             no_splits=False)
        self.assertIsInstance(result, DownloadResult)
        self.assertEqual(version, result.version_string)
        self.assertEqual(path.name, result.path.name)
        self.assertEqual(result.path, result.files[0])
        names = sorted(p.name for p in (self.tmp / "client").iterdir())
        self.assertEqual(names, sorted(p.name for p in (self.tmp / "api").iterdir()))
        self.assertEqual(names, sorted(p.name for p in result.files))
        for name in names:
            self.assertEqual((self.tmp / "api" / name).read_bytes(),
                             (self.tmp / "client" / name).read_bytes())
        self.assertEqual([f"{PKG}-{VC}.apk"],
                         [p.name for p in self.client().download(PKG, self.tmp / "b").files])

    def test_download_refuses_a_running_loop(self):
        async def main():
            self.client().download(PKG, self.tmp / "x")

        with self.assertRaises(RuntimeError) as raised:
            asyncio.run(main())
        self.assertIn("adownload", str(raised.exception))
        self.assertEqual([], self.fake.requests)  # no token was spent


class AnonymousByDefaultTest(ClientTestCase):
    env = ENV_ACCOUNT

    def test_credentials_none_ignores_the_environment_account(self):
        details = Client(config_dir=self.tmp / "c", profile="Pv").details(PKG)
        endpoints = [e for e, _, _ in self.fake.requests]
        self.assertNotIn("checkin", endpoints)
        self.assertNotIn("auth", endpoints)
        self.assertEqual("dispenser", endpoints[0])
        self.assertTrue(details.title.startswith("anon-"))
        # The module functions still follow the environment, like the CLI.
        self.fake.requests.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            api.download_app(PKG, self.tmp / "api", profile="Pv")
        self.assertIn("checkin", [e for e, _, _ in self.fake.requests])
        self.assertEqual(["env@x.test"], self.fake.bearers("auth"))

    def test_from_env_opts_in(self):
        client = Client(credentials=Credentials.from_env(), profile="Pv")
        self.assertEqual("bearer-env@x.test", client.details(PKG).title)


class MultiTenantTest(ClientTestCase):
    def tenants(self):
        a = Client(config_dir=self.tmp / "a", credentials=Credentials("a@x.test", "aas_et/a"),
                   profile="Pv")
        b = Client(config_dir=self.tmp / "b", credentials=Credentials("b@x.test", "aas_et/b"),
                   profile="Pv")
        anon_c = Client(config_dir=self.tmp / "c", profile="Pv",
                        dispenser_url="https://c.invalid/api/auth")
        anon_d = Client(config_dir=self.tmp / "d", profile="Pv",
                        dispenser_url="https://d.invalid/api/auth")
        return a, b, anon_c, anon_d

    def check(self, titles):
        a, b, c, d = titles
        self.assertEqual(("bearer-a@x.test", "bearer-b@x.test"), (a, b))
        self.assertTrue(c.startswith("anon-c.invalid-"), c)
        self.assertTrue(d.startswith("anon-d.invalid-"), d)
        for endpoint, who, directory in self.fake.requests:
            if endpoint == "auth":  # account sign-in: the client's own email
                self.assertEqual(self.tmp / who[0], directory)
            elif endpoint == "dispenser":  # who is the dispenser URL
                self.assertEqual(self.tmp / httpx.URL(who).host[0], directory)
            elif endpoint == "details":
                tenant = who.split("-")[1][0]
                self.assertEqual(self.tmp / tenant, directory)
        for tenant in ("a", "b"):  # account mode: only its device record
            names = sorted(p.name for p in (self.tmp / tenant).iterdir())
            self.assertEqual(2, len(names), names)
            self.assertTrue(all(n.startswith("direct-device-") for n in names), names)
            self.assertNotIn("bearer-", (self.tmp / tenant / names[0]).read_text())
        self.assertFalse((self.tmp / "env").exists())
        for tenant in ("c", "d"):
            pool = (self.tmp / tenant / "token-pool-arm64-profile-Pv.json").read_text()
            self.assertIn(f"anon-{tenant}.invalid", pool)
            other = "d" if tenant == "c" else "c"
            self.assertNotIn(f"anon-{other}.invalid", pool)
        self.assertEqual(2, self.fake.bearers("auth").__len__())

    def test_threads(self):
        clients = self.tenants()
        self.fake.barrier = threading.Barrier(len(clients))  # all in details at once
        titles = [None] * len(clients)

        def run(i):
            try:
                titles[i] = clients[i].details(PKG).title
            except BaseException as exc:  # noqa: BLE001
                titles[i] = repr(exc)

        threads = [threading.Thread(target=run, args=(i,)) for i in range(len(clients))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.check(titles)
        self.assert_no_scope()

    def test_gather_to_thread(self):
        clients = self.tenants()
        self.fake.barrier = threading.Barrier(len(clients))

        async def main():
            results = await asyncio.gather(
                *(asyncio.to_thread(c.details, PKG) for c in clients)
            )
            self.assert_no_scope()
            return [r.title for r in results]

        self.check(asyncio.run(main()))

    def test_concurrent_adownloads(self):
        clients = self.tenants()

        async def main():
            return await asyncio.gather(
                *(c.adownload(PKG, self.tmp / "out" / str(i)) for i, c in enumerate(clients))
            )

        results = asyncio.run(main())
        for result in results:
            self.assertEqual(FILES["/dl/base"], result.path.read_bytes())
        delivery_bearers = sorted(b.rsplit("-", 1)[0] if b.startswith("anon-") else b
                                  for b in self.fake.bearers("delivery"))
        self.assertEqual(["anon-c.invalid", "anon-d.invalid",
                          "bearer-a@x.test", "bearer-b@x.test"], delivery_bearers)
        for i in range(len(clients)):
            self.assertEqual(f"{PKG}-{VC}.apk", results[i].path.name)
            self.assertEqual(self.tmp / "out" / str(i), results[i].path.parent)
        self.assert_no_scope()


class AsyncDownloadTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = FakePlay()
        for patcher in (
            isolated_env({"GOOPDL_CONFIG_DIR": str(self.tmp / "env")}),
            patch.object(socket.socket, "connect", no_sockets),
            patch("httpx.post", side_effect=lambda *a, **k: self.fake.post(*a, **k)),
            patch("httpx.get", side_effect=lambda *a, **k: self.fake.get(*a, **k)),
            patch("time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = Client(config_dir=self.tmp / "c", profile="Pv")

    async def test_injected_client_progress_and_result(self):
        events = []
        async with REAL_ASYNC_CLIENT(transport=self.fake.transport()) as http:
            result = await self.client.adownload(
                PKG, self.tmp / "out", splits=True, http_client=http,
                on_progress=lambda *e: events.append(e), max_concurrent=1,
            )
            self.assertFalse(http.is_closed)
        self.assertEqual([f"{PKG}-{VC}.apk", f"{PKG}-{VC}-config.en.apk"],
                         [p.name for p in result.files])
        self.assertEqual("4.2", result.version_string)
        self.assertEqual(FILES["/dl/split"], result.files[1].read_bytes())
        labels = {label for label, _, _ in events}
        self.assertEqual({p.name for p in result.files}, labels)
        self.assertIn((f"{PKG}-{VC}.apk", len(FILES["/dl/base"]), len(FILES["/dl/base"])),
                      events)
        self.assertIsNone(auth._scope.get())

    async def test_cancellation_leaves_no_part_files(self):
        self.fake.hang = asyncio.Event()
        started = asyncio.Event()
        out = self.tmp / "out"
        async with REAL_ASYNC_CLIENT(transport=self.fake.transport()) as http:
            task = asyncio.create_task(self.client.adownload(
                PKG, out, http_client=http,
                on_progress=lambda _l, done, _t: done and started.set(),
            ))
            await asyncio.wait_for(started.wait(), 10)
            self.assertTrue(list(out.glob("*.part")))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(http.is_closed)
        self.assertEqual([], list(out.iterdir()))
        self.assertIsNone(auth._scope.get())

    async def test_errors_propagate_from_the_worker_thread(self):
        self.fake.faults["dispenser"] = [httpx.Response(429, headers={"Retry-After": "30"})]
        with self.assertRaises(RateLimitedError) as raised:
            await self.client.adownload(PKG, self.tmp / "out")
        self.assertEqual(30.0, raised.exception.retry_after)
        self.assertIsInstance(raised.exception.__cause__, DispenserRateLimitedError)
        with self.assertRaises(InvalidPackageError):
            await self.client.adownload("nodots", self.tmp / "out")


class ErrorsTest(ClientTestCase):
    def client(self, **kw):
        return Client(config_dir=self.tmp / "c", profile="Pv", **kw)

    def test_dispenser_429_is_rate_limited_everywhere(self):
        calls = {
            "details": lambda c: c.details(PKG),
            "search": lambda c: c.search("x"),
            "list_splits": lambda c: c.list_splits(PKG),
            "delivery": lambda c: c.delivery(PKG),
            "item": lambda c: c.item(PKG),
            "download": lambda c: c.download(PKG, self.tmp / "out"),
        }
        for name, call in calls.items():
            with self.subTest(name):
                client = Client(config_dir=self.tmp / name, profile="Pv")  # cold pool
                if name == "item":  # fetch_app_item: only a replacement's 429 is typed
                    client.details(PKG)  # warm pool, then Play 401s the token
                    # ...and no survivor remains to serve the call instead.
                    pool = next((self.tmp / name).glob("token-pool-*.json"))
                    pool.write_text(json.dumps(json.loads(pool.read_text())[:1]))
                    self.fake.faults["details"] = [httpx.Response(401)]
                self.fake.faults["dispenser"] = [  # item: refill, then replacement
                    httpx.Response(429, headers={"Retry-After": "60"})
                ] * (2 if name == "item" else 1)
                with self.assertRaises(RateLimitedError) as raised:
                    call(client)
                self.assertEqual(60.0, raised.exception.retry_after)
                self.assertIsInstance(raised.exception.__cause__, DispenserRateLimitedError)
                self.assert_no_scope()

    def test_dispenser_outage_and_unknown_profile(self):
        self.fake.faults["dispenser"] = [httpx.Response(503)] * 4
        with self.assertRaises(PlayAPIError) as raised:
            self.client().details(PKG)
        self.assertNotIsInstance(raised.exception, RateLimitedError)
        self.assertEqual(503, raised.exception.status_code)
        with self.assertRaises(PlayAPIError):
            Client(config_dir=self.tmp / "c", profile="no-such-profile").details(PKG)

    def test_play_errors_keep_their_types(self):
        self.fake.faults["details"] = [httpx.Response(404)]
        with self.assertRaises(AppNotAvailableError):
            self.client().details(PKG)
        with self.assertRaises(InvalidPackageError):
            self.client().details("bad")
        self.assertEqual([], self.fake.bearers("details")[1:])

    def test_pool_busy_propagates_from_download(self):
        with (
            patch.object(auth, "_POOL_LOCK_TIMEOUT", 0.05),
            auth._use_scope(auth._Scope(config_dir=self.tmp / "c")),
            auth._pool_lock("arm64", None, "Pv") as held,
        ):
            self.assertTrue(held)
            with self.assertRaises(PoolBusyError) as raised:
                Client(config_dir=self.tmp / "c", profile="Pv").download(PKG, self.tmp / "o")
        self.assertNotIsInstance(raised.exception, PlayAPIError)
        self.assertEqual([], self.fake.requests)
        self.assert_no_scope()

    def test_cdn_failure_is_a_download_error(self):
        self.fake.cdn = lambda request: httpx.Response(403)  # type: ignore[method-assign]
        with self.assertRaises(DownloadError) as raised:
            self.client().download(PKG, self.tmp / "out")
        self.assertEqual(403, raised.exception.status_code)
        self.assertEqual([], [p for p in (self.tmp / "out").iterdir()])

    def test_scope_is_restored_after_failures(self):
        self.fake.faults["details"] = [httpx.Response(500)]
        with self.assertRaises(PlayAPIError):
            self.client().details(PKG)
        self.assert_no_scope()
        self.assertEqual(self.tmp / "env", auth.get_config_dir())


class SecretFramesTest(ClientTestCase):
    """No email/AAS token (or bearer, dtok, signed URL) in surviving frames.

    The Client, Config and Credentials objects the caller made are accepted:
    they are the caller's own, like the auth dict in api.get_details.
    """

    def setUp(self):
        super().setUp()
        self.creds = Credentials(EMAIL, AAS)
        self.config = Config(config_dir=self.tmp / "c", credentials=self.creds, profile="Pv",
                             proxy="http://u:proxy-sentinel@localhost:1")
        self.client = Client(self.config)

    def capture(self, call):
        try:
            call()
        except Exception as exc:  # noqa: BLE001
            return exc
        self.fail("expected an exception")

    def assert_clean(self, error):
        allowed = (self.client, self.config, self.creds)
        seen, pending = set(), [error]
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
                if frame.f_code.co_name not in ("capture", "<lambda>"):
                    for name, value in frame.f_locals.items():
                        where = (frame.f_code.co_name, name)
                        if any(value is obj for obj in allowed):
                            continue
                        if isinstance(value, (Client, Config)):
                            config = value.config if isinstance(value, Client) else value
                            self.assertIs(self.creds, config.credentials, where)
                            continue
                        self.assertNotIsInstance(value, (Credentials, tuple, dict), where)
                        text = f"{value!s} {value!r}"
                        for secret in SECRETS:
                            self.assertNotIn(secret, text, where)
                tb = tb.tb_next
            pending.extend((exc.__cause__, exc.__context__))

    def test_sign_in_failures(self):
        for status in (403, 429, 500):
            with self.subTest(status=status):
                self.fake.faults["auth"] = [
                    httpx.Response(status, text="Error=BadAuthentication\n")
                ] * 50
                for call in (lambda: self.client.details(PKG),
                             lambda: self.client.details(PKG, country="DE"),
                             lambda: self.client.download(PKG, self.tmp / "o"),
                             lambda: self.client.item(PKG)):
                    error = self.capture(call)
                    self.assertIsInstance(error, PlayAPIError)
                    self.assert_clean(error)

    def test_play_and_cdn_failures(self):
        self.fake.faults["delivery"] = [httpx.Response(500)]
        error = self.capture(lambda: self.client.delivery(PKG))
        self.assertIsInstance(error, PlayAPIError)
        self.assert_clean(error)

        self.fake.cdn = lambda request: httpx.Response(503)  # type: ignore[method-assign]
        error = self.capture(lambda: self.client.download(PKG, self.tmp / "o"))
        self.assertIsInstance(error, DownloadError)
        self.assert_clean(error)

        error = self.capture(lambda: asyncio.run(self.client.adownload(PKG, self.tmp / "o")))
        self.assertIsInstance(error, DownloadError)
        self.assert_clean(error)

    def test_adownload_sign_in_failure(self):
        self.fake.faults["auth"] = [httpx.Response(403, text="Error=BadAuthentication\n")]
        error = self.capture(lambda: asyncio.run(self.client.adownload(PKG, self.tmp / "o")))
        self.assertIsInstance(error, PlayAPIError)
        self.assert_clean(error)


if __name__ == "__main__":
    unittest.main()
