"""
arXiv API から新着論文を取得するモジュール。

- エンドポイント: http://export.arxiv.org/api/query (Atom形式のXML)
- パースは標準ライブラリの xml.etree.ElementTree を使い、追加の依存を増やさない
- arXivのマナーとして User-Agent を設定し、リクエスト後に少し待つ
- 取りこぼし防止のため広め(最大200件)に取得し、あとで「過去48時間以内」に絞り込む
  (announceの周期のズレに強くするため。実際の重複排除は seen_ids.json 側で行う)
"""

import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests

ARXIV_API_URL = "http://export.arxiv.org/api/query"
USER_AGENT = "arxiv-hep-th-bot/1.0 (personal use; GitHub Actions)"

# AtomフィードとarXiv拡張の名前空間
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


def _build_search_query(categories):
    """config.yml の categories リストから search_query 文字列を組み立てる。"""
    return " OR ".join(f"cat:{c}" for c in categories)


def fetch_recent_papers(categories, hours=48, max_results=200, retries=3):
    """
    arXiv API から新着論文を取得する。

    引数:
      categories: 監視対象カテゴリのリスト (例: ["hep-th", "gr-qc"])
      hours: この時間以内に submitted された論文だけを残す
      max_results: 1回のリクエストで取得する最大件数
      retries: 通信失敗時の再試行回数

    戻り値: 論文情報の辞書のリスト。失敗時は空リスト(呼び出し側で処理継続)。
    """
    query = _build_search_query(categories)
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": max_results,
    }
    url = f"{ARXIV_API_URL}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": USER_AGENT}

    xml_text = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            xml_text = resp.text
            break
        except Exception as e:
            print(f"[arxiv_fetch] arXiv API呼び出し失敗 (試行{attempt}/{retries}): {e}")
            time.sleep(3)

    if xml_text is None:
        print("[arxiv_fetch] arXiv APIから取得できませんでした。空リストを返します。")
        return []

    # arXivのマナーとして、連続アクセスを避けるため一呼吸置く
    time.sleep(3)

    papers = _parse_atom(xml_text)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    recent = [p for p in papers if p["published"] >= cutoff]

    print(f"[arxiv_fetch] 取得件数: {len(papers)}件 / 直近{hours}時間以内: {len(recent)}件")
    return recent


def _parse_atom(xml_text):
    """Atom XML をパースして論文情報の辞書リストを返す。壊れたentryはスキップする。"""
    papers = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[arxiv_fetch] XMLパースエラー: {e}")
        return papers

    for entry in root.findall("atom:entry", NS):
        try:
            raw_id = entry.findtext("atom:id", default="", namespaces=NS)
            # 例: http://arxiv.org/abs/2507.01234v1 -> 2507.01234
            arxiv_id = raw_id.split("/abs/")[-1]
            arxiv_id = re.sub(r"v\d+$", "", arxiv_id)

            title = entry.findtext("atom:title", default="", namespaces=NS)
            title = " ".join(title.split())  # 改行・余分な空白を除去

            summary = entry.findtext("atom:summary", default="", namespaces=NS)
            summary = " ".join(summary.split())

            authors = [
                a.findtext("atom:name", default="", namespaces=NS)
                for a in entry.findall("atom:author", NS)
            ]

            published_str = entry.findtext("atom:published", default="", namespaces=NS)
            published = datetime.strptime(published_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )

            primary_category_el = entry.find("arxiv:primary_category", NS)
            primary_category = (
                primary_category_el.get("term") if primary_category_el is not None else ""
            )

            papers.append(
                {
                    "id": arxiv_id,
                    "title": title,
                    "authors": authors,
                    "abstract": summary,
                    "primary_category": primary_category,
                    "url": f"https://arxiv.org/abs/{arxiv_id}",
                    "published": published,
                }
            )
        except Exception as e:
            print(f"[arxiv_fetch] エントリのパースに失敗、スキップします: {e}")
            continue

    return papers
