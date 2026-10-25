"""Local pool transactions: spawn workers also exercise Windows' independent handles."""
from __future__ import annotations

import errno
import json
import multiprocessing as mp
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from goopdl import auth
from goopdl.profiles import find_profile


def token(name: str, gsf: str | None = None) -> dict:
    return {"authToken": name, "gsfId": gsf or name, "_cached_at": time.time()}


def pool_worker(directory, operation, profile, output, started, minted, release,
                counter, loops=1, begin=None):
    # Spawn does not inherit parent patches or environment cleanup.
    os.environ.pop("GOOPDL_ACCOUNT_EMAIL", None)
    os.environ.pop("GOOPDL_AAS_TOKEN", None)
    auth._CONFIG_DIR = Path(directory)
    auth._POOL_LOCK_TIMEOUT = 5
    real_read = auth._read_index

    def mint(**kwargs):
        with counter.get_lock():
            counter.value += 1
            number = counter.value
        minted.set()
        if not release.wait(10):
            raise RuntimeError("test mint release timed out")
        return token(f"mint-{number}")

    def read(*args):
        value = real_read(*args)
        time.sleep(0.01)  # Widen the old read/write race, inside the new transaction.
        return value

    try:
        with (patch.object(auth, "fetch_token", side_effect=mint),
              patch.object(auth, "_read_index", side_effect=read),
              patch.object(auth.console, "print")):
            started.set()
            if begin is not None and not begin.wait(10):
                raise RuntimeError("test start gate timed out")
            if operation == "hold":
                with auth._pool_lock("arm64", "US", profile) as acquired:
                    output.put(acquired)
                    release.wait(10)
                return
            results = []
            for _ in range(loops):
                if operation == "replace":
                    result = auth.replace_pool_token(token("failed"), country="US", profile=profile)
                else:
                    result = auth.pick_pool_token(country="us", profile=profile, size=1)
                results.append(result)
            output.put(results)
    except BaseException as exc:
        output.put((type(exc).__name__, str(exc)))


class PoolTransactionsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for patcher in (patch.object(auth, "_CONFIG_DIR", self.directory),
                        patch.object(auth, "_POOL_LOCK_TIMEOUT", 5),
                        # Keep SystemRoot/PATH for Windows spawn and DLL loading.
                        patch.dict(os.environ, {"GOOPDL_ACCOUNT_EMAIL": "",
                                                "GOOPDL_AAS_TOKEN": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)
        messages = patch.object(auth.console, "print")
        self.messages = messages.start()
        self.addCleanup(messages.stop)
        self.ctx = mp.get_context("spawn")
        self.counter = self.ctx.Value("i", 0)
        self.children = []
        self.addCleanup(self.stop_children)

    def stop_children(self):
        for process in self.children:
            if process.is_alive():
                process.terminate()
            process.join(5)
            self.assertFalse(process.is_alive())

    def start(self, operation="pick", profile="Pv", release=None, loops=1, begin=None):
        output = self.ctx.Queue()
        self.addCleanup(output.close)
        started, minted = self.ctx.Event(), self.ctx.Event()
        if release is None:
            release = self.ctx.Event()
            release.set()
        process = self.ctx.Process(target=pool_worker, args=(
            str(self.directory), operation, profile, output, started, minted,
            release, self.counter, loops, begin))
        process.start()
        self.children.append(process)
        self.assertTrue(started.wait(10), "worker did not start")
        return process, output, minted

    def result(self, worker):
        process, output, _ = worker
        result = output.get(timeout=15)
        process.join(5)
        self.assertEqual(0, process.exitcode)
        self.assertIsInstance(result, list, result)
        return result

    def seed(self, members, profile="Pv"):
        auth._save_pool(members, "arm64", "US", profile)

    def test_cold_pool_and_replacement_mint_once_across_processes(self):
        for operation in ("pick", "replace"):
            with self.subTest(operation=operation):
                for path in self.directory.glob("*.json"):
                    path.unlink()
                self.counter.value = 0
                if operation == "replace":
                    self.seed([token("failed")])
                release = self.ctx.Event()
                first = self.start(operation, release=release)
                self.assertTrue(first[2].wait(10))
                second = self.start(operation, release=release)
                try:
                    self.assertFalse(second[2].wait(0.5), "duplicate paid mint")
                finally:
                    release.set()
                a, b = self.result(first), self.result(second)
                self.assertEqual(1, self.counter.value)
                self.assertEqual(a, b)
                self.assertEqual(1, len(auth._load_pool("arm64", "US", "Pv")))

    def test_round_robin_aliases_processes_and_threads(self):
        self.seed([token(f"warm{i}") for i in range(3)])
        selected = find_profile("Pv", "arm64")
        assert selected is not None
        begin = self.ctx.Event()
        workers = [self.start(profile=profile, loops=8, begin=begin)
                   for profile in ("Pv", selected[1]["UserReadableName"], "Pv")]
        begin.set()
        results = [member["authToken"] for worker in workers for member in self.result(worker)]
        self.assertEqual(["warm0"] * 8 + ["warm1"] * 8 + ["warm2"] * 8, sorted(results))
        self.assertEqual(24, auth._read_index("arm64", "US", "Pv"))
        self.assertEqual(0, self.counter.value)
        real_read = auth._read_index

        def slow_read(*args):
            result = real_read(*args)
            time.sleep(0.01)
            return result

        with patch.object(auth, "_read_index", side_effect=slow_read):
            threads = [threading.Thread(target=auth.pick_pool_token,
                       kwargs={"country": "US", "profile": "Pv", "size": 1})
                       for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
                self.assertFalse(thread.is_alive())
        self.assertEqual(32, auth._read_index("arm64", "US", "Pv"))
        self.assertEqual(1, len(list(self.directory.glob("*.lock"))))

    def test_same_gsf_new_credential_is_not_rejected_again(self):
        failed, refreshed = token("failed", "same-gsf"), token("new", "same-gsf")
        self.seed([refreshed])
        with patch.object(auth, "fetch_token") as fetch:
            self.assertEqual(refreshed, auth.replace_pool_token(
                failed, country="US", profile="Pv"))
        fetch.assert_not_called()
        self.assertEqual([refreshed], auth._load_pool("arm64", "US", "Pv"))
        # Exact credential removal must preserve another token sharing the GSF ID.
        self.seed([failed, refreshed])
        with patch.object(auth, "fetch_token", return_value=token("extra")):
            auth.replace_pool_token(failed, country="US", profile="Pv")
        self.assertEqual(["new", "extra"], [t["authToken"] for t in
                         auth._load_pool("arm64", "US", "Pv")])

    def test_other_profile_does_not_wait_for_locked_pool(self):
        release = self.ctx.Event()
        first = self.start(release=release)
        self.assertTrue(first[2].wait(10))
        try:
            other = self.result(self.start(profile="D2"))
            self.assertEqual(1, len(other))
            self.assertTrue(first[0].is_alive())
        finally:
            release.set()
        self.result(first)

    def test_timeout_fail_closed_and_terminated_holder_releases_lock(self):
        release = self.ctx.Event()
        holder = self.start("hold", release=release)
        self.assertTrue(holder[1].get(timeout=10))
        with (patch.object(auth, "_POOL_LOCK_TIMEOUT", 0.1),
              patch.object(auth, "fetch_token") as fetch):
            start = time.monotonic()
            self.assertEqual([], auth.ensure_pool(country="US", profile="Pv", size=1))
            self.assertIsNone(auth.pick_pool_token(country="US", profile="Pv", size=1))
            self.assertIsNone(auth.replace_pool_token({}, country="US", profile="Pv"))
            self.assertEqual([], auth._load_pool("arm64", "US", "Pv"))
            self.assertLess(time.monotonic() - start, 3)
            fetch.assert_not_called()
        self.assertIn("TimeoutError", str(self.messages.call_args_list))
        holder[0].terminate()
        holder[0].join(5)
        with patch.object(auth, "fetch_token", return_value=token("after-crash")):
            self.assertEqual(1, len(auth.ensure_pool(country="US", profile="Pv", size=1)))
        lockfiles = list(self.directory.glob("*.lock"))
        self.assertEqual(1, len(lockfiles))
        self.assertEqual(1, lockfiles[0].stat().st_size)

    def test_noncontention_lock_errors_do_not_retry_or_mint(self):
        # Local native mock, so this subcase also runs on Windows.
        module, name = ("msvcrt", "locking") if os.name == "nt" else ("fcntl", "flock")
        with (patch(f"{module}.{name}", side_effect=OSError(errno.EBADF, "bad descriptor")) as lock,
              patch.object(auth.time, "sleep") as sleep,
              patch.object(auth, "fetch_token") as fetch):
            self.assertIsNone(auth.pick_pool_token(country="US", profile="Pv", size=1))
        lock.assert_called_once()
        sleep.assert_not_called()
        fetch.assert_not_called()
        with (patch.object(Path, "read_text", side_effect=PermissionError("denied")),
              patch.object(auth, "fetch_token") as fetch):
            self.assertEqual([], auth.ensure_pool(country="US", profile="Pv", size=1))
            self.assertEqual([], auth._load_pool("arm64", "US", "Pv"))
        fetch.assert_not_called()

    def test_each_mint_is_saved_and_failure_releases_lock(self):
        first = token("first-paid-request")
        with patch.object(auth, "fetch_token", side_effect=[first, KeyboardInterrupt]):
            with self.assertRaises(KeyboardInterrupt):
                auth.ensure_pool(country="US", profile="Pv", size=2)
        self.assertEqual([first], auth._load_pool("arm64", "US", "Pv"))
        with patch.object(auth, "fetch_token", return_value=token("second")):
            self.assertEqual(2, len(auth.ensure_pool(country="US", profile="Pv", size=2)))
        real_open = Path.open
        handles = []

        def capture_open(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            handles.append(handle)
            return handle

        native = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        for stage in (native, "goopdl.auth.os.fstat"):
            with self.subTest(stage=stage):
                with (patch.object(Path, "open", capture_open),
                      patch(stage, side_effect=KeyboardInterrupt)):
                    with self.assertRaises(KeyboardInterrupt):
                        auth.ensure_pool(country="US", profile="Pv", size=2)
                self.assertTrue(all(handle.closed for handle in handles))
                with auth._pool_lock("arm64", "US", "Pv", blocking=False) as acquired:
                    self.assertTrue(acquired)

    def test_atomic_snapshots_and_failed_publication_cleanup(self):
        old, new = [token("old")], [token("new"), token("new2")]
        self.seed(old)
        auth._write_index(7, "arm64", "US", "Pv")
        real_replace = os.replace

        def inspect_replace(source, destination):
            # Temp is closed (Windows replace requires it); both documents are complete.
            self.assertTrue(Path(source).is_file())
            json.loads(Path(source).read_text())
            json.loads(Path(destination).read_text())
            real_replace(source, destination)

        with patch.object(auth.os, "replace", side_effect=inspect_replace) as replace:
            self.seed(new)
            auth._write_index(8, "arm64", "US", "Pv")
        self.assertEqual(2, replace.call_count)
        self.assertEqual(new, auth._load_pool("arm64", "US", "Pv"))
        for save in (lambda: self.seed(old),
                     lambda: auth._write_index(9, "arm64", "US", "Pv")):
            with self.subTest(save=save), patch.object(auth.os, "replace", side_effect=OSError("disk")):
                with self.assertRaises(OSError):
                    save()
            self.assertEqual(new, auth._load_pool("arm64", "US", "Pv"))
            self.assertEqual(8, auth._read_index("arm64", "US", "Pv"))
            self.assertEqual([], list(self.directory.glob("*.tmp")))
        def partial_write(data, stream):
            stream.write("[")
            raise OSError("disk full")

        with patch.object(auth.json, "dump", side_effect=partial_write):
            with self.assertRaises(OSError):
                self.seed(old)
        self.assertEqual(new, auth._load_pool("arm64", "US", "Pv"))
        self.assertEqual([], list(self.directory.glob("*.tmp")))
        with (patch.object(auth.os, "replace", side_effect=OSError("disk")),
              patch.object(auth, "fetch_token") as fetch):
            self.assertIsNone(auth.pick_pool_token(country="US", profile="Pv", size=1))
        fetch.assert_not_called()

    def test_advisory_busy_snapshot_is_unavailable_not_partial_or_blocking(self):
        warm = [token("other-warm-profile")]
        self.seed(warm, "D2")
        release = self.ctx.Event()
        worker = self.start(release=release)
        self.assertTrue(worker[2].wait(10))
        self.messages.reset_mock()
        try:
            with patch.object(auth, "fetch_token") as fetch:
                start = time.monotonic()
                self.assertEqual([], auth._load_pool("arm64", "US", "Pv"))
                self.assertEqual(warm, auth._load_pool("arm64", "US", "D2"))
                self.assertLess(time.monotonic() - start, 1)
                fetch.assert_not_called()
            self.messages.assert_not_called()
        finally:
            release.set()
        self.result(worker)
        self.assertEqual(1, len(auth._load_pool("arm64", "US", "Pv")))

    def test_direct_auth_does_not_create_lock_or_cache_directory(self):
        auth._CONFIG_DIR = self.directory / "must-not-exist"
        with (patch.object(auth, "direct_auth_enabled", return_value=True),
              patch.object(auth, "fetch_token", return_value=token("direct")),
              patch.object(auth, "_pool_path", side_effect=AssertionError("path touched"))):
            self.assertEqual(1, len(auth.ensure_pool(size=1)))
            self.assertIsNotNone(auth.pick_pool_token())
            self.assertIsNotNone(auth.replace_pool_token({}))
        self.assertFalse(auth._CONFIG_DIR.exists())


if __name__ == "__main__":
    unittest.main()
