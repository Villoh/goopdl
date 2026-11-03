"""Delivery extras must retain their advertised sizes without requiring digests."""

import unittest
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from goopdl import cli
from goopdl.api import AdditionalFile, AppDetails, DeliveryResult, _build_specs
from goopdl.download import IntegrityError, download_batch, download_files
from test_download_hardening import IsolatedEnvironment, digest


class ExtraSpecsAsyncTest(IsolatedEnvironment, unittest.IsolatedAsyncioTestCase):
    async def test_api_extra_sizes_prevent_publication(self):
        delivery = DeliveryResult(additional_files=[
            AdditionalFile(file_type=0, size=12, url="https://play.googleapis.com/obb"),
            AdditionalFile(file_type=2, size=12, url="https://play.googleapis.com/asset"),
        ])
        for spec in _build_specs("com.example.app", 1, self.root, delivery, True, False)[1:]:
            with self.subTest(extra=spec.dest.name):
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda _request: httpx.Response(200, content=b"obb")
                )) as client:
                    with self.assertRaisesRegex(IntegrityError, "size mismatch"):
                        await download_files([spec], require_integrity=True, client=client)
                self.assert_no_files()


class ExtraSpecsHardeningTest(IsolatedEnvironment):
    def delivery(self, size):
        return DeliveryResult(
            download_url="https://play.googleapis.com/apk", download_size=3, sha256=digest(b"apk"),
            additional_files=[
                AdditionalFile(file_type=0, size=size, url="https://play.googleapis.com/obb"),
                AdditionalFile(file_type=2, size=size, url="https://play.googleapis.com/asset"),
            ],
        )

    def test_api_propagates_positive_extra_sizes(self):
        for size in (12, 0, -1):
            with self.subTest(size=size):
                specs = _build_specs("com.example.app", 1, self.root, self.delivery(size), True, False)
                self.assertEqual([s.expected_size for s in specs[1:]], [max(size, 0)] * 2)
                self.assertTrue(all(not s.integrity_required for s in specs[1:]))

    def test_cli_propagates_extra_sizes_and_rejects_short_obb(self):
        real_client = httpx.AsyncClient
        seen = []

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"apk" if request.url.path == "/apk" else b"obb")
            ), **kwargs)

        def batch(specs, require_integrity=False):
            seen.extend(specs)
            return download_batch(specs, require_integrity=require_integrity)

        with (
            patch("goopdl.cli.pick_pool_token", return_value={"authToken": "t"}),
            patch("goopdl.cli._acquire_delivery", return_value=(
                AppDetails("com.example.app", version_string="1.0"), 1, self.delivery(12)
            )),
            patch("goopdl.cli.download_batch", batch),
            patch("goopdl.download.httpx.AsyncClient", client),
        ):
            result = CliRunner().invoke(cli.app, [
                "download", "com.example.app", "--extras", "--integrity-manifest",
                str(self.root / "manifest.json"), "-o", str(self.root)
            ])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("size mismatch", result.output)
        self.assertEqual([s.expected_size for s in seen[1:]], [12, 12])
        self.assertFalse(any(self.root.glob("*.obb")))
        self.assertFalse(any(self.root.glob("*.part")))


if __name__ == "__main__":
    unittest.main()
