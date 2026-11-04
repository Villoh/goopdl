"""An invisible app is not an unavailable requested version (gold: exit 1)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from typer.testing import CliRunner

from _envutil import isolated_env
from goopdl import auth, cli


class DownloadNotFoundTest(unittest.TestCase):
    def test_persistent_details_404_preserves_gold_counts_and_exit(self):
        with (
            isolated_env(),
            tempfile.TemporaryDirectory() as directory,
            patch.object(auth, "_CONFIG_DIR", Path(directory) / "cache", create=True),
            patch.object(httpx, "post", return_value=httpx.Response(
                200, json={"authToken": "token", "gsfId": "1"}
            )) as post,
            patch.object(httpx, "get", return_value=httpx.Response(404)) as get,
            patch("time.sleep") as sleep,
        ):
            result = CliRunner().invoke(cli.app, [
                "download", "com.example.pkg", "-o", str(Path(directory) / "out")
            ])
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(post.call_count, 6)
        self.assertEqual(get.call_count, 2)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
