"""judge_translate.pyのJSONパース・カテゴリフォールバックのテスト(標準ライブラリunittestのみ使用)。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from judge_translate import parse_judgement  # noqa: E402


class TestParseJudgement(unittest.TestCase):
    def test_valid_category(self):
        text = (
            '{"score": 9, "category": "must_read", "title_ja": "タイトル", '
            '"reason": "理由", "abstract_ja": "和訳", "one_liner": "要約", '
            '"check_points": "確認点", "suggested_action": "行動"}'
        )
        result = parse_judgement(text, score_threshold=6)
        self.assertEqual(result["category"], "must_read")
        self.assertEqual(result["score"], 9)
        self.assertEqual(result["check_points"], "確認点")

    def test_missing_category_falls_back_by_score_above_threshold(self):
        text = '{"score": 8, "reason": "理由", "abstract_ja": "和訳", "one_liner": "要約"}'
        result = parse_judgement(text, score_threshold=6)
        self.assertEqual(result["category"], "worth_reading")

    def test_missing_category_falls_back_by_score_below_threshold(self):
        text = '{"score": 2, "reason": "理由"}'
        result = parse_judgement(text, score_threshold=6)
        self.assertEqual(result["category"], "ignore")

    def test_invalid_category_value_falls_back(self):
        text = '{"score": 9, "category": "super_important", "reason": "理由"}'
        result = parse_judgement(text, score_threshold=6)
        self.assertEqual(result["category"], "worth_reading")

    def test_broken_json_raises_and_caller_can_fallback(self):
        text = "これはJSONではありません"
        with self.assertRaises(Exception):
            parse_judgement(text, score_threshold=6)

    def test_none_response_returns_default_ignore(self):
        result = parse_judgement(None, score_threshold=6)
        self.assertEqual(result["category"], "ignore")
        self.assertEqual(result["score"], 0)

    def test_json_wrapped_in_code_fence(self):
        text = '```json\n{"score": 7, "category": "worth_reading", "reason": "r"}\n```'
        result = parse_judgement(text, score_threshold=6)
        self.assertEqual(result["category"], "worth_reading")
        self.assertEqual(result["score"], 7)


if __name__ == "__main__":
    unittest.main()
