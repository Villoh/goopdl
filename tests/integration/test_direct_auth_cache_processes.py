"""Cold processes sharing one config directory. Spawn is what Windows and macOS use."""
import multiprocessing
import os
import socket
from unittest.mock import patch

from _envutil import isolated_env
from _nonet import no_sockets
from goopdl import auth
from test_direct_auth_cache import AAS, EMAIL, DirectCacheCase, DirectFake


def _cold_process(config_dir, start, results):
    """One spawned process with a cold memory cache (see ColdProcessesTest)."""
    fake = DirectFake()
    with isolated_env({"GOOPDL_CONFIG_DIR": config_dir, "GOOPDL_ACCOUNT_EMAIL": EMAIL,
                       "GOOPDL_AAS_TOKEN": AAS}), \
            patch.object(socket.socket, "connect", no_sockets), \
            patch("goopdl._http.post", side_effect=fake.post), \
            patch("goopdl._http.get", side_effect=fake.get):
        start.wait(60)
        try:
            token = auth.pick_pool_token(profile="Pv", raise_on_error=True)
            results.put((fake.n("checkin"), fake.n("auth"), token["gsfId"], os.getpid()))
        except BaseException as exc:  # noqa: BLE001
            results.put(repr(exc))


class ColdProcessesTest(DirectCacheCase):
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
