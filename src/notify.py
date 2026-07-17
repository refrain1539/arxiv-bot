"""
LINE / メール通知モジュール。

- LINE Messaging APIのPush Messageを第一優先で使う
- LINEが未設定、または送信失敗した場合はGmail SMTPでメール送信にフォールバックする
- always_email設定がTrueの場合は、LINE成功時にもメールを送る
- 通知はカテゴリ別に見出しを付けて全件表示する(must_read/worth_reading/abstract_only)
- must_readにもスコア(★)とアブストラクト全訳を表示する
- 著者アラート(author_alert)論文は🔔付きで最上部に表示する
- LINEは5000字制限があるため、実際の文字数を毎回ログに出力する。基本は全件表示する
  方針だが、上限に近づいた場合のみ優先度の低いカテゴリ(abstract_only→worth_reading)の
  スコアが低い論文から丸ごと間引く(must_read・著者アラートは間引かない)。
  それでも収まらない場合のみ最終手段として末尾を強制的に切り詰める
"""

import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_MAX_LEN = 5000
# LINE側の文字数カウントとの誤差に備えて、切り詰めの目標は上限より少し余裕を持たせる
LINE_SAFE_MAX_LEN = LINE_MAX_LEN - 100

CATEGORY_TIER_ORDER = ["must_read", "worth_reading", "abstract_only"]
CATEGORY_LABELS = {
    "must_read": "🔴 must_read(今すぐ読むべき)",
    "worth_reading": "🟡 worth_reading(今週中に目を通す価値あり)",
    "abstract_only": "⚪ abstract_only(動向として要約のみ把握)",
}


def _short_date(date_str):
    """'2026-07-05' -> '7/5' のような短い日付表記に変換する。"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{d.month}/{d.day}"
    except ValueError:
        return date_str


def _display_title(p):
    """LINEでは原題(英語)を表示する。"""
    return p.get("title") or p.get("title_ja", "")


def _format_alert_block(p):
    matched = p.get("matched_author", "")
    authors = ", ".join(p.get("authors", []))
    reason = p.get("reason") or p.get("one_liner") or ""
    return (
        f"🔔 著者アラート: {matched}\n"
        f"{_display_title(p)}\n"
        f"著者: {authors}\n"
        f"{reason}\n"
        f"{p['url']}\n\n"
    )


def _format_must_read_block(index, p):
    authors = ", ".join(p.get("authors", []))
    reason = p.get("reason") or p.get("one_liner") or ""
    abstract = p.get("abstract_ja") or "(翻訳なし)"
    return (
        f"[{index}] {_display_title(p)} (★{p.get('score', 0)})\n"
        f"著者: {authors}\n"
        f"{reason}\n"
        f"{abstract}\n"
        f"{p['url']}\n\n"
    )


def _comment_text(p):
    """one_linerが空の場合でも何かしら表示されるようにフォールバックする。"""
    return p.get("one_liner") or p.get("reason") or "(コメントなし)"


def _format_worth_reading_block(index, p):
    return f"[{index}] {_display_title(p)} (★{p.get('score', 0)})\n{_comment_text(p)}\n{p['url']}\n\n"


def _format_abstract_only_block(index, p):
    return f"[{index}] {_display_title(p)} (★{p.get('score', 0)})\n{_comment_text(p)}\n{p['url']}\n\n"


_BLOCK_FORMATTERS = {
    "must_read": _format_must_read_block,
    "worth_reading": _format_worth_reading_block,
    "abstract_only": _format_abstract_only_block,
}


def _render_line_text(header, footer, alert_blocks, tier_groups, omitted_counts):
    parts = list(alert_blocks)
    index = 0
    for category, group in tier_groups.items():
        parts.append(f"――― {CATEGORY_LABELS[category]} ―――\n")
        if not group and not omitted_counts.get(category):
            parts.append("該当なし\n\n")
            continue
        formatter = _BLOCK_FORMATTERS[category]
        for p in group:
            index += 1
            parts.append(formatter(index, p))
        if omitted_counts.get(category):
            parts.append(f"(文字数の都合で他{omitted_counts[category]}件省略 → Issue参照)\n\n")
    return header + "".join(parts) + footer


def build_line_text(papers, issue_url, date_str, notify_categories=None):
    """
    LINE用のテキストメッセージを組み立てる。基本は全件表示し、省略は行わない。
    ただし合計がLINEの上限(5000字)に近づいた場合のみ、優先度の低いカテゴリ
    (abstract_only→worth_reading)のスコアが低い論文から丸ごと間引く
    (must_read・著者アラートは間引かない)。
    notify_categoriesに含まれるカテゴリは、該当論文が0件でも見出しと
    「該当なし」を表示する(通知対象外のカテゴリについては何も表示しない)。
    """
    header = f"📄 今朝の hep-th ({_short_date(date_str)}) — {len(papers)}件\n\n"
    footer = f"\n👍/👎 はこちら: {issue_url}"

    tracked_categories = notify_categories if notify_categories is not None else CATEGORY_TIER_ORDER

    if not papers:
        return header + "本日は該当する論文がありませんでした。" + footer

    alert_papers = [p for p in papers if p.get("author_alert")]
    non_alert = [p for p in papers if not p.get("author_alert")]

    alert_blocks = [_format_alert_block(p) for p in alert_papers]

    # カテゴリごとに論文リスト(スコア降順のまま)を保持しておき、必要な場合のみ
    # 末尾(=スコアが低い方)から間引く
    tier_groups = {
        category: [p for p in non_alert if p.get("category") == category]
        for category in CATEGORY_TIER_ORDER
        if category in tracked_categories
    }
    omitted_counts = {category: 0 for category in tier_groups}

    text = _render_line_text(header, footer, alert_blocks, tier_groups, omitted_counts)

    drop_order = [c for c in ("abstract_only", "worth_reading") if c in tier_groups]
    for category in drop_order:
        while len(text) > LINE_SAFE_MAX_LEN and tier_groups[category]:
            tier_groups[category].pop()
            omitted_counts[category] += 1
            text = _render_line_text(header, footer, alert_blocks, tier_groups, omitted_counts)
        if len(text) <= LINE_SAFE_MAX_LEN:
            break

    if len(text) > LINE_MAX_LEN:
        print(
            f"[notify] 警告: 著者アラート・must_readだけでもLINE上限{LINE_MAX_LEN}字を"
            f"超えています({len(text)}字)。末尾を強制的に切り詰めます"
        )
        text = text[:LINE_MAX_LEN]
    else:
        print(f"[notify] LINE本文の文字数: {len(text)}字 / 上限{LINE_MAX_LEN}字(安全域{LINE_SAFE_MAX_LEN}字)")

    return text


def build_email_html(papers, issue_url, date_str):
    """メール用のHTML本文を組み立てる(文字数制限がないため全件表示)。"""
    alert_papers = [p for p in papers if p.get("author_alert")]
    non_alert = [p for p in papers if not p.get("author_alert")]

    rows = []
    if not papers:
        rows.append("<p>本日は該当する論文がありませんでした。</p>")

    for p in alert_papers:
        authors = ", ".join(p.get("authors", []))
        rows.append(
            f"""
            <h3>🔔 著者アラート: {p.get('matched_author', '')} —
            <a href="{p['url']}">{_display_title(p)}</a></h3>
            <p>著者: {authors}</p>
            <p>{p.get('reason') or p.get('one_liner', '')}</p>
            <p style="white-space: pre-wrap;">{p.get('abstract_ja', '')}</p>
            <hr/>
            """
        )

    for category in CATEGORY_TIER_ORDER:
        group = [p for p in non_alert if p.get("category") == category]
        if not group:
            continue
        rows.append(f"<h2>{CATEGORY_LABELS[category]}</h2>")
        for i, p in enumerate(group, start=1):
            authors = ", ".join(p.get("authors", []))
            rows.append(
                f"""
                <h3>[{category} {i}] <a href="{p['url']}">{_display_title(p)}</a></h3>
                <p>著者: {authors}</p>
                <p>スコア: {p.get('score', 0)}/10 ・ 理由: {p.get('reason') or p.get('one_liner', '')}</p>
                <p style="white-space: pre-wrap;">{p.get('abstract_ja', '')}</p>
                <hr/>
                """
            )

    return f"""
    <html><body>
    <h2>📄 今朝の hep-th ({_short_date(date_str)}) — {len(papers)}件</h2>
    {''.join(rows)}
    <p>👍/👎 はこちら: <a href="{issue_url}">{issue_url}</a></p>
    </body></html>
    """


def send_line_message(text, token, user_id):
    """LINE Messaging APIでメッセージを送信する。成功したらTrueを返す。"""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    try:
        resp = requests.post(LINE_PUSH_URL, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        print("[notify] LINE通知に成功しました")
        return True
    except Exception as e:
        print(f"[notify] LINE通知に失敗しました: {e}")
        return False


def send_email(subject, html_body, gmail_address, gmail_app_password, mail_to):
    """Gmail SMTP(STARTTLS)でHTMLメールを送信する。成功したらTrueを返す。"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = gmail_address
        msg["To"] = mail_to
        msg.attach(MIMEText(html_body, "html", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.starttls()
            server.login(gmail_address, gmail_app_password)
            server.sendmail(gmail_address, [mail_to], msg.as_string())

        print("[notify] メール送信に成功しました")
        return True
    except Exception as e:
        print(f"[notify] メール送信に失敗しました: {e}")
        return False


def notify(papers, issue_url, date_str, env, always_email=False, notify_categories=None):
    """
    LINEを第一優先、失敗/未設定時はメールにフォールバックして通知する。
    両方失敗しても例外は投げず、ログに残すだけにする(Issueは既に作成済みのため)。
    """
    line_token = env.get("LINE_CHANNEL_ACCESS_TOKEN")
    line_user_id = env.get("LINE_USER_ID")
    gmail_address = env.get("GMAIL_ADDRESS")
    gmail_app_password = env.get("GMAIL_APP_PASSWORD")
    mail_to = env.get("MAIL_TO")

    line_ok = False
    if line_token and line_user_id:
        text = build_line_text(papers, issue_url, date_str, notify_categories=notify_categories)
        line_ok = send_line_message(text, line_token, line_user_id)
    else:
        print("[notify] LINEの環境変数が未設定のため、メールにフォールバックします")

    email_configured = bool(gmail_address and gmail_app_password and mail_to)
    need_email = (not line_ok) or always_email

    if need_email:
        if email_configured:
            subject = f"📄 今朝の hep-th ({_short_date(date_str)}) — {len(papers)}件"
            html = build_email_html(papers, issue_url, date_str)
            send_email(subject, html, gmail_address, gmail_app_password, mail_to)
        else:
            print("[notify] メールの環境変数も未設定のため、メール送信をスキップしました")

    if not line_ok and not email_configured:
        print("[notify] LINE・メールともに送信できませんでした。Issueで内容を確認してください。")
