import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import check_updates


class CheckUpdatesTest(unittest.TestCase):
    def test_accept_pending_promotes_only_successful_artists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_file = root / "source_hashes.json"
            pending_file = root / "source_hashes.pending.json"
            cache_file.write_text(
                json.dumps({"ado": {"https://old": {"hash": "old"}}})
            )
            pending_file.write_text(
                json.dumps(
                    {
                        "ado": {"https://new": {"hash": "new"}},
                        "yuzu": {"https://yuzu": {"hash": "pending"}},
                    }
                )
            )

            with patch.object(check_updates, "CACHE_FILE", cache_file), patch.object(
                check_updates, "PENDING_CACHE_FILE", pending_file
            ):
                check_updates._accept_pending(["ado"])

            cache = json.loads(cache_file.read_text())
            pending = json.loads(pending_file.read_text())
            self.assertEqual(cache["ado"]["https://old"]["hash"], "old")
            self.assertEqual(cache["ado"]["https://new"]["hash"], "new")
            self.assertNotIn("yuzu", cache)
            self.assertEqual(pending, {"yuzu": {"https://yuzu": {"hash": "pending"}}})

    def test_accept_pending_rejects_artist_without_pending_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(
                check_updates, "CACHE_FILE", root / "source_hashes.json"
            ), patch.object(
                check_updates,
                "PENDING_CACHE_FILE",
                root / "source_hashes.pending.json",
            ):
                with self.assertRaises(ValueError):
                    check_updates._accept_pending(["ado"])

    def test_content_hash_ignores_long_timestamps(self):
        before = b"<html><body>LIVE 1710000000</body></html>"
        after = b"<html><body>LIVE 1810000000</body></html>"
        self.assertEqual(
            check_updates._content_hash(before),
            check_updates._content_hash(after),
        )


if __name__ == "__main__":
    unittest.main()
