"""search_apps returns apps only: a non-app block in the results must not become a hit."""

import unittest
from unittest.mock import patch

import httpx

from goopdl import api, auth


def f(number, value):
    if isinstance(value, int):
        return auth._proto_varint(number, value)
    return auth._proto_bytes(number, value)


def app(package, title, creator="Dev"):
    return f(1, package) + f(5, title) + f(6, creator)


def play_pass_banner():
    # Seen in a real response: field 1 is a nested message (it starts with 0x0a and holds
    # a URL, so it has a dot and no space) and field 5 is a text.
    inner = (f(1, "fbtag;playpass") + f(2, "Play\u00a0Pass".encode()) + f(3, 0)
             + f(4, f(1, "https://play-lh.googleusercontent.com/abcDEF-123_xyz")))
    return f(1, inner) + f(5, "fbdim;playpass")


def response(*docs):
    return f(1, f(2, b"".join(f(1, doc) for doc in docs)))


class FindDocV2Test(unittest.TestCase):
    def test_a_banner_is_not_an_app(self):
        found = api._find_docv2(response(play_pass_banner(), app("com.google.android.calculator", "Calculator")))
        self.assertEqual([d["package"] for d in found], ["com.google.android.calculator"])

    def test_a_banner_alone_gives_nothing(self):
        self.assertEqual(api._find_docv2(response(play_pass_banner())), [])

    def test_real_package_names_are_kept(self):
        names = ["com.whatsapp", "org.videolan.vlc", "com.example.app_1", "io.github.user.App", "a.b"]
        # Real documents are much larger than 20 bytes, which the parser needs to descend into them.
        found = api._find_docv2(response(*[app(n, "A realistic application title") for n in names]))
        self.assertEqual([d["package"] for d in found], names)

    def test_text_that_is_not_a_package_is_dropped(self):
        for text in ("Play Pass", "not.a package", "1.2.3", "fbtag;playpass", "has.dot-and-dash", ".leading", "trailing."):
            with self.subTest(text=text):
                self.assertEqual(api._find_docv2(response(app(text, "Title"))), [])


class SearchAppsTest(unittest.TestCase):
    def test_search_skips_the_banner_and_keeps_order_and_limit(self):
        body = response(play_pass_banner(), app("com.a.one", "One"), play_pass_banner(),
                        app("com.a.two", "Two"), app("com.a.one", "One again"), app("com.a.three", "Three"))
        with patch("goopdl._http.get", return_value=httpx.Response(200, content=body)):
            hits = api.search_apps("q", {"authToken": "t"}, limit=2)
        self.assertEqual([h["package"] for h in hits], ["com.a.one", "com.a.two"])
        self.assertEqual([h["title"] for h in hits], ["One", "Two"])


if __name__ == "__main__":
    unittest.main()
