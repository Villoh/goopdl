"""auth._use_scope: per-context cache directory and credentials (no network)."""

import asyncio
import os
import socket
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import httpx

from _envutil import isolated_env
from goopdl import auth

ENV_ACCOUNT = {"GOOPDL_ACCOUNT_EMAIL": "env@x.test", "GOOPDL_AAS_TOKEN": "aas_et/env"}


def _no_sockets(*_a, **_k):
    raise AssertionError("real network access attempted")


class FakeEndpoints:
    """Records (endpoint, Email, cache dir) for every request; answers like Google."""

    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self.barrier = barrier
        self.lock = threading.Lock()
        self.calls: list[tuple[str, str, Path]] = []

    def post(self, url, *, data=None, json=None, **_kwargs):
        endpoint = str(url).rsplit("/", 1)[-1]
        email = (data or {}).get("Email", "")
        with self.lock:
            self.calls.append((endpoint, email, auth.get_config_dir()))
        if self.barrier is not None and endpoint in ("checkin", "auth"):
            self.barrier.wait(timeout=10)  # both threads are mid-call here
        if endpoint == "checkin":
            return httpx.Response(
                200, content=auth._proto_varint(7, 1) + auth._proto_bytes(12, "c")
            )
        if endpoint == "uploadDeviceConfig":
            return httpx.Response(200, content=auth._proto_bytes(
                1, auth._proto_bytes(28, auth._proto_bytes(1, "cfg"))))
        if endpoint == "auth" and data is not None:
            return httpx.Response(200, text=f"Auth=bearer-{email}\n")
        # Anonymous dispenser.
        return httpx.Response(200, json={"authToken": "anon", "gsfId": "1"})

    def get(self, *_args, **_kwargs):  # toc
        return httpx.Response(404)


@contextmanager
def scope(directory=None, credentials=None, explicit=True):
    if hasattr(auth, "_use_scope"):
        with auth._use_scope(auth._Scope(
            config_dir=directory, credentials=credentials, explicit_credentials=explicit,
        )):
            yield
        return
    # Without the scope (older goopdl) the only knob is the process-wide
    # environment, so these tests then fail by mixing tenants, as intended.
    env = {"GOOPDL_CONFIG_DIR": str(directory)} if directory else {}
    if explicit:
        email, token = credentials or ("", "")
        env.update(GOOPDL_ACCOUNT_EMAIL=email, GOOPDL_AAS_TOKEN=token)
    with patch.dict(os.environ, env):
        yield


def current_scope():
    var = getattr(auth, "_scope", None)
    return var.get() if var is not None else None


class ScopeTestCase(unittest.TestCase):
    env: dict = {}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.fake = FakeEndpoints()
        for patcher in (
            isolated_env({"GOOPDL_CONFIG_DIR": str(self.tmp / "env"), **self.env}),
            patch.object(socket.socket, "connect", _no_sockets),
            patch("httpx.post", side_effect=lambda *a, **k: self.fake.post(*a, **k)),
            patch("httpx.get", side_effect=lambda *a, **k: self.fake.get(*a, **k)),
            patch("goopdl.auth.time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class WithoutScopeTest(ScopeTestCase):
    def test_resolution_and_credentials_are_unchanged(self) -> None:
        self.assertIsNone(current_scope())
        self.assertEqual(self.tmp / "env", auth.get_config_dir())
        self.assertFalse(auth.direct_auth_enabled())
        with patch.dict("os.environ", ENV_ACCOUNT):
            self.assertEqual(("env@x.test", "aas_et/env"), auth._direct_credentials())
        with patch.object(auth, "_CONFIG_DIR", self.tmp / "patched", create=True):
            self.assertEqual(self.tmp / "patched", auth.get_config_dir())
            # A scope without config_dir keeps today's resolution, patch included.
            with scope():
                self.assertEqual(self.tmp / "patched", auth.get_config_dir())

    def test_scope_directory_wins_over_environment_and_patch(self) -> None:
        with patch.object(auth, "_CONFIG_DIR", self.tmp / "patched", create=True):
            with scope(self.tmp / "a"):
                self.assertEqual(self.tmp / "a", auth.get_config_dir())
            self.assertEqual(self.tmp / "patched", auth.get_config_dir())

    def test_repr_hides_credentials(self) -> None:
        text = repr(auth._Scope(credentials=("mail@x", "aas_et/secret")))
        self.assertNotIn("secret", text)
        self.assertNotIn("mail@x", text)


class ExplicitAnonymousTest(ScopeTestCase):
    env = ENV_ACCOUNT

    def test_explicit_none_ignores_the_environment_account(self) -> None:
        self.assertTrue(auth.direct_auth_enabled())  # sanity: env is set
        with scope(self.tmp / "a"):
            self.assertFalse(auth.direct_auth_enabled())
            token = auth.pick_pool_token(size=1, raise_on_error=True)
        self.assertEqual("anon", (token or {}).get("authToken"))
        endpoints = [call[0] for call in self.fake.calls]
        self.assertNotIn("checkin", endpoints)
        self.assertEqual(["auth"], endpoints)  # the dispenser's /api/auth
        self.assertEqual("", self.fake.calls[0][1])
        self.assertTrue(list((self.tmp / "a").glob("token-pool-*.json")))

    def test_non_explicit_scope_keeps_the_environment_account(self) -> None:
        with scope(self.tmp / "a", explicit=False):
            self.assertTrue(auth.direct_auth_enabled())


class ConcurrentScopesTest(ScopeTestCase):
    def test_threads_do_not_mix_accounts_or_directories(self) -> None:
        self.fake.barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def worker(name: str) -> None:
            try:
                with scope(self.tmp / name, (f"{name}@x.test", f"aas_et/{name}")):
                    bundle = auth.fetch_token(profile="Pv", raise_on_error=True)
                    results[name] = (bundle or {}).get("authToken")
            except BaseException as exc:  # noqa: BLE001 -- reported below
                results[name] = exc

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual({"a": "bearer-a@x.test", "b": "bearer-b@x.test"}, results)
        for endpoint, email, directory in self.fake.calls:
            if endpoint == "auth":
                self.assertEqual(self.tmp / email.split("@")[0], directory)
        self.assertIsNone(current_scope())

    def test_gather_to_thread_pools_stay_in_their_directory(self) -> None:
        self.fake.barrier = threading.Barrier(2)

        def mint(name: str):
            with scope(self.tmp / name):
                return auth.pick_pool_token(size=1, raise_on_error=True)

        async def main():
            return await asyncio.gather(
                asyncio.to_thread(mint, "a"), asyncio.to_thread(mint, "b")
            )

        # Both dispenser requests (/api/auth) are in flight at the same time.
        tokens = asyncio.run(main())
        self.assertTrue(all(tokens))
        for name in ("a", "b"):
            files = sorted(p.name for p in (self.tmp / name).iterdir())
            self.assertIn("token-pool-arm64.json", files)
        self.assertFalse((self.tmp / "env").exists())
        self.assertEqual({self.tmp / "a", self.tmp / "b"},
                         {call[2] for call in self.fake.calls})


class RestoreTest(ScopeTestCase):
    def test_restored_after_exception(self) -> None:
        with self.assertRaises(ZeroDivisionError), scope(self.tmp / "a"):
            raise ZeroDivisionError
        self.assertIsNone(current_scope())
        self.assertEqual(self.tmp / "env", auth.get_config_dir())

    def test_nested_scopes_restore_the_outer_one(self) -> None:
        with scope(self.tmp / "a"):
            with scope(self.tmp / "b"):
                self.assertEqual(self.tmp / "b", auth.get_config_dir())
            self.assertEqual(self.tmp / "a", auth.get_config_dir())

    def test_restored_after_cancellation(self) -> None:
        seen = []

        async def scoped():
            with scope(self.tmp / "a"):
                seen.append(auth.get_config_dir())
                await asyncio.sleep(10)

        async def main():
            task = asyncio.create_task(scoped())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            # The task ran in a copied context: this one was never touched.
            self.assertIsNone(current_scope())
            return task

        task = asyncio.run(main())
        self.assertEqual([self.tmp / "a"], seen)
        self.assertTrue(task.cancelled())
        self.assertIsNone(current_scope())

    def test_cancellation_inside_one_context_resets_it(self) -> None:
        async def main():
            async def scoped():
                with scope(self.tmp / "a"):
                    await asyncio.sleep(10)

            inner = asyncio.ensure_future(scoped())
            await asyncio.sleep(0)
            inner.cancel()
            await asyncio.gather(inner, return_exceptions=True)
            # The task's own context was reset by the with-block, too.
            return inner.get_context().run(current_scope)

        self.assertIsNone(asyncio.run(main()))


if __name__ == "__main__":
    unittest.main()
