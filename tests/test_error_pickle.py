"""Public errors must survive process boundaries without losing metadata."""
import multiprocessing
import os
import pickle
import unittest
from concurrent.futures import ProcessPoolExecutor
from unittest.mock import patch

from goopdl import errors


def worker_error():
    return errors.DispenserUnavailableError(503, reason="server")


@patch.dict(os.environ, {}, clear=True)
class PublicErrorPickleTest(unittest.TestCase):
    def test_all_public_errors_round_trip_with_attributes(self):
        special = {
            "DirectAuthError": (("checkin",), {"stage": "checkin", "status": 503, "retry_after": 5}),
            "DispenserUnavailableError": ((503,), {"reason": "custom-reason"}),
            "DispenserRateLimitedError": ((120,), {}),
            "PoolBusyError": ((16, "pool busy", "pool.lock"), {}),
            "RateLimitedError": (("limited",), {"retry_after": 120}),
            "DownloadError": (("cdn",), {"status_code": 503, "retryable": True, "retry_after": 5}),
        }
        for name in errors.__all__:
            cls = getattr(errors, name)
            args, kwargs = special.get(name, (("failure",), {}))
            if issubclass(cls, errors.PlayAPIError) and name != "RateLimitedError":
                kwargs = {"status_code": 502}
            original = cls(*args, **kwargs)
            with self.subTest(error=name):
                self.assertIsInstance(original, errors.GoopdlError)
                for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
                    with self.subTest(protocol=protocol):
                        # Trusted objects created above, never external pickle data.
                        restored = pickle.loads(pickle.dumps(original, protocol=protocol))  # noqa: S301
                        self.assertIs(type(restored), cls)
                        self.assertEqual(restored.args, original.args)
                        self.assertEqual(str(restored), str(original))
                        self.assertEqual(restored.__dict__, original.__dict__)
                        for attr in ("errno", "strerror", "filename", "filename2"):
                            if hasattr(original, attr):
                                self.assertEqual(getattr(restored, attr), getattr(original, attr))

    def test_real_process_pool_can_return_dispenser_error(self):
        with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
            restored = pool.submit(worker_error).result(timeout=20)
        self.assertIsInstance(restored, errors.DispenserUnavailableError)
        self.assertEqual(restored.status, 503)
        self.assertEqual(restored.reason, "server")
        self.assertIsNone(restored.stage)
        self.assertIsNone(restored.retry_after)


if __name__ == "__main__":
    unittest.main()
