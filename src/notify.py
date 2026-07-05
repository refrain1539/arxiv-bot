"""
LINE / メール通知モジュール。

- LINE Messaging APIのPush Messageを第一優先で使う
- LINEが未設定、または送信失敗した場合はGmail SMTPでメール送信にフォールバックする
- always_email設定がTrueの場合は、LINE成功時にもメールを送る
"""

import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_MAX_LEN = 5000


def _short_date(date_str):
    """'2026-07-05' -> '7/5' のような短い日付表記に変換する。"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        return f"{d.month}/{d.day}"
    except ValueError:
        return date_str


def build_line_text(papers, issue_url, date_str):
    """LINE用のテキストメッセージを組み立てる(5000字制限に収める)。"""
    header = f"📄 今朝の hep-th ({_short_date(date_str)}) — {len(papers)}件\n\n"
    footer = f"\n👍/👎 はこちら: {issue_url}"

    if not papers:
        return header + "本日は該当する論文がありませんでした。" + footer

    budget = LINE_MAX_LEN - len(header) - len(footer) - 50  # 余裕を持たせる
    body_parts = []
    used = 0
    truncated = False

    for i, p in enumerate(papers, start=1):
        abstract_preview = (p.get("abstract_ja") or "")[:200]
        block = (
            f"[{i}] {p['title']}\n"
            f"★{p.get('score', 0)} {p.get('one_liner', '')}\n"
            f"{abstract_preview}…\n"
            f"{p['url']}\n\n"
        )
        if used + len(block) > budget:
            truncated = True
            break
        body_parts.append(block)
        used += len(block)

    text = header + "".join(body_parts)
    if truncated:
        text += "(全文はIssue参照)\n"
    text += footer
    return text[:LINE_MAX_LEN]


def build_email_html(papers, issue_url, date_str):
    """メール用のHTML本文を組み立てる。"""
    rows = []
    if not papers:
        rows.append("<p>本日は該当する論文がありませんでした。</p>")
    for i, p in enumerate(papers, start=1):
        rows.append(
            f"""
            <h3>[{i}] <a href="{p['url']}">{p['title']}</a></h3>
            <p>スコア: {p.get('score', 0)}/10 ・ {p.get('one_liner', '')}</p>
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


def notify(papers, issue_url, date_str, env, always_email=False):
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
        text = build_line_text(papers, issue_url, date_str)
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
