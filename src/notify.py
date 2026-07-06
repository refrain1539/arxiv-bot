"""
LINE / メール通知モジュール。

- LINE Messaging APIのPush Messageを第一優先で使う
- LINEが未設定、または送信失敗した場合はGmail SMTPでメール送信にフォールバックする
- always_email設定がTrueの場合は、LINE成功時にもメールを送る
- 通知は二層構造: must_read(詳細) / worth_reading(簡易) / abstract_only(件数のみ)
- 著者アラート(author_alert)論文は🔔付きで最上部に表示する
- LINEは5000字制限があるため、収まらない場合はworth_reading以下を省略しIssue誘導に置き換える
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


def _display_title(p):
    return p.get("title_ja") or p.get("title", "")


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
    return f"[{index}] {_display_title(p)}\n著者: {authors}\n{reason}\n{p['url']}\n\n"


def _format_worth_reading_block(index, p):
    return f"[{index}] {_display_title(p)} (★{p.get('score', 0)})\n{p['url']}\n\n"


def build_line_text(papers, issue_url, date_str, abstract_only_count=0):
    """LINE用のテキストメッセージを組み立てる(5000字制限に収める)。"""
    header = f"📄 今朝の hep-th ({_short_date(date_str)}) — {len(papers)}件\n\n"
    footer = f"\n👍/👎 はこちら: {issue_url}"

    if not papers and not abstract_only_count:
        return header + "本日は該当する論文がありませんでした。" + footer

    alert_papers = [p for p in papers if p.get("author_alert")]
    non_alert = [p for p in papers if not p.get("author_alert")]
    must_read = [p for p in non_alert if p.get("category") == "must_read"]
    worth_reading = [p for p in non_alert if p.get("category") != "must_read"]

    budget = LINE_MAX_LEN - len(header) - len(footer) - 50  # 余裕を持たせる
    body_parts = []
    used = 0

    # 著者アラートはスコア・文字数に関係なく必ず含める
    for p in alert_papers:
        block = _format_alert_block(p)
        body_parts.append(block)
        used += len(block)

    must_read_truncated = False
    for i, p in enumerate(must_read, start=1):
        block = _format_must_read_block(i, p)
        if used + len(block) > budget:
            must_read_truncated = True
            break
        body_parts.append(block)
        used += len(block)

    worth_reading_omitted = len(worth_reading) if must_read_truncated else 0
    if not must_read_truncated:
        for i, p in enumerate(worth_reading, start=len(must_read) + 1):
            block = _format_worth_reading_block(i, p)
            if used + len(block) > budget:
                worth_reading_omitted = len(worth_reading) - (i - len(must_read) - 1)
                break
            body_parts.append(block)
            used += len(block)

    text = header + "".join(body_parts)

    notes = []
    if worth_reading_omitted:
        notes.append(f"worth_reading等{worth_reading_omitted}件は文字数の都合で省略")
    if abstract_only_count:
        notes.append(f"他に要約把握{abstract_only_count}本")
    if notes:
        text += "(" + " / ".join(notes) + " → Issue参照)\n"

    text += footer
    return text[:LINE_MAX_LEN]


def build_email_html(papers, issue_url, date_str, abstract_only_count=0):
    """メール用のHTML本文を組み立てる(文字数制限がないため全件表示)。"""
    alert_papers = [p for p in papers if p.get("author_alert")]
    non_alert = [p for p in papers if not p.get("author_alert")]
    must_read = [p for p in non_alert if p.get("category") == "must_read"]
    worth_reading = [p for p in non_alert if p.get("category") != "must_read"]

    rows = []
    if not papers and not abstract_only_count:
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

    for i, p in enumerate(must_read, start=1):
        authors = ", ".join(p.get("authors", []))
        rows.append(
            f"""
            <h3>[must_read {i}] <a href="{p['url']}">{_display_title(p)}</a></h3>
            <p>著者: {authors}</p>
            <p>スコア: {p.get('score', 0)}/10 ・ 理由: {p.get('reason', '')}</p>
            <p>チェック点: {p.get('check_points', '')}</p>
            <p>推奨行動: {p.get('suggested_action', '')}</p>
            <p style="white-space: pre-wrap;">{p.get('abstract_ja', '')}</p>
            <hr/>
            """
        )

    for i, p in enumerate(worth_reading, start=1):
        authors = ", ".join(p.get("authors", []))
        rows.append(
            f"""
            <h3>[worth_reading {i}] <a href="{p['url']}">{_display_title(p)}</a></h3>
            <p>著者: {authors}</p>
            <p>スコア: {p.get('score', 0)}/10 ・ 理由: {p.get('reason', '')}</p>
            <p style="white-space: pre-wrap;">{p.get('abstract_ja', '')}</p>
            <hr/>
            """
        )

    if abstract_only_count:
        rows.append(f"<p>他に要約把握のみ {abstract_only_count} 本 → Issueをご確認ください。</p>")

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


def notify(papers, issue_url, date_str, env, always_email=False, abstract_only_count=0):
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
        text = build_line_text(papers, issue_url, date_str, abstract_only_count=abstract_only_count)
        line_ok = send_line_message(text, line_token, line_user_id)
    else:
        print("[notify] LINEの環境変数が未設定のため、メールにフォールバックします")

    email_configured = bool(gmail_address and gmail_app_password and mail_to)
    need_email = (not line_ok) or always_email

    if need_email:
        if email_configured:
            subject = f"📄 今朝の hep-th ({_short_date(date_str)}) — {len(papers)}件"
            html = build_email_html(papers, issue_url, date_str, abstract_only_count=abstract_only_count)
            send_email(subject, html, gmail_address, gmail_app_password, mail_to)
        else:
            print("[notify] メールの環境変数も未設定のため、メール送信をスキップしました")

    if not line_ok and not email_configured:
        print("[notify] LINE・メールともに送信できませんでした。Issueで内容を確認してください。")
