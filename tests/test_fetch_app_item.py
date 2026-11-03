import unittest
from unittest.mock import patch

from goopdl import api
from goopdl.api import fetch_app_item


class FetchAppItemRetryTest(unittest.TestCase):
    def test_retry_uses_replacement_token(self) -> None:
        stale, fresh = {"authToken": "stale"}, {"authToken": "fresh"}
        with (
            patch.object(api, "pick_pool_token", return_value=stale) as pick,
            patch.object(api, "replace_pool_token", return_value=fresh),
            patch.object(
                api,
                "get_details_raw",
                side_effect=[api.AuthExpiredError("expired"), {}],
            ) as details,
        ):
            self.assertEqual({}, fetch_app_item("com.example.pkg"))
        pick.assert_called_once()
        self.assertIs(fresh, details.call_args_list[1].args[1])


if __name__ == "__main__":
    unittest.main()
