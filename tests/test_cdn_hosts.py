"""The CDN host Play delivery URLs really redirect to must be downloadable.

Observed with a real anonymous download: play.googleapis.com/download/by-token
answers 302 and the file is served from rr<N>---sn-<id>.gvt1.com.
"""

import tempfile
import unittest
from pathlib import Path

import httpx

from goopdl.api import _safe_cdn_url
from goopdl.download import DownloadError, DownloadSpec, download_files

REAL_CDN = "rr4---sn-ajh5q5-5w.gvt1.com"
DELIVERY = "https://play.googleapis.com/download/by-token/download?token=T"


class CdnHostTest(unittest.IsolatedAsyncioTestCase):
    async def download(self, handler):
        with tempfile.TemporaryDirectory() as directory:
            dest = Path(directory) / "base.apk"
            client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler), follow_redirects=True
            )
            try:
                await download_files([DownloadSpec(DELIVERY, dest, label="base.apk")], client=client)
            finally:
                await client.aclose()
            return dest.read_bytes()

    async def test_follows_the_real_delivery_redirect_to_gvt1(self):
        def handler(request):
            if request.url.host == "play.googleapis.com":
                return httpx.Response(
                    302, headers={"Location": f"https://{REAL_CDN}/play-apps-download-default/x"}
                )
            return httpx.Response(200, content=b"APK")

        self.assertEqual(await self.download(handler), b"APK")

    async def test_lookalike_of_the_cdn_host_is_still_refused(self):
        for host in ("gvt1.com.attacker.invalid", "evilgvt1.com", "gvt1.com.evil.com"):
            with self.subTest(host=host):
                def handler(request, host=host):
                    return httpx.Response(302, headers={"Location": f"https://{host}/x"})

                with self.assertRaises(DownloadError):
                    await self.download(handler)


class SafeCdnUrlTest(unittest.TestCase):
    def test_accepts_gvt1_over_https_only(self):
        url = f"https://{REAL_CDN}/play-apps-download-default/x"
        self.assertEqual(_safe_cdn_url(url), url)
        self.assertEqual(_safe_cdn_url(url.replace("https://", "http://")), "")
        self.assertEqual(_safe_cdn_url("https://evilgvt1.com/x"), "")


if __name__ == "__main__":
    unittest.main()
