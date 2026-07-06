"""bibtex.pyのBibTeX entry生成テスト(著者1名/複数名/非ASCII名)。"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bibtex import build_bibtex_entry, make_citation_key  # noqa: E402


def _paper(authors, title="Sample Title", published_year=2026, arxiv_id="2507.00001"):
    return {
        "id": arxiv_id,
        "title": title,
        "authors": authors,
        "primary_category": "hep-th",
        "published": datetime(published_year, 7, 5, tzinfo=timezone.utc),
    }


class TestBibtex(unittest.TestCase):
    def test_single_author(self):
        p = _paper(["Tadashi Takayanagi"])
        entry = build_bibtex_entry(p, set())
        self.assertIn("@article{Takayanagi2026,", entry)
        self.assertIn('author = "Tadashi Takayanagi",', entry)
        self.assertIn('eprint = "2507.00001",', entry)
        self.assertIn('primaryClass = "hep-th",', entry)
        self.assertIn('year = "2026"', entry)

    def test_multiple_authors_joined_with_and(self):
        p = _paper(["Juan Maldacena", "Edward Witten", "Tadashi Takayanagi"])
        entry = build_bibtex_entry(p, set())
        self.assertIn("@article{Maldacena2026,", entry)
        self.assertIn('author = "Juan Maldacena and Edward Witten and Tadashi Takayanagi",', entry)

    def test_non_ascii_author_key_is_asciified_but_field_kept_verbatim(self):
        p = _paper(["中村 真"])
        entry = build_bibtex_entry(p, set())
        # citation keyはASCII化(非ASCII文字は除去)
        self.assertIn("2026,", entry.splitlines()[0])
        self.assertNotIn("中村", entry.splitlines()[0])
        # author fieldは非ASCIIのまま残す(lualatex環境のため)
        self.assertIn('author = "中村 真",', entry)

    def test_duplicate_key_gets_suffix(self):
        used_keys = set()
        p1 = _paper(["Tadashi Takayanagi"])
        p2 = _paper(["Someone Takayanagi"], arxiv_id="2507.00002")
        key1 = make_citation_key(p1, used_keys)
        key2 = make_citation_key(p2, used_keys)
        self.assertEqual(key1, "Takayanagi2026")
        self.assertEqual(key2, "Takayanagi2026a")


if __name__ == "__main__":
    unittest.main()
