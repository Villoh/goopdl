import unittest
from unittest.mock import Mock, patch

from goopdl.auth import fetch_token


class FetchTokenRedactionTest(unittest.TestCase):
    @patch("goopdl.auth.get_priority_profiles")
    @patch("goopdl.auth.httpx.post")
    @patch("goopdl.auth.console.print")
    def test_does_not_log_dispenser_response(
        self, console_print, httpx_post, get_priority_profiles
    ) -> None:
        get_priority_profiles.return_value = [("test", {"UserReadableName": "Test"})]
        httpx_post.return_value = Mock(
            status_code=200,
            json=Mock(return_value={"error": "secret response body"}),
        )

        self.assertIsNone(fetch_token())

        output = " ".join(str(call) for call in console_print.call_args_list)
        self.assertNotIn("secret response body", output)
        self.assertIn("No authToken in dispenser response", output)

    @patch("goopdl.auth.get_priority_profiles")
    @patch("goopdl.auth.httpx.post")
    @patch("goopdl.auth.console.print")
    def test_rate_limit_stops_profile_fallback_but_other_rejections_do_not(
        self, console_print, httpx_post, get_priority_profiles
    ) -> None:
        get_priority_profiles.return_value = [
            ("first", {"UserReadableName": "First"}),
            ("second", {"UserReadableName": "Second"}),
        ]
        for status, requests in ((429, 1), (403, 2)):
            with self.subTest(status=status):
                httpx_post.reset_mock()
                httpx_post.side_effect = [
                    Mock(status_code=status),
                    Mock(status_code=200, json=Mock(return_value={"authToken": "valid"})),
                ]
                result = fetch_token()
                self.assertEqual(httpx_post.call_count, requests)
                if status == 429:
                    self.assertIsNone(result)
                else:
                    self.assertEqual((result or {}).get("authToken"), "valid")

    @patch("goopdl.auth.get_priority_profiles")
    @patch("goopdl.auth.httpx.post")
    @patch("goopdl.auth.console.print")
    def test_does_not_log_exception_details(
        self, console_print, httpx_post, get_priority_profiles
    ) -> None:
        get_priority_profiles.return_value = [("test", {"UserReadableName": "Test"})]
        httpx_post.side_effect = RuntimeError("https://example.invalid/?token=secret")

        self.assertIsNone(fetch_token())

        output = " ".join(str(call) for call in console_print.call_args_list)
        self.assertNotIn("example.invalid", output)
        self.assertNotIn("token=secret", output)
        self.assertIn("RuntimeError", output)


if __name__ == "__main__":
    unittest.main()
