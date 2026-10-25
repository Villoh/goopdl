from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path, PureWindowsPath
from unittest.mock import patch

from typer.testing import CliRunner

from goopdl import auth
from goopdl.api import (
    AppDetails,
    AuthExpiredError,
    _acquire_single_token,
    fetch_app_item,
)
from goopdl.cli import _require_auth, app
from goopdl.profiles import find_profile


class ProfileTokenIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        for patcher in (
            patch.object(auth, "_CONFIG_DIR", self.directory),
            patch.dict(os.environ, {}, clear=True),
            patch.object(auth.console, "print"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def token(self, name: str) -> dict:
        return {"authToken": name, "gsfId": name, "_cached_at": time.time()}

    def seed_pool(self, profile: str | None, tokens: list[dict]) -> None:
        with patch.object(auth, "fetch_token", side_effect=tokens):
            auth.ensure_pool(country="US", profile=profile, size=len(tokens))

    def snapshot(self) -> dict:
        # Stable transaction sidecars are coordination state, not cached credentials.
        return {path.name: path.read_bytes() for path in self.directory.iterdir()
                if not (path.name.startswith("token-pool-") and path.suffix == ".lock")}

    def test_auth_clear_removes_all_token_caches_but_preserves_locks(self) -> None:
        for profile in (None, "Pv", "D2"):
            auth.save_auth(self.token("legacy"), country="US", profile=profile)
            self.seed_pool(profile, [self.token("pooled")])
            auth._write_index(2, "arm64", "US", profile)
        unrelated = self.directory / "preferences.json"
        unrelated.write_text('{"keep": true}')
        locks = {path.name: path.read_bytes() for path in self.directory.glob("*.lock")}
        result = CliRunner().invoke(app, ["auth", "--clear"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.snapshot(), {unrelated.name: unrelated.read_bytes()})
        self.assertEqual(
            locks, {path.name: path.read_bytes() for path in self.directory.glob("*.lock")}
        )

    def test_warm_default_and_other_profile_pool_are_not_reused(self) -> None:
        self.seed_pool(None, [self.token("legacy")])
        self.seed_pool("Pv", [self.token("pixel")])
        before = self.snapshot()
        samsung = self.token("samsung")
        with patch.object(auth, "fetch_token", return_value=samsung) as fetch:
            result = auth.ensure_pool(country="US", profile="D2", size=1)
        self.assertEqual([samsung], result)
        fetch.assert_called_once()
        self.assertEqual("D2", fetch.call_args.kwargs["profile"])
        for name, content in before.items():
            self.assertEqual(content, (self.directory / name).read_bytes())

    def test_round_robin_indexes_are_independent_and_persisted(self) -> None:
        for profile, prefix in ((None, "legacy"), ("Pv", "pixel"), ("D2", "samsung")):
            self.seed_pool(
                profile, [self.token(prefix + "0"), self.token(prefix + "1")]
            )
        with patch.object(auth, "fetch_token") as fetch:
            for profile, expected in (
                ("Pv", "pixel0"),
                ("Pv", "pixel1"),
                ("D2", "samsung0"),
                (None, "legacy0"),
                ("Pv", "pixel0"),
                ("D2", "samsung1"),
            ):
                with self.subTest(profile=profile, expected=expected):
                    result = auth.pick_pool_token(country="US", profile=profile, size=2)
                    assert result is not None
                    self.assertEqual(expected, result["authToken"])
        fetch.assert_not_called()
        indexes = [
            json.loads(path.read_text())["i"]
            for path in self.directory.glob("pool-index-*.json")
        ]
        self.assertEqual([1, 2, 3], sorted(indexes))

    def test_replacement_prunes_only_target_profile_pool(self) -> None:
        failed, survivor, expired = (
            self.token(name) for name in ("failed", "live", "old")
        )
        expired["_cached_at"] -= auth._MAX_TOKEN_AGE + 1
        self.seed_pool("Pv", [failed, survivor, expired])
        self.seed_pool("D2", [self.token("failed")])
        self.seed_pool(None, [self.token("failed")])
        before = self.snapshot()
        fresh = self.token("fresh")
        with patch.object(auth, "fetch_token", return_value=fresh) as fetch:
            self.assertEqual(
                fresh, auth.replace_pool_token(failed, country="US", profile="Pv")
            )
            fetch.assert_called_once()
            self.assertEqual("Pv", fetch.call_args.kwargs["profile"])
        changed = [
            name
            for name, content in before.items()
            if content != (self.directory / name).read_bytes()
        ]
        self.assertEqual(1, len(changed))
        self.assertEqual(
            [survivor, fresh], json.loads((self.directory / changed[0]).read_text())
        )
        self.assertEqual(3, len(before))

    def test_aliases_share_canonical_profile_pool_and_index(self) -> None:
        selected = find_profile("Pv", "arm64")
        assert selected is not None
        alias = selected[1]["UserReadableName"]
        self.seed_pool("Pv", [self.token("pixel0"), self.token("pixel1")])
        with patch.object(auth, "fetch_token") as fetch:
            first = auth.pick_pool_token(country="US", profile=alias, size=2)
            second = auth.pick_pool_token(country="US", profile="Pv", size=2)
        assert first is not None and second is not None
        self.assertEqual("pixel0", first["authToken"])
        self.assertEqual("pixel1", second["authToken"])
        fetch.assert_not_called()
        self.assertEqual(1, len(list(self.directory.glob("token-pool-*.json"))))
        self.assertEqual(1, len(list(self.directory.glob("pool-index-*.json"))))

    def test_ensure_auth_isolates_profile_cache_and_force_refresh(self) -> None:
        legacy, pixel, samsung, refreshed = (
            self.token(name) for name in ("legacy", "pixel", "samsung", "refreshed")
        )
        auth.save_auth(legacy, country="US")
        with patch.object(
            auth, "fetch_token", side_effect=[pixel, samsung, refreshed]
        ) as fetch:
            self.assertEqual(pixel, auth.ensure_auth(country="US", profile="Pv"))
            self.assertEqual(samsung, auth.ensure_auth(country="US", profile="D2"))
            self.assertEqual(
                refreshed,
                auth.ensure_auth(country="US", profile="Pv", force_refresh=True),
            )
            self.assertEqual(3, fetch.call_count)
        selected = find_profile("Pv", "arm64")
        assert selected is not None
        with patch.object(auth, "fetch_token") as fetch:
            self.assertEqual(
                refreshed,
                auth.ensure_auth(country="US", profile=selected[1]["UserReadableName"]),
            )
            self.assertEqual(samsung, auth.load_cached_auth(country="US", profile="D2"))
            self.assertEqual(legacy, auth.ensure_auth(country="US"))
        fetch.assert_not_called()
        self.assertEqual(3, len(list(self.directory.glob("auth-*.json"))))

    def test_cli_auth_saves_only_explicit_profile_single_cache(self) -> None:
        legacy = self.token("legacy")
        auth.save_auth(legacy, country="US")
        pixel = self.token("pixel")
        with patch("goopdl.cli.fetch_token", return_value=pixel):
            result = CliRunner().invoke(
                app, ["auth", "--country", "US", "--profile", "Pv"]
            )
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(legacy, auth.load_cached_auth(country="US"))
        self.assertEqual(pixel, auth.load_cached_auth(country="US", profile="Pv"))

    def test_fast_single_token_and_require_auth_honor_profile(self) -> None:
        self.seed_pool(None, [self.token("legacy") for _ in range(5)])
        self.seed_pool("Pv", [self.token("pixel") for _ in range(5)])
        samsung = self.token("samsung")
        with patch.object(
            auth, "fetch_token", side_effect=lambda **kwargs: samsung.copy()
        ) as fetch:
            result = _acquire_single_token("arm64", "US", None, None, "D2")
            assert result is not None
            self.assertEqual("samsung", result["authToken"])
            result = _require_auth("arm64", None, country="US", profile="D2")
            self.assertEqual("samsung", result["authToken"])
        self.assertEqual(5, fetch.call_count)

    def test_expired_raw_details_forwards_profile_to_replacement(self) -> None:
        with (
            patch("goopdl.api.pick_pool_token", return_value=self.token("pixel")),
            patch(
                "goopdl.api.get_details_raw",
                side_effect=[AuthExpiredError("expired"), {}],
            ),
            patch("goopdl.api.replace_pool_token") as replace,
        ):
            fetch_app_item("example.app", country="US", profile="Pv")
        self.assertEqual("Pv", replace.call_args.kwargs.get("profile"))

    def test_expired_cli_info_forwards_profile_to_replacement(self) -> None:
        with (
            patch("goopdl.cli._require_auth", return_value=self.token("pixel")),
            patch(
                "goopdl.cli.get_details",
                side_effect=[AuthExpiredError("expired"), AppDetails("example.app")],
            ),
            patch("goopdl.cli.replace_pool_token") as replace,
        ):
            result = CliRunner().invoke(
                app, ["info", "example.app", "--country", "US", "--profile", "Pv"]
            )
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual("Pv", replace.call_args.kwargs.get("profile"))

    def test_legacy_paths_are_unchanged(self) -> None:
        for country, suffix in ((None, ""), ("us", "-US")):
            self.assertEqual(
                self.directory / f"auth-arm64{suffix}.json",
                auth._auth_path("arm64", country),
            )
            self.assertEqual(
                self.directory / f"token-pool-arm64{suffix}.json",
                auth._pool_path("arm64", country),
            )
            self.assertEqual(
                self.directory / f"pool-index-arm64{suffix}.json",
                auth._index_path("arm64", country),
            )

    def test_unsafe_profile_keys_are_rejected_before_cache_io(self) -> None:
        for profile in ("../../escape", "a/b", "a\\b", "..", "\x00"):
            for helper in (auth.ensure_pool, auth.pick_pool_token, auth.ensure_auth):
                with self.subTest(profile=profile, helper=helper.__name__):
                    with patch.object(auth, "fetch_token", return_value=None) as fetch:
                        with self.assertRaises(ValueError):
                            helper(country="US", profile=profile)
                    fetch.assert_not_called()
        # Also validate a canonical key, not just the user's alias.
        with patch.object(auth, "find_profile", return_value=("../escape", {})):
            with self.assertRaises(ValueError):
                auth.ensure_pool(profile="alias")
        self.assertEqual({}, self.snapshot())

    def test_unknown_keys_cannot_alias_valid_profile_caches_on_windows(self) -> None:
        self.assertIsNone(find_profile("pv", "arm64"))
        for helper in (auth._auth_path, auth._pool_path, auth._index_path):
            with self.subTest(helper=helper.__name__):
                valid = PureWindowsPath(helper("arm64", "US", "Pv"))
                unknown = PureWindowsPath(helper("arm64", "US", "pv"))
                self.assertNotEqual(valid, unknown)

    def test_unknown_profile_does_not_fall_back_to_legacy_pool(self) -> None:
        self.seed_pool(None, [self.token("legacy")])
        with patch.object(auth, "httpx") as http:
            self.assertEqual(
                [], auth.ensure_pool(country="US", profile="unknown-profile", size=1)
            )
        http.post.assert_not_called()

    def test_direct_auth_bypasses_profile_cache_and_indexes(self) -> None:
        self.seed_pool(None, [self.token("legacy")])
        self.seed_pool("Pv", [self.token("pixel")])
        before = self.snapshot()
        direct = self.token("direct")
        with (
            patch.dict(
                os.environ,
                {
                    "GOOPDL_ACCOUNT_EMAIL": "a@example.test",
                    "GOOPDL_AAS_TOKEN": "aas_et/secret",
                },
            ),
            patch.object(auth, "fetch_token", return_value=direct) as fetch,
            patch.object(
                auth, "_pool_path", side_effect=AssertionError("cache path used")
            ),
            patch.object(
                auth, "_index_path", side_effect=AssertionError("cache path used")
            ),
            patch.object(
                auth, "_auth_path", side_effect=AssertionError("cache path used")
            ),
        ):
            self.assertEqual([direct, direct], auth.ensure_pool(profile="Pv", size=2))
            self.assertIs(direct, auth.pick_pool_token(profile="Pv"))
            self.assertIs(direct, auth.replace_pool_token({}, profile="Pv"))
            self.assertIs(direct, auth.ensure_auth(profile="Pv"))
        self.assertEqual(5, fetch.call_count)
        self.assertTrue(
            all(call.kwargs["profile"] == "Pv" for call in fetch.call_args_list)
        )
        self.assertEqual(before, self.snapshot())


if __name__ == "__main__":
    unittest.main()
