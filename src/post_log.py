"""
Discordに投稿した論文のmessage_idを記録するファイル(data/posted_messages.json)の
読み書きを行うモジュール。

後日Discordのリアクション(フィードバック)を回収する際に、どのarXiv IDがどの
message_id/channel_idで投稿されたかを引くために使う。
"""

import json
import os
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POSTED_MESSAGES_PATH = os.path.join(BASE_DIR, "data", "posted_messages.json")
RETENTION_DAYS = 90


def load_posted(path=POSTED_MESSAGES_PATH):
    """posted_messages.jsonを読み込んでdictを返す。

    ファイルが存在しない・JSONが壊れている・中身がdictでない(壊れたファイル対策)
    場合は空dictを返す。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_posted(posted, path=POSTED_MESSAGES_PATH):
    """postedをJSONファイルに書き出す。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(posted, f, ensure_ascii=False, indent=2)


def record_post(posted, arxiv_id, message_id, channel_id, date_str, title=""):
    """postedにarxiv_idの投稿記録を追加し、posted自体を返す(破壊的更新)。

    message_id/channel_idはDiscordの64bit IDで、JSONの数値として保存すると
    精度を失う可能性があるため、必ずstrに変換して保存する。
    """
    posted[arxiv_id] = {
        "message_id": str(message_id),
        "channel_id": str(channel_id),
        "date": date_str,
        "title": title,
    }
    return posted


def prune_posted(posted, retention_days=RETENTION_DAYS, now=None):
    """retention_daysより古いエントリを削除した新しいdictを返す(元のdictは変更しない)。

    dateの形式が壊れている、またはdateキーが無いエントリは、安全側に倒して残す
    (main.pyのprune_seen_idsと同じ方針)。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    pruned = {}
    for arxiv_id, entry in posted.items():
        try:
            d = datetime.strptime(entry["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if d >= cutoff:
                pruned[arxiv_id] = entry
        except (ValueError, TypeError, KeyError):
            # 日付形式が壊れている・dateキーが無い場合は安全側に倒して残す
            pruned[arxiv_id] = entry
    return pruned


def iter_recent(posted, days, now=None):
    """dateが過去days日以内のエントリのみを(arxiv_id, entry)のタプルのリストで返す。

    日付が壊れている(またはdateキーが無い)エントリはスキップする
    (pruneとは逆に、ここでは含めない)。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    result = []
    for arxiv_id, entry in posted.items():
        try:
            d = datetime.strptime(entry["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, KeyError):
            continue
        if d >= cutoff:
            result.append((arxiv_id, entry))
    return result
