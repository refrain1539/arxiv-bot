"""
reactions.py のテスト。

requests を完全にモックし、ネットワークには一切出ない。
Bot自身のリアクションの除外・feedback.json への追記と重複防止・
📖リクエストの抽出・DRY_RUN の挙動を確認する。
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import notify_discord  # noqa: E402
import reactions  # noqa: E402
from reactions import (  # noqa: E402
    _has_human_reaction,
    build_feedback_entries,
    collect_reactions,
    get_bot_user_id,
    get_read_requests,
    scan_reactions,
)

BOT_ID = "111111111111111111"
HUMAN_ID = "222222222222222222"
NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

LIKE_ENCODED = "%F0%9F%91%8D"
DISLIKE_ENCODED = "%F0%9F%91%8E"
READ_ENCODED = "%F0%9F%93%96"


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} Error")
            error.response = self
            raise error


def _fake_api(reaction_map, bot_ok=True):
    """
    URL に応じた応答を返すダミー。
    reaction_map: {(message_id, 絵文字のURLエンコード): [user dict, ...]}
    """

    def handler(method, url, **kwargs):
        if url.endswith("/users/@me"):
            if not bot_ok:
                return FakeResponse(status_code=401)
            return FakeResponse(json_data={"id": BOT_ID})
        for (message_id, emoji), users in reaction_map.items():
            if f"/messages/{message_id}/reactions/{emoji}" in url:
                return FakeResponse(json_data=users)
        return FakeResponse(json_data=[])

    return mock.Mock(side_effect=handler)


def _entry(message_id, title="Sample Title", date="2026-08-29"):
    return {
        "message_id": message_id,
        "channel_id": "999",
        "date": date,
        "title": title,
    }


class TestBotIdExclusion(unittest.TestCase):
    def test_get_bot_user_id_returns_str(self):
        fake = _fake_api({})
        with mock.patch.object(notify_discord.requests, "request", fake):
            self.assertEqual(get_bot_user_id("tok"), BOT_ID)

    def test_get_bot_user_id_returns_none_on_failure(self):
        fake = _fake_api({}, bot_ok=False)
        with mock.patch.object(notify_discord.requests, "request", fake), mock.patch.object(
            notify_discord.time, "sleep", mock.Mock()
        ):
            self.assertIsNone(get_bot_user_id("tok"))

    def test_bot_only_reaction_is_not_human(self):
        self.assertFalse(_has_human_reaction([{"id": BOT_ID}], BOT_ID))

    def test_human_reaction_is_detected(self):
        self.assertTrue(_has_human_reaction([{"id": BOT_ID}, {"id": HUMAN_ID}], BOT_ID))

    def test_empty_users_is_not_human(self):
        self.assertFalse(_has_human_reaction([], BOT_ID))

    def test_bot_user_id_compared_as_string(self):
        """Discord の ID は数値で返ることもあるため、str に揃えて比較する。"""
        self.assertFalse(_has_human_reaction([{"id": int(BOT_ID)}], BOT_ID))


class TestScanReactions(unittest.TestCase):
    def test_bot_reactions_alone_yield_nothing(self):
        # Bot が投稿直後に 3種類とも付けている状態
        reaction_map = {
            ("10", LIKE_ENCODED): [{"id": BOT_ID}],
            ("10", DISLIKE_ENCODED): [{"id": BOT_ID}],
            ("10", READ_ENCODED): [{"id": BOT_ID}],
        }
        fake = _fake_api(reaction_map)
        with mock.patch.object(notify_discord.requests, "request", fake):
            scanned = scan_reactions([("2608.00001", _entry("10"))], "tok", BOT_ID)

        self.assertFalse(scanned["2608.00001"]["like"])
        self.assertFalse(scanned["2608.00001"]["dislike"])
        self.assertFalse(scanned["2608.00001"]["read"])

    def test_human_like_and_read_are_detected(self):
        reaction_map = {
            ("10", LIKE_ENCODED): [{"id": BOT_ID}, {"id": HUMAN_ID}],
            ("10", DISLIKE_ENCODED): [{"id": BOT_ID}],
            ("10", READ_ENCODED): [{"id": BOT_ID}, {"id": HUMAN_ID}],
        }
        fake = _fake_api(reaction_map)
        with mock.patch.object(notify_discord.requests, "request", fake):
            scanned = scan_reactions([("2608.00001", _entry("10"))], "tok", BOT_ID)

        self.assertTrue(scanned["2608.00001"]["like"])
        self.assertFalse(scanned["2608.00001"]["dislike"])
        self.assertTrue(scanned["2608.00001"]["read"])

    def test_deleted_message_404_is_treated_as_no_reaction(self):
        fake = mock.Mock(return_value=FakeResponse(status_code=404))
        with mock.patch.object(notify_discord.requests, "request", fake):
            scanned = scan_reactions([("2608.00001", _entry("10"))], "tok", BOT_ID)
        self.assertFalse(scanned["2608.00001"]["like"])

    def test_entry_without_message_id_is_skipped(self):
        fake = _fake_api({})
        entry = {"channel_id": "999", "date": "2026-08-29"}
        with mock.patch.object(notify_discord.requests, "request", fake):
            scanned = scan_reactions([("2608.00001", entry)], "tok", BOT_ID)
        self.assertEqual(scanned, {})

    def test_channel_id_falls_back_to_default(self):
        entry = {"message_id": "10", "date": "2026-08-29"}
        fake = _fake_api({("10", LIKE_ENCODED): [{"id": HUMAN_ID}]})
        with mock.patch.object(notify_discord.requests, "request", fake):
            scanned = scan_reactions(
                [("2608.00001", entry)], "tok", BOT_ID, default_channel_id="777"
            )
        self.assertTrue(scanned["2608.00001"]["like"])
        self.assertTrue(any("/channels/777/" in c.args[1] for c in fake.call_args_list))


class TestBuildFeedbackEntries(unittest.TestCase):
    def _scanned(self, like=False, dislike=False, read=False, title="Sample Title"):
        return {
            "2608.00001": {
                "entry": _entry("10", title=title),
                "like": like,
                "dislike": dislike,
                "read": read,
            }
        }

    def test_like_becomes_like_verdict(self):
        entries = build_feedback_entries(self._scanned(like=True), [], "2026-08-30")
        self.assertEqual(
            entries,
            [
                {
                    "arxiv_id": "2608.00001",
                    "title": "Sample Title",
                    "verdict": "like",
                    "date": "2026-08-30",
                }
            ],
        )

    def test_dislike_becomes_dislike_verdict(self):
        entries = build_feedback_entries(self._scanned(dislike=True), [], "2026-08-30")
        self.assertEqual(entries[0]["verdict"], "dislike")

    def test_no_reaction_yields_no_entry(self):
        self.assertEqual(build_feedback_entries(self._scanned(read=True), [], "2026-08-30"), [])

    def test_like_wins_when_both_pressed(self):
        entries = build_feedback_entries(
            self._scanned(like=True, dislike=True), [], "2026-08-30"
        )
        self.assertEqual(entries[0]["verdict"], "like")

    def test_already_recorded_id_is_not_duplicated(self):
        existing = [{"arxiv_id": "2608.00001", "title": "x", "verdict": "like", "date": "2026-08-29"}]
        self.assertEqual(
            build_feedback_entries(self._scanned(like=True), existing, "2026-08-30"), []
        )

    def test_title_falls_back_to_arxiv_id(self):
        """judge_translate が f["title"] を直接参照するため、title は必ず入れる。"""
        entries = build_feedback_entries(self._scanned(like=True, title=""), [], "2026-08-30")
        self.assertEqual(entries[0]["title"], "2608.00001")


class TestGetReadRequests(unittest.TestCase):
    def test_returns_ids_with_read_reaction(self):
        scanned = {
            "a": {"entry": _entry("1"), "like": False, "dislike": False, "read": True},
            "b": {"entry": _entry("2"), "like": True, "dislike": False, "read": False},
        }
        self.assertEqual(get_read_requests(scanned), ["a"])


class TestCollectReactions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.feedback_path = os.path.join(self.tmp.name, "feedback.json")
        self.posted_path = os.path.join(self.tmp.name, "posted_messages.json")
        with open(self.feedback_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        with open(self.posted_path, "w", encoding="utf-8") as f:
            json.dump({"2608.00001": _entry("10", title="Sample Title")}, f)
        self.addCleanup(self.tmp.cleanup)

    def _call(self, fake, **kwargs):
        with mock.patch.object(notify_discord.requests, "request", fake):
            return collect_reactions(
                "tok",
                channel_id="999",
                feedback_path=self.feedback_path,
                posted_path=self.posted_path,
                now=NOW,
                **kwargs,
            )

    def _read_feedback(self):
        with open(self.feedback_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_writes_feedback_entry(self):
        fake = _fake_api(
            {
                ("10", LIKE_ENCODED): [{"id": BOT_ID}, {"id": HUMAN_ID}],
                ("10", READ_ENCODED): [{"id": BOT_ID}, {"id": HUMAN_ID}],
            }
        )
        new_entries, read_ids = self._call(fake)

        self.assertEqual(len(new_entries), 1)
        self.assertEqual(read_ids, ["2608.00001"])
        saved = self._read_feedback()
        self.assertEqual(saved[0]["arxiv_id"], "2608.00001")
        self.assertEqual(saved[0]["verdict"], "like")
        # JST基準の日付が入る
        self.assertEqual(saved[0]["date"], "2026-08-30")

    def test_dry_run_does_not_write_files(self):
        fake = _fake_api({("10", DISLIKE_ENCODED): [{"id": BOT_ID}, {"id": HUMAN_ID}]})
        new_entries, _ = self._call(fake, dry_run=True)

        self.assertEqual(len(new_entries), 1)
        self.assertEqual(self._read_feedback(), [])

    def test_missing_token_does_not_touch_network(self):
        fake = mock.Mock()
        with mock.patch.object(notify_discord.requests, "request", fake):
            self.assertEqual(
                collect_reactions(
                    "",
                    feedback_path=self.feedback_path,
                    posted_path=self.posted_path,
                    now=NOW,
                ),
                ([], []),
            )
        fake.assert_not_called()

    def test_aborts_when_bot_user_id_is_unknown(self):
        """Bot自身を特定できないまま回収すると、自分のリアクションを人間の反応と誤認する。"""
        fake = _fake_api({("10", LIKE_ENCODED): [{"id": HUMAN_ID}]}, bot_ok=False)
        with mock.patch.object(notify_discord.time, "sleep", mock.Mock()):
            new_entries, read_ids = self._call(fake)

        self.assertEqual((new_entries, read_ids), ([], []))
        self.assertEqual(self._read_feedback(), [])

    def test_old_posts_are_out_of_lookback_window(self):
        with open(self.posted_path, "w", encoding="utf-8") as f:
            json.dump({"2606.00001": _entry("10", date="2026-06-01")}, f)
        fake = _fake_api({("10", LIKE_ENCODED): [{"id": HUMAN_ID}]})
        new_entries, _ = self._call(fake)

        self.assertEqual(new_entries, [])
        # /users/@me は呼ばれるが、リアクション取得は行われない
        self.assertFalse(any("/reactions/" in c.args[1] for c in fake.call_args_list))

    def test_prunes_posted_messages_on_write(self):
        with open(self.posted_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "2608.00001": _entry("10"),
                    "2601.00001": _entry("11", date="2026-01-01"),
                },
                f,
            )
        fake = _fake_api({})
        self._call(fake)

        with open(self.posted_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertIn("2608.00001", saved)
        self.assertNotIn("2601.00001", saved)


if __name__ == "__main__":
    unittest.main()
