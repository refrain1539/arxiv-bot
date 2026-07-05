"""
GitHub Issueを使ったフィードバック機構。

- 毎朝、推薦論文一覧を Issue として作成する(ラベル: daily-papers)
- Issueへのコメント (`+1 -3` のような書式) を回収して feedback.json に追記する
- 回収済み、または作成から7日経過した daily-papers Issue は close する
- 認証は GitHub Actions が自動発行する GITHUB_TOKEN を使うため追加設定は不要
"""

import json
import re
from datetime import datetime, timezone

import requests

GITHUB_API_BASE = "https://api.github.com"
LABEL_NAME = "daily-papers"

# Issue本文末尾に埋め込む隠しメタデータ: <!-- papers: {"1": {"id": "...", "title": "..."}, ...} -->
META_PATTERN = re.compile(r"<!--\s*papers:\s*(\{.*?\})\s*-->", re.DOTALL)
# コメント中の "+1" "-3" のようなパターン
VERDICT_PATTERN = re.compile(r"[+-]\d+")


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "arxiv-hep-th-bot",
    }


def ensure_label_exists(repo, token):
    """daily-papers ラベルが存在しなければ作成する。"""
    url = f"{GITHUB_API_BASE}/repos/{repo}/labels/{LABEL_NAME}"
    try:
        resp = requests.get(url, headers=_headers(token), timeout=30)
        if resp.status_code == 200:
            return
    except Exception as e:
        print(f"[feedback] ラベル確認に失敗しました: {e}")

    create_url = f"{GITHUB_API_BASE}/repos/{repo}/labels"
    payload = {"name": LABEL_NAME, "color": "1d76db", "description": "毎朝の推薦論文Issue"}
    try:
        requests.post(create_url, headers=_headers(token), json=payload, timeout=30)
    except Exception as e:
        print(f"[feedback] ラベル作成に失敗しました: {e}")


def create_daily_issue(repo, token, date_str, papers):
    """
    本日の推薦論文一覧をIssueとして作成する。
    本文末尾に隠しメタデータ(HTMLコメント)を埋め込み、翌朝のフィードバック回収に使う。

    戻り値: (issue_number, issue_html_url)。失敗時は (None, None)。
    """
    ensure_label_exists(repo, token)

    body_lines = [f"# 📄 {date_str} の推薦論文\n"]
    meta = {}

    if not papers:
        body_lines.append("本日は該当する論文がありませんでした。\n")
    else:
        for i, p in enumerate(papers, start=1):
            body_lines.append(f"## [{i}] {p['title']}")
            body_lines.append(f"- スコア: {p.get('score', 0)}/10")
            body_lines.append(f"- 一言: {p.get('one_liner', '')}")
            body_lines.append(f"- リンク: {p['url']}")
            body_lines.append("")
            body_lines.append("**アブスト和訳:**")
            body_lines.append(p.get("abstract_ja", "") or "(翻訳なし)")
            body_lines.append("")
            meta[str(i)] = {"id": p["id"], "title": p["title"]}

    body_lines.append("---")
    body_lines.append(
        "このIssueにコメントで `+1 -3` のように書いてください"
        "(+ = 興味あり、- = 興味なし、数字は論文番号)。"
        "複数OK、スペース区切り。翌朝の実行時に自動回収されます。"
    )
    body_lines.append("")
    body_lines.append(f"<!-- papers: {json.dumps(meta, ensure_ascii=False)} -->")

    body = "\n".join(body_lines)
    title = f"📄 {date_str} の推薦論文"

    url = f"{GITHUB_API_BASE}/repos/{repo}/issues"
    payload = {"title": title, "body": body, "labels": [LABEL_NAME]}

    try:
        resp = requests.post(url, headers=_headers(token), json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        print(f"[feedback] Issueを作成しました: {data['html_url']}")
        return data["number"], data["html_url"]
    except Exception as e:
        print(f"[feedback] Issue作成に失敗しました: {e}")
        return None, None


def _fetch_open_daily_issues(repo, token):
    url = f"{GITHUB_API_BASE}/repos/{repo}/issues"
    params = {"state": "open", "labels": LABEL_NAME, "per_page": 100}
    try:
        resp = requests.get(url, headers=_headers(token), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[feedback] Issue一覧の取得に失敗しました: {e}")
        return []


def _fetch_comments(repo, token, issue_number):
    url = f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}/comments"
    try:
        resp = requests.get(url, headers=_headers(token), params={"per_page": 100}, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[feedback] コメント取得に失敗しました (#{issue_number}): {e}")
        return []


def _close_issue(repo, token, issue_number):
    url = f"{GITHUB_API_BASE}/repos/{repo}/issues/{issue_number}"
    try:
        resp = requests.patch(url, headers=_headers(token), json={"state": "closed"}, timeout=30)
        resp.raise_for_status()
        print(f"[feedback] Issue #{issue_number} をcloseしました")
    except Exception as e:
        print(f"[feedback] Issueのcloseに失敗しました (#{issue_number}): {e}")


def collect_feedback(repo, token, dry_run=False):
    """
    openなdaily-papers Issueからフィードバックを回収する。
    回収済み、または7日経過したIssueはcloseする(dry_run時はcloseせずログのみ)。

    戻り値: 新規に追加されたfeedbackエントリのリスト
    """
    new_entries = []
    issues = _fetch_open_daily_issues(repo, token)
    now = datetime.now(timezone.utc)

    for issue in issues:
        number = issue["number"]
        body = issue.get("body", "") or ""
        try:
            created_at = datetime.strptime(issue["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            age_days = (now - created_at).days
        except Exception:
            age_days = 0

        meta = {}
        meta_match = META_PATTERN.search(body)
        if meta_match:
            try:
                meta = json.loads(meta_match.group(1))
            except Exception as e:
                print(f"[feedback] メタデータのパースに失敗しました (#{number}): {e}")

        comments = _fetch_comments(repo, token, number)
        processed = False

        for comment in comments:
            text = comment.get("body", "") or ""
            for match in VERDICT_PATTERN.findall(text):
                sign = match[0]
                idx = match[1:]
                paper_meta = meta.get(idx)
                if not paper_meta:
                    continue
                verdict = "like" if sign == "+" else "dislike"
                new_entries.append(
                    {
                        "arxiv_id": paper_meta["id"],
                        "title": paper_meta["title"],
                        "verdict": verdict,
                        "date": now.strftime("%Y-%m-%d"),
                    }
                )
                processed = True

        should_close = processed or age_days >= 7
        if should_close:
            if not dry_run:
                _close_issue(repo, token, number)
            else:
                print(f"[feedback] (DRY_RUN) Issue #{number} をcloseする予定でした")

    return new_entries


def load_feedback(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_feedback(path, feedback_list):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(feedback_list, f, ensure_ascii=False, indent=2)
