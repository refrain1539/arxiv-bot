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
        # 対象が0件と分かった時点で打ち切るため、API は一切叩かれない
        fake.assert_not_called()

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


class TestSkipAlreadyProcessed(unittest.TestCase):
    """15分おきに走るため、処理済みのリアクションは問い合わせ自体を行わない。"""

    def _urls(self, fake):
        return [call.args[1] for call in fake.call_args_list]

    def test_settled_verdict_is_not_polled_again(self):
        entry = dict(_entry("10"), reactions_done=["like"])
        fake = _fake_api({("10", LIKE_ENCODED): [{"id": HUMAN_ID}]})
        with mock.patch.object(notify_discord.requests, "request", fake):
            scanned = scan_reactions([("2608.00001", entry)], "tok", BOT_ID)

        # 判定済みなので、押されたままでも新規扱いにはしない
        self.assertFalse(scanned["2608.00001"]["like"])
        urls = self._urls(fake)
        self.assertFalse(any(LIKE_ENCODED in u for u in urls))
        self.assertFalse(any(DISLIKE_ENCODED in u for u in urls))
        # 📖 はまだ未処理なので問い合わせる
        self.assertTrue(any(READ_ENCODED in u for u in urls))

    def test_dislike_is_not_polled_once_like_is_recorded(self):
        entry = dict(_entry("10"), reactions_done=["dislike"])
        fake = _fake_api({})
        with mock.patch.object(notify_discord.requests, "request", fake):
            scan_reactions([("2608.00001", entry)], "tok", BOT_ID)
        urls = self._urls(fake)
        self.assertFalse(any(LIKE_ENCODED in u for u in urls))

    def test_detected_read_is_not_polled_again(self):
        entry = dict(_entry("10"), reactions_done=["read"])
        fake = _fake_api({})
        with mock.patch.object(notify_discord.requests, "request", fake):
            scan_reactions([("2608.00001", entry)], "tok", BOT_ID)
        self.assertFalse(any(READ_ENCODED in u for u in self._urls(fake)))

    def test_explained_entry_is_not_polled_for_read(self):
        entry = dict(_entry("10"), explained=True)
        fake = _fake_api({})
        with mock.patch.object(notify_discord.requests, "request", fake):
            scan_reactions([("2608.00001", entry)], "tok", BOT_ID)
        self.assertFalse(any(READ_ENCODED in u for u in self._urls(fake)))

    def test_fully_settled_entry_costs_no_api_call(self):
        entry = dict(_entry("10"), reactions_done=["like", "read"])
        fake = _fake_api({})
        with mock.patch.object(notify_discord.requests, "request", fake):
            scan_reactions([("2608.00001", entry)], "tok", BOT_ID)
        fake.assert_not_called()


class TestLookbackWindow(unittest.TestCase):
    """1回の実行を軽くするため、走査対象は過去3日以内に限る。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.feedback_path = os.path.join(self.tmp.name, "feedback.json")
        self.posted_path = os.path.join(self.tmp.name, "posted.json")
        with open(self.feedback_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        self.addCleanup(self.tmp.cleanup)

    def _run_with_post_dated(self, date):
        with open(self.posted_path, "w", encoding="utf-8") as f:
            json.dump({"2608.00001": _entry("10", date=date)}, f)
        fake = _fake_api({("10", LIKE_ENCODED): [{"id": BOT_ID}, {"id": HUMAN_ID}]})
        with mock.patch.object(notify_discord.requests, "request", fake):
            return collect_reactions(
                "tok",
                feedback_path=self.feedback_path,
                posted_path=self.posted_path,
                now=NOW,
            )

    def test_default_lookback_is_three_days(self):
        self.assertEqual(reactions.LOOKBACK_DAYS, 3)

    def test_two_days_old_post_is_included(self):
        new_entries, _ = self._run_with_post_dated("2026-08-28")
        self.assertEqual(len(new_entries), 1)

    def test_six_days_old_post_is_excluded(self):
        """以前の7日設定なら拾われていた投稿が、3日設定では対象外になる。"""
        new_entries, _ = self._run_with_post_dated("2026-08-24")
        self.assertEqual(new_entries, [])


class TestIdempotentRuns(unittest.TestCase):
    """15分おきの再実行で、二重処理・空コミット・ログのノイズが出ないこと。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.feedback_path = os.path.join(self.tmp.name, "feedback.json")
        self.posted_path = os.path.join(self.tmp.name, "posted.json")
        with open(self.feedback_path, "w", encoding="utf-8") as f:
            json.dump([], f)
        with open(self.posted_path, "w", encoding="utf-8") as f:
            json.dump({"2608.00001": _entry("10", title="Sample Title")}, f)
        self.addCleanup(self.tmp.cleanup)

    def _call(self, reaction_map):
        fake = _fake_api(reaction_map)
        with mock.patch.object(notify_discord.requests, "request", fake):
            return collect_reactions(
                "tok",
                feedback_path=self.feedback_path,
                posted_path=self.posted_path,
                now=NOW,
            )

    def _read_json(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_first_run_records_processed_flags(self):
        self._call(
            {
                ("10", LIKE_ENCODED): [{"id": BOT_ID}, {"id": HUMAN_ID}],
                ("10", READ_ENCODED): [{"id": BOT_ID}, {"id": HUMAN_ID}],
            }
        )
        entry = self._read_json(self.posted_path)["2608.00001"]
        self.assertIn("like", entry["reactions_done"])
        self.assertIn("read", entry["reactions_done"])

    def test_second_run_does_not_duplicate_feedback(self):
        reaction_map = {("10", LIKE_ENCODED): [{"id": BOT_ID}, {"id": HUMAN_ID}]}
        self._call(reaction_map)
        new_entries, _ = self._call(reaction_map)

        self.assertEqual(new_entries, [])
        self.assertEqual(len(self._read_json(self.feedback_path)), 1)

    def test_unchanged_run_does_not_rewrite_posted_file(self):
        """変化がないときはファイルを書かない(空コミットを積まないため)。"""
        save_mock = mock.Mock()
        with mock.patch.object(reactions, "save_posted", save_mock):
            self._call({})
        save_mock.assert_not_called()

    def test_verdict_already_in_feedback_is_still_marked_done(self):
        """
        GitHub Issue 経由で既に評価済みの論文は feedback.json に追記されないが、
        処理済みにしておかないと毎回問い合わせ続けてしまう。
        """
        with open(self.feedback_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {
                        "arxiv_id": "2608.00001",
                        "title": "Sample Title",
                        "verdict": "like",
                        "date": "2026-08-29",
                    }
                ],
                f,
            )
        new_entries, _ = self._call(
            {("10", DISLIKE_ENCODED): [{"id": BOT_ID}, {"id": HUMAN_ID}]}
        )

        self.assertEqual(new_entries, [])
        entry = self._read_json(self.posted_path)["2608.00001"]
        self.assertIn("dislike", entry["reactions_done"])

    def test_pending_explanations_survives_across_runs(self):
        """📖 は解説書が作られるまで待ち行列に残り続ける。"""
        self._call({("10", READ_ENCODED): [{"id": BOT_ID}, {"id": HUMAN_ID}]})
        _, pending = self._call({})
        self.assertEqual(pending, ["2608.00001"])

    def test_explained_entry_leaves_the_queue(self):
        posted = self._read_json(self.posted_path)
        posted["2608.00001"]["reactions_done"] = ["read"]
        posted["2608.00001"]["explained"] = True
        with open(self.posted_path, "w", encoding="utf-8") as f:
            json.dump(posted, f)

        _, pending = self._call({})
        self.assertEqual(pending, [])


class TestGenerateAndPostExplanations(unittest.TestCase):
    """📖 が押された論文の解説書を生成し、元メッセージへの返信として添付する。"""

    def _posted(self, **flags):
        entry = dict(_entry("10", title="Sample Title"), reactions_done=["read"])
        entry.update(flags)
        return {"2608.00001": entry}

    def _run(self, posted, generate_result, api_key="gkey", **kwargs):
        gen = mock.Mock(return_value=generate_result)
        send = mock.Mock(return_value="555")
        with mock.patch.object(reactions, "generate_explanation", gen), mock.patch.object(
            reactions, "send_file_reply", send
        ):
            sent, changed = reactions.generate_and_post_explanations(
                posted, "tok", api_key, **kwargs
            )
        return sent, changed, gen, send

    def test_filename_is_sanitised(self):
        self.assertEqual(reactions._explanation_filename("hep-th/9711200"), "hep_th_9711200.html")
        self.assertEqual(reactions._explanation_filename("2608.00001"), "2608.00001.html")

    def test_nothing_pending_does_nothing(self):
        posted = {"2608.00001": _entry("10")}  # read が押されていない
        sent, changed, gen, send = self._run(posted, ("<html>", None))
        self.assertEqual((sent, changed), (0, False))
        gen.assert_not_called()
        send.assert_not_called()

    def test_missing_api_key_skips_generation(self):
        sent, changed, gen, send = self._run(self._posted(), ("<html>", None), api_key="")
        self.assertEqual((sent, changed), (0, False))
        gen.assert_not_called()

    def test_success_attaches_html_and_marks_explained(self):
        posted = self._posted()
        sent, changed, gen, send = self._run(posted, ("<!doctype html>解説", None))

        self.assertEqual((sent, changed), (1, True))
        self.assertTrue(posted["2608.00001"]["explained"])

        args = send.call_args.args
        self.assertEqual(args[0], "999")            # channel_id
        self.assertEqual(args[1], "10")             # 元メッセージのID(=返信先)
        self.assertEqual(args[3], "2608.00001.html")
        self.assertEqual(args[4], "<!doctype html>解説".encode("utf-8"))

    def test_transient_failure_is_retried_next_run(self):
        posted = self._posted()
        sent, changed, gen, send = self._run(posted, (None, "transient"))

        self.assertEqual((sent, changed), (0, False))
        send.assert_not_called()
        # explained が立たないので、次回の実行でまた対象になる
        self.assertNotIn("explained", posted["2608.00001"])
        self.assertEqual(reactions.pending_explanations(posted), ["2608.00001"])

    def test_permanent_failure_replies_and_stops_retrying(self):
        posted = self._posted()
        sent, changed, gen, send = self._run(posted, (None, "permanent"))

        self.assertEqual((sent, changed), (0, True))
        send.assert_called_once()
        self.assertIn("生成できませんでした", send.call_args.kwargs["text"])
        # 同じ失敗を毎回繰り返さないよう、処理済みにする
        self.assertTrue(posted["2608.00001"]["explained"])
        self.assertEqual(reactions.pending_explanations(posted), [])

    def test_send_failure_leaves_it_pending(self):
        posted = self._posted()
        gen = mock.Mock(return_value=("<html>", None))
        send = mock.Mock(side_effect=requests.ConnectionError("boom"))
        with mock.patch.object(reactions, "generate_explanation", gen), mock.patch.object(
            reactions, "send_file_reply", send
        ):
            sent, changed = reactions.generate_and_post_explanations(posted, "tok", "gkey")

        self.assertEqual((sent, changed), (0, False))
        self.assertEqual(reactions.pending_explanations(posted), ["2608.00001"])

    def test_limit_caps_work_per_run(self):
        posted = {}
        for i in range(5):
            posted[f"2608.0000{i}"] = dict(
                _entry(str(10 + i), title=f"P{i}"), reactions_done=["read"]
            )
        sent, changed, gen, send = self._run(posted, ("<html>", None), limit=2)

        self.assertEqual(sent, 2)
        self.assertEqual(gen.call_count, 2)
        self.assertEqual(len(reactions.pending_explanations(posted)), 3)

    def test_dry_run_generates_nothing(self):
        posted = self._posted()
        sent, changed, gen, send = self._run(posted, ("<html>", None), dry_run=True)

        self.assertEqual((sent, changed), (0, False))
        gen.assert_not_called()
        send.assert_not_called()
        self.assertNotIn("explained", posted["2608.00001"])

    def test_entry_without_message_id_is_skipped(self):
        posted = {"2608.00001": {"date": "2026-08-29", "reactions_done": ["read"]}}
        sent, changed, gen, send = self._run(posted, ("<html>", None))
        self.assertEqual((sent, changed), (0, False))
        gen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
