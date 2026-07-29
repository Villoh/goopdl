import unittest
from unittest.mock import patch

import httpx

from goopdl.api import get_delivery
from goopdl.cli import _parse_locales


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


class LanguageSplitTest(unittest.TestCase):
    def test_normalizes_locales_and_keeps_english(self):
        self.assertEqual(
            _parse_locales("de, fr,zh_CN,en-US"),
            ["en-US", "de", "fr", "zh-CN"],
        )
        self.assertIsNone(_parse_locales(None))

    @patch("goopdl.api.httpx.get")
    def test_sends_requested_languages_to_delivery(self, get):
        app_data = encoded_field(3, 2, b"https://play.googleapis.com/base")
        response = encoded_field(2, 2, app_data)
        raw = encoded_field(1, 2, encoded_field(21, 2, response))
        get.return_value = httpx.Response(200, content=raw)

        delivery = get_delivery(
            "pkg",
            1,
            {"authToken": "token"},
            locales=["en-US", "de", "fr"],
        )

        self.assertEqual(delivery.download_url, "https://play.googleapis.com/base")
        self.assertEqual(
            get.call_args.kwargs["headers"]["X-DFE-UserLanguages"],
            "en-US,de,fr",
        )


if __name__ == "__main__":
    unittest.main()
