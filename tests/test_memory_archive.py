"""Tests for the memory-loss fixes: no channel history is deleted unsummarized."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

from src.utils import memory_service
from src.utils.database import DatabaseManager
from src.utils.state_manager import BotStateManager
from tests.dashboard_support import DashboardTestCase


class FakeSummarizer:
    """Chat client that records each channel-summary request.

    ``on_call`` runs while the request is "in flight", to simulate messages
    landing mid-summary; ``hold`` blocks the call until it is set. Per-user
    notes requests are recorded separately and answered with ``notes_reply``.
    """

    def __init__(self, reply="the summary", on_call=None, hold=None, notes_reply="{}"):
        self.reply = reply
        self.on_call = on_call
        self.hold = hold
        self.notes_reply = notes_reply
        self.prompts = []
        self.notes_prompts = []

    async def send_message_with_history(self, messages, model=None, system_prompt=None, images=None, **kwargs):
        if system_prompt == memory_service._USER_NOTES_SYSTEM:
            self.notes_prompts.append(messages[0]["content"])
            return self.notes_reply if not self.reply.startswith("⚠️") else self.reply
        self.prompts.append(messages[0]["content"])
        if self.on_call:
            await self.on_call()
        if self.hold:
            await self.hold.wait()
        return self.reply


def failing_summarizer():
    return FakeSummarizer(reply="⚠️ Error: upstream unavailable")


class RealStateTestCase(unittest.IsolatedAsyncioTestCase):
    """A real BotStateManager over a temp SQLite database."""

    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patcher in (
            mock.patch("src.utils.database.core.DATA_DIRECTORY", tmp.name),
            mock.patch("src.utils.state_manager.DatabaseManager", lambda: DatabaseManager()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        self.state = object.__new__(BotStateManager)
        await self.state.initialize_state()
        self.addCleanup(self.state.close_db)
        self.db = self.state.db_manager
        memory_service._channel_locks.clear()

    def add(self, channel_id, content, hours_ago, **kwargs):
        return self.db.add_message(
            kwargs.pop("role", "user"), content, datetime.now() - timedelta(hours=hours_ago),
            channel_id=channel_id, user_id=kwargs.pop("user_id", "u1"),
            user_name=kwargs.pop("user_name", "Alice"), **kwargs)

    def contents(self, channel_id):
        return [m["content"] for m in self.state.get_full_channel_history(channel_id)]

    def memories(self, channel_id):
        return self.state.get_channel_memories(channel_id)


# ─── A. Database primitives ─────────────────────────────────────────────────

class MessagePrimitiveTests(RealStateTestCase):
    async def test_history_rows_carry_primary_key(self):
        pk = self.add("c1", "m0", 1)
        self.assertEqual(self.state.get_channel_history("c1")[0]["message_pk"], pk)

    async def test_full_history_ignores_window(self):
        await self.state.set_max_channel_history(3)
        for i in range(8):
            self.add("c1", f"m{i}", 8 - i)
        self.assertEqual(len(self.state.get_channel_history("c1")), 3)
        self.assertEqual(self.contents("c1"), [f"m{i}" for i in range(8)])

    async def test_delete_through_pk_is_inclusive_and_channel_scoped(self):
        pks = [self.add("c1", f"m{i}", 5 - i) for i in range(4)]
        self.add("c2", "other", 10)
        self.assertEqual(self.state.delete_channel_messages_through("c1", pks[1]), 2)
        self.assertEqual(self.contents("c1"), ["m2", "m3"])
        self.assertEqual(self.contents("c2"), ["other"])

    async def test_prune_keeps_excluded_channels_only(self):
        self.add("keep", "old-keep", 100)
        self.add("drop", "old-drop", 100)
        self.add("drop", "new-drop", 1)
        self.db.add_thread("t1", None, "thread", datetime.now())
        self.db.add_message("user", "old-thread", datetime.now() - timedelta(hours=100), thread_id="t1")

        self.state.prune_old_data(retain_channel_ids={"keep"})

        self.assertEqual(self.contents("keep"), ["old-keep"])
        self.assertEqual(self.contents("drop"), ["new-drop"])
        self.assertEqual(self.state.get_discord_thread_history("t1"), [])

    async def test_channels_with_messages_before(self):
        self.add("old", "a", 100)
        self.add("new", "b", 1)
        cutoff = datetime.now() - timedelta(hours=48)
        self.assertEqual(self.state.get_channel_ids_with_messages_before(cutoff), ["old"])


# ─── B. Session rotation ────────────────────────────────────────────────────

class RotationTests(RealStateTestCase):
    async def test_rotation_summarizes_rows_beyond_history_window(self):
        await self.state.set_max_channel_history(3)
        for i in range(10):
            self.add("c1", f"msg-{i}", 40 - i)
        client = FakeSummarizer()

        self.assertTrue(await memory_service.check_and_rotate_session("c1", self.state, {"openrouter": client}))

        for i in range(10):
            self.assertIn(f"msg-{i}", client.prompts[0])
        [memory] = self.memories("c1")
        self.assertEqual(memory["message_count"], 10)
        self.assertEqual(self.contents("c1"), [])

    async def test_rotation_keeps_messages_recorded_mid_summary(self):
        for i in range(4):
            self.add("c1", f"old-{i}", 30 - i)

        async def new_message_lands():
            await self.state.add_to_channel_history("c1", {"role": "user", "content": "fresh", "name": "Bob"})

        client = FakeSummarizer(on_call=new_message_lands)
        await memory_service.check_and_rotate_session("c1", self.state, {"openrouter": client})

        self.assertEqual(self.contents("c1"), ["fresh"])
        self.assertEqual(len(self.memories("c1")), 1)

    async def test_active_session_does_not_rotate(self):
        self.add("c1", "recent", 1)
        client = FakeSummarizer()
        self.assertFalse(await memory_service.check_and_rotate_session("c1", self.state, {"openrouter": client}))
        self.assertEqual(client.prompts, [])


# ─── C. Pre-prune archive sweep ─────────────────────────────────────────────

class ArchiveBeforePruneTests(RealStateTestCase):
    async def prune(self, client):
        return await memory_service.archive_and_prune(self.state, {"openrouter": client})

    async def test_quiet_channel_is_summarized_before_prune(self):
        for i in range(5):
            self.add("quiet", f"weekend-{i}", 80 - i)

        stats = await self.prune(FakeSummarizer())

        [memory] = self.memories("quiet")
        self.assertEqual((memory["summary"], memory["message_count"]), ("the summary", 5))
        self.assertEqual(self.contents("quiet"), [])
        self.assertEqual(stats["messages_pruned"], 0)  # already archived by the sweep

    async def test_slow_channel_archives_only_expiring_rows(self):
        for i in range(4):
            self.add("slow", f"old-{i}", 60 - i)
        self.add("slow", "recent", 2)
        client = FakeSummarizer()

        await self.prune(client)

        self.assertNotIn("recent", client.prompts[0])
        self.assertEqual(self.memories("slow")[0]["message_count"], 4)
        self.assertEqual(self.contents("slow"), ["recent"])

    async def test_failed_summary_keeps_history_for_retry(self):
        for i in range(4):
            self.add("flaky", f"keep-{i}", 70 - i)
        for i in range(4):
            self.add("fine", f"gone-{i}", 70 - i)

        real = memory_service._summarize_history

        async def summarize(history, channel_id, state, clients):
            if channel_id == "flaky":
                return None
            return await real(history, channel_id, state, clients)

        with mock.patch.object(memory_service, "_summarize_history", summarize):
            await self.prune(FakeSummarizer())

        self.assertEqual(self.contents("flaky"), [f"keep-{i}" for i in range(4)])
        self.assertEqual(self.memories("flaky"), [])
        self.assertEqual(self.contents("fine"), [])
        self.assertEqual(len(self.memories("fine")), 1)

        # The next prune retries and succeeds
        await self.prune(FakeSummarizer())
        self.assertEqual(self.contents("flaky"), [])
        self.assertEqual(len(self.memories("flaky")), 1)

    async def test_llm_error_reply_keeps_history(self):
        for i in range(3):
            self.add("c1", f"m{i}", 70 - i)
        await self.prune(failing_summarizer())
        self.assertEqual(len(self.contents("c1")), 3)
        self.assertEqual(self.memories("c1"), [])

    async def test_no_client_prunes_unsummarized(self):
        for i in range(3):
            self.add("c1", f"m{i}", 70 - i)
        await memory_service.archive_and_prune(self.state, {"openrouter": None})
        self.assertEqual(self.contents("c1"), [])
        self.assertEqual(self.memories("c1"), [])

    async def test_summary_disabled_prunes_without_llm_call(self):
        await self.state.set_memory_summary_enabled(False)
        for i in range(3):
            self.add("c1", f"m{i}", 70 - i)
        client = FakeSummarizer()
        await self.prune(client)
        self.assertEqual(client.prompts, [])
        self.assertEqual(self.contents("c1"), [])

    async def test_too_few_rows_pruned_without_summary(self):
        self.add("c1", "lonely", 70)
        client = FakeSummarizer()
        await self.prune(client)
        self.assertEqual(client.prompts, [])
        self.assertEqual(self.contents("c1"), [])

    async def test_sweep_waits_for_channel_lock(self):
        for i in range(3):
            self.add("c1", f"m{i}", 70 - i)
        client = FakeSummarizer()
        lock = memory_service._channel_lock("c1")
        await lock.acquire()
        task = asyncio.create_task(self.prune(client))
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(client.prompts, [])  # blocked behind rotation/compaction
        lock.release()
        await asyncio.wait_for(task, 2)
        self.assertEqual(len(self.memories("c1")), 1)


# ─── D. Dashboard endpoints ─────────────────────────────────────────────────

class DashboardMemoryTests(DashboardTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.login()
        memory_service._channel_locks.clear()

    def add(self, content, hours_ago=1, channel_id="100"):
        return self.db.add_message("user", content, datetime.now() - timedelta(hours=hours_ago),
                                   channel_id=channel_id, user_id="u1", user_name="Alice")

    def contents(self, channel_id="100"):
        return [m["content"] for m in self.state.get_full_channel_history(channel_id)]

    async def test_summarize_covers_rows_beyond_window(self):
        await self.state.set_max_channel_history(3)
        for i in range(8):
            self.add(f"m{i}", hours_ago=8 - i)
        self.bot.openrouter_client = FakeSummarizer()

        resp = await self.client.post("/api/memory/100/summarize")

        self.assertEqual(resp.status, 200, await resp.text())
        self.assertEqual((await resp.json())["messages_archived"], 8)
        self.assertEqual(self.contents(), [])
        self.assertEqual(self.state.get_channel_memories("100")[0]["message_count"], 8)

    async def test_summarize_keeps_messages_arriving_mid_call(self):
        for i in range(3):
            self.add(f"m{i}")

        async def new_message_lands():
            self.add("fresh", hours_ago=0)

        self.bot.openrouter_client = FakeSummarizer(on_call=new_message_lands)
        resp = await self.client.post("/api/memory/100/summarize")
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.contents(), ["fresh"])

    async def test_summarize_failure_keeps_history(self):
        for i in range(3):
            self.add(f"m{i}")
        self.bot.openrouter_client = failing_summarizer()
        resp = await self.client.post("/api/memory/100/summarize")
        self.assertEqual(resp.status, 502)
        self.assertEqual(len(self.contents()), 3)

    async def test_summarize_empty_history_is_400(self):
        self.bot.openrouter_client = FakeSummarizer()
        resp = await self.client.post("/api/memory/100/summarize")
        self.assertEqual(resp.status, 400)

    async def test_summarize_waits_for_channel_lock(self):
        for i in range(3):
            self.add(f"m{i}")
        client = FakeSummarizer()
        self.bot.openrouter_client = client
        lock = memory_service._channel_lock("100")
        await lock.acquire()
        request = asyncio.create_task(self.client.post("/api/memory/100/summarize"))
        await asyncio.sleep(0.05)
        self.assertEqual(client.prompts, [])
        lock.release()
        resp = await asyncio.wait_for(request, 2)
        self.assertEqual(resp.status, 200)

    async def test_manual_prune_archives_quiet_channels(self):
        for i in range(3):
            self.add(f"m{i}", hours_ago=70 - i)
        self.bot.openrouter_client = FakeSummarizer()
        resp = await self.client.post("/api/diagnostics/prune")
        self.assertEqual(resp.status, 200, await resp.text())
        self.assertEqual(self.contents(), [])
        self.assertEqual(len(self.state.get_channel_memories("100")), 1)
