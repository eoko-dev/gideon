"""Tests for the mention-reply race fix and outbound '@Name' -> '<@id>' pings."""
import asyncio
import os
import shutil
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

from src.cogs.mention_commands import MentionCommands
from src.utils import memory_service
from src.utils.database import DatabaseManager
from src.utils.llm_formatting import apply_outbound_mentions, collect_mention_targets


# ─── A. apply_outbound_mentions ─────────────────────────────────────────────

class ApplyOutboundMentionsTests(unittest.TestCase):
    TARGETS = {"alice": "111", "bob": "222", "carol ann": "333", "carol": "444"}

    def test_basic_conversion(self):
        self.assertEqual(apply_outbound_mentions("hi @Alice", self.TARGETS), "hi <@111>")

    def test_case_insensitive(self):
        self.assertEqual(apply_outbound_mentions("hi @ALICE and @alice", self.TARGETS), "hi <@111> and <@111>")

    def test_trailing_punctuation(self):
        for text, expected in [
            ("hey @Alice, ok", "hey <@111>, ok"),
            ("done @Alice.", "done <@111>."),
            ("(@Alice)", "(<@111>)"),
            ("@Alice!", "<@111>!"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(apply_outbound_mentions(text, self.TARGETS), expected)

    def test_longest_name_first(self):
        self.assertEqual(apply_outbound_mentions("yo @Carol Ann", self.TARGETS), "yo <@333>")
        self.assertEqual(apply_outbound_mentions("yo @Carol", self.TARGETS), "yo <@444>")
        self.assertEqual(apply_outbound_mentions("@Carol Ann and @Carol", self.TARGETS), "<@333> and <@444>")

    def test_longer_name_wins_even_if_listed_last(self):
        targets = {"carol": "444", "carol ann": "333"}
        self.assertEqual(apply_outbound_mentions("@Carol Ann", targets), "<@333>")

    def test_no_conversion_when_name_is_a_prefix_of_a_longer_word(self):
        self.assertEqual(apply_outbound_mentions("@Alices said hi", self.TARGETS), "@Alices said hi")

    def test_no_conversion_inside_email(self):
        self.assertEqual(apply_outbound_mentions("mail me@alice.com", self.TARGETS), "mail me@alice.com")

    def test_unknown_names_untouched(self):
        self.assertEqual(apply_outbound_mentions("hello @Nobody", self.TARGETS), "hello @Nobody")

    def test_existing_mention_token_untouched(self):
        self.assertEqual(apply_outbound_mentions("ping <@123> now", self.TARGETS), "ping <@123> now")
        self.assertEqual(apply_outbound_mentions("ping <@111>", self.TARGETS), "ping <@111>")

    def test_regex_special_characters_in_names(self):
        targets = {"a.b": "1", "x+y": "2", "c(d)": "3"}
        self.assertEqual(apply_outbound_mentions("hi @a.b and @x+y and @c(d)!", targets),
                         "hi <@1> and <@2> and <@3>!")
        # '.' must not act as a wildcard
        self.assertEqual(apply_outbound_mentions("hi @axb", targets), "hi @axb")

    def test_empty_targets_returns_text_unchanged(self):
        self.assertEqual(apply_outbound_mentions("hi @Alice", {}), "hi @Alice")

    def test_empty_text(self):
        self.assertEqual(apply_outbound_mentions("", self.TARGETS), "")


# ─── B. collect_mention_targets ─────────────────────────────────────────────

def _user(id_, display_name=None, name=None, bot=False):
    return types.SimpleNamespace(id=id_, display_name=display_name, name=name, bot=bot)


class CollectMentionTargetsTests(unittest.TestCase):
    def test_collects_user_rows(self):
        history = [
            {"role": "user", "name": "Alice", "user_id": "111", "content": "x"},
            {"role": "user", "name": "Bob", "user_id": 222, "content": "y"},
        ]
        self.assertEqual(collect_mention_targets(history), {"alice": "111", "bob": "222"})

    def test_ignores_assistant_rows_and_rows_missing_ids(self):
        history = [
            {"role": "assistant", "name": "Gideon", "user_id": "999", "content": "x"},
            {"role": "user", "name": "NoId", "content": "y"},
            {"role": "user", "user_id": "5", "content": "no name"},
            {"role": "user", "name": "Ok", "user_id": "7", "content": "z"},
        ]
        self.assertEqual(collect_mention_targets(history), {"ok": "7"})

    def test_extra_users_by_display_name_and_name(self):
        targets = collect_mention_targets([], [_user(5, "Carol Ann", "carolann")])
        self.assertEqual(targets, {"carol ann": "5", "carolann": "5"})

    def test_skips_bots_and_none(self):
        targets = collect_mention_targets([], [_user(1, "Gideon", "gideon", bot=True), None, _user(2, "Eve", "eve")])
        self.assertEqual(targets, {"eve": "2"})

    def test_handles_none_history(self):
        self.assertEqual(collect_mention_targets(None, [_user(2, "Eve", "eve")]), {"eve": "2"})


# ─── C. MessageManager `until` against a real SQLite DB ─────────────────────

class HistoryUntilTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = DatabaseManager(os.path.join(self.tmp, "t.db"))
        self.addCleanup(self.db.close)
        self.base = datetime(2026, 1, 1, 12, 0, 0)
        self.ts = [self.base + timedelta(seconds=i) for i in range(5)]
        for i, ts in enumerate(self.ts):
            self.db.add_message("user", f"m{i}", ts, channel_id="c1", user_id=str(i), user_name=f"u{i}")
        self.db.add_message("user", "other", self.ts[0], channel_id="c2", user_id="x", user_name="x")

    def contents(self, rows):
        return [r["content"] for r in rows]

    def test_no_until_returns_everything(self):
        self.assertEqual(self.contents(self.db.get_channel_history("c1")), ["m0", "m1", "m2", "m3", "m4"])

    def test_until_is_inclusive_and_excludes_later_rows(self):
        rows = self.db.get_channel_history("c1", until=self.ts[2])
        self.assertEqual(self.contents(rows), ["m0", "m1", "m2"])

    def test_until_with_limit_returns_latest_n_chronologically(self):
        rows = self.db.get_channel_history("c1", limit=2, until=self.ts[3])
        self.assertEqual(self.contents(rows), ["m2", "m3"])

    def test_until_before_everything_is_empty(self):
        self.assertEqual(self.db.get_channel_history("c1", until=self.base - timedelta(seconds=1)), [])

    def test_until_via_message_manager_directly(self):
        rows = self.db.messages.get_channel_history("c1", limit=10, until=self.ts[1])
        self.assertEqual(self.contents(rows), ["m0", "m1"])
        self.assertEqual(rows[-1]["name"], "u1")
        self.assertEqual(rows[-1]["user_id"], "1")


# ─── D. memory_service locking / background compaction ──────────────────────

class FakeMemoryState:
    def __init__(self, count=50, max_history=10):
        self.count = count
        self.max_history = max_history
        base = datetime(2026, 1, 1, 12, 0, 0)
        self.rows = [{"role": "user", "name": "U", "content": f"m{i}", "timestamp": base + timedelta(seconds=i)}
                     for i in range(count)]
        self.memories_added = []
        self.deleted_before = []

    def get_max_channel_history(self):
        return self.max_history

    def get_channel_message_count(self, channel_id):
        return self.count

    def get_channel_history(self, channel_id, limit=None, until=None):
        return list(self.rows)

    def get_full_channel_history(self, channel_id, until=None):
        return list(self.rows)

    def get_effective_memory_summary_enabled(self, channel_id):
        return True

    def get_effective_user_memory_enabled(self, channel_id):
        return False

    def get_effective_model(self, channel_id):
        return "openrouter/test-model"

    def get_channel_memories(self, channel_id, limit=5):
        return []

    def add_channel_memory(self, channel_id, summary, count, conversation_start=None):
        self.memories_added.append((channel_id, summary, count))

    def get_effective_max_memory_summaries(self, channel_id):
        return 5

    def prune_channel_memories(self, channel_id, max_summaries):
        pass

    def delete_channel_messages_before(self, channel_id, ts):
        self.deleted_before.append((channel_id, ts))


class BlockingClient:
    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def send_message_with_history(self, messages, model, system_prompt, images):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        return "a summary"


class MemoryServiceConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        memory_service._channel_locks.clear()

    async def test_concurrent_compactions_summarize_once(self):
        state, client = FakeMemoryState(), BlockingClient()
        clients = {"openrouter": client}

        first = asyncio.create_task(memory_service.maybe_compact_history("c1", state, clients))
        await asyncio.wait_for(client.started.wait(), 2)

        # Lock is held by the first call: the second must bail out immediately
        second = await asyncio.wait_for(memory_service.maybe_compact_history("c1", state, clients), 2)
        self.assertFalse(second)

        client.release.set()
        self.assertTrue(await asyncio.wait_for(first, 2))
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(state.memories_added), 1)
        self.assertEqual(len(state.deleted_before), 1)

    async def test_lock_is_per_channel(self):
        state, client = FakeMemoryState(), BlockingClient()
        clients = {"openrouter": client}
        t1 = asyncio.create_task(memory_service.maybe_compact_history("c1", state, clients))
        await asyncio.wait_for(client.started.wait(), 2)
        t2 = asyncio.create_task(memory_service.maybe_compact_history("c2", state, clients))
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(client.calls, 2)  # other channel is not blocked
        client.release.set()
        self.assertEqual(await asyncio.gather(t1, t2), [True, True])

    async def test_lock_released_after_compaction(self):
        state, client = FakeMemoryState(), BlockingClient()
        client.release.set()
        clients = {"openrouter": client}
        self.assertTrue(await memory_service.maybe_compact_history("c1", state, clients))
        self.assertFalse(memory_service._channel_lock("c1").locked())
        self.assertTrue(await memory_service.maybe_compact_history("c1", state, clients))
        self.assertEqual(client.calls, 2)

    async def test_schedule_compaction_does_not_await_llm(self):
        state, client = FakeMemoryState(), BlockingClient()
        clients = {"openrouter": client}

        memory_service.schedule_compaction("c1", state, clients)  # synchronous, returns None
        self.assertEqual(len(memory_service._background_tasks), 1)
        task = next(iter(memory_service._background_tasks))

        await asyncio.wait_for(client.started.wait(), 2)
        self.assertFalse(task.done())  # LLM call still in flight
        self.assertEqual(state.memories_added, [])

        client.release.set()
        await asyncio.wait_for(task, 2)
        await asyncio.sleep(0)
        self.assertEqual(len(state.memories_added), 1)
        self.assertEqual(len(memory_service._background_tasks), 0)

    async def test_schedule_compaction_swallows_errors(self):
        state = FakeMemoryState()
        state.get_max_channel_history = mock.Mock(side_effect=RuntimeError("boom"))
        memory_service.schedule_compaction("c1", state, {})
        task = next(iter(memory_service._background_tasks))
        await asyncio.wait_for(task, 2)  # must not raise

    async def test_session_rotation_rechecks_expiry_under_lock(self):
        class RotState:
            def __init__(self):
                self.rows = [{"role": "user", "content": "old",
                              "timestamp": datetime.now() - timedelta(hours=10)}]

            def get_effective_session_timeout(self, channel_id):
                return 1

            def get_channel_history(self, channel_id, limit=None, until=None):
                return list(self.rows)

        state = RotState()
        lock = memory_service._channel_lock("c1")
        await lock.acquire()
        rotate = mock.AsyncMock(return_value=True)
        with mock.patch.object(memory_service, "_rotate_session", rotate):
            waiter = asyncio.create_task(memory_service.check_and_rotate_session("c1", state, {}))
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            # Someone else rotates / posts a fresh message while we wait
            state.rows = [{"role": "user", "content": "new", "timestamp": datetime.now()}]
            lock.release()
            self.assertFalse(await asyncio.wait_for(waiter, 2))
            rotate.assert_not_awaited()


# ─── E. Incident regression for MentionCommands ─────────────────────────────

BOT_ID, BOB_ID, ALICE_ID = 1000, 2000, 3000


class FakeState:
    """In-memory stand-in for BotStateManager covering what the mention path uses."""

    def __init__(self):
        self.rows = []
        self._last = datetime(2026, 1, 1, 12, 0, 0)

    async def add_to_channel_history(self, channel_id, message):
        # Strictly increasing timestamps, like real arrival order
        self._last += timedelta(seconds=1)
        self.rows.append({**message, "timestamp": self._last, "channel_id": channel_id})
        return self._last

    def get_channel_history(self, channel_id, limit=None, until=None):
        rows = [r for r in self.rows if r["channel_id"] == channel_id
                and (until is None or r["timestamp"] <= until)]
        return [{k: v for k, v in r.items() if k != "channel_id"} for r in rows]

    def resolve_model(self, channel_id):
        return "openrouter", "test-model"

    def get_tool_calling_enabled(self):
        return False

    def get_effective_system_prompt(self, channel_id, query=None, speaker_id=None, history=None):
        return "sys"


class BlockingChatClient:
    def __init__(self, reply):
        self.reply = reply
        self.received = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def model_supports_vision(self, model_name):
        return True

    async def send_message_with_history(self, messages, model, system_prompt, images):
        self.received = [dict(m) for m in messages]
        self.started.set()
        await self.release.wait()
        return self.reply


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class MentionRaceRegressionTests(unittest.IsolatedAsyncioTestCase):
    def make_cog(self, client):
        self.sent = []

        async def send_response(channel, text, channel_id):
            self.sent.append(text)

        bot = types.SimpleNamespace(
            user=types.SimpleNamespace(id=BOT_ID, name="Gideon", display_name="Gideon", bot=True),
            state_manager=FakeState(),
            openrouter_client=client,
            openai_client=None,
            ai_horde_client=None,
            webhook_sender=types.SimpleNamespace(send_response=send_response),
        )
        cog = MentionCommands(bot)
        cog._active_channels = set()
        return cog

    def make_message(self, msg_id, author, content, mentions=()):
        channel = types.SimpleNamespace(id=555, typing=lambda: _Typing(), send=mock.AsyncMock())
        return types.SimpleNamespace(
            id=msg_id, author=author, content=content, mentions=list(mentions), role_mentions=[],
            channel_mentions=[], attachments=[], webhook_id=None, mention_everyone=False, channel=channel,
        )

    async def test_reply_ignores_messages_that_arrive_during_generation(self):
        client = BlockingChatClient("On it @Bob")
        cog = self.make_cog(client)
        bob = types.SimpleNamespace(id=BOB_ID, display_name="Bob", name="bob", bot=False)
        alice = types.SimpleNamespace(id=ALICE_ID, display_name="Alice", name="alice", bot=False)
        bot_user = cog.bot.user

        bob_msg = self.make_message(1, bob, f"<@{BOT_ID}> do the thing", mentions=[bot_user])
        alice_msg = self.make_message(2, alice, "Anyone there?")

        with mock.patch("src.cogs.mention_commands.check_and_rotate_session", mock.AsyncMock(return_value=False)), \
             mock.patch("src.cogs.mention_commands.schedule_compaction"):
            bob_task = asyncio.create_task(cog.on_message(bob_msg))
            await asyncio.wait_for(client.started.wait(), 2)

            # Bob's reply is "in flight"; Alice talks meanwhile
            await asyncio.wait_for(cog.on_message(alice_msg), 2)
            alice_rows = [r for r in cog.state.rows if r["name"] == "Alice"]
            self.assertEqual(len(alice_rows), 1)  # Alice's message was recorded, just not read

            client.release.set()
            await asyncio.wait_for(bob_task, 2)

        received = client.received
        self.assertTrue(received, "client was never called")
        self.assertEqual(received[-1]["role"], "user")
        self.assertEqual(received[-1]["name"], "Bob")
        self.assertIn("do the thing", received[-1]["content"])
        self.assertFalse(any("Anyone there" in str(m.get("content")) for m in received))

        # The reply pings Bob for real
        self.assertEqual(self.sent, [f"On it <@{BOB_ID}>"])
        # The anchor is cleaned up afterwards
        self.assertEqual(cog._history_anchors, {})
        # Both user rows and the assistant reply are persisted
        self.assertEqual([r["role"] for r in cog.state.rows], ["user", "user", "assistant"])

    async def test_reply_ignores_messages_landing_between_record_and_history_read(self):
        """Alice posts after Bob is recorded but before the history read (awaited image download).

        This is the window the `until` anchor protects; without it the reply
        would read Alice's turn as the latest one.
        """
        client = BlockingChatClient("On it @Bob")
        client.release.set()  # the LLM call itself doesn't block here
        cog = self.make_cog(client)
        bob = types.SimpleNamespace(id=BOB_ID, display_name="Bob", name="bob", bot=False)
        alice = types.SimpleNamespace(id=ALICE_ID, display_name="Alice", name="alice", bot=False)

        read_entered = asyncio.Event()
        read_release = asyncio.Event()

        async def slow_read():
            read_entered.set()
            await read_release.wait()
            return b"png-bytes"

        attachment = types.SimpleNamespace(filename="a.png", content_type="image/png", read=slow_read)
        bob_msg = self.make_message(1, bob, f"<@{BOT_ID}> look at this", mentions=[cog.bot.user])
        bob_msg.attachments = [attachment]
        alice_msg = self.make_message(2, alice, "Anyone there?")

        with mock.patch("src.cogs.mention_commands.check_and_rotate_session", mock.AsyncMock(return_value=False)), \
             mock.patch("src.cogs.mention_commands.schedule_compaction"):
            bob_task = asyncio.create_task(cog.on_message(bob_msg))
            await asyncio.wait_for(read_entered.wait(), 2)

            await asyncio.wait_for(cog.on_message(alice_msg), 2)
            self.assertEqual([r["name"] for r in cog.state.rows], ["Bob", "Alice"])

            read_release.set()
            await asyncio.wait_for(bob_task, 2)

        received = client.received
        self.assertTrue(received, "client was never called")
        self.assertEqual(received[-1]["name"], "Bob")
        self.assertIn("look at this", received[-1]["content"])
        self.assertFalse(any("Anyone there" in str(m.get("content")) for m in received))
        self.assertEqual(self.sent, [f"On it <@{BOB_ID}>"])

    async def test_without_anchor_later_messages_would_be_visible(self):
        """Sanity check that the fake state really would leak Alice's turn without `until`."""
        state = FakeState()
        t1 = await state.add_to_channel_history("c", {"role": "user", "name": "Bob", "content": "a"})
        await state.add_to_channel_history("c", {"role": "user", "name": "Alice", "content": "b"})
        self.assertEqual(len(state.get_channel_history("c")), 2)
        self.assertEqual([r["name"] for r in state.get_channel_history("c", until=t1)], ["Bob"])

    async def test_non_mention_message_is_recorded_but_not_answered(self):
        client = BlockingChatClient("never")
        cog = self.make_cog(client)
        cog._active_channels = {"555"}
        alice = types.SimpleNamespace(id=ALICE_ID, display_name="Alice", name="alice", bot=False)
        msg = self.make_message(9, alice, "just chatting")
        with mock.patch("src.cogs.mention_commands.check_and_rotate_session", mock.AsyncMock(return_value=False)), \
             mock.patch("src.cogs.mention_commands.schedule_compaction") as sched:
            await asyncio.wait_for(cog.on_message(msg), 2)
        sched.assert_called_once()
        self.assertIsNone(client.received)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(cog.state.rows), 1)
        self.assertEqual(cog._history_anchors, {})


if __name__ == "__main__":
    unittest.main()
