import unittest
from unittest.mock import Mock, patch

from gpdl.auth import fetch_token


class FetchTokenRedactionTest(unittest.TestCase):
    @patch("gpdl.auth.get_priority_profiles")
    @patch("gpdl.auth.httpx.post")
    @patch("gpdl.auth.console.print")
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

    @patch("gpdl.auth.get_priority_profiles")
    @patch("gpdl.auth.httpx.post")
    @patch("gpdl.auth.console.print")
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
