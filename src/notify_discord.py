"""
Discord 通知モジュール(Bot Token による REST 送信のみ)。

- Gateway(WebSocket)には接続しない。GitHub Actions から cron で叩くだけで完結する
- 1論文 = 1メッセージ = 1 embed で投稿する(POST /channels/{cid}/messages)
- 投稿後、Bot 自身が 📖 👍 👎 のリアクションを付ける。ユーザーはこれを押すだけで
  フィードバックを返せる(回収は reactions.py が別ワークフローで行う)
- Discord の embed 制限に合わせて description を切り詰める
  (title 256字 / description 4096字 / 1メッセージ内の全embed合計 6000字)。
  LINE版のような「優先度の低い論文を丸ごと間引く」ロジックは不要で、
  1論文1メッセージなので長すぎる論文の本文末尾を削るだけでよい
- arXiv の URL は <...> で囲み、Discord 側のリンクプレビュー展開を抑制する
- 429 (レート制限) が返った場合は retry_after 秒だけ待って再送する

環境変数:
  DISCORD_BOT_TOKEN  ... Discord Developer Portal で発行した Bot Token
  DISCORD_CHANNEL_ID ... 投稿先チャンネルのID(開発者モードでコピーできる)
DRY_RUN=1 のときは送信せず、送信予定の内容をログ出力するだけにする(main.py の流儀に合わせる)。
"""

import json
import time
from urllib.parse import quote

import requests

DISCORD_API_BASE = "https://discord.com/api/v10"

# Discord の embed 制限
EMBED_TITLE_MAX = 256
EMBED_DESCRIPTION_MAX = 4096
EMBED_TOTAL_MAX = 6000

READ_EMOJI = "📖"
LIKE_EMOJI = "👍"
DISLIKE_EMOJI = "👎"
# 投稿直後に Bot 自身が付けるリアクション(この順番でユーザーに見える)
REACTION_EMOJIS = (READ_EMOJI, LIKE_EMOJI, DISLIKE_EMOJI)

CATEGORY_LABELS = {
    "must_read": "🔴 must_read",
    "worth_reading": "🟡 worth_reading",
    "abstract_only": "⚪ abstract_only",
    "ignore": "⚫ ignore",
}
CATEGORY_COLORS = {
    "must_read": 0xE74C3C,
    "worth_reading": 0xF1C40F,
    "abstract_only": 0x95A5A6,
    "ignore": 0x95A5A6,
}
ALERT_COLOR = 0x9B59B6

# 429 を受けたときの再試行回数と、1回あたりの最大待機秒数。
# Actions のジョブが待ちっぱなしにならないよう上限を設ける。
MAX_RETRIES = 3
MAX_RETRY_WAIT_SEC = 60
# 連投によるレート制限を避けるため、メッセージ間に少しだけ間隔を空ける
SEND_INTERVAL_SEC = 0.5

TIMEOUT_SEC = 30
# ファイル送信は本文投稿より時間がかかるため、専用のタイムアウトを設ける
UPLOAD_TIMEOUT_SEC = 120


def _headers(token):
    return {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
        "User-Agent": "arxiv-hep-th-bot",
    }


def _multipart_headers(token):
    """
    ファイル添付(multipart/form-data)用のヘッダ。
    Content-Type は requests が boundary 付きで自動生成するため、
    ここでは含めない(_headers() の application/json をそのまま使うと壊れる)。
    """
    return {
        "Authorization": f"Bot {token}",
        "User-Agent": "arxiv-hep-th-bot",
    }


def _truncate(text, limit):
    """limit 字に収まるよう末尾を切り詰める(切り詰めた場合は末尾を … にする)。"""
    if not text:
        return ""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def suppress_preview(url):
    """arXiv の URL を <...> で囲み、Discord のリンクプレビュー展開を抑制する。"""
    if not url:
        return ""
    return f"<{url}>"


def _retry_after_seconds(resp):
    """429 応答から待機秒数を読む。ボディが壊れていてもヘッダにフォールバックする。"""
    wait = None
    try:
        wait = resp.json().get("retry_after")
    except Exception:
        wait = None
    if wait is None:
        wait = resp.headers.get("Retry-After")
    try:
        wait = float(wait)
    except (TypeError, ValueError):
        wait = 1.0
    # 負値や極端に長い待機は握りつぶす
    return max(0.0, min(wait, MAX_RETRY_WAIT_SEC))


def discord_request(method, path, token, json_body=None, params=None):
    """
    Discord API を叩く。429 が返ったら retry_after 秒待って再試行する。
    429以外の 2xx でないレスポンスは例外を投げる。
    リアクション回収 (reactions.py) からも使うため公開関数にしている。
    """
    url = f"{DISCORD_API_BASE}{path}"
    last_resp = None
    for attempt in range(MAX_RETRIES + 1):
        resp = requests.request(
            method,
            url,
            headers=_headers(token),
            json=json_body,
            params=params,
            timeout=TIMEOUT_SEC,
        )
        last_resp = resp
        if resp.status_code == 429:
            wait = _retry_after_seconds(resp)
            print(
                f"[notify_discord] レート制限(429)を受けました。{wait}秒待って再試行します "
                f"({attempt + 1}/{MAX_RETRIES})"
            )
            if attempt < MAX_RETRIES:
                time.sleep(wait)
                continue
            break
        resp.raise_for_status()
        return resp

    print(f"[notify_discord] レート制限の再試行回数({MAX_RETRIES}回)を使い切りました: {method} {path}")
    last_resp.raise_for_status()
    return last_resp


def _display_title(paper):
    """embed のタイトルには和訳を優先して使う(原題は本文側に出す)。"""
    return paper.get("title_ja") or paper.get("title") or "(タイトルなし)"


def _build_description_lines(paper):
    """
    embed 本文の行を組み立てる。
    切り詰めは末尾から行われるため、URL や著者など「必ず残したい情報」を先頭側に置き、
    長くなりがちなアブスト和訳を最後に置いている。
    """
    lines = []

    if paper.get("author_alert"):
        lines.append(f"🔔 **著者アラート: {paper.get('matched_author', '')}**")

    category = paper.get("category", "ignore")
    lines.append(f"★{paper.get('score', 0)}/10 ・ {CATEGORY_LABELS.get(category, category)}")
    lines.append(suppress_preview(paper.get("url", "")))

    authors = ", ".join(paper.get("authors", []))
    if authors:
        lines.append(f"**著者:** {authors}")

    title_ja = paper.get("title_ja")
    if title_ja and paper.get("title") and title_ja != paper.get("title"):
        lines.append(f"**原題:** {paper['title']}")

    if paper.get("reason"):
        lines.append("")
        lines.append(f"**理由:** {paper['reason']}")
    if paper.get("one_liner"):
        lines.append(f"**一言:** {paper['one_liner']}")
    if paper.get("check_points"):
        lines.append(f"**チェック点:** {paper['check_points']}")
    if paper.get("suggested_action"):
        lines.append(f"**推奨行動:** {paper['suggested_action']}")

    if paper.get("abstract_ja"):
        lines.append("")
        lines.append("**アブスト和訳**")
        lines.append(paper["abstract_ja"])

    return lines


def build_embed(paper):
    """
    1論文分の embed を組み立てる。
    title は 256字、description は 4096字、かつ title + description + footer の合計が
    6000字を超えないよう description 側を切り詰める。
    """
    title = _display_title(paper)
    if paper.get("author_alert"):
        title = f"🔔 {title}"
    title = _truncate(title, EMBED_TITLE_MAX)

    footer_text = f"arXiv:{paper.get('id', '')}"

    description = "\n".join(_build_description_lines(paper))
    # 6000字の制限は title / description / footer の文字数の合計にかかる
    budget = min(EMBED_DESCRIPTION_MAX, EMBED_TOTAL_MAX - len(title) - len(footer_text))
    description = _truncate(description, budget)

    color = ALERT_COLOR if paper.get("author_alert") else CATEGORY_COLORS.get(
        paper.get("category", "ignore"), CATEGORY_COLORS["ignore"]
    )

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "footer": {"text": footer_text},
    }
    if paper.get("url"):
        # embed.url を付けるとタイトル自体がリンクになる(URLは6000字の集計対象外)
        embed["url"] = paper["url"]
    return embed


def send_paper(paper, token, channel_id, dry_run=False):
    """
    1論文を1メッセージとして投稿し、message_id(str)を返す。
    DRY_RUN のときは送信せず None を返す。
    """
    embed = build_embed(paper)
    if dry_run:
        print(
            f"[notify_discord] (DRY_RUN) 送信予定: {embed['title']} "
            f"(本文{len(embed['description'])}字)"
        )
        return None

    resp = discord_request(
        "POST",
        f"/channels/{channel_id}/messages",
        token,
        json_body={"embeds": [embed]},
    )
    message_id = str(resp.json()["id"])
    print(f"[notify_discord] 投稿しました: {paper.get('id', '')} -> message_id={message_id}")
    return message_id


def add_reactions(message_id, token, channel_id, emojis=REACTION_EMOJIS, dry_run=False):
    """
    投稿済みメッセージに Bot 自身がリアクションを付ける。
    絵文字は URL エンコードする必要がある(PUT .../reactions/{emoji}/@me)。
    1つ失敗しても残りは試みる(通知自体は成功しているため)。
    """
    if dry_run:
        print(f"[notify_discord] (DRY_RUN) リアクション付与をスキップしました: {' '.join(emojis)}")
        return

    for emoji in emojis:
        encoded = quote(emoji, safe="")
        try:
            discord_request(
                "PUT",
                f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
                token,
            )
        except Exception as e:
            print(
                f"[notify_discord] リアクション {emoji} の付与に失敗しました "
                f"(message_id={message_id}): {e}"
            )


def notify_discord(papers, env, date_str, dry_run=False):
    """
    論文リストを Discord に投稿する。

    戻り値: {arxiv_id: {"message_id": str, "channel_id": str, "date": str, "title": str}}
    DRY_RUN の場合と、送信に失敗した論文は戻り値に含めない。
    1論文の失敗で全体を止めないよう、例外は論文ごとに握りつぶしてログに残す。
    """
    token = env.get("DISCORD_BOT_TOKEN")
    channel_id = env.get("DISCORD_CHANNEL_ID")

    if not token or not channel_id:
        print(
            "[notify_discord] DISCORD_BOT_TOKEN / DISCORD_CHANNEL_ID が未設定のため、"
            "Discord通知をスキップします"
        )
        return {}

    if not papers:
        print("[notify_discord] 通知対象の論文がないため、何も投稿しませんでした")
        return {}

    posted = {}
    for i, paper in enumerate(papers):
        if i > 0 and not dry_run:
            time.sleep(SEND_INTERVAL_SEC)
        try:
            message_id = send_paper(paper, token, channel_id, dry_run=dry_run)
        except Exception as e:
            print(f"[notify_discord] 投稿に失敗しました ({paper.get('id', '')}): {e}")
            continue

        if message_id is None:
            continue

        add_reactions(message_id, token, channel_id, dry_run=dry_run)
        posted[paper["id"]] = {
            "message_id": message_id,
            "channel_id": str(channel_id),
            "date": date_str,
            "title": paper.get("title", ""),
        }

    print(f"[notify_discord] {len(posted)}件を投稿しました")
    return posted


def send_file_reply(channel_id, message_id, token, filename, file_bytes,
                     text="", dry_run=False):
    """
    既存メッセージへの返信として、ファイルを添付したメッセージを送る。

    📖 リアクションを受けて生成した解説書(HTML)を、元の論文メッセージへの
    返信として投稿するために使う。multipart/form-data で送るため、
    application/json 前提の discord_request / _headers はそのまま使えない。

    message_reference.fail_if_not_exists を False にしているのは、元メッセージが
    削除されていても返信ではなく通常メッセージとして投稿させ、解説書自体は
    失わないようにするため。

    429 (レート制限) のリトライ方針は discord_request と同じ(_retry_after_seconds を
    使い、MAX_RETRIES 回まで待って再試行し、使い切ったら最後のレスポンスで
    raise_for_status する)。discord_request 自体は json ボディ専用のためここでは
    使わず、ループを自前で書いている。

    戻り値: 送信したメッセージのID(str)。DRY_RUN のときは送信せず None を返す。
    """
    if dry_run:
        print(
            f"[notify_discord] (DRY_RUN) 返信予定: {filename} ({len(file_bytes)}バイト)"
        )
        return None

    url = f"{DISCORD_API_BASE}/channels/{channel_id}/messages"
    payload = {
        "content": text,
        "message_reference": {
            "message_id": str(message_id),
            "channel_id": str(channel_id),
            "fail_if_not_exists": False,
        },
        "attachments": [{"id": 0, "filename": filename}],
    }
    data = {"payload_json": json.dumps(payload, ensure_ascii=False)}
    files = {"files[0]": (filename, file_bytes, "text/html")}

    last_resp = None
    for attempt in range(MAX_RETRIES + 1):
        resp = requests.request(
            "POST",
            url,
            headers=_multipart_headers(token),
            data=data,
            files=files,
            timeout=UPLOAD_TIMEOUT_SEC,
        )
        last_resp = resp
        if resp.status_code == 429:
            wait = _retry_after_seconds(resp)
            print(
                f"[notify_discord] レート制限(429)を受けました。{wait}秒待って再試行します "
                f"({attempt + 1}/{MAX_RETRIES})"
            )
            if attempt < MAX_RETRIES:
                time.sleep(wait)
                continue
            break
        resp.raise_for_status()
        message_id_out = str(resp.json()["id"])
        print(f"[notify_discord] 解説書を返信しました: message_id={message_id_out}")
        return message_id_out

    print(
        f"[notify_discord] レート制限の再試行回数({MAX_RETRIES}回)を使い切りました: "
        f"POST /channels/{channel_id}/messages (ファイル添付)"
    )
    last_resp.raise_for_status()
    return str(last_resp.json()["id"])
