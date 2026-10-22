"""Tests for Phase 2 conversation context: message descriptions, the context
block and cache split, replies, user threads and the @everyone toggle."""
import asyncio
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

import discord

from src.cogs.mention_commands import MentionCommands
from src.utils.llm_formatting import (
    PROMPT_CACHE_BREAK, describe_message, flatten_system_prompt, split_system_prompt,
)
from src.utils.openrouter_client import OpenRouterClient
from src.utils.webhook_sender import WebhookSender

from tests.dashboard_support import DashboardTestCase
from tests.test_mention_race_and_pings import ALICE_ID, BOT_ID, BlockingChatClient, _Typing


def _msg(content="", author_name="Alice", attachments=(), embeds=(), snapshots=(), stickers=()):
    return types.SimpleNamespace(
        content=content, mentions=[], role_mentions=[], channel_mentions=[],
        author=types.SimpleNamespace(display_name=author_name, name=author_name.lower()),
        attachments=[types.SimpleNamespace(filename=f) for f in attachments],
        embeds=list(embeds), snapshots=list(snapshots), stickers=list(stickers))


class DescribeMessageTests(unittest.TestCase):
    def test_plain_text_is_unchanged(self):
        self.assertEqual(describe_message(_msg("hello there")), "hello there")

    def test_reply_includes_the_quoted_message(self):
        original = _msg("the raid starts at 9", author_name="Sam")
        text = describe_message(_msg("@Gideon what time?"), referenced=original)
        self.assertIn('(replying to Sam: "the raid starts at 9")', text)
        self.assertTrue(text.endswith("@Gideon what time?"))

    def test_attachment_only_message_is_not_empty(self):
        self.assertEqual(describe_message(_msg(attachments=["cat.png"])), "[attachments: cat.png]")

    def test_embeds_forwards_and_stickers(self):
        embed = types.SimpleNamespace(title="Patch notes", description="Big changes")
        snapshot = types.SimpleNamespace(content="forwarded text")
        sticker = types.SimpleNamespace(name="wave")
        text = describe_message(_msg("look", embeds=[embed], snapshots=[snapshot], stickers=[sticker]))
        self.assertIn("[embed: Patch notes — Big changes]", text)
        self.assertIn('(forwarded message: "forwarded text")', text)
        self.assertIn("[sticker: wave]", text)

    def test_long_quotes_are_trimmed(self):
        text = describe_message(_msg("ok"), referenced=_msg("x" * 1000))
        self.assertLess(len(text), 300)


class PromptCacheTests(unittest.TestCase):
    def test_split_and_flatten(self):
        prompt = "stable" + PROMPT_CACHE_BREAK + "volatile"
        self.assertEqual(split_system_prompt(prompt), ("stable", "volatile"))
        self.assertEqual(flatten_system_prompt(prompt), "stable\n\nvolatile")
        self.assertEqual(split_system_prompt("plain"), ("plain", ""))

    def test_anthropic_models_get_a_cache_breakpoint(self):
        prompt = "stable" + PROMPT_CACHE_BREAK + "volatile"
        parts = OpenRouterClient._system_content(prompt, "anthropic/claude-sonnet-4.5")
        self.assertEqual(parts[0], {"type": "text", "text": "stable", "cache_control": {"type": "ephemeral"}})
        self.assertEqual(parts[1]["text"], "volatile")
        self.assertEqual(OpenRouterClient._system_content(prompt, "openai/gpt-4o-mini"), "stable\n\nvolatile")


class SystemPromptContextTests(DashboardTestCase):
    def test_context_block_and_layout(self):
        guild = types.SimpleNamespace(name="Friends")
        channel = types.SimpleNamespace(name="general", topic="Weekend plans", guild=guild)
        self.state.set_channel_resolver(lambda cid: channel if cid == 100 else None)
        prompt = self.state.get_effective_system_prompt("100", query="hi")
        stable, volatile = split_system_prompt(prompt)
        self.assertIn("--- Context ---", volatile)
        self.assertIn("Server: Friends", volatile)
        self.assertIn("Channel: #general", volatile)
        self.assertIn("Channel topic: Weekend plans", volatile)
        self.assertIn(str(datetime.now().year), volatile)
        # The date must not land in the cacheable prefix
        self.assertNotIn("Current date", stable)

    def test_recent_summaries_are_stable_and_matches_are_volatile(self):
        self.db.memory.add_memory("100", "We planned a camping trip.", 5,
                                  conversation_start=datetime.now() - timedelta(days=40))
        for i in range(3):
            self.db.memory.add_memory("100", f"Recent chat {i}.", 3)
        prompt = self.state.get_effective_system_prompt("100", query="camping trip")
        stable, volatile = split_system_prompt(prompt)
        self.assertIn("Recent chat 2.", stable)
        self.assertIn("camping trip", volatile)
        self.assertIn("--- Related Channel Memory ---", volatile)


class RespondToEveryoneStateTests(DashboardTestCase):
    async def test_channel_override_and_thread_inheritance(self):
        self.assertTrue(self.state.get_effective_respond_to_everyone("100"))
        self.state.set_channel_memory_config("100", respond_to_everyone=False)
        self.assertFalse(self.state.get_effective_respond_to_everyone("100"))
        # A thread follows its parent
        await self.state.add_discord_thread("555", "100", "t")
        self.assertFalse(self.state.get_effective_respond_to_everyone("555"))
        # Global off, channel inherits
        self.state.set_channel_memory_config("100", respond_to_everyone=None)
        await self.state.set_respond_to_everyone(False)
        self.assertFalse(self.state.get_effective_respond_to_everyone("100"))
        self.state.set_channel_memory_config("100", respond_to_everyone=True)
        self.assertTrue(self.state.get_effective_respond_to_everyone("100"))

    async def test_dashboard_round_trip(self):
        await self.login()
        resp = await self.client.put("/api/settings", json={"respond_to_everyone": False})
        self.assertEqual(resp.status, 200)
        self.assertFalse((await (await self.client.get("/api/settings")).json())["respond_to_everyone"])

        for value in (True, False, None):
            resp = await self.client.put("/api/channels/100", json={"respond_to_everyone": value})
            self.assertEqual(resp.status, 200, await resp.text())
            body = await (await self.client.get("/api/channels/100")).json()
            self.assertEqual(body["respond_to_everyone"], None if value is None else int(value))
            self.assertEqual(body["sources"]["respond_to_everyone"], "global" if value is None else "channel")
            self.assertEqual(body["effective_respond_to_everyone"], False if value is None else value)

        resp = await self.client.put("/api/channels/100", json={"respond_to_everyone": "yes"})
        self.assertEqual(resp.status, 400)

        await self.client.put("/api/channels/100", json={"respond_to_everyone": True})
        await self.client.delete("/api/channels/100")
        self.assertIsNone((await (await self.client.get("/api/channels/100")).json())["respond_to_everyone"])


# ─── Mention cog: toggle, replies to Gideon, user threads ───────────────────

class FakeState:
    def __init__(self, respond_to_everyone=True):
        self.channel_rows = []
        self.thread_rows = []
        self.respond = respond_to_everyone
        self._last = datetime(2026, 1, 1, 12, 0, 0)

    def _ts(self):
        self._last += timedelta(seconds=1)
        return self._last

    async def add_to_channel_history(self, channel_id, message):
        ts = self._ts()
        self.channel_rows.append({**message, "timestamp": ts, "channel_id": channel_id})
        return ts

    async def add_discord_thread_message(self, thread_id, message):
        ts = self._ts()
        self.thread_rows.append({**message, "timestamp": ts, "thread_id": thread_id})
        return ts

    def get_channel_history(self, channel_id, limit=None, until=None):
        return [dict(r) for r in self.channel_rows if r["channel_id"] == channel_id
                and (until is None or r["timestamp"] <= until)]

    def get_discord_thread_history(self, thread_id, hours_limit=None, until=None):
        return [dict(r) for r in self.thread_rows if r["thread_id"] == thread_id
                and (until is None or r["timestamp"] <= until)]

    def get_discord_thread(self, thread_id):
        return None

    def get_discord_thread_config(self, thread_id):
        return {}

    def get_active_trivia_session(self, thread_id):
        return None

    def get_time_window_hours(self):
        return 48

    def get_effective_respond_to_everyone(self, channel_id):
        return self.respond

    def resolve_model(self, channel_id):
        return "openrouter", "test-model"

    def get_tool_calling_enabled(self):
        return False

    def get_effective_system_prompt(self, channel_id, query=None, speaker_id=None, history=None, **kwargs):
        return "sys"


class MentionContextTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, state):
        self.sent = []
        self.client = BlockingChatClient("Hi!")
        self.client.release.set()

        async def send_response(channel, text, channel_id, **kwargs):
            self.sent.append((channel.id, text))

        self.bot_user = types.SimpleNamespace(id=BOT_ID, name="Gideon", display_name="Gideon", bot=True)
        bot = types.SimpleNamespace(
            user=self.bot_user, state_manager=state, openrouter_client=self.client,
            openai_client=None, ai_horde_client=None,
            webhook_sender=types.SimpleNamespace(send_response=send_response),
            get_cog=lambda name: None,
        )
        cog = MentionCommands(bot)
        cog._active_channels = set()
        return cog

    def make_message(self, msg_id, content, channel, mentions=(), mention_everyone=False, reference=None):
        alice = types.SimpleNamespace(id=ALICE_ID, display_name="Alice", name="alice", bot=False)
        return types.SimpleNamespace(
            id=msg_id, author=alice, content=content, mentions=list(mentions), role_mentions=[],
            channel_mentions=[], attachments=[], embeds=[], webhook_id=None,
            mention_everyone=mention_everyone, channel=channel, reference=reference)

    def channel(self, id_=555):
        return types.SimpleNamespace(id=id_, typing=lambda: _Typing(), send=mock.AsyncMock())

    async def run_message(self, cog, message):
        with mock.patch("src.cogs.mention_commands.check_and_rotate_session", mock.AsyncMock(return_value=False)), \
             mock.patch("src.cogs.mention_commands.schedule_compaction"):
            await asyncio.wait_for(cog.on_message(message), 2)

    async def test_everyone_ignored_when_toggle_is_off(self):
        cog = self.make_cog(FakeState(respond_to_everyone=False))
        await self.run_message(cog, self.make_message(1, "@everyone dinner?", self.channel(), mention_everyone=True))
        self.assertEqual(self.sent, [])

    async def test_direct_mention_still_works_when_toggle_is_off(self):
        cog = self.make_cog(FakeState(respond_to_everyone=False))
        message = self.make_message(1, f"<@{BOT_ID}> hi", self.channel(), mentions=[self.bot_user])
        await self.run_message(cog, message)
        self.assertEqual(self.sent, [(555, "Hi!")])

    async def test_reply_to_gideon_counts_as_a_mention(self):
        cog = self.make_cog(FakeState())
        gideon_message = types.SimpleNamespace(author=self.bot_user, content="Want a reminder?", webhook_id=None,
                                               mentions=[], role_mentions=[], channel_mentions=[], attachments=[])
        reference = types.SimpleNamespace(message_id=9, resolved=gideon_message, type="default")
        await self.run_message(cog, self.make_message(1, "yes please", self.channel(), reference=reference))
        self.assertEqual(self.sent, [(555, "Hi!")])
        # The model sees what was replied to
        self.assertIn('(replying to Gideon: "Want a reminder?")', self.client.received[-1]["content"])

    async def test_user_thread_mention_uses_the_threads_own_history(self):
        state = FakeState()
        cog = self.make_cog(state)
        thread = mock.MagicMock(spec=discord.Thread)
        thread.id = 777
        thread.parent_id = 555
        thread.owner_id = ALICE_ID
        thread.typing = lambda: _Typing()
        thread.send = mock.AsyncMock()

        await self.run_message(cog, self.make_message(1, "context first", thread))
        await self.run_message(cog, self.make_message(2, f"<@{BOT_ID}> summarize", thread, mentions=[self.bot_user]))

        self.assertEqual(self.sent, [(777, "Hi!")])
        self.assertEqual(state.channel_rows, [])
        self.assertEqual([r["role"] for r in state.thread_rows], ["user", "assistant"])
        # Passive messages before the first mention aren't recorded
        self.assertNotIn("context first", [r["content"] for r in state.thread_rows])


class ReplyReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_standard_send_replies_without_pinging(self):
        sender = WebhookSender(types.SimpleNamespace())
        channel = types.SimpleNamespace(send=mock.AsyncMock())
        original = mock.Mock()
        original.to_reference.return_value = "REF"
        await sender._standard_send(channel, "answer", reference=original)
        kwargs = channel.send.call_args.kwargs
        self.assertEqual(kwargs["reference"], "REF")
        self.assertFalse(kwargs["mention_author"])
        original.to_reference.assert_called_once_with(fail_if_not_exists=False)


if __name__ == "__main__":
    unittest.main()
