import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from goopdl.api import (
    AppNotPurchasedError,
    AppNotSupportedError,
    AuthExpiredError,
    DeliveryResult,
    DexMetadata,
    PlayAPIError,
    RateLimitedError,
    SplitInfo,
    VersionUnavailableError,
    _build_specs,
    _extract_delivery_from_fields,
    _parse_retry_after,
    get_delivery,
    get_details,
    list_splits,
    purchase,
    search_apps,
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
    @staticmethod
    def _successful_response() -> httpx.Response:
        url = encoded_field(3, 2, b"https://play.googleapis.com/base")
        payload = encoded_field(2, 2, url)
        return httpx.Response(
            200,
            content=encoded_field(1, 2, encoded_field(21, 2, payload)),
        )

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

        self.assertEqual(purchase("com.example.pkg", 1, {"authToken": "token"}), "dtok")

    @patch("goopdl.api.time.sleep")
    @patch("goopdl.api.httpx.get")
    def test_429_retries_and_recovers(self, get, sleep):
        get.side_effect = [
            httpx.Response(429, headers={"Retry-After": "2.5"}),
            self._successful_response(),
        ]

        result = get_delivery("com.example.pkg", 1, {"authToken": "token"})

        self.assertEqual(result.download_url, "https://play.googleapis.com/base")
        sleep.assert_called_once_with(2.5)
        self.assertEqual(get.call_count, 2)

    @patch("goopdl.api.time.sleep")
    @patch("goopdl.api.httpx.get")
    def test_repeated_429_stops_after_two_retries(self, get, sleep):
        get.side_effect = [httpx.Response(429) for _ in range(3)]

        with self.assertRaises(RateLimitedError) as raised:
            get_delivery("com.example.pkg", 1, {"authToken": "token"})

        self.assertEqual(raised.exception.status_code, 429)
        self.assertNotIsInstance(raised.exception, VersionUnavailableError)
        self.assertIsNone(raised.exception.retry_after)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_args_list[0].args, (1.0,))
        self.assertEqual(sleep.call_args_list[1].args, (3.0,))

    @patch("goopdl.api.time.sleep")
    @patch("goopdl.api.httpx.get")
    def test_invalid_retry_after_is_ignored(self, get, sleep):
        get.side_effect = [
            httpx.Response(429, headers={"Retry-After": "nan"}),
            httpx.Response(429, headers={"Retry-After": "not-a-number"}),
            httpx.Response(429, headers={"Retry-After": "-1"}),
        ]

        with self.assertRaises(RateLimitedError) as raised:
            get_delivery("com.example.pkg", 1, {"authToken": "token"})

        self.assertIsNone(raised.exception.retry_after)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 3.0])

    @patch("goopdl.api.time.sleep")
    @patch("goopdl.api.httpx.get")
    def test_long_retry_after_raises_without_sleeping_or_retrying(self, get, sleep):
        get.side_effect = [httpx.Response(429, headers={"Retry-After": "120"})] * 3

        with self.assertRaises(RateLimitedError) as raised:
            get_delivery("com.example.pkg", 1, {"authToken": "token"})

        self.assertEqual(raised.exception.retry_after, 120.0)
        sleep.assert_not_called()
        self.assertEqual(get.call_count, 1)

    @patch("goopdl.api.time.sleep")
    @patch("goopdl.api.httpx.get")
    def test_retry_after_never_shortens_the_backoff(self, get, sleep):
        past = format_datetime(
            datetime.now(timezone.utc) - timedelta(days=1), usegmt=True
        )
        get.side_effect = [
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(429, headers={"Retry-After": past}),
            httpx.Response(429, headers={"Retry-After": "0"}),
        ]

        with self.assertRaises(RateLimitedError) as raised:
            get_delivery("com.example.pkg", 1, {"authToken": "token"})

        self.assertEqual(raised.exception.retry_after, 0.0)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 3.0])

    @patch("goopdl.api.time.sleep")
    @patch("goopdl.api.httpx.get")
    def test_retry_after_longer_than_backoff_is_honoured(self, get, sleep):
        get.side_effect = [
            httpx.Response(429, headers={"Retry-After": "5"}),
            self._successful_response(),
        ]

        get_delivery("com.example.pkg", 1, {"authToken": "token"})

        sleep.assert_called_once_with(5.0)

    def test_retry_after_accepts_http_date(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=90)
        delay = _parse_retry_after({"Retry-After": format_datetime(when, usegmt=True)})
        assert delay is not None
        self.assertAlmostEqual(delay, 90, delta=2)

        past = format_datetime(when - timedelta(days=1), usegmt=True)
        self.assertEqual(_parse_retry_after({"Retry-After": past}), 0.0)

    @patch("goopdl.api.httpx.post")
    def test_purchase_raises_only_on_auth_and_throttling(self, post):
        post.return_value = httpx.Response(429, headers={"Retry-After": "7"})
        with self.assertRaises(RateLimitedError) as raised:
            purchase("com.example.pkg", 1, {"authToken": "token"})
        self.assertEqual(raised.exception.retry_after, 7.0)

        post.return_value = httpx.Response(401)
        with self.assertRaises(AuthExpiredError):
            purchase("com.example.pkg", 1, {"authToken": "token"})

    @patch("goopdl.api.httpx.post")
    def test_other_purchase_failures_leave_classification_to_delivery(self, post):
        for status in (204, 400, 402, 403, 404, 409, 410, 412, 500, 502, 503):
            with self.subTest(status=status):
                post.return_value = httpx.Response(status)
                self.assertEqual(
                    purchase("com.example.pkg", 1, {"authToken": "token"}), ""
                )

    @patch("goopdl.api.httpx.get")
    def test_details_and_search_raise_typed_errors(self, get):
        calls = {
            "details": lambda: get_details("com.example.pkg", {"authToken": "token"}),
            "search": lambda: search_apps("q", {"authToken": "token"}),
            "splits": lambda: list_splits("com.example.pkg", {"authToken": "token"}),
        }
        for name, call in calls.items():
            with self.subTest(name):
                get.return_value = httpx.Response(429, headers={"Retry-After": "120"})
                with self.assertRaises(RateLimitedError) as raised:
                    call()
                self.assertEqual(raised.exception.retry_after, 120.0)

                get.return_value = httpx.Response(503)
                with self.assertRaises(PlayAPIError) as raised:
                    call()
                self.assertNotIsInstance(raised.exception, RateLimitedError)
                self.assertEqual(raised.exception.status_code, 503)

    def test_play_api_error_status_code_is_optional(self):
        self.assertIsNone(PlayAPIError("boom").status_code)
        self.assertEqual(RateLimitedError("slow").status_code, 429)

    @patch("goopdl.api.httpx.get")
    def test_401_404_and_other_statuses_keep_their_error_types(self, get):
        get.return_value = httpx.Response(401)
        with self.assertRaises(AuthExpiredError):
            get_delivery("com.example.pkg", 1, {"authToken": "token"})

        get.return_value = httpx.Response(404)
        with self.assertRaises(VersionUnavailableError):
            get_delivery("com.example.pkg", 1, {"authToken": "token"})

        get.return_value = httpx.Response(503)
        with self.assertRaises(PlayAPIError) as raised:
            get_delivery("com.example.pkg", 1, {"authToken": "token"})
        self.assertNotIsInstance(raised.exception, RateLimitedError)

    @patch("goopdl.api.httpx.get")
    def test_delivery_token_is_sent_and_status_is_distinguished(self, get):
        unsupported = encoded_field(1, 2, encoded_field(21, 2, encoded_field(1, 0, 2)))
        get.return_value = httpx.Response(200, content=unsupported)
        with self.assertRaises(AppNotSupportedError):
            get_delivery("com.example.pkg", 1, {"authToken": "token"}, delivery_token="dtok")
        self.assertEqual(get.call_args.kwargs["params"]["dtok"], "dtok")

        not_purchased = encoded_field(
            1, 2, encoded_field(21, 2, encoded_field(1, 0, 3))
        )
        get.return_value = httpx.Response(200, content=not_purchased)
        with self.assertRaises(AppNotPurchasedError):
            get_delivery("com.example.pkg", 1, {"authToken": "token"})


if __name__ == "__main__":
    unittest.main()
