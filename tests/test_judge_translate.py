"""judge_translate.pyのバッチJSONパース・カテゴリフォールバックのテスト(標準ライブラリunittestのみ使用)。"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from judge_translate import (  # noqa: E402
    parse_batch_judgements,
    repair_json_escapes,
    salvage_json_objects,
)


class TestParseBatchJudgements(unittest.TestCase):
    def test_valid_batch_all_indices_present(self):
        text = (
            '[{"index": 1, "score": 9, "category": "must_read", "title_ja": "タイトル1", '
            '"reason": "理由1", "abstract_ja": "和訳1", "one_liner": "要約1", '
            '"check_points": "確認点1", "suggested_action": "行動1"},'
            '{"index": 2, "score": 3, "category": "abstract_only", "title_ja": "タイトル2", '
            '"reason": "理由2", "abstract_ja": "", "one_liner": "要約2", '
            '"check_points": "", "suggested_action": ""}]'
        )
        result = parse_batch_judgements(text, batch_size=2, score_threshold=6)
        self.assertEqual(set(result.keys()), {1, 2})
        self.assertEqual(result[1]["category"], "must_read")
        self.assertEqual(result[1]["score"], 9)
        self.assertEqual(result[2]["category"], "abstract_only")

    def test_missing_category_falls_back_by_score(self):
        text = '[{"index": 1, "score": 8, "reason": "理由"}, {"index": 2, "score": 2, "reason": "理由"}]'
        result = parse_batch_judgements(text, batch_size=2, score_threshold=6)
        self.assertEqual(result[1]["category"], "worth_reading")
        self.assertEqual(result[2]["category"], "ignore")

    def test_invalid_category_value_falls_back(self):
        text = '[{"index": 1, "score": 9, "category": "super_important", "reason": "理由"}]'
        result = parse_batch_judgements(text, batch_size=1, score_threshold=6)
        self.assertEqual(result[1]["category"], "worth_reading")

    def test_out_of_range_index_is_ignored(self):
        text = '[{"index": 1, "score": 5, "category": "abstract_only"}, {"index": 9, "score": 5, "category": "abstract_only"}]'
        result = parse_batch_judgements(text, batch_size=2, score_threshold=6)
        self.assertEqual(set(result.keys()), {1})

    def test_partial_batch_missing_index_leaves_gap(self):
        """論文2の判定がレスポンスに含まれない場合、呼び出し側でignoreフォールバックする想定。"""
        text = '[{"index": 1, "score": 7, "category": "worth_reading"}]'
        result = parse_batch_judgements(text, batch_size=3, score_threshold=6)
        self.assertEqual(set(result.keys()), {1})

    def test_broken_json_raises(self):
        text = "これはJSONではありません"
        with self.assertRaises(Exception):
            parse_batch_judgements(text, batch_size=2, score_threshold=6)

    def test_json_array_wrapped_in_code_fence(self):
        text = '```json\n[{"index": 1, "score": 7, "category": "worth_reading"}]\n```'
        result = parse_batch_judgements(text, batch_size=1, score_threshold=6)
        self.assertEqual(result[1]["category"], "worth_reading")
        self.assertEqual(result[1]["score"], 7)


class TestLatexEscapeRecovery(unittest.TestCase):
    """
    Geminiが理由や翻訳にLaTeXを書くと \\i 等が不正なエスケープになり、
    バッチ8件が丸ごと ignore に落ちていた回帰(2026-08-29 の本番で15バッチ中7バッチが全損)。
    """

    def test_repair_keeps_valid_escapes(self):
        text = '{"a": "line\\nbreak", "b": "quote\\"here", "c": "\\u3042"}'
        self.assertEqual(repair_json_escapes(text), text)

    def test_repair_fixes_latex_escape(self):
        text = '{"reason": "\\infty の対称性"}'
        repaired = repair_json_escapes(text)
        self.assertEqual(json.loads(repaired)["reason"], "\\infty の対称性")

    def test_batch_with_latex_escape_is_parsed(self):
        text = (
            '[{"index": 1, "score": 8, "category": "worth_reading", '
            '"title_ja": "自己双対重力", '
            '"reason": "セレスティアルホログラフィーにおける $Lw_{1+\\infty}$ 対称性を議論している。", '
            '"abstract_ja": "", "one_liner": "要約", "check_points": "", "suggested_action": ""},'
            '{"index": 2, "score": 3, "category": "ignore", "title_ja": "別の論文", '
            '"reason": "\\mathcal N=4 とは無関係。", "abstract_ja": "", "one_liner": "要約", '
            '"check_points": "", "suggested_action": ""}]'
        )
        result = parse_batch_judgements(text, batch_size=2, score_threshold=6)
        self.assertEqual(set(result.keys()), {1, 2})
        self.assertEqual(result[1]["category"], "worth_reading")
        self.assertIn("Lw_{1+", result[1]["reason"])

    def test_truncated_array_salvages_complete_objects(self):
        """出力が途中で切れても、閉じている論文分だけは救出する。"""
        text = (
            '[{"index": 1, "score": 7, "category": "worth_reading", "title_ja": "1本目"},'
            '{"index": 2, "score": 5, "categ'
        )
        result = parse_batch_judgements(text, batch_size=2, score_threshold=6)
        self.assertEqual(set(result.keys()), {1})

    def test_salvage_returns_empty_for_non_json(self):
        self.assertEqual(salvage_json_objects("これはJSONではありません"), [])


if __name__ == "__main__":
    unittest.main()
