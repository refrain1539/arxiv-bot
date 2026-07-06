"""arxiv_fetch.tag_author_alertsのテスト(部分一致・大文字小文字無視・空リストで後方互換)。"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from arxiv_fetch import tag_author_alerts  # noqa: E402


def _papers():
    return [
        {"id": "1", "title": "A", "authors": ["Tadashi Takayanagi", "Someone Else"]},
        {"id": "2", "title": "B", "authors": ["Juan Maldacena"]},
        {"id": "3", "title": "C", "authors": ["nobody relevant"]},
    ]


class TestAuthorAlert(unittest.TestCase):
    def test_partial_case_insensitive_match(self):
        papers = _papers()
        tag_author_alerts(papers, ["takayanagi"])
        self.assertTrue(papers[0].get("author_alert"))
        self.assertEqual(papers[0].get("matched_author"), "Tadashi Takayanagi")
        self.assertFalse(papers[1].get("author_alert", False))
        self.assertFalse(papers[2].get("author_alert", False))

    def test_empty_watch_authors_is_noop(self):
        papers = _papers()
        tag_author_alerts(papers, [])
        for p in papers:
            self.assertNotIn("author_alert", p)

    def test_none_watch_authors_is_noop(self):
        papers = _papers()
        tag_author_alerts(papers, None)
        for p in papers:
            self.assertNotIn("author_alert", p)

    def test_multiple_watch_authors(self):
        papers = _papers()
        tag_author_alerts(papers, ["Maldacena", "Takayanagi"])
        self.assertTrue(papers[0].get("author_alert"))
        self.assertTrue(papers[1].get("author_alert"))
        self.assertFalse(papers[2].get("author_alert", False))


if __name__ == "__main__":
    unittest.main()
