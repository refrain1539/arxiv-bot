"""
explain.py のテスト。

requests を完全にモックし、ネットワークには一切出ない。
プロンプトの要件(専門用語を訳させない・途中計算の式を出させない)・
HTMLの組み上げ・失敗の transient/permanent の切り分けを確認する。
"""

import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import explain  # noqa: E402
from explain import (  # noqa: E402
    MAX_PDF_BYTES,
    build_prompt,
    clean_fragment,
    count_formulas,
    generate_explanation,
    render_html,
)

PAPER = {
    "id": "2608.01234",
    "title": "Pseudo-entropy in dS3/CFT2",
    "authors": ["Tadashi Takayanagi", "Seiya Tanaka"],
    "url": "https://arxiv.org/abs/2608.01234",
    "abstract": "We study pseudo-entropy.",
}


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"", text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.content = content
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} Error")
            error.response = self
            raise error


def _gemini_response(text):
    return FakeResponse(json_data={"candidates": [{"content": {"parts": [{"text": text}]}}]})


class TestPrompt(unittest.TestCase):
    """プロンプトは品質の核なので、要件が抜け落ちていないことを検査する。"""

    def setUp(self):
        self.prompt = build_prompt(PAPER, "私は Godel 時空のホログラフィーを研究しています。")

    def test_includes_paper_metadata(self):
        self.assertIn("Pseudo-entropy in dS3/CFT2", self.prompt)
        self.assertIn("Tadashi Takayanagi", self.prompt)
        self.assertIn("https://arxiv.org/abs/2608.01234", self.prompt)

    def test_includes_profile(self):
        self.assertIn("Godel 時空のホログラフィー", self.prompt)

    def test_forbids_translating_technical_terms(self):
        """non-unitary が「非探偵」になる事故を防ぐ指示が入っていること。"""
        self.assertIn("英語のまま", self.prompt)
        self.assertIn("非探偵", self.prompt)
        self.assertIn("non-unitary", self.prompt)
        self.assertIn("pseudo-entropy", self.prompt)

    def test_forbids_intermediate_equations(self):
        """読み手は結果を見たいので、途中計算の式を禁じる指示が入っていること。"""
        self.assertIn("途中計算の式は書かないでください", self.prompt)
        self.assertIn("文章", self.prompt)

    def test_requires_setup_and_result_equations(self):
        """action / metric と最終結果は式で出させること。"""
        self.assertIn("action", self.prompt)
        self.assertIn("metric", self.prompt)
        self.assertIn("$$", self.prompt)

    def test_asks_for_html_not_markdown(self):
        self.assertIn("HTML", self.prompt)
        self.assertIn("Markdown 記法", self.prompt)

    def test_has_meaning_section(self):
        """「結果の意味」を最も厚く書かせる節があること。"""
        self.assertIn("結果の意味", self.prompt)
        self.assertIn("導出の道筋", self.prompt)


class TestCleanFragment(unittest.TestCase):
    def test_strips_code_fence(self):
        self.assertEqual(clean_fragment("```html\n<h2>A</h2>\n```"), "<h2>A</h2>")

    def test_strips_bare_fence(self):
        self.assertEqual(clean_fragment("```\n<h2>A</h2>\n```"), "<h2>A</h2>")

    def test_strips_document_tags(self):
        got = clean_fragment("<!doctype html><html><body><h2>A</h2></body></html>")
        self.assertEqual(got, "<h2>A</h2>")

    def test_leaves_plain_fragment_untouched(self):
        self.assertEqual(clean_fragment("<h2>A</h2>\n<p>b</p>"), "<h2>A</h2>\n<p>b</p>")

    def test_does_not_strip_headers_named_body(self):
        """<h2>body</h2> のようなテキストを誤って消さないこと。"""
        self.assertIn("bodyについて", clean_fragment("<p>bodyについて</p>"))


class TestRenderHtml(unittest.TestCase):
    def setUp(self):
        self.page = render_html(PAPER, "<h2>一言でいうと</h2><p>$E=mc^2$</p>", "gemini-3.6-flash")

    def test_is_a_complete_document(self):
        self.assertTrue(self.page.startswith("<!doctype html>"))
        self.assertIn("</html>", self.page)

    def test_includes_mathjax(self):
        self.assertIn("tex-mml-chtml.js", self.page)

    def test_keeps_latex_delimiters(self):
        self.assertIn("$E=mc^2$", self.page)

    def test_escapes_title_and_authors(self):
        page = render_html(
            {"title": "A <script>bad</script>", "authors": ["X & Y"], "url": "https://x"},
            "<p>ok</p>",
            "m",
        )
        self.assertNotIn("<script>bad</script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("X &amp; Y", page)

    def test_supports_dark_mode(self):
        self.assertIn("prefers-color-scheme: dark", self.page)


class TestCountFormulas(unittest.TestCase):
    def test_counts_display_and_inline(self):
        display, inline = count_formulas("$$a$$ text $b$ and $c$ $$d$$")
        self.assertEqual(display, 2)
        self.assertEqual(inline, 2)

    def test_no_formulas(self):
        self.assertEqual(count_formulas("<p>式がありません</p>"), (0, 0))


class TestGenerateExplanation(unittest.TestCase):
    def _patch(self, meta_resp, pdf_resp, gemini_resp=None):
        """arXiv メタ取得 / PDF取得 / Gemini呼び出しをまとめて差し替える。"""
        return (
            mock.patch.object(explain, "fetch_paper_meta", lambda _id: meta_resp),
            mock.patch.object(explain.requests, "get", mock.Mock(return_value=pdf_resp)),
            mock.patch.object(
                explain.requests, "post", mock.Mock(return_value=gemini_resp or FakeResponse())
            ),
            mock.patch.object(explain.time, "sleep", mock.Mock()),
        )

    def test_missing_api_key_is_permanent(self):
        page, error = generate_explanation("2608.01234", "")
        self.assertIsNone(page)
        self.assertEqual(error, "permanent")

    def test_happy_path_returns_full_page(self):
        patches = self._patch(
            PAPER,
            FakeResponse(content=b"%PDF-1.5 fake"),
            _gemini_response("<h2>一言でいうと</h2><p>$$S=\\frac{A}{4G}$$</p>"),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            page, error = generate_explanation("2608.01234", "key")

        self.assertIsNone(error)
        self.assertIn("<!doctype html>", page)
        self.assertIn("S=\\frac{A}{4G}", page)
        self.assertIn("Pseudo-entropy in dS3/CFT2", page)

    def test_pdf_is_sent_as_inline_data(self):
        post = mock.Mock(return_value=_gemini_response("<h2>x</h2>"))
        with mock.patch.object(explain, "fetch_paper_meta", lambda _id: PAPER), mock.patch.object(
            explain.requests, "get", mock.Mock(return_value=FakeResponse(content=b"%PDF-1.5 fake"))
        ), mock.patch.object(explain.requests, "post", post):
            generate_explanation("2608.01234", "key")

        parts = post.call_args.kwargs["json"]["contents"][0]["parts"]
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "application/pdf")
        self.assertTrue(parts[1]["inline_data"]["data"])

    def test_oversized_pdf_is_permanent(self):
        big = FakeResponse(content=b"x" * (MAX_PDF_BYTES + 1))
        with mock.patch.object(explain, "fetch_paper_meta", lambda _id: PAPER), mock.patch.object(
            explain.requests, "get", mock.Mock(return_value=big)
        ):
            page, error = generate_explanation("2608.01234", "key")

        self.assertIsNone(page)
        self.assertEqual(error, "permanent")

    def test_pdf_404_is_permanent(self):
        with mock.patch.object(explain, "fetch_paper_meta", lambda _id: PAPER), mock.patch.object(
            explain.requests, "get", mock.Mock(return_value=FakeResponse(status_code=404))
        ):
            page, error = generate_explanation("2608.01234", "key")
        self.assertEqual(error, "permanent")

    def test_pdf_server_error_is_transient(self):
        with mock.patch.object(explain, "fetch_paper_meta", lambda _id: PAPER), mock.patch.object(
            explain.requests, "get", mock.Mock(return_value=FakeResponse(status_code=503))
        ):
            page, error = generate_explanation("2608.01234", "key")
        self.assertEqual(error, "transient")

    def test_gemini_400_is_permanent(self):
        with mock.patch.object(explain, "fetch_paper_meta", lambda _id: PAPER), mock.patch.object(
            explain.requests, "get", mock.Mock(return_value=FakeResponse(content=b"pdf"))
        ), mock.patch.object(
            explain.requests, "post", mock.Mock(return_value=FakeResponse(status_code=400, text="too big"))
        ), mock.patch.object(explain.time, "sleep", mock.Mock()):
            page, error = generate_explanation("2608.01234", "key")

        self.assertIsNone(page)
        self.assertEqual(error, "permanent")

    def test_gemini_429_retries_then_gives_up_as_transient(self):
        post = mock.Mock(return_value=FakeResponse(status_code=429))
        with mock.patch.object(explain, "fetch_paper_meta", lambda _id: PAPER), mock.patch.object(
            explain.requests, "get", mock.Mock(return_value=FakeResponse(content=b"pdf"))
        ), mock.patch.object(explain.requests, "post", post), mock.patch.object(
            explain.time, "sleep", mock.Mock()
        ):
            page, error = generate_explanation("2608.01234", "key")

        self.assertEqual(error, "transient")
        self.assertEqual(post.call_count, explain.MAX_RETRIES)

    def test_gemini_429_waits_on_a_minute_scale(self):
        """
        無料枠の制限はRPM(1分あたり)なので、2/4/8秒では枠が戻らない。
        judge_translate と同じ待ち方になっていること。
        """
        sleep = mock.Mock()
        post = mock.Mock(return_value=FakeResponse(status_code=429))
        with mock.patch.object(explain, "fetch_paper_meta", lambda _id: PAPER), mock.patch.object(
            explain.requests, "get", mock.Mock(return_value=FakeResponse(content=b"pdf"))
        ), mock.patch.object(explain.requests, "post", post), mock.patch.object(
            explain.time, "sleep", sleep
        ):
            generate_explanation("2608.01234", "key")

        waits = [c.args[0] for c in sleep.call_args_list]
        self.assertEqual(waits, [30, 60])   # 3回目は待たずに打ち切る

    def test_gemini_429_honours_retry_delay_from_body(self):
        sleep = mock.Mock()
        post = mock.Mock(
            return_value=FakeResponse(
                status_code=429, json_data={"error": {"details": [{"retryDelay": "45s"}]}}
            )
        )
        with mock.patch.object(explain, "fetch_paper_meta", lambda _id: PAPER), mock.patch.object(
            explain.requests, "get", mock.Mock(return_value=FakeResponse(content=b"pdf"))
        ), mock.patch.object(explain.requests, "post", post), mock.patch.object(
            explain.time, "sleep", sleep
        ):
            generate_explanation("2608.01234", "key")

        self.assertEqual([c.args[0] for c in sleep.call_args_list], [45.0, 45.0])

    def test_gemini_503_is_retried_as_transient(self):
        """503 はサーバー側の一時的な不調なので、再試行して transient で返す。"""
        sleep = mock.Mock()
        post = mock.Mock(return_value=FakeResponse(status_code=503))
        with mock.patch.object(explain, "fetch_paper_meta", lambda _id: PAPER), mock.patch.object(
            explain.requests, "get", mock.Mock(return_value=FakeResponse(content=b"pdf"))
        ), mock.patch.object(explain.requests, "post", post), mock.patch.object(
            explain.time, "sleep", sleep
        ):
            page, error = generate_explanation("2608.01234", "key")

        self.assertEqual(error, "transient")
        self.assertEqual(post.call_count, explain.MAX_RETRIES)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [20, 40])

    def test_gemini_503_then_success(self):
        responses = [FakeResponse(status_code=503), _gemini_response("<h2>x</h2>")]
        with mock.patch.object(explain, "fetch_paper_meta", lambda _id: PAPER), mock.patch.object(
            explain.requests, "get", mock.Mock(return_value=FakeResponse(content=b"pdf"))
        ), mock.patch.object(
            explain.requests, "post", mock.Mock(side_effect=responses)
        ), mock.patch.object(explain.time, "sleep", mock.Mock()):
            page, error = generate_explanation("2608.01234", "key")

        self.assertIsNone(error)
        self.assertIn("<h2>x</h2>", page)

    def test_works_when_metadata_lookup_fails(self):
        """メタデータが引けなくても、タイトルさえ渡されば生成は続く。"""
        with mock.patch.object(explain, "fetch_paper_meta", lambda _id: None), mock.patch.object(
            explain.requests, "get", mock.Mock(return_value=FakeResponse(content=b"pdf"))
        ), mock.patch.object(
            explain.requests, "post", mock.Mock(return_value=_gemini_response("<h2>x</h2>"))
        ):
            page, error = generate_explanation("2608.01234", "key", title="Fallback Title")

        self.assertIsNone(error)
        self.assertIn("Fallback Title", page)


if __name__ == "__main__":
    unittest.main()
