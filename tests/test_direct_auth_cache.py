"""Account-mode (direct auth) session cache against a fake Google; no sockets.

Like Google, the fake hands out a new androidId on every checkin and a new
bearer on every /auth, so request counts show reused and new devices.
"""

import json
import logging
import multiprocessing
import os
import socket
import stat
import tempfile
import threading
import time
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx

from _envutil import isolated_env
from _nonet import no_sockets
from goopdl import Client, Credentials, auth
from goopdl.errors import (
    AuthExpiredError,
    DirectAuthError,
    PlayAPIError,
    PoolBusyError,
    RateLimitedError,
)
from test_client import FakePlay, f

PKG = "com.example.app"
EMAIL, AAS = "email-sentinel@x.test", "aas_et/aas-sentinel"
SECRETS = ("email-sentinel", "aas-sentinel", "bearer-")
HANDSHAKE = ("checkin", "uploadDeviceConfig", "auth", "toc")
REAL_ASYNC_CLIENT = httpx.AsyncClient


class DirectFake(FakePlay):
    """FakePlay with a new device per checkin and a new bearer per /auth."""

    def __init__(self):
        super().__init__()
        self.next_id = 0x1000
        self.auth_extra = ""   # extra /auth response lines, e.g. ExpiresInDurationSec
        self.android_ids = []  # androidId sent to /auth
        self.reject = set()    # bearers Play answers 401 for
        self.gate = None       # threading.Event a checkin waits on

    def post(self, url, *, data=None, content=None, **kw):
        endpoint = str(url).rsplit("/", 1)[-1]
        if "android.clients.google.com" not in str(url):  # dispenser
            endpoint = "dispenser"
        if endpoint == "checkin":
            if self.gate is not None:
                self.gate.wait(10)
            fault = self.record("checkin", "")
            if fault is not None:
                return fault
            with self.lock:
                self.next_id += 1
                aid = self.next_id
            return httpx.Response(200, content=f(7, aid) + f(12, f"cons-{aid:x}"))
        if endpoint == "auth":
            fault = self.record("auth", data["Email"])
            with self.lock:
                self.android_ids.append(data["androidId"])
                self.tokens += 1
                n = self.tokens
            return fault or httpx.Response(
                200, text=f"Auth=bearer-{data['Email']}-{n}\n{self.auth_extra}"
            )
        return super().post(url, data=data, content=content, **kw)

    def get(self, url, *, params=None, headers=None, **kw):
        bearer = headers["Authorization"].split(" ", 1)[1]
        if str(url).endswith("/details") and bearer in self.reject:
            self.record("details", bearer)
            return httpx.Response(401)
        return super().get(url, params=params, headers=headers, **kw)

    def n(self, endpoint):
        return sum(1 for e, _, _ in self.requests if e == endpoint)

    def handshake(self):
        return {e: self.n(e) for e in HANDSHAKE}


class DirectCacheCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = DirectFake()
        for patcher in (
            isolated_env({"GOOPDL_CONFIG_DIR": str(self.tmp / "env")}),
            patch.object(socket.socket, "connect", no_sockets),
            patch("httpx.post", side_effect=lambda *a, **k: self.fake.post(*a, **k)),
            patch("httpx.get", side_effect=lambda *a, **k: self.fake.get(*a, **k)),
            patch("httpx.AsyncClient", side_effect=self.async_client),
            patch("time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        auth._reset_direct_cache()
        self.addCleanup(auth._reset_direct_cache)

    def async_client(self, *args, **kwargs):
        kwargs["transport"] = self.fake.transport()
        return REAL_ASYNC_CLIENT(*args, **kwargs)

    def client(self, name="c", email=EMAIL, token=AAS, **kw):
        kw.setdefault("profile", "Pv")
        return Client(config_dir=self.tmp / name, credentials=Credentials(email, token), **kw)

    def scope(self, name="c", email=EMAIL, token=AAS):
        return auth._use_scope(auth._Scope(
            config_dir=self.tmp / name, credentials=(email, token),
            explicit_credentials=True,
        ))

    def at(self, offset):
        """Patch the clock goopdl.auth sees (only auth's, not the whole process)."""
        now = time.time() + offset
        clock = types.SimpleNamespace(time=lambda: now, monotonic=time.monotonic,
                                      sleep=time.sleep)
        return patch.object(auth, "time", clock)

    def run_threads(self, n, call):
        barrier = threading.Barrier(n)
        errors = []

        def worker():
            try:
                barrier.wait(10)
                call()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual([], errors)


class MemoryCacheTest(DirectCacheCase):
    def test_cold_threads_share_one_handshake(self):
        for n in (8, 32):
            with self.subTest(threads=n):
                self.fake.requests.clear()
                client = self.client(f"cold{n}")
                self.run_threads(n, lambda: client.details(PKG))
                self.assertEqual(dict.fromkeys(HANDSHAKE, 1), self.fake.handshake())
                self.assertEqual(n, self.fake.n("details"))

    def test_warm_cache_makes_no_handshake(self):
        client = self.client()
        client.details(PKG)
        for _ in range(50):
            client.details(PKG)
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda _: client.search("x"), range(50)))
        client.item(PKG)
        client.delivery(PKG)
        self.assertEqual(dict.fromkeys(HANDSHAKE, 1), self.fake.handshake())
        self.assertEqual(53, self.fake.n("details"))

    def test_module_functions_share_the_session(self):
        with isolated_env({"GOOPDL_CONFIG_DIR": str(self.tmp / "m"),
                           "GOOPDL_ACCOUNT_EMAIL": EMAIL, "GOOPDL_AAS_TOKEN": AAS}):
            first = auth.pick_pool_token(profile="Pv")
            self.assertEqual(first, auth.pick_pool_token(profile="Pv"))
            self.assertEqual(first, auth.ensure_auth(profile="Pv"))
            from goopdl import api
            api.fetch_app_item(PKG, profile="Pv")
        self.assertEqual(1, self.fake.n("auth"))

    def test_401_invalidates_and_retries_once(self):
        client = self.client()
        self.fake.faults["details"] = [httpx.Response(401)]
        client.details(PKG)
        self.assertEqual({"checkin": 1, "uploadDeviceConfig": 1, "auth": 2, "toc": 2},
                         self.fake.handshake())
        self.assertEqual(1, len(set(self.fake.android_ids)))  # same device
        first, second = self.fake.bearers("details")
        self.assertNotEqual(first, second)
        client.details(PKG)  # the replacement is cached
        self.assertEqual(2, self.fake.n("auth"))

    def test_simultaneous_401_mints_once(self):
        client = self.client()
        client.details(PKG)
        self.fake.reject.add(self.fake.bearers("details")[0])
        self.run_threads(8, lambda: client.details(PKG))
        self.assertEqual({"checkin": 1, "uploadDeviceConfig": 1, "auth": 2, "toc": 2},
                         self.fake.handshake())

    def test_second_401_in_a_row_registers_a_new_device(self):
        client = self.client()
        self.fake.faults["details"] = [httpx.Response(401)] * 2
        with self.assertRaises(AuthExpiredError):  # one retry, like before
            client.details(PKG)
        self.assertEqual(1, self.fake.n("checkin"))
        self.fake.faults["details"] = [httpx.Response(401)]
        client.details(PKG)
        self.assertEqual(2, self.fake.n("checkin"))
        self.assertEqual(3, self.fake.n("auth"))
        self.assertEqual(2, len(set(self.fake.android_ids)))

    def test_ttl(self):
        cases = (("", 2999, 3001), ("ExpiresInDurationSec=301\n", 0.5, 2),
                 ("Expiry={exp}\n", 290, 310), ("ExpiresInDurationSec=soon\n", 2999, 3001))
        for i, (extra, fresh, stale) in enumerate(cases):
            with self.subTest(extra=extra):
                self.fake.requests.clear()
                self.fake.auth_extra = extra.format(exp=int(time.time()) + 600)
                client = self.client(f"ttl{i}")
                client.details(PKG)
                with self.at(fresh):
                    client.details(PKG)
                self.assertEqual(1, self.fake.n("auth"))
                with self.at(stale):
                    client.details(PKG)
                self.assertEqual({"checkin": 1, "uploadDeviceConfig": 1, "auth": 2,
                                  "toc": 2}, self.fake.handshake())

    def test_keys(self):
        client = self.client("k", profile=None)
        priority = auth.get_priority_profiles("arm64")[0][0]
        client.details(PKG)
        client.details(PKG, profile=priority)  # None == the priority profile
        self.assertEqual(1, self.fake.n("checkin"))
        client.details(PKG, profile="D2")
        alias = auth.find_profile("D2", "arm64")[1]["UserReadableName"]
        client.details(PKG, profile=alias)  # an alias is its canonical key
        self.assertEqual(2, self.fake.n("checkin"))
        client.details(PKG, profile="Pv", country="DE")
        client.details(PKG, arch="armv7")
        self.assertEqual(4, self.fake.n("checkin"))
        client.details(PKG, profile="Pv", proxy="http://proxy.invalid:1")  # not a key
        self.assertEqual(4, self.fake.n("auth"))
        # A rotated AAS token keeps the account's device but signs in again.
        self.client("k", token="aas_et/rotated", profile="Pv").details(PKG)
        self.assertEqual((4, 5), (self.fake.n("checkin"), self.fake.n("auth")))
        self.assertEqual(self.fake.android_ids[0], self.fake.android_ids[-1])

    def test_ensure_auth_force_refresh_keeps_the_device(self):
        with self.scope():
            first = auth.ensure_auth(profile="Pv")
            self.assertEqual(first, auth.ensure_auth(profile="Pv"))
            second = auth.ensure_auth(profile="Pv", force_refresh=True)
        self.assertNotEqual(first["authToken"], second["authToken"])
        self.assertEqual(first["gsfId"], second["gsfId"])
        self.assertEqual((1, 2), (self.fake.n("checkin"), self.fake.n("auth")))

    def test_fetch_token_and_ensure_pool_stay_fresh(self):
        with self.scope():
            auth.pick_pool_token(profile="Pv")
            for _ in range(3):
                auth.fetch_token(profile="Pv")
            self.assertEqual(3, len(auth.ensure_pool(profile="Pv", size=3)))
        self.assertEqual(7, self.fake.n("checkin"))

    def test_returned_tokens_are_copies(self):
        with self.scope():
            token = auth.pick_pool_token(profile="Pv")
            token["authToken"] = "mutated"
            token["deviceInfoProvider"]["mccMnc"] = "mutated"
            again = auth.pick_pool_token(profile="Pv")
        self.assertNotIn("mutated", repr(again))

    def test_failures_are_not_cached_and_keep_their_types(self):
        client = self.client()
        self.fake.faults["checkin"] = [httpx.Response(429, headers={"Retry-After": "120"})]
        with self.assertRaises(RateLimitedError) as raised, \
                self.assertLogs("goopdl", "ERROR") as logs:
            client.details(PKG)
        self.assertEqual(120.0, raised.exception.retry_after)
        self.assertEqual(["ERROR:goopdl.auth:Direct Google authentication failed at "
                          "checkin (HTTP 429)."], logs.output)
        self.fake.faults["checkin"] = [httpx.Response(429, headers={"Retry-After": "7"})]
        with self.scope(), self.assertLogs("goopdl", "ERROR"):
            with self.assertRaises(DirectAuthError) as raised:
                auth.pick_pool_token(profile="Pv", raise_on_error=True)
            self.assertEqual(("checkin", 429, 7.0), (raised.exception.stage,
                             raised.exception.status, raised.exception.retry_after))
            self.assertIsNotNone(auth.pick_pool_token(profile="Pv"))  # not cached
        self.fake.faults["auth"] = [httpx.Response(403, text="Error=BadAuthentication\n")]
        with self.assertRaises(PlayAPIError) as raised, self.assertLogs("goopdl", "ERROR"):
            self.client("bad").details(PKG)
        self.assertEqual(403, raised.exception.status_code)
        self.assertIn("BadAuthentication", str(raised.exception))
        self.assertEqual(2, self.fake.n("auth"))  # no retry of a revoked credential

    def test_download_retries_a_5xx_exchange_on_the_same_device(self):
        from goopdl import api
        self.fake.faults["auth"] = [httpx.Response(503)] * 2
        with self.scope(), self.assertLogs("goopdl", "ERROR"):
            api.download_app(PKG, self.tmp / "out", profile="Pv")
        self.assertEqual((1, 3), (self.fake.n("checkin"), self.fake.n("auth")))

    def test_pool_busy(self):
        gate = self.fake.gate = threading.Event()
        holder = threading.Thread(target=lambda: self.client().details(PKG))
        holder.start()
        try:
            deadline = time.monotonic() + 10
            while not auth._direct_locks and time.monotonic() < deadline:
                threading.Event().wait(0.01)
            with self.scope(), patch.object(auth, "_POOL_LOCK_TIMEOUT", 0.05):
                with self.assertRaises(PoolBusyError):
                    auth.pick_pool_token(profile="Pv", raise_on_error=True)
                with self.assertLogs("goopdl", "ERROR") as logs:
                    self.assertIsNone(auth.pick_pool_token(profile="Pv"))
            self.assertIn("lock acquisition timed out", logs.output[0])
        finally:
            gate.set()
            holder.join(10)
        self.assertEqual(1, self.fake.n("checkin"))

    def test_clear_auth_forgets_the_sessions_of_its_directory(self):
        self.client("a").details(PKG)
        self.client("b").details(PKG)
        with self.scope("a"):
            auth.clear_auth()
        self.client("a").details(PKG)
        self.client("b").details(PKG)
        self.assertEqual(3, self.fake.n("auth"))

    def test_anonymous_mode_never_uses_the_session_cache(self):
        with patch.object(auth, "_direct_session", side_effect=AssertionError):
            Client(config_dir=self.tmp / "anon", profile="Pv").details(PKG)
        self.assertEqual({}, auth._direct_sessions)

    @unittest.skipUnless(hasattr(os, "fork"), "needs os.fork")
    def test_fork_resets_the_cache_and_its_locks(self):
        self.client().details(PKG)
        lock = next(iter(auth._direct_locks.values()))
        lock.acquire()  # as if another thread were signing in during fork
        try:
            with patch.object(auth, "_POOL_LOCK_TIMEOUT", 5):
                pid = os.fork()
                if pid == 0:  # child: empty cache, fresh locks, signs in again
                    code = 1
                    try:
                        if not auth._direct_sessions and not auth._direct_locks:
                            self.client().details(PKG)
                            code = 0 if self.fake.n("auth") == 2 else 3
                    finally:
                        os._exit(code)
                _, status = os.waitpid(pid, 0)
        finally:
            lock.release()
        self.assertEqual(0, os.waitstatus_to_exitcode(status))
        self.assertEqual(1, len(auth._direct_sessions))  # the parent keeps its own


def _cold_process(config_dir, start, results):
    """One spawned process with a cold memory cache (see DiskTest)."""
    fake = DirectFake()
    with isolated_env({"GOOPDL_CONFIG_DIR": config_dir, "GOOPDL_ACCOUNT_EMAIL": EMAIL,
                       "GOOPDL_AAS_TOKEN": AAS}), \
            patch.object(socket.socket, "connect", no_sockets), \
            patch("httpx.post", side_effect=fake.post), \
            patch("httpx.get", side_effect=fake.get):
        start.wait(60)
        try:
            token = auth.pick_pool_token(profile="Pv", raise_on_error=True)
            results.put((fake.n("checkin"), fake.n("auth"), token["gsfId"], os.getpid()))
        except BaseException as exc:  # noqa: BLE001
            results.put(repr(exc))


class DiskTest(DirectCacheCase):
    def records(self, name="c"):
        return sorted((self.tmp / name).glob("direct-device-*.json"))

    def test_record_name_contents_and_permissions(self):
        self.client().details(PKG)
        (record,) = self.records()
        self.assertRegex(record.name, r"^direct-device-[0-9a-f]{16}-arm64-profile-Pv\.json$")
        data = json.loads(record.read_text())
        self.assertEqual({"v", "acct", "gsfId", "deviceCheckInConsistencyToken",
                          "deviceConfigToken", "created_at"}, set(data))
        self.assertEqual((1, "1001"), (data["v"], data["gsfId"]))
        lock = record.with_suffix(".lock")
        self.assertTrue(lock.is_file())
        if os.name != "nt":
            self.assertEqual(0o600, stat.S_IMODE(record.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(lock.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(record.parent.stat().st_mode))

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_an_open_directory_is_tightened(self):
        (self.tmp / "open").mkdir(mode=0o755)
        os.chmod(self.tmp / "open", 0o755)
        self.client("open").details(PKG)
        self.assertEqual(0o700, stat.S_IMODE((self.tmp / "open").stat().st_mode))

    def test_a_new_process_reuses_the_device(self):
        self.client().details(PKG)
        auth._reset_direct_cache()  # what a new process starts with
        self.client().details(PKG)
        self.assertEqual({"checkin": 1, "uploadDeviceConfig": 1, "auth": 2, "toc": 2},
                         self.fake.handshake())
        self.assertEqual(1, len(set(self.fake.android_ids)))

    def test_unusable_records_are_discarded(self):
        self.client().details(PKG)
        (record,) = self.records()
        good = json.loads(record.read_text())
        cases = {
            "truncated": record.read_text()[:20], "empty": "", "list": "[]",
            "version": json.dumps({**good, "v": 2}),
            "account": json.dumps({**good, "acct": "0" * 16}),
            "types": json.dumps({**good, "gsfId": 4097}),
            "control": json.dumps({**good, "deviceConfigToken": "a\nb"}),
            "latin1": b"\xff\xfe",
        }
        for checkins, (name, content) in enumerate(cases.items(), start=2):
            with self.subTest(name):
                if isinstance(content, bytes):
                    record.write_bytes(content)
                else:
                    record.write_text(content)
                auth._reset_direct_cache()
                with self.assertLogs("goopdl", "WARNING") as logs:
                    self.client().details(PKG)
                self.assertEqual(checkins, self.fake.n("checkin"))
                self.assertIn("Discarding an unusable direct-auth device record",
                              logs.output[0])
                for secret in (*SECRETS, "100"):
                    self.assertNotIn(secret, " ".join(logs.output))
                self.assertEqual(1, json.loads(record.read_text())["v"])

    def test_a_directory_in_place_of_the_record_falls_back_to_memory(self):
        self.client().details(PKG)
        (record,) = self.records()
        record.unlink()
        record.mkdir()
        auth._reset_direct_cache()
        with self.assertLogs("goopdl", "WARNING") as logs:
            self.client().details(PKG)
            self.client().details(PKG, country="US")
        self.assertIn("continuing in memory", logs.output[0])
        self.assertTrue(record.is_dir())

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlinks_are_not_followed(self):
        outside = self.tmp / "outside.json"
        for target in ("json", "lock"):
            with self.subTest(target):
                outside.write_text("untouched")
                name = f"s{target}"
                self.client(name).details(PKG)
                (record,) = self.records(name)
                link = record.with_suffix(f".{target}")
                link.unlink()
                link.symlink_to(outside)
                auth._reset_direct_cache()
                with self.assertLogs("goopdl", "WARNING") as logs:
                    self.client(name).details(PKG)
                self.assertIn("unavailable (OSError); continuing in memory", logs.output[0])
                self.assertEqual("untouched", outside.read_text())
                self.assertTrue(link.is_symlink())

    def test_an_unwritable_disk_keeps_working_in_memory(self):
        with patch.object(auth, "_atomic_pool_json", side_effect=PermissionError), \
                self.assertLogs("goopdl", "WARNING") as logs:
            self.client().details(PKG)
            self.client().details(PKG, profile="D2")
            with self.at(3001):  # expired bearer: same in-memory device
                self.client().details(PKG)
        self.assertEqual("WARNING:goopdl.auth:Direct auth device cache unavailable "
                         "(PermissionError); continuing in memory.", logs.output[0])
        self.assertEqual((2, 3), (self.fake.n("checkin"), self.fake.n("auth")))
        self.assertEqual([], self.records())

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "POSIX, not root")
    def test_a_read_only_directory_keeps_working(self):
        (self.tmp / "ro").mkdir(mode=0o500)
        self.addCleanup(os.chmod, self.tmp / "ro", 0o700)
        with self.assertLogs("goopdl", "WARNING"):
            self.assertEqual(4242, self.client("ro").details(PKG).version_code)
        self.assertEqual([], list((self.tmp / "ro").iterdir()))

    def test_tenants_and_accounts_never_share_a_record(self):
        self.client("t1").details(PKG)
        self.client("t2").details(PKG)  # same account, other config_dir
        self.client("t1", email="other@x.test", token="aas_et/other").details(PKG)
        self.assertEqual(3, self.fake.n("checkin"))
        self.assertEqual(1, len(self.records("t2")))
        mine, other = self.records("t1")
        self.assertNotEqual(mine.name.split("-")[2], other.name.split("-")[2])
        self.assertNotEqual(json.loads(mine.read_text())["gsfId"],
                            json.loads(other.read_text())["gsfId"])

    def test_no_secrets_in_any_file(self):
        client = self.client()
        self.fake.faults["details"] = [httpx.Response(401)] * 3
        for call in (client.details, client.details, client.item, client.list_splits):
            try:
                call(PKG)
            except AuthExpiredError:
                pass
        client.download(PKG, self.tmp / "out")
        client.details(PKG, country="DE")
        files = [p for p in (self.tmp / "c").rglob("*")]
        self.assertTrue(files)
        for path in files:
            content = path.read_bytes().decode("utf-8", "replace")
            for secret in (*SECRETS, "x.test"):
                self.assertNotIn(secret, path.name)
                self.assertNotIn(secret, content, path.name)

    def test_second_401_in_a_row_replaces_the_record(self):
        client = self.client()
        self.fake.faults["details"] = [httpx.Response(401)] * 3
        with self.assertRaises(AuthExpiredError):
            client.details(PKG)
        client.details(PKG)
        (record,) = self.records()
        self.assertEqual("1002", json.loads(record.read_text())["gsfId"])

    def test_clear_auth_removes_records_but_keeps_locks(self):
        self.client().details(PKG)
        self.client(email="other@x.test", token="aas_et/other").details(PKG)
        with self.scope():
            auth.clear_auth()
        self.assertEqual([], self.records())
        self.assertEqual(2, len(list((self.tmp / "c").glob("direct-device-*.lock"))))
        self.client().details(PKG)
        self.assertEqual(3, self.fake.n("checkin"))

    def test_downloads_without_a_profile_reuse_the_registered_device(self):
        from goopdl import api
        client = self.client(profile=None)
        picks = iter((0, -1))
        with patch.object(api.random, "choice", side_effect=lambda c: c[next(picks)]):
            client.download(PKG, self.tmp / "o1")
            client.download(PKG, self.tmp / "o2")  # cold B would be a 2nd device
        self.assertEqual(1, self.fake.n("checkin"))

    def test_a_busy_device_lock_is_pool_busy(self):
        with self.scope():
            path = auth._device_path(auth._account_hash(EMAIL), "arm64", None, "Pv")
            handle = auth._acquire_file_lock(path.with_suffix(".lock"))
        try:
            with self.scope(), patch.object(auth, "_POOL_LOCK_TIMEOUT", 0.05):
                with self.assertRaises(PoolBusyError):
                    auth.pick_pool_token(profile="Pv", raise_on_error=True)
                with self.assertLogs("goopdl", "ERROR"):
                    self.assertIsNone(auth.pick_pool_token(profile="Pv"))
        finally:
            handle.close()
        self.assertEqual([], self.fake.requests)

    def test_cold_processes_register_one_device(self):
        ctx = multiprocessing.get_context("spawn")  # what Windows and macOS use
        start, results = ctx.Event(), ctx.Queue()
        procs = [ctx.Process(target=_cold_process,
                             args=(str(self.tmp / "p"), start, results))
                 for _ in range(3)]
        for proc in procs:
            proc.start()
        start.set()
        try:
            outcomes = [results.get(timeout=90) for _ in procs]
        finally:
            for proc in procs:
                proc.join(30)
                if proc.is_alive():
                    proc.terminate()
        self.assertTrue(all(isinstance(o, tuple) for o in outcomes), outcomes)
        self.assertEqual(1, sum(o[0] for o in outcomes), outcomes)  # one checkin
        self.assertEqual([1, 1, 1], [o[1] for o in outcomes])
        self.assertEqual(1, len({o[2] for o in outcomes}))
        self.assertEqual(3, len({o[3] for o in outcomes} - {os.getpid()}))


class SecretsTest(DirectCacheCase):
    def assert_clean(self, error):
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
                if not frame.f_code.co_name.startswith("test_"):
                    for name, value in frame.f_locals.items():
                        if isinstance(value, (Client, Credentials)):
                            continue  # the caller's own objects
                        text = f"{value!s} {value!r}"
                        for secret in SECRETS:
                            self.assertNotIn(secret, text, (frame.f_code.co_name, name))
                tb = tb.tb_next
            pending.extend((exc.__cause__, exc.__context__))

    def test_no_secrets_in_logs_reprs_or_exception_frames(self):
        logger = logging.getLogger("goopdl")
        records = []

        class Collect(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Collect(logging.DEBUG)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        level = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, level)

        client = self.client()
        client.details(PKG)
        failures = {
            "checkin": httpx.Response(429), "uploadDeviceConfig": httpx.Response(503),
            "auth": httpx.Response(403, text="Error=BadAuthentication\n"),
        }
        for i, (endpoint, response) in enumerate(failures.items()):
            for warm in (False, True):
                with self.subTest(endpoint=endpoint, warm=warm):
                    cold = self.client(f"x{i}{warm}")
                    if warm:  # the failure hits a 401 replacement
                        cold.details(PKG)
                        self.fake.faults["details"] = [httpx.Response(401)]
                    self.fake.faults[endpoint] = [response]
                    if warm and endpoint != "auth":
                        self.fake.faults["auth"] = [httpx.Response(500)]
                    for call in (cold.details, cold.item):
                        try:
                            call(PKG)
                        except Exception as exc:  # noqa: BLE001
                            self.assert_clean(exc)
                        self.fake.faults.clear()
        with self.scope(), patch.object(auth, "_exchange", side_effect=KeyError(AAS)):
            auth._reset_direct_cache()
            try:
                auth.pick_pool_token(profile="Pv", raise_on_error=True)
            except DirectAuthError as exc:
                self.assert_clean(exc)
            else:
                self.fail("expected DirectAuthError")
        self.assertTrue(records)
        for record in records:
            for secret in SECRETS:
                self.assertNotIn(secret, record.getMessage())
        for secret in SECRETS:
            self.assertNotIn(secret, repr(auth._direct_sessions))
            self.assertNotIn(secret, repr(auth._direct_devices))


if __name__ == "__main__":
    unittest.main()
