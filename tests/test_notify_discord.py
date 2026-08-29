"""
notify_discord.py のテスト。

requests を完全にモックし、ネットワークには一切出ない。
embed の文字数制限・リンクプレビュー抑制・リアクション付与のURLエンコード・
429リトライ・DRY_RUN の挙動を確認する。
"""

import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import notify_discord  # noqa: E402
from notify_discord import (  # noqa: E402
    EMBED_DESCRIPTION_MAX,
    EMBED_TITLE_MAX,
    EMBED_TOTAL_MAX,
    add_reactions,
    build_embed,
    notify_discord as notify_discord_func,
    send_paper,
    suppress_preview,
)


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"{self.status_code} Error")
            error.response = self
            raise error


def _paper(**overrides):
    paper = {
        "id": "2608.01234",
        "title": "Holography of Godel spacetime",
        "title_ja": "ゲーデル時空のホログラフィー",
        "authors": ["Tadashi Takayanagi", "Seiya Tanaka"],
        "url": "https://arxiv.org/abs/2608.01234",
        "score": 9,
        "category": "must_read",
        "reason": "研究テーマと直接一致する。",
        "one_liner": "ゲーデル時空の境界理論を構成した。",
        "abstract_ja": "本論文ではゲーデル時空のホログラフィック双対を議論する。",
        "check_points": "3節の計量の導出",
        "suggested_action": "30分で3節まで読む",
        "author_alert": False,
        "matched_author": "",
    }
    paper.update(overrides)
    return paper


class TestBuildEmbed(unittest.TestCase):
    def test_basic_fields(self):
        embed = build_embed(_paper())
        self.assertEqual(embed["title"], "ゲーデル時空のホログラフィー")
        self.assertEqual(embed["url"], "https://arxiv.org/abs/2608.01234")
        self.assertEqual(embed["footer"]["text"], "arXiv:2608.01234")
        self.assertIn("★9/10", embed["description"])
        self.assertIn("Tadashi Takayanagi, Seiya Tanaka", embed["description"])
        self.assertIn("Holography of Godel spacetime", embed["description"])
        self.assertIn("本論文ではゲーデル時空", embed["description"])

    def test_url_is_wrapped_in_angle_brackets(self):
        """プレビュー展開を抑制するため、本文中のURLは <...> で囲む。"""
        embed = build_embed(_paper())
        self.assertIn("<https://arxiv.org/abs/2608.01234>", embed["description"])

    def test_suppress_preview_helper(self):
        self.assertEqual(suppress_preview("https://example.com"), "<https://example.com>")
        self.assertEqual(suppress_preview(""), "")

    def test_author_alert_is_marked(self):
        embed = build_embed(_paper(author_alert=True, matched_author="Tadashi Takayanagi"))
        self.assertTrue(embed["title"].startswith("🔔 "))
        self.assertIn("著者アラート: Tadashi Takayanagi", embed["description"])
        self.assertEqual(embed["color"], notify_discord.ALERT_COLOR)

    def test_long_title_is_truncated_to_limit(self):
        embed = build_embed(_paper(title_ja="あ" * 500))
        self.assertEqual(len(embed["title"]), EMBED_TITLE_MAX)
        self.assertTrue(embed["title"].endswith("…"))

    def test_long_description_is_truncated_within_limits(self):
        embed = build_embed(_paper(abstract_ja="あ" * 10000))
        self.assertLessEqual(len(embed["description"]), EMBED_DESCRIPTION_MAX)
        total = len(embed["title"]) + len(embed["description"]) + len(embed["footer"]["text"])
        self.assertLessEqual(total, EMBED_TOTAL_MAX)
        self.assertTrue(embed["description"].endswith("…"))

    def test_url_survives_truncation(self):
        """本文が切り詰められてもURLは残る(URLを先頭側に置いているため)。"""
        embed = build_embed(_paper(abstract_ja="あ" * 10000))
        self.assertIn("<https://arxiv.org/abs/2608.01234>", embed["description"])

    def test_missing_optional_keys_do_not_crash(self):
        embed = build_embed({"id": "2608.99999", "title": "Minimal", "url": "https://x/y"})
        self.assertEqual(embed["title"], "Minimal")
        self.assertIn("<https://x/y>", embed["description"])


class TestSendPaper(unittest.TestCase):
    def test_posts_embed_and_returns_message_id(self):
        fake = mock.Mock(return_value=FakeResponse(json_data={"id": 1234567890123456789}))
        with mock.patch.object(notify_discord.requests, "request", fake):
            message_id = send_paper(_paper(), "tok", "999")

        self.assertEqual(message_id, "1234567890123456789")
        self.assertIsInstance(message_id, str)

        args, kwargs = fake.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(args[1], "https://discord.com/api/v10/channels/999/messages")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bot tok")
        self.assertEqual(len(kwargs["json"]["embeds"]), 1)

    def test_dry_run_does_not_send(self):
        fake = mock.Mock()
        with mock.patch.object(notify_discord.requests, "request", fake):
            message_id = send_paper(_paper(), "tok", "999", dry_run=True)
        self.assertIsNone(message_id)
        fake.assert_not_called()


class TestAddReactions(unittest.TestCase):
    def test_puts_three_url_encoded_emojis(self):
        fake = mock.Mock(return_value=FakeResponse(status_code=204))
        with mock.patch.object(notify_discord.requests, "request", fake):
            add_reactions("42", "tok", "999")

        urls = [call.args[1] for call in fake.call_args_list]
        self.assertEqual(len(urls), 3)
        self.assertTrue(all(call.args[0] == "PUT" for call in fake.call_args_list))
        base = "https://discord.com/api/v10/channels/999/messages/42/reactions"
        self.assertEqual(urls[0], f"{base}/%F0%9F%93%96/@me")  # 📖
        self.assertEqual(urls[1], f"{base}/%F0%9F%91%8D/@me")  # 👍
        self.assertEqual(urls[2], f"{base}/%F0%9F%91%8E/@me")  # 👎

    def test_one_failure_does_not_stop_the_rest(self):
        responses = [FakeResponse(status_code=403), FakeResponse(204), FakeResponse(204)]
        fake = mock.Mock(side_effect=responses)
        with mock.patch.object(notify_discord.requests, "request", fake):
            add_reactions("42", "tok", "999")
        self.assertEqual(fake.call_count, 3)

    def test_dry_run_does_not_send(self):
        fake = mock.Mock()
        with mock.patch.object(notify_discord.requests, "request", fake):
            add_reactions("42", "tok", "999", dry_run=True)
        fake.assert_not_called()


class TestRateLimit(unittest.TestCase):
    def test_429_waits_retry_after_and_retries(self):
        responses = [
            FakeResponse(status_code=429, json_data={"retry_after": 2.5}),
            FakeResponse(json_data={"id": "77"}),
        ]
        fake = mock.Mock(side_effect=responses)
        sleep = mock.Mock()
        with mock.patch.object(notify_discord.requests, "request", fake), mock.patch.object(
            notify_discord.time, "sleep", sleep
        ):
            message_id = send_paper(_paper(), "tok", "999")

        self.assertEqual(message_id, "77")
        self.assertEqual(fake.call_count, 2)
        sleep.assert_called_once_with(2.5)

    def test_429_falls_back_to_retry_after_header(self):
        # ボディに retry_after が無い場合は Retry-After ヘッダにフォールバックする
        responses = [
            FakeResponse(status_code=429, headers={"Retry-After": "1.5"}),
            FakeResponse(json_data={"id": "77"}),
        ]
        fake = mock.Mock(side_effect=responses)
        sleep = mock.Mock()
        with mock.patch.object(notify_discord.requests, "request", fake), mock.patch.object(
            notify_discord.time, "sleep", sleep
        ):
            send_paper(_paper(), "tok", "999")
        sleep.assert_called_once_with(1.5)

    def test_429_wait_is_capped(self):
        responses = [
            FakeResponse(status_code=429, json_data={"retry_after": 99999}),
            FakeResponse(json_data={"id": "77"}),
        ]
        fake = mock.Mock(side_effect=responses)
        sleep = mock.Mock()
        with mock.patch.object(notify_discord.requests, "request", fake), mock.patch.object(
            notify_discord.time, "sleep", sleep
        ):
            send_paper(_paper(), "tok", "999")
        sleep.assert_called_once_with(float(notify_discord.MAX_RETRY_WAIT_SEC))

    def test_429_gives_up_after_max_retries(self):
        fake = mock.Mock(return_value=FakeResponse(status_code=429, json_data={"retry_after": 0.1}))
        with mock.patch.object(notify_discord.requests, "request", fake), mock.patch.object(
            notify_discord.time, "sleep", mock.Mock()
        ):
            with self.assertRaises(requests.HTTPError):
                send_paper(_paper(), "tok", "999")
        self.assertEqual(fake.call_count, notify_discord.MAX_RETRIES + 1)


class TestNotifyDiscord(unittest.TestCase):
    def setUp(self):
        self.env = {"DISCORD_BOT_TOKEN": "tok", "DISCORD_CHANNEL_ID": "999"}

    def test_returns_posted_message_map(self):
        fake = mock.Mock(return_value=FakeResponse(json_data={"id": "555"}))
        with mock.patch.object(notify_discord.requests, "request", fake), mock.patch.object(
            notify_discord.time, "sleep", mock.Mock()
        ):
            posted = notify_discord_func([_paper()], self.env, "2026-08-30")

        self.assertEqual(
            posted,
            {
                "2608.01234": {
                    "message_id": "555",
                    "channel_id": "999",
                    "date": "2026-08-30",
                    "title": "Holography of Godel spacetime",
                }
            },
        )

    def test_missing_env_skips_without_network(self):
        fake = mock.Mock()
        with mock.patch.object(notify_discord.requests, "request", fake):
            self.assertEqual(notify_discord_func([_paper()], {}, "2026-08-30"), {})
        fake.assert_not_called()

    def test_dry_run_does_not_send_and_returns_empty(self):
        fake = mock.Mock()
        with mock.patch.object(notify_discord.requests, "request", fake):
            posted = notify_discord_func([_paper()], self.env, "2026-08-30", dry_run=True)
        self.assertEqual(posted, {})
        fake.assert_not_called()

    def test_one_paper_failure_does_not_stop_the_others(self):
        def side_effect(method, url, **kwargs):
            body = kwargs.get("json") or {}
            embeds = body.get("embeds") or [{}]
            if embeds[0].get("footer", {}).get("text") == "arXiv:bad":
                raise requests.ConnectionError("boom")
            return FakeResponse(json_data={"id": "555"})

        fake = mock.Mock(side_effect=side_effect)
        papers = [_paper(id="bad"), _paper()]
        with mock.patch.object(notify_discord.requests, "request", fake), mock.patch.object(
            notify_discord.time, "sleep", mock.Mock()
        ):
            posted = notify_discord_func(papers, self.env, "2026-08-30")

        self.assertNotIn("bad", posted)
        self.assertIn("2608.01234", posted)

    def test_empty_paper_list_sends_nothing(self):
        fake = mock.Mock()
        with mock.patch.object(notify_discord.requests, "request", fake):
            self.assertEqual(notify_discord_func([], self.env, "2026-08-30"), {})
        fake.assert_not_called()


if __name__ == "__main__":
    unittest.main()
