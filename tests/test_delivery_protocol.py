import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from goopdl.api import (
    AppNotPurchasedError,
    AppNotSupportedError,
    DeliveryResult,
    DexMetadata,
    SplitInfo,
    _build_specs,
    _extract_delivery_from_fields,
    get_delivery,
    purchase,
)


def varint(value):
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def encoded_field(number, wire_type, value):
    output = varint((number << 3) | wire_type)
    if wire_type == 0:
        return output + varint(value)
    return output + varint(len(value)) + value


class DeliveryProtocolTest(unittest.TestCase):
    def test_parses_compressed_base_splits_and_field_five_cookies(self):
        compressed_base = b"".join(
            [
                encoded_field(1, 0, 2),
                encoded_field(2, 0, 80),
                encoded_field(3, 2, b"https://play.googleapis.com/base.gz"),
            ]
        )
        compressed_split = b"".join(
            [
                encoded_field(1, 0, 2),
                encoded_field(2, 0, 40),
                encoded_field(3, 2, b"https://play.googleapis.com/split.gz"),
            ]
        )
        split = b"".join(
            [
                encoded_field(1, 2, b"config.arm64_v8a"),
                encoded_field(2, 0, 50),
                encoded_field(5, 2, b"https://play.googleapis.com/split"),
                encoded_field(8, 2, compressed_split),
            ]
        )
        cookie = b"".join(
            [encoded_field(1, 2, b"MarketDA"), encoded_field(2, 2, b"secret")]
        )

        delivery = _extract_delivery_from_fields(
            [
                (1, 0, 100),
                (3, 2, b"https://play.googleapis.com/base"),
                (5, 2, cookie),
                (15, 2, split),
                (18, 2, compressed_base),
            ]
        )

        self.assertEqual(delivery.gzipped_url, "https://play.googleapis.com/base.gz")
        self.assertEqual(delivery.gzipped_size, 80)
        self.assertEqual(delivery.cookies, [{"name": "MarketDA", "value": "secret"}])
        self.assertEqual(delivery.additional_files, [])
        self.assertEqual(
            delivery.splits[0].gzipped_url, "https://play.googleapis.com/split.gz"
        )
        self.assertEqual(delivery.splits[0].gzipped_size, 40)

    def test_parses_and_builds_optional_dex_metadata(self):
        dex = b"".join(
            [
                encoded_field(1, 0, 25),
                encoded_field(2, 2, b"D" * 43),
                encoded_field(3, 2, b"https://play.googleapis.com/base.dm"),
            ]
        )
        delivery = _extract_delivery_from_fields(
            [
                (1, 0, 100),
                (3, 2, b"https://play.googleapis.com/base"),
                (21, 2, dex),
            ]
        )

        self.assertEqual(
            delivery.dex_metadata,
            DexMetadata(
                url="https://play.googleapis.com/base.dm",
                size=25,
                sha256="D" * 43,
            ),
        )
        specs = _build_specs("pkg", 1, Path("."), delivery, True, True, dm=True)
        self.assertEqual(specs[-1].dest.name, "pkg-1.dm")
        self.assertEqual(specs[-1].expected_size, 25)
        self.assertEqual(specs[-1].sha256, "D" * 43)

    def test_specs_prefer_compressed_urls_but_verify_final_sizes(self):
        delivery = DeliveryResult(
            download_url="https://play.googleapis.com/base",
            download_size=100,
            gzipped_url="https://play.googleapis.com/base.gz",
            gzipped_size=80,
            sha256="A" * 43,
            splits=[
                SplitInfo(
                    "config.arm64_v8a",
                    url="https://play.googleapis.com/split",
                    size=50,
                    gzipped_url="https://play.googleapis.com/split.gz",
                    gzipped_size=40,
                    sha256="B" * 43,
                )
            ],
        )

        specs = _build_specs("pkg", 1, Path("."), delivery, False, True)

        self.assertEqual(
            (specs[0].url, specs[0].gzipped, specs[0].expected_size),
            (delivery.gzipped_url, True, 100),
        )
        self.assertEqual(
            (specs[1].url, specs[1].gzipped, specs[1].expected_size),
            (delivery.splits[0].gzipped_url, True, 50),
        )

    @patch("goopdl.api.httpx.post")
    def test_purchase_returns_delivery_token(self, post):
        token = encoded_field(1, 2, encoded_field(4, 2, encoded_field(55, 2, b"dtok")))
        post.return_value = httpx.Response(200, content=token)

        self.assertEqual(purchase("pkg", 1, {"authToken": "token"}), "dtok")

    @patch("goopdl.api.httpx.get")
    def test_delivery_token_is_sent_and_status_is_distinguished(self, get):
        unsupported = encoded_field(1, 2, encoded_field(21, 2, encoded_field(1, 0, 2)))
        get.return_value = httpx.Response(200, content=unsupported)
        with self.assertRaises(AppNotSupportedError):
            get_delivery("pkg", 1, {"authToken": "token"}, delivery_token="dtok")
        self.assertIn("dtok=dtok", get.call_args.args[0])

        not_purchased = encoded_field(
            1, 2, encoded_field(21, 2, encoded_field(1, 0, 3))
        )
        get.return_value = httpx.Response(200, content=not_purchased)
        with self.assertRaises(AppNotPurchasedError):
            get_delivery("pkg", 1, {"authToken": "token"})


if __name__ == "__main__":
    unittest.main()
