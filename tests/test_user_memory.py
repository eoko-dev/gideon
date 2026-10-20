"""Tests for per-user memory: notes are unique per (channel, user) and never cross channels."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

import discord

from src.cogs.chat_commands import ChatCommands
from src.cogs.thread_commands import ThreadCommands
from src.utils import memory_service, user_memory
from src.utils.tool_registry import execute_tool, get_tool_names
from tests.dashboard_support import DashboardTestCase
from tests.test_memory_archive import RealStateTestCase

ALICE, BOB, BOT = "111", "222", "999"


class NotesClient:
    """Chat client answering channel summaries with text and notes requests with JSON.

    ``notes`` is the reply to a notes request — a dict (serialized), a raw
    string, or a callable taking the prompt. ``during_notes`` runs while the
    notes request is in flight.
    """

    def __init__(self, notes=None, during_notes=None):
        self.notes = notes if notes is not None else {}
        self.during_notes = during_notes
        self.notes_prompts = []
        self.summary_calls = 0

    async def send_message_with_history(self, messages, model=None, system_prompt=None, images=None, **kwargs):
        if system_prompt != memory_service._USER_NOTES_SYSTEM:
            self.summary_calls += 1
            return "a channel summary"
        prompt = messages[0]["content"]
        self.notes_prompts.append(prompt)
        if self.during_notes:
            await self.during_notes()
        reply = self.notes(prompt) if callable(self.notes) else self.notes
        return reply if isinstance(reply, str) else json.dumps(reply)


class UserMemoryTestCase(RealStateTestCase):
    def say(self, channel_id, user_id, name, content, hours_ago, **kwargs):
        return self.db.add_message("user", content, datetime.now() - timedelta(hours=hours_ago),
                                   channel_id=channel_id, user_id=user_id, user_name=name, **kwargs)

    def reply(self, channel_id, content, hours_ago):
        return self.db.add_message("assistant", content, datetime.now() - timedelta(hours=hours_ago),
                                   channel_id=channel_id)

    def notes(self, channel_id, user_id):
        row = self.state.get_user_notes(channel_id, user_id)
        return user_memory.split_notes(row["notes"]) if row else []

    async def rotate(self, channel_id, client):
        return await memory_service.check_and_rotate_session(channel_id, self.state, {"openrouter": client})


# ─── A. Storage isolation ───────────────────────────────────────────────────

class StorageIsolationTests(UserMemoryTestCase):
    async def test_same_user_has_separate_notes_per_channel(self):
        self.state.set_user_notes("A", ALICE, "- Likes Rust", "Alice")
        self.state.set_user_notes("B", ALICE, "- Likes Go", "Alice")
        self.assertEqual(self.notes("A", ALICE), ["Likes Rust"])
        self.assertEqual(self.notes("B", ALICE), ["Likes Go"])
        self.assertEqual(self.state.get_user_notes("C", ALICE), None)

    async def test_bulk_read_is_scoped_to_one_channel(self):
        self.state.set_user_notes("A", ALICE, "- a", "Alice")
        self.state.set_user_notes("B", BOB, "- b", "Bob")
        self.assertEqual(set(self.state.get_user_notes_for("A", [ALICE, BOB])), {ALICE})

    async def test_manager_requires_channel_and_user(self):
        manager = self.db.user_memory
        for call in (lambda: manager.get_notes("", ALICE), lambda: manager.get_notes("A", None),
                     lambda: manager.set_notes(None, ALICE, "- x"), lambda: manager.set_notes("A", "", "- x"),
                     lambda: manager.get_notes_for_users("", [ALICE]), lambda: manager.delete_notes("A", "")):
            with self.assertRaises(ValueError):
                call()

    async def test_deletes_only_touch_their_target(self):
        for channel in ("A", "B"):
            for user in (ALICE, BOB):
                self.state.set_user_notes(channel, user, f"- {channel}{user}")
        self.assertEqual(self.state.delete_user_notes("A", ALICE), 1)
        self.assertEqual(self.notes("B", ALICE), ["B111"])
        self.assertEqual(self.notes("A", BOB), ["A222"])

        self.assertEqual(self.state.delete_user_notes_everywhere(BOB), 2)
        self.assertEqual(self.notes("B", ALICE), ["B111"])

        self.assertEqual(self.state.delete_channel_user_notes("B"), 1)
        self.assertEqual(self.state.get_user_memory_stats(), {})


# ─── B. Memory scope ────────────────────────────────────────────────────────

class ScopeTests(UserMemoryTestCase):
    async def test_tracked_thread_shares_parent_scope(self):
        self.db.add_thread("T1", "A", "thread", datetime.now())
        self.assertEqual(self.state.get_memory_scope("T1"), "A")
        self.state.set_user_notes("T1", ALICE, "- from the thread")
        self.assertEqual(self.notes("A", ALICE), ["from the thread"])

    async def test_untracked_thread_resolved_through_discord(self):
        thread = mock.MagicMock(spec=discord.Thread)
        thread.parent_id = 42
        self.state.set_channel_resolver(lambda cid: thread if cid == 77 else None)
        self.assertEqual(self.state.get_memory_scope("77"), "42")
        self.assertEqual(self.state.get_memory_scope("78"), "78")

    async def test_text_channel_is_its_own_scope(self):
        channel = mock.MagicMock(spec=discord.TextChannel)
        self.state.set_channel_resolver(lambda cid: channel)
        self.assertEqual(self.state.get_memory_scope("55"), "55")

    async def test_thread_follows_parent_enabled_setting(self):
        self.db.add_thread("T1", "A", "thread", datetime.now())
        self.state.set_channel_memory_config("A", user_memory_enabled=False)
        self.assertFalse(self.state.get_effective_user_memory_enabled("T1"))
        self.assertTrue(self.state.get_effective_user_memory_enabled("B"))


# ─── C. Extraction ──────────────────────────────────────────────────────────

class ExtractionTests(UserMemoryTestCase):
    def two_person_chat(self, channel="A"):
        self.say(channel, ALICE, "Alice", "I'm learning Rust", 30)
        self.reply(channel, "Nice!", 29.9)
        self.say(channel, BOB, "Bob", "Alice is secretly a cat", 29.8)
        self.say(channel, BOB, "Bob", "I live in Oslo", 29.7)

    async def test_rotation_writes_notes_for_each_speaker_in_this_channel(self):
        self.two_person_chat()
        client = NotesClient({"P1": ["Is learning Rust"], "P2": ["Lives in Oslo"]})

        await self.rotate("A", client)

        self.assertEqual(self.notes("A", ALICE), ["Is learning Rust"])
        self.assertEqual(self.notes("A", BOB), ["Lives in Oslo"])
        self.assertEqual(self.state.get_user_memory_stats(), {"A": 2})
        row = self.state.get_user_notes("A", BOB)
        self.assertEqual((row["display_name"], row["message_count"]), ("Bob", 2))

    async def test_labels_are_assigned_in_speaking_order(self):
        self.two_person_chat()
        client = NotesClient()
        await self.rotate("A", client)
        prompt = client.notes_prompts[0]
        self.assertIn("[P1] Alice: I'm learning Rust", prompt)
        self.assertIn("[P2] Bob: I live in Oslo", prompt)
        self.assertIn("Gideon: Nice!", prompt)
        self.assertIn("Never record what one person claims about another", prompt)

    async def test_unknown_labels_are_dropped(self):
        self.two_person_chat()
        await self.rotate("A", NotesClient({"P7": ["Injected"], "U123": ["Nope"], ALICE: ["Raw id"]}))
        self.assertEqual(self.state.get_user_memory_stats(), {})

    async def test_other_channels_notes_never_enter_the_prompt(self):
        self.state.set_user_notes("B", ALICE, "- SECRET-FROM-B")
        self.state.set_user_notes("A", ALICE, "- Known in A")
        self.two_person_chat("A")
        client = NotesClient({"P1": ["Known in A", "Is learning Rust"]})

        await self.rotate("A", client)

        self.assertNotIn("SECRET-FROM-B", client.notes_prompts[0])
        self.assertIn("Known in A", client.notes_prompts[0])
        self.assertEqual(self.notes("B", ALICE), ["SECRET-FROM-B"])
        self.assertEqual(self.notes("A", ALICE), ["Known in A", "Is learning Rust"])

    async def test_bots_and_opted_out_users_get_no_label_or_notes(self):
        self.state.set_user_memory_opt_out(BOB, True)
        self.say("A", ALICE, "Alice", "hi", 30)
        self.say("A", BOB, "Bob", "I like tea", 29.9)
        self.say("A", BOT, "Helper", "beep", 29.8, is_bot=True)
        client = NotesClient({"P1": ["Says hi"], "P2": ["Likes tea"], "P3": ["Is a bot"]})

        await self.rotate("A", client)

        prompt = client.notes_prompts[0]
        self.assertIn("Bob: I like tea", prompt)
        self.assertNotIn("] Bob", prompt)
        self.assertNotIn("] Helper", prompt)
        self.assertEqual(self.notes("A", ALICE), ["Says hi"])
        self.assertEqual(self.state.get_user_notes("A", BOB), None)
        self.assertEqual(self.state.get_user_notes("A", BOT), None)

    async def test_only_humans_trigger_an_extraction(self):
        self.say("A", BOT, "Helper", "beep", 30, is_bot=True)
        self.reply("A", "boop", 29.9)
        client = NotesClient()
        await self.rotate("A", client)
        self.assertEqual(client.notes_prompts, [])

    async def test_disabled_channel_skips_extraction(self):
        self.state.set_channel_memory_config("A", user_memory_enabled=False)
        self.two_person_chat()
        client = NotesClient({"P1": ["x"]})
        await self.rotate("A", client)
        self.assertEqual(client.notes_prompts, [])
        self.assertEqual(self.state.get_user_memory_stats(), {})

    async def test_notes_saved_mid_extraction_are_kept(self):
        self.state.set_user_notes("A", ALICE, "- Old fact")
        self.two_person_chat()

        async def remember_lands():
            self.state.set_user_notes("A", ALICE, "- Old fact\n- Remembered meanwhile")

        await self.rotate("A", NotesClient({"P1": ["Old fact", "Is learning Rust"]}, during_notes=remember_lands))
        self.assertEqual(self.notes("A", ALICE), ["Old fact", "Is learning Rust", "Remembered meanwhile"])

    async def test_notes_cleared_mid_extraction_stay_cleared(self):
        self.state.set_user_notes("A", ALICE, "- Old fact")
        self.two_person_chat()

        async def forget_lands():
            self.state.delete_user_notes("A", ALICE)

        await self.rotate("A", NotesClient({"P1": ["Old fact", "New"]}, during_notes=forget_lands))
        self.assertEqual(self.state.get_user_notes("A", ALICE), None)

    async def test_opt_out_mid_extraction_is_respected(self):
        self.two_person_chat()

        async def opt_out():
            self.state.set_user_memory_opt_out(ALICE, True)

        await self.rotate("A", NotesClient({"P1": ["x"], "P2": ["y"]}, during_notes=opt_out))
        self.assertEqual(self.state.get_user_notes("A", ALICE), None)
        self.assertEqual(self.notes("A", BOB), ["y"])

    async def test_notes_are_capped(self):
        self.say("A", ALICE, "Alice", "lots", 30)
        many = [f"fact {i}" for i in range(40)] + ["x" * 500]
        await self.rotate("A", NotesClient({"P1": many}))
        bullets = self.notes("A", ALICE)
        self.assertEqual(len(bullets), user_memory.MAX_NOTE_BULLETS)
        self.assertTrue(all(len(b) <= user_memory.MAX_BULLET_CHARS for b in bullets))

    async def test_parse_accepts_fenced_json_and_rejects_garbage(self):
        labels = {"P1": ALICE}
        self.assertEqual(memory_service._parse_note_updates('```json\n{"P1": ["a"]}\n```', labels), {ALICE: ["a"]})
        self.assertEqual(memory_service._parse_note_updates('{"[P1]": "- a\\n- b"}', labels), {ALICE: ["a", "b"]})
        self.assertIsNone(memory_service._parse_note_updates("no json here", labels))
        self.assertIsNone(memory_service._parse_note_updates("[1, 2]", labels))


class PruneExtractionTests(UserMemoryTestCase):
    async def prune(self, client):
        return await memory_service.archive_and_prune(self.state, {"openrouter": client})

    async def test_thread_messages_are_extracted_into_parent_scope(self):
        self.db.add_thread("T1", "A", "thread", datetime.now())
        self.db.add_message("user", "I use Vim", datetime.now() - timedelta(hours=70),
                            thread_id="T1", user_id=ALICE, user_name="Alice")

        await self.prune(NotesClient({"P1": ["Uses Vim"]}))

        self.assertEqual(self.notes("A", ALICE), ["Uses Vim"])
        self.assertEqual(self.state.get_user_notes("T1", ALICE)["channel_id"], "A")
        self.assertEqual(self.state.get_full_thread_history("T1"), [])

    async def test_failed_thread_extraction_keeps_its_messages(self):
        self.db.add_thread("T1", "A", "thread", datetime.now())
        self.db.add_message("user", "I use Vim", datetime.now() - timedelta(hours=70),
                            thread_id="T1", user_id=ALICE, user_name="Alice")

        await self.prune(NotesClient("⚠️ Error: down"))

        self.assertEqual(len(self.state.get_full_thread_history("T1")), 1)

    async def test_failed_channel_extraction_keeps_its_messages(self):
        for i in range(3):
            self.say("A", ALICE, "Alice", f"m{i}", 70 - i)
        await self.prune(NotesClient("⚠️ Error: down"))
        self.assertEqual(len(self.state.get_full_channel_history("A")), 3)

    async def test_malformed_reply_does_not_block_pruning(self):
        for i in range(3):
            self.say("A", ALICE, "Alice", f"m{i}", 70 - i)
        await self.prune(NotesClient("I couldn't find anything."))
        self.assertEqual(self.state.get_full_channel_history("A"), [])
        self.assertEqual(self.state.get_user_memory_stats(), {})


# ─── D. Prompt injection ────────────────────────────────────────────────────

class PromptInjectionTests(UserMemoryTestCase):
    def history(self, *speakers):
        return [{"role": "user", "user_id": uid, "name": name, "content": "hi", "is_bot": is_bot}
                for uid, name, is_bot in speakers]

    async def test_speaker_notes_only_in_their_own_channel(self):
        self.state.set_user_notes("A", ALICE, "- Prefers metric", "Alice")
        prompt_a = self.state.get_effective_system_prompt("A", speaker_id=ALICE)
        prompt_b = self.state.get_effective_system_prompt("B", speaker_id=ALICE)
        self.assertIn("Prefers metric", prompt_a)
        self.assertIn("Alice (the person you are replying to)", prompt_a)
        self.assertNotIn("Prefers metric", prompt_b)
        self.assertNotIn(user_memory.PEOPLE_BLOCK_HEADER, prompt_b)

    async def test_participants_get_short_notes_and_absentees_none(self):
        self.state.set_user_notes("A", ALICE, "- a1", "Alice")
        self.state.set_user_notes("A", BOB, "\n".join(f"- b{i}" for i in range(8)), "Bob")
        self.state.set_user_notes("A", "333", "- absent person", "Carol")
        self.state.set_user_notes("A", BOT, "- bot note", "Helper")

        prompt = self.state.get_effective_system_prompt(
            "A", speaker_id=ALICE,
            history=self.history((BOB, "Bobby", False), (BOT, "Helper", True), (ALICE, "Alice", False)))

        self.assertIn("Bobby:\n- b0", prompt)
        self.assertIn("- b3", prompt)
        self.assertNotIn("- b4", prompt)
        self.assertNotIn("absent person", prompt)
        self.assertNotIn("bot note", prompt)

    async def test_disabled_channel_injects_nothing(self):
        self.state.set_user_notes("A", ALICE, "- a1")
        self.state.set_channel_memory_config("A", user_memory_enabled=False)
        self.assertNotIn("a1", self.state.get_effective_system_prompt("A", speaker_id=ALICE))

    async def test_thread_prompt_override_keeps_framing_and_parent_notes(self):
        self.db.add_thread("T1", "A", "thread", datetime.now())
        self.state.set_user_notes("A", ALICE, "- a1", "Alice")
        prompt = self.state.get_effective_system_prompt("A", speaker_id=ALICE, base_prompt="Be a pirate.")
        self.assertTrue(prompt.startswith("Be a pirate."))
        self.assertIn("--- Conversation Format ---", prompt)
        self.assertIn("- a1", prompt)

    async def test_no_speaker_means_no_people_block(self):
        self.state.set_user_notes("A", ALICE, "- a1")
        self.assertNotIn("a1", self.state.get_effective_system_prompt("A"))


# ─── E. Tools ───────────────────────────────────────────────────────────────

class MemoryToolTests(UserMemoryTestCase):
    def ctx(self, channel_id="A", user_id=ALICE, name="Alice"):
        author = SimpleNamespace(id=int(user_id), display_name=name)
        return {"state": self.state, "channel_id": channel_id, "message": SimpleNamespace(author=author)}

    async def test_tools_are_registered(self):
        self.assertTrue({"remember_about_me", "forget_about_me"} <= set(get_tool_names()))

    async def test_remember_writes_only_to_author_in_this_channel(self):
        result = await execute_tool("remember_about_me",
                                    {"fact": "Prefers metric", "user_id": BOB, "channel_id": "B"}, self.ctx())
        self.assertIn("Saved", result)
        self.assertEqual(self.notes("A", ALICE), ["Prefers metric"])
        self.assertEqual(self.state.get_user_memory_stats(), {"A": 1})

    async def test_remember_refused_when_opted_out_or_disabled(self):
        self.state.set_user_memory_opt_out(ALICE, True)
        self.assertTrue((await execute_tool("remember_about_me", {"fact": "x"}, self.ctx())).startswith("Error"))
        self.state.set_user_memory_opt_out(ALICE, False)
        self.state.set_channel_memory_config("A", user_memory_enabled=False)
        self.assertTrue((await execute_tool("remember_about_me", {"fact": "x"}, self.ctx())).startswith("Error"))
        self.assertEqual(self.state.get_user_memory_stats(), {})

    async def test_forget_matching_note_or_everything_here(self):
        self.state.set_user_notes("A", ALICE, "- Likes tea\n- Lives in Oslo")
        self.state.set_user_notes("B", ALICE, "- Likes tea")

        await execute_tool("forget_about_me", {"fact": "tea"}, self.ctx())
        self.assertEqual(self.notes("A", ALICE), ["Lives in Oslo"])

        await execute_tool("forget_about_me", {}, self.ctx())
        self.assertEqual(self.state.get_user_notes("A", ALICE), None)
        self.assertEqual(self.notes("B", ALICE), ["Likes tea"])


# ─── F. Opt-out ─────────────────────────────────────────────────────────────

class OptOutTests(UserMemoryTestCase):
    async def test_opt_out_deletes_everywhere_and_opt_in_restores_tracking(self):
        self.state.set_user_notes("A", ALICE, "- a")
        self.state.set_user_notes("B", ALICE, "- b")
        self.state.set_user_notes("A", BOB, "- bob")
        self.assertEqual(self.state.set_user_memory_opt_out(ALICE, True), 2)
        self.assertTrue(self.state.is_user_memory_opted_out(ALICE))
        self.assertEqual(self.notes("A", BOB), ["bob"])

        self.state.set_user_memory_opt_out(ALICE, False)
        self.assertFalse(self.state.is_user_memory_opted_out(ALICE))


# ─── G. Slash commands ──────────────────────────────────────────────────────

def slash_ctx(channel_id, user_id=ALICE, admin=False):
    ctx = mock.MagicMock()
    ctx.channel.id = int(channel_id) if str(channel_id).isdigit() else channel_id
    ctx.author.id = int(user_id)
    ctx.author.guild_permissions.administrator = admin
    ctx.respond = mock.AsyncMock()
    ctx.defer = mock.AsyncMock()
    return ctx


class SlashCommandTests(UserMemoryTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.cog = ChatCommands(SimpleNamespace(state_manager=self.state), None, None, None)

    def replied(self, ctx):
        return ctx.respond.await_args.args[0]

    async def test_memory_me_shows_only_this_channel(self):
        self.state.set_user_notes("100", ALICE, "- in hundred")
        self.state.set_user_notes("200", ALICE, "- in two hundred")
        ctx = slash_ctx("100")
        await self.cog.memory_me_slash.callback(self.cog, ctx)
        self.assertIn("in hundred", self.replied(ctx))
        self.assertNotIn("two hundred", self.replied(ctx))
        self.assertTrue(ctx.respond.await_args.kwargs.get("ephemeral"))

    async def test_forget_this_channel_vs_everywhere(self):
        for channel in ("100", "200"):
            self.state.set_user_notes(channel, ALICE, "- x")
        await self.cog.memory_forget_slash.callback(self.cog, slash_ctx("100"), scope="this channel")
        self.assertEqual(self.state.get_user_memory_stats(), {"200": 1})
        await self.cog.memory_forget_slash.callback(self.cog, slash_ctx("100"), scope="everywhere")
        self.assertEqual(self.state.get_user_memory_stats(), {})

    async def test_forget_user_requires_admin(self):
        self.state.set_user_notes("100", BOB, "- bob")
        member = SimpleNamespace(id=int(BOB), display_name="Bob")
        await self.cog.memory_forget_user_slash.callback(self.cog, slash_ctx("100"), user=member)
        self.assertEqual(self.notes("100", BOB), ["bob"])
        await self.cog.memory_forget_user_slash.callback(self.cog, slash_ctx("100", admin=True), user=member)
        self.assertEqual(self.state.get_user_notes("100", BOB), None)

    async def test_optout_command(self):
        self.state.set_user_notes("100", ALICE, "- x")
        await self.cog.memory_optout_slash.callback(self.cog, slash_ctx("100"))
        self.assertTrue(self.state.is_user_memory_opted_out(ALICE))
        self.assertEqual(self.state.get_user_memory_stats(), {})


class ThreadMessageScopeTests(UserMemoryTestCase):
    async def test_thread_message_refused_from_another_channel(self):
        self.db.add_thread("500", "100", "thread", datetime.now())
        llm = mock.MagicMock()
        cog = ThreadCommands(SimpleNamespace(state_manager=self.state, openrouter_client=llm))
        ctx = slash_ctx("200")
        await cog.thread_chat_slash.callback(cog, ctx, id="500", message="hello")
        self.assertIn("thread or its parent channel", self.replied(ctx))
        self.assertEqual(self.state.get_discord_thread_history("500"), [])

    def replied(self, ctx):
        return ctx.respond.await_args.args[0]


# ─── H. Dashboard ───────────────────────────────────────────────────────────

class DashboardUserMemoryTests(DashboardTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.login()

    async def test_list_is_scoped_to_channel(self):
        self.state.set_user_notes("100", ALICE, "- a", "Alice")
        self.state.set_user_notes("200", BOB, "- b", "Bob")
        data = await (await self.client.get("/api/memory/100/users")).json()
        self.assertTrue(data["enabled"])
        self.assertEqual([(u["user_id"], u["notes"]) for u in data["users"]], [(ALICE, "- a")])

    async def test_edit_and_delete(self):
        self.state.set_user_notes("100", ALICE, "- a", "Alice")
        self.state.set_user_notes("200", ALICE, "- other", "Alice")

        resp = await self.client.put(f"/api/memory/100/users/{ALICE}", json={"notes": "new one\n- second"})
        self.assertEqual(resp.status, 200, await resp.text())
        self.assertEqual(self.state.get_user_notes("100", ALICE)["notes"], "- new one\n- second")

        self.assertEqual((await self.client.put(f"/api/memory/100/users/{BOB}", json={"notes": "x"})).status, 404)
        self.assertEqual((await self.client.put(f"/api/memory/100/users/{ALICE}", json={"notes": "  "})).status, 400)

        await self.client.delete(f"/api/memory/100/users/{ALICE}")
        self.assertIsNone(self.state.get_user_notes("100", ALICE))
        self.assertEqual(self.state.get_user_notes("200", ALICE)["notes"], "- other")

    async def test_clear_channel_people_notes(self):
        self.state.set_user_notes("100", ALICE, "- a")
        self.state.set_user_notes("100", BOB, "- b")
        self.state.set_user_notes("200", ALICE, "- c")
        body = await (await self.client.delete("/api/memory/100/users")).json()
        self.assertEqual(body["deleted"], 2)
        self.assertEqual(self.state.get_user_memory_stats(), {"200": 1})

    async def test_stats_include_people_counts(self):
        self.state.add_channel_memory("100", "summary", 3)
        self.state.set_user_notes("100", ALICE, "- a")
        self.state.set_user_notes("300", BOB, "- b")
        stats = {s["channel_id"]: s for s in await (await self.client.get("/api/memory")).json()}
        self.assertEqual((stats["100"]["summary_count"], stats["100"]["user_count"]), (1, 1))
        self.assertEqual((stats["300"]["summary_count"], stats["300"]["user_count"]), (0, 1))

    async def test_settings_and_channel_override(self):
        resp = await self.client.put("/api/settings", json={"user_memory_enabled": False})
        self.assertEqual(resp.status, 200, await resp.text())
        self.assertFalse((await (await self.client.get("/api/settings")).json())["user_memory_enabled"])

        resp = await self.client.put("/api/channels/100", json={"user_memory_enabled": True})
        self.assertEqual(resp.status, 200, await resp.text())
        detail = await (await self.client.get("/api/channels/100")).json()
        self.assertEqual((detail["user_memory_enabled"], detail["effective_user_memory_enabled"],
                          detail["sources"]["user_memory_enabled"]), (1, True, "channel"))

        self.assertEqual((await self.client.put("/api/channels/100", json={"user_memory_enabled": "maybe"})).status, 400)

        self.assertEqual((await self.client.delete("/api/channels/100")).status, 200)
        detail = await (await self.client.get("/api/channels/100")).json()
        self.assertEqual((detail["user_memory_enabled"], detail["effective_user_memory_enabled"]), (None, False))

    async def test_config_export_excludes_people_notes(self):
        self.state.set_user_notes("100", ALICE, "- private")
        export = self.db.export_config_json()
        self.assertNotIn("CHANNEL_USER_MEMORY", export["tables"])
        self.assertNotIn("private", json.dumps(export, default=str))
