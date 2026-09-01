"""
Gemini の判定に失敗した論文が失われないことのテスト。

2026-09-01 の実行で、Gemini の 429 により4バッチ(32件)が ignore 扱いになり、
それらが seen_ids.json に登録されて二度と評価されなくなる事故が起きた。
その再発を防ぐための検査:

  1. judge_translate: 判定できなかった論文に judge_failed=True が付くこと
  2. judge_translate: 429 の待ち時間が「分あたり枠」に見合う長さになること
  3. main: judge_failed の論文を seen_ids に登録しないこと
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import judge_translate  # noqa: E402
import main  # noqa: E402
from judge_translate import (  # noqa: E402
    DEFAULT_JUDGEMENT,
    _gemini_retry_delay,
    judge_and_translate_papers,
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        pass


def _paper(arxiv_id):
    return {
        "id": arxiv_id,
        "title": f"Paper {arxiv_id}",
        "authors": ["A"],
        "url": f"https://arxiv.org/abs/{arxiv_id}",
        "abstract": "abstract",
        "primary_category": "hep-th",
        "published": datetime(2026, 9, 1, tzinfo=timezone.utc),
    }


class TestJudgeFailedFlag(unittest.TestCase):
    def test_default_judgement_is_marked_failed(self):
        """判定できなかったことを、判定した結果 ignore だったことと区別する。"""
        self.assertTrue(DEFAULT_JUDGEMENT["judge_failed"])

    def test_successful_judgement_clears_the_flag(self):
        response = json.dumps(
            [
                {
                    "index": 1,
                    "score": 3,
                    "category": "ignore",
                    "title_ja": "題",
                    "reason": "関連なし",
                    "abstract_ja": "",
                    "one_liner": "",
                    "check_points": "",
                    "suggested_action": "",
                }
            ]
        )
        with mock.patch.object(judge_translate, "_call_gemini_api", lambda *a, **k: response):
            results = judge_and_translate_papers(
                [_paper("2608.00001")], "profile", [], "key", "model", 6
            )

        self.assertEqual(len(results), 1)
        # Geminiが「ignoreと判定した」場合は失敗扱いにしない
        self.assertEqual(results[0]["category"], "ignore")
        self.assertFalse(results[0]["judge_failed"])

    def test_no_response_marks_every_paper_in_the_batch_as_failed(self):
        with mock.patch.object(judge_translate, "_call_gemini_api", lambda *a, **k: None):
            results = judge_and_translate_papers(
                [_paper("2608.00001"), _paper("2608.00002")],
                "profile",
                [],
                "key",
                "model",
                6,
            )

        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["judge_failed"] for r in results))


class TestGeminiRetryDelay(unittest.TestCase):
    def test_uses_retry_delay_from_response_body(self):
        resp = FakeResponse(
            status_code=429,
            json_data={"error": {"details": [{"retryDelay": "42s"}]}},
        )
        self.assertEqual(_gemini_retry_delay(resp, 1), 42.0)

    def test_falls_back_to_minute_scale_backoff(self):
        """RPM制限なので、2/4/8秒のような短い待機では枠が戻らない。"""
        resp = FakeResponse(status_code=429)
        self.assertEqual(_gemini_retry_delay(resp, 1), 30)
        self.assertEqual(_gemini_retry_delay(resp, 2), 60)
        self.assertEqual(_gemini_retry_delay(resp, 3), 90)

    def test_caps_the_wait(self):
        resp = FakeResponse(
            status_code=429, json_data={"error": {"details": [{"retryDelay": "9999s"}]}}
        )
        self.assertEqual(_gemini_retry_delay(resp, 1), judge_translate.GEMINI_MAX_RETRY_WAIT_SEC)

    def test_broken_body_does_not_raise(self):
        self.assertGreater(_gemini_retry_delay(FakeResponse(status_code=429), 1), 0)


class TestSeenIdsSkipsFailedPapers(unittest.TestCase):
    """判定できなかった論文を既読にしないこと(これが今回の事故の本体)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.seen_path = os.path.join(self.tmp.name, "seen_ids.json")
        self.feedback_path = os.path.join(self.tmp.name, "feedback.json")
        self.posted_path = os.path.join(self.tmp.name, "posted.json")
        for path, empty in (
            (self.seen_path, {}),
            (self.feedback_path, []),
            (self.posted_path, {}),
        ):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(empty, f)
        self.addCleanup(self.tmp.cleanup)

    def _run(self, papers, judged):
        os.environ["GEMINI_API_KEY"] = "dummy"
        for key in ("DRY_RUN", "GITHUB_TOKEN", "GITHUB_REPOSITORY",
                    "DISCORD_BOT_TOKEN", "DISCORD_CHANNEL_ID"):
            os.environ.pop(key, None)

        with mock.patch.object(main, "SEEN_IDS_PATH", self.seen_path), mock.patch.object(
            main, "FEEDBACK_PATH", self.feedback_path
        ), mock.patch.object(
            main, "fetch_recent_papers", lambda *a, **k: [dict(p) for p in papers]
        ), mock.patch.object(
            main, "judge_and_translate_papers", lambda *a, **k: judged
        ), mock.patch.object(
            main, "load_posted", lambda: {}
        ), mock.patch.object(main, "save_posted", lambda posted: None):
            main.main()

        with open(self.seen_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def test_failed_papers_are_not_marked_seen(self):
        papers = [_paper("2608.00001"), _paper("2608.00002")]
        judged = [
            dict(papers[0], category="ignore", score=2, judge_failed=False),
            dict(papers[1], **DEFAULT_JUDGEMENT),   # judge_failed=True
        ]
        seen = self._run(papers, judged)

        self.assertIn("2608.00001", seen)
        # 判定できなかった論文は既読にしない -> 次回の実行で再評価される
        self.assertNotIn("2608.00002", seen)

    def test_all_failed_leaves_seen_ids_empty(self):
        papers = [_paper("2608.00001"), _paper("2608.00002")]
        judged = [dict(p, **DEFAULT_JUDGEMENT) for p in papers]
        self.assertEqual(self._run(papers, judged), {})

    def test_papers_missing_from_judged_are_not_marked_seen(self):
        """judged に現れなかった論文(例外で処理が落ちた場合)も既読にしない。"""
        papers = [_paper("2608.00001"), _paper("2608.00002")]
        judged = [dict(papers[0], category="worth_reading", score=7, judge_failed=False)]
        seen = self._run(papers, judged)

        self.assertIn("2608.00001", seen)
        self.assertNotIn("2608.00002", seen)


if __name__ == "__main__":
    unittest.main()
