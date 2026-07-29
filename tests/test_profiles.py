import unittest
from unittest.mock import patch

from goopdl.api import AppDetails, AppNotSupportedError, DeliveryResult
from goopdl.cli import _acquire_delivery, _parse_archs
from goopdl.profiles import (
    VALID_ARCHS,
    get_compat_profiles,
    get_discovery_profiles,
    get_priority_profiles,
    is_tv_profile,
)


class ProfileSelectionTest(unittest.TestCase):
    def test_supports_tv_and_x86_device_types(self):
        self.assertIn("tv", VALID_ARCHS)
        self.assertIn("x86_64", VALID_ARCHS)
        self.assertTrue(get_priority_profiles("x86_64"))
        self.assertTrue(
            any(is_tv_profile(profile) for _, profile in get_priority_profiles("tv"))
        )

    def test_compatibility_profiles_prefer_lower_sdk_and_discovery_is_unique(self):
        compat = get_compat_profiles("arm64")
        sdks = [
            int(profile.get("Build.VERSION.SDK_INT", "99")) for _, profile in compat
        ]
        self.assertEqual(sdks, sorted(sdks))

        discovery = get_discovery_profiles("arm64")
        keys = [key for key, _ in discovery]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(any(is_tv_profile(profile) for _, profile in discovery))

    def test_parses_multiple_architectures_and_rejects_unknown_values(self):
        self.assertEqual(_parse_archs("arm64,armv7,arm64"), ["arm64", "armv7"])
        with self.assertRaisesRegex(Exception, "Unknown architecture"):
            _parse_archs("mips")

    @patch("goopdl.cli.fetch_token")
    @patch("goopdl.cli.get_delivery")
    @patch("goopdl.cli.purchase", return_value="")
    @patch("goopdl.cli.get_details")
    def test_retries_incompatible_delivery_with_compatible_profile(
        self, get_details, _purchase, get_delivery, fetch_token
    ):
        details = AppDetails("pkg", title="App", version_code=1)
        delivery = DeliveryResult(download_url="https://play.googleapis.com/base")
        get_details.return_value = details
        get_delivery.side_effect = [AppNotSupportedError("unsupported"), delivery]
        fetch_token.return_value = {"authToken": "retry"}

        result = _acquire_delivery(
            "pkg", 1, "arm64", {"authToken": "initial"}, None, None, None, None
        )

        self.assertEqual(result, (details, 1, delivery))
        self.assertTrue(fetch_token.call_args.kwargs["profile"])


if __name__ == "__main__":
    unittest.main()
