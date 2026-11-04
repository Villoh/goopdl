"""Busy pool lock: exponential poll backoff, capped, never past the deadline."""
import errno
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from _envutil import isolated_env
from _nonet import no_sockets

from goopdl import auth
from goopdl.errors import PoolBusyError

NATIVE = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
BUSY = errno.EACCES if os.name == "nt" else errno.EAGAIN


def acquire(path):
    handle = auth._acquire_file_lock(path)
    assert handle is not None
    return handle


class LockBackoffTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=tempfile.gettempdir()))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.lock = self.root / "c" / "test.lock"
        for patcher in (isolated_env({"GOOPDL_CONFIG_DIR": str(self.root / "c"),
                                      "HOME": str(self.root)}),
                        patch.object(socket.socket, "connect", no_sockets),
                        patch.object(auth, "logger")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_clock(self, busy_calls, *, jitter=False):
        """Native lock busy *busy_calls* times; sleep advances a fake monotonic clock."""
        now = [1000.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        calls = [0]

        def native(*args):
            calls[0] += 1
            if calls[0] <= busy_calls:
                raise OSError(BUSY, "busy")

        patches = [patch.object(auth.time, "monotonic", lambda: now[0]),
                   patch.object(auth.time, "sleep", side_effect=sleep),
                   patch(NATIVE, side_effect=native)]
        if not jitter:  # upper bound: the nominal backoff, deterministic
            patches.append(patch.object(auth._LOCK_POLL_JITTER, "uniform",
                                        side_effect=lambda low, high: high))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        return sleeps

    def test_sleeps_double_from_min_up_to_the_cap(self):
        sleeps = self.fake_clock(busy_calls=9)
        acquire(self.lock).close()
        self.assertEqual([0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.05, 0.05, 0.05],
                         [round(s, 6) for s in sleeps])

    def test_jitter_stays_within_half_and_full_backoff(self):
        sleeps = self.fake_clock(busy_calls=12, jitter=True)
        acquire(self.lock).close()
        nominal = [min(auth._LOCK_POLL_MIN * 2 ** i, auth._LOCK_POLL_MAX) for i in range(12)]
        self.assertEqual(12, len(sleeps))
        for got, high in zip(sleeps, nominal):
            self.assertTrue(high / 2 <= got <= high, (got, high))

    def test_constants_are_patchable(self):
        sleeps = self.fake_clock(busy_calls=4)
        with (patch.object(auth, "_LOCK_POLL_MIN", 0.01),
              patch.object(auth, "_LOCK_POLL_MAX", 0.03)):
            acquire(self.lock).close()
        self.assertEqual([0.01, 0.02, 0.03, 0.03], [round(s, 6) for s in sleeps])

    def test_last_sleep_is_cut_at_the_deadline_then_times_out(self):
        sleeps = self.fake_clock(busy_calls=10 ** 6)
        with patch.object(auth, "_POOL_LOCK_TIMEOUT", 0.1):
            with self.assertRaises(TimeoutError) as raised:
                auth._acquire_file_lock(self.lock)
        self.assertNotIsInstance(raised.exception, PoolBusyError)
        self.assertEqual([0.001, 0.002, 0.004, 0.008, 0.016, 0.032, 0.037],
                         [round(s, 6) for s in sleeps])
        self.assertAlmostEqual(0.1, sum(sleeps))

    def test_pool_lock_timeout_is_pool_busy_or_false(self):
        sleeps = self.fake_clock(busy_calls=10 ** 6)
        with patch.object(auth, "_POOL_LOCK_TIMEOUT", 0.2):
            with self.assertRaises(PoolBusyError):
                with auth._pool_lock("arm64", "US", "Pv", raise_on_error=True):
                    self.fail("acquired a busy lock")
            self.assertLessEqual(max(sleeps), auth._LOCK_POLL_MAX)
            sleeps.clear()
            with auth._pool_lock("arm64", "US", "Pv") as acquired:
                self.assertFalse(acquired)
            self.assertAlmostEqual(0.2, sum(sleeps))

    def test_non_blocking_never_sleeps(self):
        sleeps = self.fake_clock(busy_calls=10 ** 6)
        self.assertIsNone(auth._acquire_file_lock(self.lock, blocking=False))
        self.assertEqual([], sleeps)

    def test_real_thread_waits_for_a_quick_release(self):
        holder = acquire(self.lock)
        real_sleep = time.sleep
        sleeps, result = [], []

        def counting_sleep(seconds):
            sleeps.append(seconds)
            real_sleep(seconds)

        def waiter():
            started = time.monotonic()
            acquire(self.lock).close()
            result.append(time.monotonic() - started)

        with patch.object(auth.time, "sleep", side_effect=counting_sleep):
            thread = threading.Thread(target=waiter)
            thread.start()
            release = threading.Timer(0.1, holder.close)
            release.start()
            thread.join(10)
            release.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(result))
        self.assertLess(result[0], 5)
        # It really polled, starting short; a fixed 50 ms tick would be ~2 sleeps.
        self.assertGreaterEqual(len(sleeps), 3)
        self.assertLessEqual(sleeps[0], auth._LOCK_POLL_MIN)
        self.assertLessEqual(max(sleeps), auth._LOCK_POLL_MAX)


if __name__ == "__main__":
    unittest.main()
