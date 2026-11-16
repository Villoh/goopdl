import unittest
from unittest.mock import patch

from goopdl.api import AppDetails, AppNotSupportedError, DeliveryResult, SplitInfo
from goopdl.cli import _acquire_delivery, _parse_archs, _warn_abi_mismatch
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

    def test_armv7_pools_exclude_arm64_capable_profiles(self):
        # Issue #70: dual-ABI devices make Play serve config.arm64_v8a splits.
        for getter in (get_priority_profiles, get_compat_profiles):
            profiles = getter("armv7")
            self.assertTrue(profiles)
            for key, profile in profiles:
                self.assertNotIn("arm64-v8a", profile["Platforms"], key)

    def test_armv7_priority_prefers_modern_gj_and_compat_reaches_it(self):
        self.assertEqual(get_priority_profiles("armv7")[0][0], "Gj")
        # min_api 32 apps (Duolingo): compat fallback must reach an SDK>=32 profile.
        self.assertIn("Gj", [key for key, _ in get_compat_profiles("armv7")[:6]])

    def test_warns_only_when_abi_splits_do_not_fit_requested_arch(self):
        def warned(arch, *names):
            delivery = DeliveryResult(splits=[SplitInfo(n) for n in names])
            with patch("goopdl.cli.err") as err:
                _warn_abi_mismatch(delivery, arch)
            return err.print.called

        self.assertTrue(warned("armv7", "base", "config.arm64_v8a"))
        self.assertFalse(warned("armv7", "base", "config.armeabi_v7a"))
        self.assertFalse(warned("arm64", "config.armeabi_v7a"))  # 32-bit-only app
        self.assertFalse(warned("x86_64", "config.x86_64"))
        self.assertFalse(warned("armv7", "base", "config.xhdpi"))  # no ABI split
        self.assertFalse(warned("tv", "config.arm64_v8a"))

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
