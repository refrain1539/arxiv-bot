"""post_log.pyのposted_messages.json読み書きテスト。

ネットワークには一切出ない。ファイルI/OはすべてtempfileのTemporaryDirectory配下で
行い、data/posted_messages.json本体には書き込まない。
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from post_log import (  # noqa: E402
    iter_recent,
    load_posted,
    prune_posted,
    record_post,
    save_posted,
)


class TestLoadPosted(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "does_not_exist.json")
            self.assertEqual(load_posted(path), {})

    def test_broken_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ this is not json")
            self.assertEqual(load_posted(path), {})

    def test_non_dict_json_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "list.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("[]")
            self.assertEqual(load_posted(path), {})


class TestSaveLoadRoundtrip(unittest.TestCase):
    def test_roundtrip_preserves_japanese_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "posted.json")
            posted = {
                "2507.00001": {
                    "message_id": "123456789012345678",
                    "channel_id": "987654321098765432",
                    "date": "2026-08-30",
                    "title": "重力とホログラフィーに関する論文",
                }
            }
            save_posted(posted, path)
            loaded = load_posted(path)
            self.assertEqual(loaded, posted)
            self.assertEqual(loaded["2507.00001"]["title"], "重力とホログラフィーに関する論文")


class TestRecordPost(unittest.TestCase):
    def test_message_id_and_channel_id_are_stored_as_str(self):
        posted = {}
        result = record_post(
            posted,
            "2507.00001",
            message_id=123456789012345678,
            channel_id=987654321098765432,
            date_str="2026-08-30",
            title="テスト論文",
        )
        # 戻り値はposted自身(破壊的更新)
        self.assertIs(result, posted)
        entry = posted["2507.00001"]
        self.assertEqual(entry["message_id"], "123456789012345678")
        self.assertEqual(entry["channel_id"], "987654321098765432")
        self.assertIsInstance(entry["message_id"], str)
        self.assertIsInstance(entry["channel_id"], str)
        self.assertEqual(entry["date"], "2026-08-30")
        self.assertEqual(entry["title"], "テスト論文")


class TestPrunePosted(unittest.TestCase):
    def _now(self):
        return datetime(2026, 8, 30, tzinfo=timezone.utc)

    def test_old_entries_removed_recent_kept(self):
        now = self._now()
        old_date = (now - timedelta(days=91)).strftime("%Y-%m-%d")
        recent_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        posted = {
            "old": {"message_id": "1", "channel_id": "2", "date": old_date, "title": ""},
            "recent": {"message_id": "3", "channel_id": "4", "date": recent_date, "title": ""},
        }
        pruned = prune_posted(posted, now=now)
        self.assertNotIn("old", pruned)
        self.assertIn("recent", pruned)

    def test_broken_date_and_missing_date_key_are_kept(self):
        now = self._now()
        posted = {
            "broken": {"message_id": "1", "channel_id": "2", "date": "not-a-date", "title": ""},
            "no_date_key": {"message_id": "3", "channel_id": "4", "title": ""},
        }
        pruned = prune_posted(posted, now=now)
        self.assertIn("broken", pruned)
        self.assertIn("no_date_key", pruned)

    def test_original_dict_is_not_mutated(self):
        now = self._now()
        old_date = (now - timedelta(days=200)).strftime("%Y-%m-%d")
        posted = {
            "old": {"message_id": "1", "channel_id": "2", "date": old_date, "title": ""},
        }
        posted_copy = json.loads(json.dumps(posted))
        prune_posted(posted, now=now)
        self.assertEqual(posted, posted_copy)


class TestIterRecent(unittest.TestCase):
    def _now(self):
        return datetime(2026, 8, 30, tzinfo=timezone.utc)

    def test_returns_only_entries_within_days(self):
        now = self._now()
        within_date = (now - timedelta(days=3)).strftime("%Y-%m-%d")
        outside_date = (now - timedelta(days=10)).strftime("%Y-%m-%d")
        posted = {
            "within": {"message_id": "1", "channel_id": "2", "date": within_date, "title": ""},
            "outside": {"message_id": "3", "channel_id": "4", "date": outside_date, "title": ""},
        }
        result = iter_recent(posted, days=7, now=now)
        ids = [arxiv_id for arxiv_id, _ in result]
        self.assertIn("within", ids)
        self.assertNotIn("outside", ids)

    def test_broken_date_entries_are_skipped(self):
        now = self._now()
        posted = {
            "broken": {"message_id": "1", "channel_id": "2", "date": "not-a-date", "title": ""},
            "no_date_key": {"message_id": "3", "channel_id": "4", "title": ""},
        }
        result = iter_recent(posted, days=7, now=now)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
