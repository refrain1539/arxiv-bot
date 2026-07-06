"""
must_read論文のBibTeX entryを組み立てるモジュール。

ハルシネーション防止のため、Geminiには生成させず、arXiv APIから取得済みの
メタデータ(ID・タイトル・著者・投稿年・主カテゴリ)からプログラムで機械的に
組み立てる。citation keyは「第一著者の姓(ASCII化・スペース除去)+ 西暦4桁」とし、
同日内で重複した場合は a, b, ... を付与する。
"""

import re
import unicodedata


def _ascii_lastname(author_name):
    """著者名(フルネーム)から姓を取り出し、ASCII化・記号除去する(citation key用)。"""
    if not author_name or not author_name.strip():
        return "unknown"
    lastname = author_name.strip().split()[-1]
    normalized = unicodedata.normalize("NFKD", lastname)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_only = re.sub(r"[^A-Za-z0-9]", "", ascii_only)
    return ascii_only or "unknown"


def make_citation_key(paper, used_keys):
    """
    citation keyを生成する。used_keys(set)に既存キーとの重複チェック・登録を行う
    (呼び出し側で複数論文にまたがって同じsetを使い回すことで、同日内の重複にa, bを付与する)。
    """
    first_author = (paper.get("authors") or [""])[0]
    lastname = _ascii_lastname(first_author)
    year = paper["published"].strftime("%Y")
    base_key = f"{lastname}{year}"

    key = base_key
    suffix_index = 0
    while key in used_keys:
        key = base_key + chr(ord("a") + suffix_index)
        suffix_index += 1

    used_keys.add(key)
    return key


def build_bibtex_entry(paper, used_keys):
    """paper辞書(id, title, authors, primary_category, published)からBibTeX entryを組み立てる。"""
    key = make_citation_key(paper, used_keys)
    authors = " and ".join(paper.get("authors") or [])
    title = paper.get("title", "")
    eprint = paper.get("id", "")
    primary_category = paper.get("primary_category", "")
    year = paper["published"].strftime("%Y")

    return (
        f"@article{{{key},\n"
        f'    author = "{authors}",\n'
        f'    title = "{{{title}}}",\n'
        f'    eprint = "{eprint}",\n'
        f'    archivePrefix = "arXiv",\n'
        f'    primaryClass = "{primary_category}",\n'
        f'    year = "{year}"\n'
        f"}}"
    )
