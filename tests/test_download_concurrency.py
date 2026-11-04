import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import cast

import httpx

from goopdl.download import DownloadSpec, _download_one, make_progress


class GatedStream:
    """Writes `first`, signals `started`, then waits for `release` before `rest`."""

    def __init__(self, first, rest, started, release):
        self.headers: dict[str, str] = {}
        self.first, self.rest = first, rest
        self.started, self.release = started, release

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, chunk_size):
        yield self.first
        self.started.set()
        await self.release.wait()
        yield self.rest


class ConcurrentSameDestTest(unittest.IsolatedAsyncioTestCase):
    async def test_second_download_does_not_corrupt_the_first(self):
        a_started, a_release = asyncio.Event(), asyncio.Event()
        b_started, b_release = asyncio.Event(), asyncio.Event()
        streams = iter(
            [
                GatedStream(b"AAAA", b"AAAA", a_started, a_release),
                GatedStream(b"BB", b"BB", b_started, b_release),
            ]
        )

        class Client:
            def stream(self, *_args, **_kwargs):
                return next(streams)

        client = cast(httpx.AsyncClient, Client())
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "base.apk"
            spec = DownloadSpec("https://example.invalid/base", dest, label="base.apk")

            async def run():
                return await _download_one(
                    spec, client, make_progress(), asyncio.Semaphore(1)
                )

            task_a = asyncio.create_task(run())
            await a_started.wait()
            task_b = asyncio.create_task(run())
            await b_started.wait()

            a_release.set()
            await task_a
            # A finished while B is still mid-write: dest must hold A's full bytes,
            # not B's partial file.
            self.assertEqual(dest.read_bytes(), b"AAAAAAAA")

            b_release.set()
            await task_b
            self.assertEqual(dest.read_bytes(), b"BBBB")
            self.assertEqual([p.name for p in Path(directory).iterdir()], ["base.apk"])


if __name__ == "__main__":
    unittest.main()
