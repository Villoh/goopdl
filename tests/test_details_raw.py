import json
import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from goopdl import cli
from goopdl.api import get_details_raw, parse_app_item


def message_field(number: int, value: bytes) -> bytes:
    # These small fixtures use single-byte tags and lengths.
    assert number < 16 and len(value) < 128
    return bytes([(number << 3) | 2, len(value)]) + value


def details_response(creator: bytes) -> bytes:
    # A valid nested message whose encoded bytes are also printable UTF-8.
    nested = message_field(4, b"a" * 32)
    doc = (
        message_field(1, b"com.example.app")
        + message_field(6, creator)
        + message_field(9, nested)
    )
    return message_field(1, message_field(2, message_field(4, doc)))


class RawDetailsTest(unittest.TestCase):
    def test_creator_is_text_without_flattening_printable_nested_messages(self):
        for creator in ("Xiaomi Inc.", "Google LLC", "開発者", ""):
            with self.subTest(creator=creator), patch(
                "goopdl.api._fetch_details_raw",
                return_value=details_response(creator.encode("utf-8")),
            ):
                raw = get_details_raw("com.example.app", {})
                doc = raw["1"]["2"]["4"]
                self.assertEqual(doc["6"], creator)
                self.assertEqual(doc["9"], {"4": "a" * 32})
                self.assertEqual(parse_app_item(raw)["developer"], creator)

    def test_raw_cli_preserves_the_developer_name(self):
        with (
            patch("goopdl.cli.direct_auth_enabled", return_value=False),
            patch("goopdl.api.pick_pool_token", return_value={"authToken": "dummy"}),
            patch(
                "goopdl.api._fetch_details_raw",
                return_value=details_response(b"Xiaomi Inc."),
            ),
        ):
            result = CliRunner().invoke(cli.app, ["info", "com.example.app", "--raw"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["developer"], "Xiaomi Inc.")

    def test_repeated_creator_and_unknown_fields_keep_their_order(self):
        doc = (
            message_field(6, b"Xiaomi Inc.")
            + message_field(6, b"Google LLC")
            + message_field(9, message_field(4, b"a" * 32))
            + message_field(9, message_field(4, b"b" * 32))
        )
        response = message_field(1, message_field(2, message_field(4, doc)))
        with patch("goopdl.api._fetch_details_raw", return_value=response):
            raw = get_details_raw("com.example.app", {})
        self.assertEqual(
            raw["1"]["2"]["4"],
            {
                "6": ["Xiaomi Inc.", "Google LLC"],
                "9": [{"4": "a" * 32}, {"4": "b" * 32}],
            },
        )

    def test_missing_creator_and_malformed_responses_are_unchanged(self):
        for response, expected in (
            (
                message_field(1, message_field(2, message_field(4, b"\x08\x01"))),
                {"1": {"2": {"4": {"1": 1}}}},
            ),
            (
                message_field(1, message_field(2, message_field(4, b"not a doc"))),
                {"1": {"2": {"4": "not a doc"}}},
            ),
            (
                bytes.fromhex("0a09120722053007320141"),
                {"1": {"2": {"4": {"6": [7, "A"]}}}},
            ),
            (
                bytes.fromhex("0a07120522033201ff"),
                {"1": {"2": {"4": {"6": "/w=="}}}},
            ),
            (b"\x0a\xff", {}),
        ):
            with self.subTest(response=response), patch(
                "goopdl.api._fetch_details_raw", return_value=response
            ):
                self.assertEqual(get_details_raw("com.example.app", {}), expected)

    def test_response_without_doc_is_unchanged(self):
        response = message_field(1, message_field(2, message_field(5, b"not found")))
        with patch("goopdl.api._fetch_details_raw", return_value=response):
            self.assertEqual(
                get_details_raw("com.example.app", {}),
                {"1": {"2": {"5": "not found"}}},
            )


if __name__ == "__main__":
    unittest.main()
