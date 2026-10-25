from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from goopdl import api, auth
from goopdl.profiles import get_latest_probe_profiles


class WarmDownloadProfileTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.output = self.directory / "output"
        self.candidates = get_latest_probe_profiles("arm64", n=2)
        self.a, self.b = (candidate[0] for candidate in self.candidates)
        self.details = api.AppDetails("example.app", version_code=42, version_string="1.2.3")
        self.fresh = self.token("fresh")
        for patcher in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(auth, "_CONFIG_DIR", self.directory / "cache"),
            patch.object(auth.console, "print"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        fetch = patch.object(auth, "fetch_token", return_value=self.fresh)
        self.fetch = fetch.start()
        self.addCleanup(fetch.stop)
        delivery = patch.object(
            api,
            "_fetch_latest_delivery",
            return_value=(self.details, api.DeliveryResult(download_url="https://example.test/app")),
        )
        self.delivery = delivery.start()
        self.addCleanup(delivery.stop)
        batch = patch.object(api, "download_batch")
        self.batch = batch.start()
        self.addCleanup(batch.stop)
        # The old random choice picks cold A; after filtering it must pick warm B.
        choice = patch.object(api.random, "choice", side_effect=lambda candidates: candidates[0])
        self.choice = choice.start()
        self.addCleanup(choice.stop)

    def token(self, name: str) -> dict:
        return {"authToken": name, "gsfId": name, "_cached_at": time.time()}

    def seed(
        self, profile: str | None, token: dict, arch: str = "arm64", country: str | None = "US"
    ) -> None:
        auth._save_pool([token], arch, country, profile)

    def download(self, profile: str | None = None) -> None:
        result = api.download_app("example.app", self.output, country="US", profile=profile)
        self.assertIsInstance(result, tuple)
        self.assertIsInstance(result[0], Path)
        self.assertIsInstance(result[1], str)
        self.assertEqual((self.output / "example.app-42.apk", "1.2.3"), result)
        self.delivery.assert_called_once()
        self.batch.assert_called_once()
        specs = self.batch.call_args.args[0]
        self.assertEqual(1, len(specs))
        self.assertEqual(result[0], specs[0].dest)

    def test_prefers_warm_b_over_random_cold_a_without_minting(self) -> None:
        warm = self.token("warm-b")
        self.seed(self.b, warm)
        self.download()
        self.fetch.assert_not_called()
        self.assertEqual(warm, self.delivery.call_args.args[1])
        self.choice.assert_called_once_with([self.candidates[1]])

    def test_expired_tokens_do_not_make_a_candidate_warm(self) -> None:
        expired = self.token("expired-b")
        expired["_cached_at"] -= auth._MAX_TOKEN_AGE + 1
        self.seed(self.b, expired)
        self.download()
        self.fetch.assert_called_once()
        self.assertEqual(self.a, self.fetch.call_args.kwargs["profile"])
        self.assertEqual(self.fresh, self.delivery.call_args.args[1])
        self.choice.assert_called_once_with(self.candidates)

    def test_wrong_scope_and_single_auth_cache_do_not_count_as_warm(self) -> None:
        other = get_latest_probe_profiles("arm64", n=3)[2][0]
        for profile, arch, country in (
            (self.b, "arm64", "GB"),
            (self.b, "x86_64", "US"),
            (other, "arm64", "US"),
            (None, "arm64", "US"),
            (self.b, "arm64", None),
        ):
            self.seed(profile, self.token("wrong-scope"), arch=arch, country=country)
        auth.save_auth(self.token("single-auth"), country="US", profile=self.b)
        self.download()
        self.fetch.assert_called_once()
        self.assertEqual(self.a, self.fetch.call_args.kwargs["profile"])
        self.assertEqual(self.fresh, self.delivery.call_args.args[1])
        self.choice.assert_called_once_with(self.candidates)

    def test_explicit_profile_overrides_warm_other_without_scanning(self) -> None:
        self.seed(self.b, self.token("warm-b"))
        pool_path = auth._pool_path

        def only_explicit(arch: str, country: str | None, profile: str | None = None) -> Path:
            self.assertEqual(self.a, profile, "scanned another profile's cache")
            return pool_path(arch, country, profile)

        with (
            patch.object(auth, "_pool_path", side_effect=only_explicit),
            patch.object(api, "get_latest_probe_profiles") as latest,
        ):
            self.download(profile=self.a)
        latest.assert_not_called()
        self.choice.assert_not_called()
        self.fetch.assert_called_once()
        self.assertEqual(self.a, self.fetch.call_args.kwargs["profile"])
        self.assertEqual(self.fresh, self.delivery.call_args.args[1])

    def test_direct_auth_bypasses_anonymous_cache_even_with_warm_other(self) -> None:
        self.seed(self.b, self.token("anonymous-b"))
        before = {path.name: path.read_bytes() for path in auth._CONFIG_DIR.iterdir()}
        with (
            patch.dict(
                os.environ,
                {"GOOPDL_ACCOUNT_EMAIL": "a@example.test", "GOOPDL_AAS_TOKEN": "aas_et/test"},
            ),
            patch.object(auth, "_pool_path", side_effect=AssertionError("anonymous pool read")),
            patch.object(auth, "_index_path", side_effect=AssertionError("anonymous index read")),
            patch.object(auth, "_auth_path", side_effect=AssertionError("anonymous auth read")),
        ):
            self.download()
        self.fetch.assert_called_once_with(arch="arm64", country="US", proxy=None, profile=self.a)
        self.assertEqual(self.fresh, self.delivery.call_args.args[1])
        self.choice.assert_called_once_with(self.candidates)
        self.assertEqual(before, {path.name: path.read_bytes() for path in auth._CONFIG_DIR.iterdir()})

    def test_both_cold_preserve_random_choice_one_mint_and_no_probing(self) -> None:
        self.choice.side_effect = lambda candidates: candidates[-1]
        self.download()
        self.choice.assert_called_once_with(self.candidates)
        self.fetch.assert_called_once_with(
            dispenser_url=None, arch="arm64", proxy=None, profile=self.b, country="US"
        )
        self.assertEqual(self.fresh, self.delivery.call_args.args[1])
        self.assertEqual(1, len(auth._load_pool("arm64", "US", self.b)))
        self.assertEqual([], auth._load_pool("arm64", "US", self.a))

    def test_both_warm_keep_random_choice_within_warm_candidates(self) -> None:
        warm_a, warm_b = self.token("warm-a"), self.token("warm-b")
        self.seed(self.a, warm_a)
        self.seed(self.b, warm_b)
        self.choice.side_effect = lambda candidates: candidates[-1]
        self.download()
        self.choice.assert_called_once_with(self.candidates)
        self.fetch.assert_not_called()
        self.assertEqual(warm_b, self.delivery.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
