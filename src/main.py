"""
arXiv hep-th 論文推薦Bot のエントリポイント。

毎朝GitHub Actionsから実行され、以下の流れで処理する:
  1. フィードバック回収 (GitHub Issueのコメントを読む)
  2. arXivから新着論文を取得し、著者ウォッチリストと照合
  3. Geminiで関連度判定・4段階分類(must_read/worth_reading/abstract_only/ignore)・翻訳
  4. 通知カテゴリで選別
  5. LINE / メールで通知
  6. GitHub Issueを作成(フィードバック収集用。ignore以外の全カテゴリを記録)
  7. 状態ファイル (seen_ids.json / feedback.json) を更新

環境変数 DRY_RUN=1 のときは、通知・Issue作成/close・状態ファイル保存を行わず、
ログ出力のみで動作確認できる(テスト用)。
"""

import json
import os
from datetime import datetime, timedelta, timezone

import yaml

from arxiv_fetch import fetch_recent_papers, tag_author_alerts
from feedback import collect_feedback, create_daily_issue, load_feedback, save_feedback
from judge_translate import CATEGORY_ORDER, judge_and_translate_papers
from notify import notify

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yml")
SEEN_IDS_PATH = os.path.join(BASE_DIR, "data", "seen_ids.json")
FEEDBACK_PATH = os.path.join(BASE_DIR, "data", "feedback.json")

SEEN_IDS_RETENTION_DAYS = 90


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen_ids():
    try:
        with open(SEEN_IDS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen_ids(seen_ids):
    with open(SEEN_IDS_PATH, "w", encoding="utf-8") as f:
        json.dump(seen_ids, f, ensure_ascii=False, indent=2)


def prune_seen_ids(seen_ids):
    """90日より古いエントリを削除し、ファイルの肥大化を防ぐ。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_IDS_RETENTION_DAYS)
    pruned = {}
    for arxiv_id, date_str in seen_ids.items():
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if d >= cutoff:
                pruned[arxiv_id] = date_str
        except ValueError:
            # 日付形式が壊れている場合は安全側に倒して残す
            pruned[arxiv_id] = date_str
    return pruned


def _sort_key(paper):
    """著者アラートを最優先、次にカテゴリ、次にスコア降順で並べる。"""
    return (
        0 if paper.get("author_alert") else 1,
        CATEGORY_ORDER.get(paper.get("category", "ignore"), 1),
        -paper.get("score", 0),
    )


def _ensure_author_alert_fallback(new_papers, judged):
    """
    author_alert論文がGemini判定の成否(APIキー未設定・例外・応答なし)に関わらず
    必ず通知対象に含まれるよう、judgedに含まれていなければタイトル・著者・URLのみの
    最小限のエントリを補完する。
    """
    judged_ids = {p["id"] for p in judged}
    for p in new_papers:
        if p.get("author_alert") and p["id"] not in judged_ids:
            fallback = dict(p)
            fallback.update(
                {
                    "score": 0,
                    "category": "must_read",
                    "title_ja": "",
                    "abstract_ja": "",
                    "reason": "(Gemini判定が実行できなかったため、著者アラートのみで通知しています)",
                    "one_liner": "",
                    "check_points": "",
                    "suggested_action": "",
                }
            )
            judged.append(fallback)
            print(f"[main] Gemini判定なしで著者アラート論文を通知対象に追加しました: {p['id']}")
    return judged


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    if dry_run:
        print("=== DRY_RUNモードで実行します(通知・Issue作成/close・状態ファイル保存をスキップ) ===")

    config = load_config()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
    gemini_model = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"

    now_jst = datetime.now(timezone.utc) + timedelta(hours=9)
    date_str = now_jst.strftime("%Y-%m-%d")

    # --- 1. フィードバック回収 ---
    feedback_list = load_feedback(FEEDBACK_PATH)
    if repo and github_token:
        try:
            new_entries = collect_feedback(repo, github_token, dry_run=dry_run)
            if new_entries:
                feedback_list.extend(new_entries)
                print(f"[main] フィードバックを{len(new_entries)}件回収しました")
        except Exception as e:
            print(f"[main] フィードバック回収でエラーが発生しました(処理は続行します): {e}")
    else:
        print("[main] GITHUB_REPOSITORY / GITHUB_TOKEN が未設定のため、フィードバック回収をスキップします")

    # --- 2. arXiv新着取得 + 著者ウォッチリスト照合 ---
    seen_ids = load_seen_ids()
    try:
        papers = fetch_recent_papers(
            config.get("categories", ["hep-th"]),
            hours=config.get("lookback_hours", 96),
        )
    except Exception as e:
        print(f"[main] arXiv取得でエラーが発生しました: {e}")
        papers = []

    new_papers = [p for p in papers if p["id"] not in seen_ids]
    new_papers = tag_author_alerts(new_papers, config.get("watch_authors", []))
    alert_count = sum(1 for p in new_papers if p.get("author_alert"))
    print(f"[main] 新着(未通知)論文: {len(new_papers)}件 (うち著者アラート: {alert_count}件)")

    # --- 3. Geminiで判定+翻訳(4段階分類) ---
    threshold = config.get("score_threshold", 6)
    judged = []
    if new_papers:
        if not gemini_api_key:
            print("[main] GEMINI_API_KEY が未設定のため、判定をスキップします")
        else:
            try:
                judged = judge_and_translate_papers(
                    new_papers,
                    config.get("interest_profile", ""),
                    feedback_list,
                    gemini_api_key,
                    gemini_model,
                    threshold,
                )
            except Exception as e:
                print(f"[main] Gemini判定でエラーが発生しました: {e}")

        judged = _ensure_author_alert_fallback(new_papers, judged)

    # --- 4. カテゴリで選別 ---
    notify_categories = config.get("notify_categories") or ["must_read", "worth_reading"]
    max_papers = config.get("max_papers", 8)

    issue_papers = [p for p in judged if p.get("category", "ignore") != "ignore"]
    issue_papers.sort(key=_sort_key)

    alert_papers = [p for p in judged if p.get("author_alert")]
    category_papers = [
        p for p in judged if not p.get("author_alert") and p.get("category") in notify_categories
    ]
    category_papers.sort(key=_sort_key)
    notify_papers = alert_papers + category_papers[:max_papers]
    notify_papers.sort(key=_sort_key)

    notify_paper_ids = {p["id"] for p in notify_papers}
    abstract_only_count = sum(
        1 for p in judged if p.get("category") == "abstract_only" and p["id"] not in notify_paper_ids
    )

    print(
        f"[main] Issue記録対象: {len(issue_papers)}件 / 通知対象: {len(notify_papers)}件 "
        f"(要約のみ{abstract_only_count}件)"
    )

    notify_when_empty = config.get("notify_when_empty", False)
    should_notify = bool(notify_papers) or bool(abstract_only_count) or notify_when_empty

    issue_url = None

    if should_notify:
        if dry_run:
            print("[main] (DRY_RUN) Issue作成をスキップしました")
        elif repo and github_token:
            try:
                _, issue_url = create_daily_issue(repo, github_token, date_str, issue_papers)
            except Exception as e:
                print(f"[main] Issue作成でエラーが発生しました: {e}")
        else:
            print("[main] GITHUB_REPOSITORY / GITHUB_TOKEN が未設定のため、Issue作成をスキップします")

        if not dry_run:
            env = {
                "LINE_CHANNEL_ACCESS_TOKEN": os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"),
                "LINE_USER_ID": os.environ.get("LINE_USER_ID"),
                "GMAIL_ADDRESS": os.environ.get("GMAIL_ADDRESS"),
                "GMAIL_APP_PASSWORD": os.environ.get("GMAIL_APP_PASSWORD"),
                "MAIL_TO": os.environ.get("MAIL_TO"),
            }
            try:
                notify(
                    notify_papers,
                    issue_url or "(Issue作成に失敗しました。リポジトリのIssue一覧をご確認ください)",
                    date_str,
                    env,
                    always_email=config.get("always_email", False),
                    abstract_only_count=abstract_only_count,
                )
            except Exception as e:
                print(f"[main] 通知処理でエラーが発生しました: {e}")
        else:
            print("[main] (DRY_RUN) 通知をスキップしました。以下が送信予定の内容です:")
            for p in notify_papers:
                alert_mark = "🔔 " if p.get("author_alert") else ""
                print(f"  - {alert_mark}[{p.get('category')}/{p.get('score')}] {p['title']}")
    else:
        print("[main] 該当論文なし、かつ notify_when_empty=false のため通知をスキップします")

    # --- 5. 状態ファイル更新 ---
    if not dry_run:
        for p in new_papers:
            seen_ids[p["id"]] = date_str
        seen_ids = prune_seen_ids(seen_ids)
        save_seen_ids(seen_ids)
        save_feedback(FEEDBACK_PATH, feedback_list)
        print("[main] 状態ファイル (seen_ids.json / feedback.json) を更新しました")
    else:
        print("[main] (DRY_RUN) 状態ファイルの保存をスキップしました")

    print("[main] 処理完了")


if __name__ == "__main__":
    main()
