"""
arXiv hep-th 論文推薦Bot のエントリポイント。

毎朝GitHub Actionsから実行され、以下の流れで処理する:
  1. フィードバック回収 (GitHub Issueのコメントを読む)
  2. arXivから新着論文を取得
  3. Geminiで関連度判定・翻訳
  4. スコアが閾値以上の論文を選別
  5. LINE / メールで通知
  6. GitHub Issueを作成(フィードバック収集用)
  7. 状態ファイル (seen_ids.json / feedback.json) を更新

環境変数 DRY_RUN=1 のときは、通知・Issue作成/close・状態ファイル保存を行わず、
ログ出力のみで動作確認できる(テスト用)。
"""

import sys

# デバッグ用: Pythonがこのファイルを実際に実行しているかを確認するための強制出力。
# これが表示されない場合、コードの問題ではなくログ表示側の問題と判断できる。
print("[DEBUG] main.py の実行を開始しました", flush=True)
sys.stdout.flush()

import json
import os
from datetime import datetime, timedelta, timezone

import yaml

from arxiv_fetch import fetch_recent_papers
from feedback import collect_feedback, create_daily_issue, load_feedback, save_feedback
from judge_translate import judge_and_translate_papers
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

    # --- 2. arXiv新着取得 ---
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
    print(f"[main] 新着(未通知)論文: {len(new_papers)}件")

    # --- 3. Geminiで判定+翻訳 ---
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
                    config.get("score_threshold", 6),
                )
            except Exception as e:
                print(f"[main] Gemini判定でエラーが発生しました: {e}")

    # --- 4. スコアで選別 ---
    threshold = config.get("score_threshold", 6)
    max_papers = config.get("max_papers", 8)
    selected = [p for p in judged if p.get("score", 0) >= threshold]
    selected.sort(key=lambda p: p.get("score", 0), reverse=True)
