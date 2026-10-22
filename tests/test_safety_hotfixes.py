"""Tests for the Phase 0 safety fixes: pings, calculator, URL fetching,
permission checks, bot-loop protection and the video download key."""
import asyncio
import os
import tempfile
import time
import types
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

import discord

from src.utils.discord_fmt import SAFE_ALLOWED_MENTIONS
from src.utils.intent_handlers.calculation import evaluate_expression
from src.utils.openrouter_video_client import OpenRouterVideoClient
from src.utils.permissions import can_create_events, can_manage_ai_thread, has_channel_permission
from src.utils.safe_fetch import (
    PublicOnlyResolver, UnsafeURLError, fetch_text, is_public_ip, validate_url,
)
from src.utils.webhook_sender import WebhookSender

from tests.test_mention_race_and_pings import ALICE_ID, BOT_ID, MentionRaceRegressionTests


# ─── Pings ──────────────────────────────────────────────────────────────────

class AllowedMentionsTests(unittest.IsolatedAsyncioTestCase):
    def test_safe_allowed_mentions_blocks_everyone_and_roles(self):
        self.assertFalse(SAFE_ALLOWED_MENTIONS.everyone)
        self.assertFalse(SAFE_ALLOWED_MENTIONS.roles)
        self.assertTrue(SAFE_ALLOWED_MENTIONS.users)

    async def test_webhook_send_passes_safe_allowed_mentions(self):
        sender = WebhookSender(types.SimpleNamespace())
        webhook = types.SimpleNamespace(send=mock.AsyncMock())
        await sender._webhook_send(webhook, "@everyone hi", {"display_name": "Bot"})
        self.assertIs(webhook.send.call_args.kwargs["allowed_mentions"], SAFE_ALLOWED_MENTIONS)

    async def test_standard_send_passes_safe_allowed_mentions(self):
        sender = WebhookSender(types.SimpleNamespace())
        channel = types.SimpleNamespace(send=mock.AsyncMock())
        await sender._standard_send(channel, "@everyone hi")
        self.assertIs(channel.send.call_args.kwargs["allowed_mentions"], SAFE_ALLOWED_MENTIONS)


# ─── Calculator ─────────────────────────────────────────────────────────────

class CalculatorTests(unittest.TestCase):
    def test_supported_expressions(self):
        cases = {
            "15% of 250": "37.5",
            "2^10": "1024",
            "sqrt(16)": "4",
            "square root of 81": "9",
            "5 squared": "25",
            "3(4+1)": "15",
            "log10(1000)": "3",
            "5!": "120",
            "1,000,000 / 4": "250000",
            "sin(pi/2)": "1",
            "1.5e3 * 2": "3000",
            "10/3": "3.3333333333",
        }
        for expr, expected in cases.items():
            with self.subTest(expr=expr):
                self.assertEqual(evaluate_expression(expr), f"{expr} = {expected}")

    def test_rejects_code_and_unknown_names(self):
        for expr in ['__import__("os").system("id")', "().__class__", "open('x')",
                     "lambda: 1", "[1, 2]", "x + 1", "exec('1')"]:
            with self.subTest(expr=expr):
                with self.assertRaises(ValueError):
                    evaluate_expression(expr)

    def test_huge_expressions_fail_fast(self):
        for expr in ["9**9**9**9", "10**10**10", "factorial(100000)", "2**100000", "1" * 400]:
            with self.subTest(expr=expr):
                start = time.monotonic()
                with self.assertRaises(ValueError):
                    evaluate_expression(expr)
                self.assertLess(time.monotonic() - start, 0.5)

    def test_division_by_zero(self):
        with self.assertRaises(ValueError):
            evaluate_expression("1/0")


# ─── URL fetching ───────────────────────────────────────────────────────────

class ValidateUrlTests(unittest.TestCase):
    def test_public_urls_pass(self):
        self.assertEqual(validate_url(" https://example.com/a "), "https://example.com/a")
        self.assertEqual(validate_url("http://93.184.216.34/"), "http://93.184.216.34/")

    def test_blocked_urls(self):
        for url in ["http://127.0.0.1:8080/api", "http://localhost/", "http://[::1]/",
                    "http://169.254.169.254/latest/meta-data", "http://10.0.0.5/",
                    "http://192.168.1.1/", "file:///etc/passwd", "ftp://example.com/",
                    "http://user:pw@example.com/", "http://printer.local/", "not a url",
                    "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/"]:
            with self.subTest(url=url):
                with self.assertRaises(UnsafeURLError):
                    validate_url(url)

    def test_is_public_ip(self):
        self.assertTrue(is_public_ip("8.8.8.8"))
        for ip in ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.0.1", "169.254.169.254",
                   "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1", "0.0.0.0", "224.0.0.1"]:
            with self.subTest(ip=ip):
                self.assertFalse(is_public_ip(ip))


class PublicOnlyResolverTests(unittest.IsolatedAsyncioTestCase):
    def make_resolver(self, *hosts):
        inner = types.SimpleNamespace(
            resolve=mock.AsyncMock(return_value=[{"host": h, "port": 80} for h in hosts]),
            close=mock.AsyncMock(),
        )
        return PublicOnlyResolver(inner)

    async def test_drops_private_addresses(self):
        resolver = self.make_resolver("10.0.0.1", "93.184.216.34")
        result = await resolver.resolve("example.com", 80)
        self.assertEqual([r["host"] for r in result], ["93.184.216.34"])

    async def test_raises_when_only_private_addresses(self):
        resolver = self.make_resolver("127.0.0.1", "169.254.169.254")
        with self.assertRaises(UnsafeURLError):
            await resolver.resolve("rebind.example", 80)


class _FakeResponse:
    def __init__(self, url, status=200, headers=None, body=b"", charset="utf-8"):
        from yarl import URL
        self.url = URL(url)
        self.status = status
        self.headers = headers or {}
        self.charset = charset
        self._body = body
        self.content = self

    async def iter_chunked(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.requested = []

    def get(self, url, headers=None, allow_redirects=True):
        assert allow_redirects is False
        self.requested.append(url)
        return self.responses[url]


class FetchTextTests(unittest.IsolatedAsyncioTestCase):
    async def test_redirect_to_private_address_is_refused(self):
        session = _FakeSession({
            "https://example.com/": _FakeResponse(
                "https://example.com/", 302, {"Location": "http://127.0.0.1:8080/api/settings"}),
        })
        with self.assertRaises(UnsafeURLError):
            await fetch_text(session, "https://example.com/")
        self.assertEqual(session.requested, ["https://example.com/"])

    async def test_follows_public_redirect_and_caps_body(self):
        session = _FakeSession({
            "https://example.com/": _FakeResponse("https://example.com/", 301, {"Location": "/next"}),
            "https://example.com/next": _FakeResponse("https://example.com/next", 200, body=b"a" * 5000),
        })
        status, final_url, text = await fetch_text(session, "https://example.com/", max_bytes=1000)
        self.assertEqual((status, final_url), (200, "https://example.com/next"))
        self.assertEqual(len(text), 1000)


# ─── Permissions ────────────────────────────────────────────────────────────

def _perms(**granted):
    return discord.Permissions(**granted)


class _Channel:
    def __init__(self, guild, perms_by_member=None, id_=1):
        self.guild = guild
        self.id = id_
        self._perms = perms_by_member or {}

    def permissions_for(self, member):
        return self._perms.get(member.id, _perms())


class PermissionHelperTests(unittest.TestCase):
    def setUp(self):
        self.guild = object()
        self.member = types.SimpleNamespace(id=1)

    def test_has_channel_permission(self):
        channel = _Channel(self.guild, {1: _perms(manage_channels=True)})
        self.assertTrue(has_channel_permission(self.member, channel, manage_channels=True))
        self.assertFalse(has_channel_permission(self.member, channel, manage_threads=True))
        admin_channel = _Channel(self.guild, {1: _perms(administrator=True)})
        self.assertTrue(has_channel_permission(self.member, admin_channel, manage_threads=True))
        self.assertFalse(has_channel_permission(None, channel, manage_channels=True))

    def test_can_create_events(self):
        self.assertTrue(can_create_events(self.member, _Channel(self.guild, {1: _perms(manage_events=True)})))
        create_only = discord.Permissions(1 << 44)
        self.assertTrue(can_create_events(self.member, _Channel(self.guild, {1: create_only})))
        self.assertFalse(can_create_events(self.member, _Channel(self.guild, {1: _perms(send_messages=True)})))

    def _ctx(self, guild, author_id):
        return types.SimpleNamespace(guild=guild, author=types.SimpleNamespace(id=author_id))

    def test_can_manage_ai_thread(self):
        parent = _Channel(self.guild, {3: _perms(manage_threads=True)}, id_=99)
        bot = types.SimpleNamespace(get_channel=lambda cid: parent if cid == 99 else None)
        thread = {"channel_id": "99", "creator_id": "2"}

        self.assertTrue(can_manage_ai_thread(bot, self._ctx(self.guild, 2), thread))   # creator
        self.assertTrue(can_manage_ai_thread(bot, self._ctx(self.guild, 3), thread))   # Manage Threads
        self.assertFalse(can_manage_ai_thread(bot, self._ctx(self.guild, 4), thread))  # neither
        # A thread from another server can't be touched, even by its creator
        self.assertFalse(can_manage_ai_thread(bot, self._ctx(object(), 2), thread))
        self.assertFalse(can_manage_ai_thread(bot, self._ctx(None, 2), thread))


class ResetPermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_refused_without_manage_channels(self):
        from src.cogs.chat_commands import ChatCommands
        cog = ChatCommands.__new__(ChatCommands)
        cog.state = types.SimpleNamespace(clear_channel_history=mock.Mock(return_value=True))
        guild = object()
        member = types.SimpleNamespace(id=1)
        ctx = types.SimpleNamespace(author=member, channel=_Channel(guild), respond=mock.AsyncMock())

        await ChatCommands.reset_slash.callback(cog, ctx)
        cog.state.clear_channel_history.assert_not_called()
        self.assertIn("Manage Channels", ctx.respond.call_args.args[0])

        ctx.channel = _Channel(guild, {1: _perms(manage_channels=True)})
        await ChatCommands.reset_slash.callback(cog, ctx)
        cog.state.clear_channel_history.assert_called_once()


class ThreadDeletePermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_refused_for_other_server_and_non_creator(self):
        from src.cogs.thread_commands import ThreadCommands
        guild = object()
        parent = _Channel(guild, id_=99)
        cog = ThreadCommands.__new__(ThreadCommands)
        cog.bot = types.SimpleNamespace(get_channel=lambda cid: parent if cid == 99 else None)
        cog.state = types.SimpleNamespace(
            get_discord_thread=mock.Mock(return_value={"channel_id": "99", "creator_id": "2"}),
            delete_discord_thread=mock.Mock(return_value=True),
        )
        stranger = types.SimpleNamespace(guild=object(), author=types.SimpleNamespace(id=2),
                                         respond=mock.AsyncMock())
        await ThreadCommands.delete_thread_slash.callback(cog, stranger, "123")
        cog.state.delete_discord_thread.assert_not_called()

        member = types.SimpleNamespace(guild=guild, author=types.SimpleNamespace(id=5),
                                       respond=mock.AsyncMock())
        await ThreadCommands.delete_thread_slash.callback(cog, member, "123")
        cog.state.delete_discord_thread.assert_not_called()


# ─── Bot loops ──────────────────────────────────────────────────────────────

class BotLoopTests(unittest.IsolatedAsyncioTestCase):
    """Other bots are recorded as context but never answered."""

    async def test_other_bot_mentioning_gideon_gets_no_reply(self):
        # Borrow the mention-cog harness without re-running its tests
        harness = MentionRaceRegressionTests("test_everyone_alone_counts_as_mentioning_gideon")
        other_bot = types.SimpleNamespace(id=ALICE_ID, display_name="OtherBot", name="otherbot", bot=True)
        bot_user = types.SimpleNamespace(id=BOT_ID, display_name="Gideon", name="Gideon", bot=True)
        msg = harness.make_message(1, other_bot, f"<@{BOT_ID}> hi", mentions=[bot_user])
        client = await harness.run_message(msg)
        self.assertFalse(client.received)
        self.assertEqual(harness.sent, [])


# ─── Video download ─────────────────────────────────────────────────────────

class VideoDownloadKeyTests(unittest.TestCase):
    def test_only_openrouter_hosts_get_the_key(self):
        self.assertTrue(OpenRouterVideoClient._is_openrouter_host("https://openrouter.ai/api/v1/videos/x"))
        self.assertTrue(OpenRouterVideoClient._is_openrouter_host("https://cdn.openrouter.ai/v.mp4"))
        for url in ["https://evil.example/v.mp4", "http://openrouter.ai/x",
                    "https://openrouter.ai.evil.example/x", "https://evilopenrouter.ai/x"]:
            with self.subTest(url=url):
                self.assertFalse(OpenRouterVideoClient._is_openrouter_host(url))


if __name__ == "__main__":
    unittest.main()
