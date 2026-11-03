import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from goopdl.api import (
    VersionUnavailableError,
    get_delivery,
    purchase,
    search_apps,
)


def _recording_get(sent: list[httpx.URL], status: int = 200):
    """Fake httpx.get that builds the real request URL, as httpx would send it."""

    def fake_get(url, *, params=None, **_kwargs):
        sent.append(httpx.Request("GET", url, params=params).url)
        return httpx.Response(status)

    return fake_get


class QueryEncodingTest(unittest.TestCase):
    def test_search_query_special_chars_are_encoded(self):
        sent: list[httpx.URL] = []
        with patch("goopdl.api.httpx.get", side_effect=_recording_get(sent)):
            search_apps("a&dtok=x#frag", {"authToken": "t"}, country="us")

        query = sent[0].query.decode()
        self.assertNotIn("&dtok=", query)
        self.assertNotIn("#", query)
        self.assertEqual(
            parse_qs(query), {"q": ["a&dtok=x#frag"], "c": ["3"], "gl": ["US"]}
        )

    def test_delivery_token_is_encoded(self):
        sent: list[httpx.URL] = []
        with (
            patch("goopdl.api.httpx.get", side_effect=_recording_get(sent, 404)),
            self.assertRaises(VersionUnavailableError),
        ):
            get_delivery(
                "com.example.app", 1, {"authToken": "t"}, delivery_token="a&gl=XX"
            )

        params = parse_qs(sent[0].query.decode())
        self.assertEqual(params["dtok"], ["a&gl=XX"])
        self.assertNotIn("gl", params)

    @patch("goopdl.api.httpx.post")
    def test_purchase_body_is_form_encoded(self, post):
        post.return_value = httpx.Response(204)
        purchase("com.example.app", 7, {"authToken": "t"})

        body = post.call_args.kwargs["content"]
        self.assertEqual(
            parse_qs(body), {"doc": ["com.example.app"], "ot": ["1"], "vc": ["7"]}
        )


if __name__ == "__main__":
    unittest.main()
