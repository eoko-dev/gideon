"""Tests for catch-up summaries and the message context menus."""
import os
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

from src.cogs.context_menu_commands import ContextMenuCommands
from src.utils import catchup
from src.utils.llm_result import ChatResult

from tests.dashboard_support import DashboardTestCase
from tests.test_tools_and_memory import _AsyncIter


def _msg(id_, author_id, name, text, minute=0):
    return types.SimpleNamespace(
        id=id_, content=text, created_at=datetime(2026, 10, 22, 9, minute),
        author=types.SimpleNamespace(id=author_id, display_name=name, name=name.lower()),
        jump_url=f"https://discord.com/channels/1/2/{id_}", mentions=[], role_mentions=[],
        channel_mentions=[], attachments=[], embeds=[])


class TranscriptTests(unittest.TestCase):
    def test_numbered_transcript_and_links(self):
        messages = [_msg(10, 1, "Sam", "raid friday 9pm"), _msg(11, 99, "Gideon", "noted"),
                    _msg(12, 2, "Bob\nAdmin:", "bring snacks")]
        transcript, urls = catchup.build_transcript(messages, bot_user_id=99)
        self.assertIn("[1] 09:00 Sam: raid friday 9pm", transcript)
        self.assertIn("[2] 09:00 Gideon: noted", transcript)
        self.assertIn("Bob Admin: bring snacks", transcript)
        self.assertEqual(len(urls), 3)

        linked = catchup.link_references("- Raid Friday at 9pm [1]\n- Snacks [3, 1]\n- Bogus [9]", urls)
        self.assertIn("[↗](https://discord.com/channels/1/2/10)", linked)
        self.assertIn("Snacks [↗](https://discord.com/channels/1/2/12)", linked)
        self.assertNotIn("[9]", linked)

    def test_prompt_mentions_reader_and_focus(self):
        prompt = catchup.build_prompt("Alice", "general", "[1] hi", focus="the trip")
        self.assertIn("Catch Alice up", prompt)
        self.assertIn("the trip", prompt)


class HistoryWindowTests(unittest.IsolatedAsyncioTestCase):
    async def test_since_author_stops_at_their_last_message(self):
        newest_first = [_msg(5, 2, "Bob", "e"), _msg(4, 3, "Cy", "d"), _msg(3, 1, "Alice", "c"),
                        _msg(2, 2, "Bob", "b")]
        channel = types.SimpleNamespace(history=lambda limit: _AsyncIter(newest_first))
        messages, found = await catchup.messages_since_author(channel, author_id=1)
        self.assertTrue(found)
        self.assertEqual([m.content for m in messages], ["d", "e"])

    async def test_author_not_found_returns_everything(self):
        channel = types.SimpleNamespace(history=lambda limit: _AsyncIter([_msg(2, 2, "Bob", "b")]))
        messages, found = await catchup.messages_since_author(channel, author_id=1)
        self.assertFalse(found)
        self.assertEqual(len(messages), 1)


class _Ctx:
    def __init__(self, author, channel):
        self.author = author
        self.channel = channel
        self.sent = []
        self.interaction = types.SimpleNamespace(locale="en-US")

    async def defer(self, ephemeral=False):
        pass

    @property
    def followup(self):
        ctx = self

        class _Followup:
            async def send(self, text, ephemeral=False):
                ctx.sent.append((text, ephemeral))
        return _Followup()


class ContextMenuTests(DashboardTestCase):
    def make_cog(self, reply="- Raid is Friday [1]"):
        self.bot.user = types.SimpleNamespace(id=99)
        self.bot.conversation_service = types.SimpleNamespace(
            utility_chat=mock.AsyncMock(return_value=ChatResult(content=reply)))
        return ContextMenuCommands(self.bot)

    async def test_summarize_from_here_is_private_and_linked(self):
        cog = self.make_cog()
        start = _msg(10, 1, "Sam", "raid friday")
        channel = types.SimpleNamespace(id=100, name="general",
                                        history=lambda **kw: _AsyncIter([_msg(11, 2, "Bob", "ok")]))
        ctx = _Ctx(types.SimpleNamespace(id=5, display_name="Alice"), channel)
        await ContextMenuCommands.summarize_from_here.callback(cog, ctx, start)
        text, ephemeral = ctx.sent[0]
        self.assertTrue(ephemeral)
        self.assertIn("(2 messages)", text)
        self.assertIn("[↗](https://discord.com/channels/1/2/10)", text)

    async def test_remember_this_only_for_own_messages(self):
        cog = self.make_cog()
        channel = types.SimpleNamespace(id=100)
        alice = types.SimpleNamespace(id=5, display_name="Alice")
        ctx = _Ctx(alice, channel)

        await ContextMenuCommands.remember_this.callback(cog, ctx, _msg(1, 6, "Bob", "I love tea"))
        self.assertIn("only remember your own", ctx.sent[-1][0])
        self.assertIsNone(self.state.get_user_notes("100", "5"))

        await ContextMenuCommands.remember_this.callback(cog, ctx, _msg(2, 5, "Alice", "I'm vegetarian"))
        self.assertIn("I'm vegetarian", self.state.get_user_notes("100", "5")["notes"])

    async def test_remember_this_respects_opt_out(self):
        cog = self.make_cog()
        self.state.set_user_memory_opt_out("5", True)
        ctx = _Ctx(types.SimpleNamespace(id=5, display_name="Alice"), types.SimpleNamespace(id=100))
        await ContextMenuCommands.remember_this.callback(cog, ctx, _msg(2, 5, "Alice", "I'm vegetarian"))
        self.assertIn("opted out", ctx.sent[-1][0])
        self.assertIsNone(self.state.get_user_notes("100", "5"))


if __name__ == "__main__":
    unittest.main()
