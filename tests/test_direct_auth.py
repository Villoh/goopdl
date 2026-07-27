import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from gplaydl.api import PlayAPIError, get_delivery
from gplaydl.auth import (
    DirectAuthConfigurationError,
    DirectAuthError,
    build_headers,
    direct_auth_enabled,
    ensure_auth,
    fetch_token,
    pick_pool_token,
    replace_pool_token,
)
from gplaydl.cli import app


def varint(value: int) -> bytes:
    output = bytearray()
    while value > 0x7F:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def int_field(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def field(number: int, value: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(value)) + value


PROFILE = {
    "Build.BOOTLOADER": "boot",
    "Build.BRAND": "google",
    "Build.DEVICE": "device",
    "Build.FINGERPRINT": "fingerprint",
    "Build.HARDWARE": "hardware",
    "Build.ID": "build",
    "Build.MANUFACTURER": "Google",
    "Build.MODEL": "Pixel",
    "Build.PRODUCT": "product",
    "Build.RADIO": "radio",
    "Build.VERSION.RELEASE": "14",
    "Build.VERSION.SDK_INT": "34",
    "CellOperator": "310260",
    "Client": "android-google",
    "Features": "feature.one",
    "GL.Extensions": "extension.one",
    "GL.Version": "196610",
    "GSF.version": "240913000",
    "HasFiveWayNavigation": "false",
    "HasHardKeyboard": "false",
    "Keyboard": "1",
    "Locales": "en_US",
    "Navigation": "1",
    "Platforms": "arm64-v8a,armeabi-v7a",
    "Roaming": "mobile-notroaming",
    "Screen.Density": "420",
    "Screen.Height": "2400",
    "Screen.Width": "1080",
    "ScreenLayout": "2",
    "SharedLibraries": "library.one",
    "SimOperator": "310260",
    "TimeZone": "UTC",
    "TouchScreen": "3",
    "Vending.version": "84122900",
    "Vending.versionString": "41.2.29-23 [0] [PR] 639844241",
}


class DirectAuthTest(unittest.TestCase):
    @patch("gplaydl.api.httpx.get")
    def test_delivery_confirms_requested_version_code(self, get: Mock) -> None:
        app_delivery = field(3, b"https://android.clients.google.com/download") + int_field(
            29, 1561052632
        )
        get.return_value = Mock(
            status_code=200,
            content=field(1, field(21, field(2, app_delivery))),
        )
        auth = {"authToken": "temporary", "gsfId": "1234"}
        result = get_delivery("com.example.app", 1561052632, auth)
        self.assertEqual(result.version_code, 1561052632)
        with self.assertRaisesRegex(PlayAPIError, "version code mismatch"):
            get_delivery("com.example.app", 1561052633, auth)

    def test_environment_requires_both_values(self) -> None:
        cases = [
            ({}, False, None),
            ({"GPLAYDL_ACCOUNT_EMAIL": "", "GPLAYDL_AAS_TOKEN": ""}, False, None),
            (
                {"GPLAYDL_ACCOUNT_EMAIL": "account@example.test"},
                None,
                "GPLAYDL_AAS_TOKEN",
            ),
            ({"GPLAYDL_AAS_TOKEN": "secret-token"}, None, "GPLAYDL_ACCOUNT_EMAIL"),
            (
                {
                    "GPLAYDL_ACCOUNT_EMAIL": "account@example.test",
                    "GPLAYDL_AAS_TOKEN": "g.a000temporary",
                },
                None,
                "must start with aas_et/",
            ),
        ]
        for environment, enabled, missing in cases:
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, environment, clear=True),
            ):
                if missing:
                    with self.assertRaisesRegex(DirectAuthConfigurationError, missing):
                        direct_auth_enabled()
                else:
                    self.assertEqual(enabled, direct_auth_enabled())

    @patch("gplaydl.auth.time.time", return_value=1_700_000_000)
    @patch("gplaydl.auth.get_priority_profiles", return_value=[("test", PROFILE)])
    @patch("gplaydl.auth.httpx.get")
    @patch("gplaydl.auth.httpx.post")
    def test_direct_google_sequence_matches_aurora_aas_protocol(
        self, post: Mock, get: Mock, _profiles: Mock, _time: Mock
    ) -> None:
        checkin_response = (
            varint((7 << 3) | 1) + struct.pack("<Q", 0x1234) + field(12, b"consistency")
        )
        upload_response = field(1, field(28, field(1, b"config-token")))
        toc_response = field(1, field(6, field(22, b"cookie")))
        post.side_effect = [
            Mock(status_code=200, content=checkin_response),
            Mock(status_code=200, content=upload_response),
            Mock(status_code=200, text="Auth=temporary-bearer\n"),
        ]
        get.return_value = Mock(status_code=200, content=toc_response)

        with patch.dict(
            os.environ,
            {
                "GPLAYDL_ACCOUNT_EMAIL": "account@example.test",
                "GPLAYDL_AAS_TOKEN": "aas_et/persistent-secret",
            },
            clear=True,
        ):
            bundle = fetch_token()

        device_config = b"".join(
            (
                int_field(1, 3),
                int_field(2, 1),
                int_field(3, 1),
                int_field(4, 2),
                int_field(7, 420),
                int_field(8, 196610),
                int_field(12, 1080),
                int_field(13, 2400),
                int_field(5, 0),
                int_field(6, 0),
                int_field(19, 0),
                field(9, b"library.one"),
                field(10, b"feature.one"),
                field(11, b"arm64-v8a"),
                field(11, b"armeabi-v7a"),
                field(14, b"en_US"),
                field(15, b"extension.one"),
                field(26, field(1, b"feature.one") + int_field(2, 0)),
                int_field(16, 0),
            )
        )
        build = b"".join(
            (
                field(1, b"fingerprint"),
                field(2, b"hardware"),
                field(3, b"google"),
                field(4, b"radio"),
                field(5, b"boot"),
                field(6, b"android-google"),
                int_field(7, 1_700_000_000),
                int_field(8, 240913000),
                field(9, b"device"),
                field(11, b"Pixel"),
                field(12, b"Google"),
                field(13, b"product"),
                int_field(10, 34),
                int_field(14, 0),
            )
        )
        checkin_proto = b"".join(
            (
                field(1, build),
                int_field(2, 0),
                field(6, b"310260"),
                field(7, b"310260"),
                field(8, b"mobile-notroaming"),
                int_field(9, 0),
            )
        )
        expected_checkin = b"".join(
            (
                int_field(2, 0),
                field(4, checkin_proto),
                field(6, b"en_US"),
                field(12, b"UTC"),
                int_field(14, 3),
                field(18, device_config),
                int_field(20, 0),
            )
        )
        finsky_user_agent = (
            "Android-Finsky/41.2.29-23 [0] [PR] 639844241 "
            "(api=3,versionCode=84122900,sdk=34,device=device,hardware=hardware,"
            "product=product,platformVersionRelease=14,model=Pixel,buildId=build,"
            "isWideScreen=0,supportedAbis=arm64-v8a;armeabi-v7a)"
        )
        auth_user_agent = "GoogleAuth/1.4 (device build)"

        checkin_call, upload_call, auth_call = post.call_args_list
        self.assertEqual(
            "https://android.clients.google.com/checkin", checkin_call.args[0]
        )
        self.assertEqual(expected_checkin, checkin_call.kwargs["content"])
        self.assertEqual(
            {
                "app": "com.google.android.gms",
                "Content-Type": "application/x-protobuffer",
                "Host": "android.clients.google.com",
                "User-Agent": auth_user_agent,
            },
            checkin_call.kwargs["headers"],
        )

        partial_bundle = {
            "authToken": "",
            "gsfId": "1234",
            "deviceCheckInConsistencyToken": "consistency",
            "deviceConfigToken": "",
            "dfeCookie": "",
            "deviceInfoProvider": {
                "userAgentString": finsky_user_agent,
                "mccMnc": "310260",
            },
        }
        upload_headers = build_headers(partial_bundle)
        upload_headers.pop("Authorization")
        upload_headers["Content-Type"] = "application/x-protobuf"
        self.assertEqual(
            "https://android.clients.google.com/fdfe/uploadDeviceConfig",
            upload_call.args[0],
        )
        self.assertEqual(field(1, device_config), upload_call.kwargs["content"])
        self.assertEqual(upload_headers, upload_call.kwargs["headers"])
        self.assertNotIn("X-DFE-Device-Config-Token", upload_call.kwargs["headers"])

        self.assertEqual("https://android.clients.google.com/auth", auth_call.args[0])
        self.assertEqual(
            {
                "Email": "account@example.test",
                "Token": "aas_et/persistent-secret",
                "service": "oauth2:https://www.googleapis.com/auth/googleplay",
                "app": "com.android.vending",
                "client_sig": "38918a453d07199354f8b19af05ec6562ced5788",
                "callerPkg": "com.google.android.gms",
                "callerSig": "38918a453d07199354f8b19af05ec6562ced5788",
                "androidId": "1234",
                "google_play_services_version": "240913000",
                "sdk_version": "34",
                "device_country": "us",
                "lang": "en",
                "oauth2_foreground": "1",
                "token_request_options": "CAA4AVAB",
                "check_email": "1",
                "system_partition": "1",
                "droidguard_results": "null",
            },
            auth_call.kwargs["data"],
        )
        self.assertEqual(
            {
                "app": "com.google.android.gms",
                "device": "1234",
                "User-Agent": auth_user_agent,
            },
            auth_call.kwargs["headers"],
        )

        expected_bundle = {
            **partial_bundle,
            "authToken": "temporary-bearer",
            "deviceConfigToken": "config-token",
            "dfeCookie": "cookie",
        }
        self.assertEqual(expected_bundle, bundle)
        self.assertEqual(
            "https://android.clients.google.com/fdfe/toc", get.call_args.args[0]
        )
        toc_headers = build_headers({**expected_bundle, "dfeCookie": ""})
        self.assertEqual(toc_headers, get.call_args.kwargs["headers"])
        self.assertEqual("Bearer temporary-bearer", toc_headers["Authorization"])
        self.assertEqual("config-token", toc_headers["X-DFE-Device-Config-Token"])
        self.assertEqual(
            "consistency", toc_headers["X-DFE-Device-Checkin-Consistency-Token"]
        )
        self.assertNotIn("account@example.test", repr(bundle))
        self.assertNotIn("persistent-secret", repr(bundle))

        for call in (*post.call_args_list, get.call_args):
            self.assertEqual(30, call.kwargs["timeout"])
            self.assertIsNone(call.kwargs["proxy"])

        post.side_effect = [
            Mock(status_code=200, content=checkin_response),
            Mock(status_code=200, content=upload_response),
            Mock(status_code=200, text="Auth=temporary-bearer\n"),
        ]
        get.return_value = Mock(status_code=503, content=b"")
        with patch.dict(
            os.environ,
            {
                "GPLAYDL_ACCOUNT_EMAIL": "account@example.test",
                "GPLAYDL_AAS_TOKEN": "aas_et/persistent-secret",
            },
            clear=True,
        ):
            bundle_without_toc = fetch_token()
        assert bundle_without_toc is not None
        self.assertEqual("", bundle_without_toc["dfeCookie"])

        post.side_effect = [
            Mock(status_code=200, content=checkin_response),
            Mock(status_code=200, content=upload_response),
            Mock(
                status_code=403,
                text="Error=BadAuthentication\nSecret=must-not-be-logged\n",
            ),
        ]
        with (
            patch.dict(
                os.environ,
                {
                    "GPLAYDL_ACCOUNT_EMAIL": "account@example.test",
                    "GPLAYDL_AAS_TOKEN": "aas_et/persistent-secret",
                },
                clear=True,
            ),
            patch("gplaydl.auth.console.print") as console_print,
        ):
            self.assertIsNone(fetch_token())
        output = " ".join(str(call) for call in console_print.call_args_list)
        self.assertIn("HTTP 403, BadAuthentication", output)
        self.assertNotIn("must-not-be-logged", output)

    @patch(
        "gplaydl.auth._direct_auth",
        side_effect=DirectAuthError("checkin (HTTP 403)"),
    )
    @patch("gplaydl.auth.console.print")
    def test_direct_failure_reports_safe_stage(
        self, console_print: Mock, _auth: Mock
    ) -> None:
        with patch.dict(
            os.environ,
            {
                "GPLAYDL_ACCOUNT_EMAIL": "account@example.test",
                "GPLAYDL_AAS_TOKEN": "aas_et/persistent-secret",
            },
            clear=True,
        ):
            self.assertIsNone(fetch_token())

        output = " ".join(str(call) for call in console_print.call_args_list)
        self.assertIn("checkin (HTTP 403)", output)
        self.assertNotIn("account@example.test", output)
        self.assertNotIn("persistent-secret", output)

    def test_direct_mode_bypasses_all_persistence(self) -> None:
        token = {"authToken": "temporary"}
        environment = {
            "GPLAYDL_ACCOUNT_EMAIL": "account@example.test",
            "GPLAYDL_AAS_TOKEN": "aas_et/persistent-secret",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("gplaydl.auth.fetch_token", return_value=token) as fetch,
            patch("gplaydl.auth._load_pool") as load_pool,
            patch("gplaydl.auth._save_pool") as save_pool,
            patch("gplaydl.auth._read_index") as read_index,
            patch("gplaydl.auth._write_index") as write_index,
            patch("gplaydl.auth.load_cached_auth") as load_auth,
            patch("gplaydl.auth.save_auth") as save_auth,
        ):
            self.assertIs(token, pick_pool_token())
            self.assertIs(token, replace_pool_token({}))
            self.assertIs(token, ensure_auth())

        self.assertEqual(3, fetch.call_count)
        for mocked in (
            load_pool,
            save_pool,
            read_index,
            write_index,
            load_auth,
            save_auth,
        ):
            mocked.assert_not_called()

    def test_cli_rejects_partial_env_before_creating_output(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                os.environ,
                {"GPLAYDL_ACCOUNT_EMAIL": "account@example.test"},
                clear=True,
            ),
            patch("gplaydl.cli.pick_pool_token") as pick,
        ):
            output = Path(directory) / "new-output"
            result = CliRunner().invoke(
                app, ["download", "example.package", "--output", str(output)]
            )

        self.assertEqual(2, result.exit_code)
        self.assertIn("GPLAYDL_AAS_TOKEN", result.output)
        self.assertNotIn("account@example.test", result.output)
        self.assertFalse(output.exists())
        pick.assert_not_called()

    def test_cli_auth_does_not_save_or_identify_direct_credentials(self) -> None:
        environment = {
            "GPLAYDL_ACCOUNT_EMAIL": "account@example.test",
            "GPLAYDL_AAS_TOKEN": "aas_et/persistent-secret",
        }
        bundle = {"authToken": "temporary", "gsfId": "secret-gsf"}
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("gplaydl.cli.fetch_token", return_value=bundle),
            patch("gplaydl.cli.save_auth") as save_auth,
        ):
            result = CliRunner().invoke(app, ["auth"])

        self.assertEqual(0, result.exit_code)
        self.assertIn("Authenticated", result.output)
        for secret in (
            "account@example.test",
            "persistent-secret",
            "secret-gsf",
            "Saved",
        ):
            self.assertNotIn(secret, result.output)
        save_auth.assert_not_called()


if __name__ == "__main__":
    unittest.main()
