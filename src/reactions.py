"""
Discord のリアクションを回収してフィードバックに反映するモジュール。

毎晩 .github/workflows/reactions.yml から実行され、以下を行う:
  1. data/posted_messages.json から過去N日分の投稿を取り出す
  2. 各メッセージについて 👍 👎 📖 を押したユーザーを取得する
     (GET /channels/{cid}/messages/{mid}/reactions/{emoji})
  3. 👍 → verdict="like"、👎 → verdict="dislike" として data/feedback.json に追記する
     (翌朝の Gemini 判定が feedback.json を読んで好みを学習する)
  4. 📖 が押された arxiv_id の一覧を返す(解説書生成は別タスクのため、ここでは返すだけ)

注意点:
- Bot 自身が投稿直後に 📖 👍 👎 を付けているため、users を見て Bot の user_id を
  必ず除外する。除外しないと全論文が「like かつ dislike」になってしまう
- Bot の user_id が取得できなかった場合は、誤ったフィードバックを書くくらいなら
  何もしない方が安全なので、回収自体を中止する
- feedback.json への書き込みは既存の feedback.py の load_feedback / save_feedback を
  使う(feedback.py 自体は変更しない)
- 同じ arxiv_id が既に feedback.json にある場合は追記しない。reactions.yml は
  過去N日分を毎晩走査するため、これがないと同じ論文が何度も積み上がる

環境変数:
  DISCORD_BOT_TOKEN  ... Discord Bot Token
  DISCORD_CHANNEL_ID ... posted_messages.json に channel_id が無い場合のフォールバック
  DRY_RUN=1          ... feedback.json / posted_messages.json を書き換えず、ログ出力のみ
"""

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

from feedback import load_feedback, save_feedback
from notify_discord import DISLIKE_EMOJI, LIKE_EMOJI, READ_EMOJI, discord_request
from post_log import (
    POSTED_MESSAGES_PATH,
    iter_recent,
    load_posted,
    prune_posted,
    save_posted,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FEEDBACK_PATH = os.path.join(BASE_DIR, "data", "feedback.json")

# 何日前までの投稿をリアクション回収の対象にするか
LOOKBACK_DAYS = 7
# 1メッセージ・1絵文字あたりに取得するユーザー数の上限(個人利用なので100で十分)
REACTION_USER_LIMIT = 100


def get_bot_user_id(token):
    """
    Bot 自身の user_id を取得する(GET /users/@me)。
    取得できなかった場合は None を返す。
    """
    try:
        resp = discord_request("GET", "/users/@me", token)
        return str(resp.json()["id"])
    except Exception as e:
        print(f"[reactions] Bot自身のuser_idの取得に失敗しました: {e}")
        return None


def fetch_reaction_users(channel_id, message_id, emoji, token):
    """
    指定メッセージの指定絵文字にリアクションしたユーザーのリストを取得する。
    メッセージが削除されている(404)などの場合は空リストを返す。
    """
    encoded = quote(emoji, safe="")
    path = f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}"
    try:
        resp = discord_request(
            "GET", path, token, params={"limit": REACTION_USER_LIMIT}
        )
        users = resp.json()
        return users if isinstance(users, list) else []
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status == 404:
            # そのメッセージが消えている、または誰もそのリアクションを押していない
            return []
        print(f"[reactions] リアクション取得に失敗しました (message_id={message_id}, {emoji}): {e}")
        return []
    except Exception as e:
        print(f"[reactions] リアクション取得に失敗しました (message_id={message_id}, {emoji}): {e}")
        return []


def _has_human_reaction(users, bot_user_id):
    """Bot 自身を除いた上で、誰か人間が押しているかを判定する。"""
    for user in users:
        if not isinstance(user, dict):
            continue
        if str(user.get("id")) != str(bot_user_id):
            return True
    return False


def scan_reactions(entries, token, bot_user_id, default_channel_id=None):
    """
    (arxiv_id, entry) のリストを走査し、各論文のリアクション状況を返す。

    戻り値: {arxiv_id: {"entry": entry, "like": bool, "dislike": bool, "read": bool}}
    Bot 自身のリアクションは除外済み。
    """
    result = {}
    for arxiv_id, entry in entries:
        message_id = entry.get("message_id")
        channel_id = entry.get("channel_id") or default_channel_id
        if not message_id or not channel_id:
            print(f"[reactions] message_id / channel_id が無いためスキップします: {arxiv_id}")
            continue

        status = {"entry": entry, "like": False, "dislike": False, "read": False}
        for key, emoji in (
            ("like", LIKE_EMOJI),
            ("dislike", DISLIKE_EMOJI),
            ("read", READ_EMOJI),
        ):
            users = fetch_reaction_users(channel_id, message_id, emoji, token)
            status[key] = _has_human_reaction(users, bot_user_id)
        result[arxiv_id] = status

    return result


def build_feedback_entries(scanned, existing_feedback, date_str):
    """
    リアクション状況から feedback.json に追記するエントリのリストを作る。

    - 既に同じ arxiv_id のエントリがある場合は追記しない(毎晩の重複回収を防ぐ)
    - 👍 と 👎 の両方が押されている場合は like を優先する
      (後から気が変わって押し直した、と解釈する)
    - title は judge_translate.build_feedback_context が f["title"] を直接参照するため
      必ず入れる。posted_messages に無い場合は arxiv_id で代用する
    """
    known_ids = {f.get("arxiv_id") for f in existing_feedback}
    new_entries = []

    for arxiv_id, status in scanned.items():
        if not (status["like"] or status["dislike"]):
            continue
        if arxiv_id in known_ids:
            continue
        if status["like"] and status["dislike"]:
            print(f"[reactions] 👍と👎の両方が押されています。likeとして扱います: {arxiv_id}")

        verdict = "like" if status["like"] else "dislike"
        title = status["entry"].get("title") or arxiv_id
        new_entries.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "verdict": verdict,
                "date": date_str,
            }
        )
        known_ids.add(arxiv_id)

    return new_entries


def get_read_requests(scanned):
    """
    📖 が押された(= 解説書がほしい)論文の arxiv_id のリストを返す。
    解説書の生成自体は別タスクのため、ここでは一覧を返すだけにとどめる。
    """
    return [arxiv_id for arxiv_id, status in scanned.items() if status["read"]]


def collect_reactions(
    token,
    channel_id=None,
    days=LOOKBACK_DAYS,
    feedback_path=FEEDBACK_PATH,
    posted_path=POSTED_MESSAGES_PATH,
    dry_run=False,
    now=None,
):
    """
    過去 days 日分の投稿からリアクションを回収し、feedback.json に反映する。

    戻り値: (追記したfeedbackエントリのリスト, 📖が押されたarxiv_idのリスト)
    DRY_RUN のときはファイルを書き換えず、回収結果だけを返す。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    # 通知と同じ JST 基準の日付を使う
    date_str = (now + timedelta(hours=9)).strftime("%Y-%m-%d")

    if not token:
        print("[reactions] DISCORD_BOT_TOKEN が未設定のため、リアクション回収をスキップします")
        return [], []

    bot_user_id = get_bot_user_id(token)
    if bot_user_id is None:
        print(
            "[reactions] Bot自身のuser_idが分からないと、Botが付けたリアクションを"
            "人間の反応と誤認してしまうため、回収を中止します"
        )
        return [], []

    posted = load_posted(posted_path)
    entries = iter_recent(posted, days, now=now)
    print(f"[reactions] 過去{days}日分の投稿{len(entries)}件を対象にリアクションを確認します")
    if not entries:
        return [], []

    scanned = scan_reactions(entries, token, bot_user_id, default_channel_id=channel_id)

    existing_feedback = load_feedback(feedback_path)
    new_entries = build_feedback_entries(scanned, existing_feedback, date_str)
    read_ids = get_read_requests(scanned)

    for entry in new_entries:
        mark = "👍" if entry["verdict"] == "like" else "👎"
        print(f"[reactions] {mark} {entry['arxiv_id']} {entry['title']}")
    if read_ids:
        print(f"[reactions] 📖 解説書リクエスト: {', '.join(read_ids)}")

    if dry_run:
        print(
            f"[reactions] (DRY_RUN) feedback {len(new_entries)}件の追記と"
            "posted_messages.json の更新をスキップしました"
        )
        return new_entries, read_ids

    if new_entries:
        existing_feedback.extend(new_entries)
        save_feedback(feedback_path, existing_feedback)
        print(f"[reactions] feedback.json に{len(new_entries)}件追記しました")
    else:
        print("[reactions] 新しいリアクションはありませんでした")

    pruned = prune_posted(posted, now=now)
    if len(pruned) != len(posted):
        print(f"[reactions] 古い投稿記録を{len(posted) - len(pruned)}件削除しました")
    save_posted(pruned, posted_path)

    return new_entries, read_ids


def main():
    dry_run = os.environ.get("DRY_RUN") == "1"
    if dry_run:
        print("=== DRY_RUNモードで実行します(feedback.json / posted_messages.json を更新しません) ===")

    collect_reactions(
        token=os.environ.get("DISCORD_BOT_TOKEN", ""),
        channel_id=os.environ.get("DISCORD_CHANNEL_ID") or None,
        dry_run=dry_run,
    )
    print("[reactions] 処理完了")


if __name__ == "__main__":
    main()
