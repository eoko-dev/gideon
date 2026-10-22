"""Tests for the shared reply pipeline and typed LLM results."""
import asyncio
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

from src.utils import llm_result, usage
from src.utils.conversation_service import Conversation, ConversationService, MessageLike, ReplyRequest
from src.utils.llm_result import ChatResult, call_chat, kind_for_status
from src.utils.openrouter_client import OpenRouterClient


# ─── ChatResult ─────────────────────────────────────────────────────────────

class ChatResultTests(unittest.TestCase):
    def test_from_legacy(self):
        self.assertEqual(ChatResult.from_legacy("hi").content, "hi")
        self.assertEqual(ChatResult.from_legacy(None).content, "")
        tool = ChatResult.from_legacy({"content": None, "tool_calls": [{"id": "1"}]})
        self.assertEqual(tool.tool_calls, [{"id": "1"}])
        err = ChatResult.from_legacy("⚠️ API Error: boom")
        self.assertFalse(err.ok)
        self.assertEqual(err.error, "API Error: boom")

    def test_to_legacy_round_trip(self):
        self.assertEqual(ChatResult(content="x").to_legacy(), "x")
        self.assertTrue(ChatResult.failure(llm_result.RATE_LIMIT).to_legacy().startswith("⚠️"))
        self.assertEqual(ChatResult(content="c", tool_calls=[{"id": "1"}]).to_legacy(),
                         {"content": "c", "tool_calls": [{"id": "1"}]})

    def test_failure_messages_never_include_provider_detail(self):
        result = ChatResult.failure(llm_result.PROVIDER, "m", detail='{"secret": "raw body"}')
        self.assertNotIn("raw body", result.error)
        self.assertIn("raw body", result.detail)

    def test_kind_for_status(self):
        self.assertEqual(kind_for_status(429), llm_result.RATE_LIMIT)
        self.assertEqual(kind_for_status(401), llm_result.AUTH)
        self.assertEqual(kind_for_status(402), llm_result.CREDITS)
        self.assertEqual(kind_for_status(404, "No endpoints found for model x"), llm_result.MODEL)
        self.assertEqual(kind_for_status(503), llm_result.PROVIDER)
        self.assertEqual(kind_for_status(504), llm_result.TIMEOUT)


class OpenRouterParseTests(unittest.TestCase):
    def test_parses_content_and_usage(self):
        result = OpenRouterClient._parse_chat_response({
            "model": "openai/gpt-4o-mini",
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "cost": 0.0012,
                      "prompt_tokens_details": {"cached_tokens": 4}},
        }, "openai/gpt-4o-mini")
        self.assertTrue(result.ok)
        self.assertEqual(result.content, "hello")
        self.assertEqual(result.usage, {"prompt_tokens": 10, "completion_tokens": 3,
                                        "cached_tokens": 4, "cost": 0.0012})

    def test_null_content_is_not_a_crash(self):
        result = OpenRouterClient._parse_chat_response(
            {"choices": [{"message": {"content": None}}]}, "m")
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "")

    def test_error_body(self):
        result = OpenRouterClient._parse_chat_response(
            {"error": {"code": 429, "message": "Rate limit exceeded"}}, "m")
        self.assertEqual(result.error_kind, llm_result.RATE_LIMIT)

    def test_missing_choices(self):
        self.assertEqual(OpenRouterClient._parse_chat_response({}, "m").error_kind,
                         llm_result.BAD_RESPONSE)


class OpenRouterRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_rate_limits_then_succeeds(self):
        client = OpenRouterClient("key", "sys", "openrouter/m")
        responses = [
            (ChatResult.failure(llm_result.RATE_LIMIT, "m"), 0),
            (ChatResult(content="ok", model="m"), None),
        ]
        client._post_chat = mock.AsyncMock(side_effect=responses)
        with mock.patch("src.utils.openrouter_client.asyncio.sleep", mock.AsyncMock()):
            result = await client.chat(messages=[{"role": "user", "content": "hi"}], model="m")
        self.assertEqual(result.content, "ok")
        self.assertEqual(client._post_chat.await_count, 2)

    async def test_does_not_retry_auth_errors(self):
        client = OpenRouterClient("key", "sys", "openrouter/m")
        client._post_chat = mock.AsyncMock(return_value=(ChatResult.failure(llm_result.AUTH, "m"), None))
        result = await client.chat(messages=[{"role": "user", "content": "hi"}], model="m")
        self.assertEqual(result.error_kind, llm_result.AUTH)
        self.assertEqual(client._post_chat.await_count, 1)

    async def test_gives_up_after_max_attempts(self):
        client = OpenRouterClient("key", "sys", "openrouter/m")
        client._post_chat = mock.AsyncMock(return_value=(ChatResult.failure(llm_result.PROVIDER, "m"), None))
        with mock.patch("src.utils.openrouter_client.asyncio.sleep", mock.AsyncMock()):
            result = await client.chat(messages=[{"role": "user", "content": "hi"}], model="m")
        self.assertFalse(result.ok)
        self.assertEqual(client._post_chat.await_count, 3)

    async def test_model_is_never_mutated(self):
        client = OpenRouterClient("key", "sys", "openrouter/default")
        client._post_chat = mock.AsyncMock(return_value=(ChatResult(content="ok"), None))
        await client.chat(messages=[{"role": "user", "content": "hi"}], model="other")
        self.assertEqual(client.model, "openrouter/default")
        payload = client._post_chat.call_args.args[0]
        self.assertEqual(payload["model"], "other")


# ─── ConversationService ────────────────────────────────────────────────────

class FakeState:
    def __init__(self, models=None, thread_models=None, tools=False):
        self.models = models or {}
        self.thread_models = thread_models or {}
        self.tools = tools
        self.channel_rows = []
        self.thread_rows = []
        self._last = datetime(2026, 1, 1)

    def _ts(self):
        self._last += timedelta(seconds=1)
        return self._last

    def resolve_model(self, channel_id):
        return "openrouter", self.models.get(channel_id, "default-model")

    def get_effective_persona(self, channel_id):
        return None

    def get_discord_thread_config(self, thread_id):
        model = self.thread_models.get(thread_id)
        return {"model": model} if model else {}

    def get_time_window_hours(self):
        return 48

    def get_channel_history(self, channel_id, until=None):
        return [dict(r) for r in self.channel_rows
                if r["channel_id"] == channel_id and (until is None or r["timestamp"] <= until)]

    def get_discord_thread_history(self, thread_id, hours_limit=None, until=None):
        return [dict(r) for r in self.thread_rows
                if r["thread_id"] == thread_id and (until is None or r["timestamp"] <= until)]

    async def add_to_channel_history(self, channel_id, entry):
        ts = self._ts()
        self.channel_rows.append({**entry, "channel_id": channel_id, "timestamp": ts})
        return ts

    async def add_discord_thread_message(self, thread_id, entry):
        ts = self._ts()
        self.thread_rows.append({**entry, "thread_id": thread_id, "timestamp": ts})
        return ts

    def get_effective_system_prompt(self, channel_id, query=None, speaker_id=None, history=None,
                                    base_prompt=None, location_id=None):
        return base_prompt or "sys"

    def get_tool_calling_enabled(self):
        return self.tools

    def get_tool_calling_max_iterations(self):
        return 3


class RecordingClient:
    """New-style client: chat() returns ChatResult. Can block to force overlap."""

    supports_chat_result = True

    def __init__(self, replies=None, block=False):
        self.replies = list(replies or [])
        self.calls = []
        self.model = "client-default"
        self.block = block
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def model_supports_vision(self, model_name):
        return True

    async def chat(self, messages, model=None, system_prompt=None, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], "model": model,
                           "system_prompt": system_prompt, **kwargs})
        if self.block:
            self.started.set()
            await self.release.wait()
        if self.replies:
            reply = self.replies.pop(0)
            return reply if isinstance(reply, ChatResult) else ChatResult(content=reply, model=model)
        return ChatResult(content=f"reply from {model}", model=model)


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _channel(id_):
    return types.SimpleNamespace(id=id_, typing=lambda: _Typing(), send=mock.AsyncMock())


def _author(id_=1, name="Alice"):
    return types.SimpleNamespace(id=id_, display_name=name, name=name.lower(), bot=False)


class ConversationServiceTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, state, client):
        self.sent = []

        async def send_response(channel, text, channel_id):
            self.sent.append((channel.id, text))

        bot = types.SimpleNamespace(
            state_manager=state,
            openrouter_client=client, openai_client=None, ai_horde_client=None,
            webhook_sender=types.SimpleNamespace(send_response=send_response),
            get_cog=lambda name: None,
        )
        return ConversationService(bot)

    async def test_thread_model_override_is_passed_explicitly(self):
        state = FakeState(models={"P": "channel-model"}, thread_models={"T": "openrouter/thread-model"})
        client = RecordingClient()
        service = self.make_service(state, client)
        conversation = Conversation(channel=_channel(9), channel_id="P", thread_id="T")
        ts = await service.record_user_turn(conversation, _author(), "hello")

        await service.respond(ReplyRequest(conversation=conversation, author=_author(),
                                           content="hello", history_until=ts))
        self.assertEqual(client.calls[0]["model"], "thread-model")
        self.assertEqual(client.model, "client-default")
        # Thread history, not the parent channel's, holds both turns
        self.assertEqual([r["role"] for r in state.thread_rows], ["user", "assistant"])
        self.assertEqual(state.channel_rows, [])
        self.assertEqual(self.sent, [(9, "reply from thread-model")])

    async def test_concurrent_conversations_keep_their_own_models(self):
        state = FakeState(models={"A": "model-a", "B": "model-b"})
        client = RecordingClient(block=True)
        service = self.make_service(state, client)
        conv_a = Conversation(channel=_channel(1), channel_id="A")
        conv_b = Conversation(channel=_channel(2), channel_id="B")

        task_a = asyncio.create_task(service.respond(
            ReplyRequest(conversation=conv_a, author=_author(), content="a", recorded=False)))
        await asyncio.wait_for(client.started.wait(), 2)
        task_b = asyncio.create_task(service.respond(
            ReplyRequest(conversation=conv_b, author=_author(), content="b", recorded=False)))
        await asyncio.sleep(0)
        client.release.set()
        await asyncio.wait_for(asyncio.gather(task_a, task_b), 2)

        self.assertEqual(sorted(c["model"] for c in client.calls), ["model-a", "model-b"])
        self.assertIn((1, "reply from model-a"), self.sent)
        self.assertIn((2, "reply from model-b"), self.sent)

    async def test_errors_are_friendly_and_not_recorded(self):
        state = FakeState()
        client = RecordingClient(replies=[ChatResult.failure(llm_result.RATE_LIMIT, "m",
                                                             detail="429: raw provider body")])
        service = self.make_service(state, client)
        channel = _channel(1)
        conversation = Conversation(channel=channel, channel_id="C")

        result = await service.respond(ReplyRequest(conversation=conversation, author=_author(),
                                                    content="hi", recorded=False))
        self.assertFalse(result.ok)
        message = channel.send.call_args.args[0]
        self.assertTrue(message.startswith("⚠️"))
        self.assertNotIn("raw provider body", message)
        self.assertEqual([r for r in state.channel_rows if r["role"] == "assistant"], [])
        self.assertEqual(self.sent, [])

    async def test_history_stops_at_anchor(self):
        state = FakeState()
        client = RecordingClient()
        service = self.make_service(state, client)
        conversation = Conversation(channel=_channel(1), channel_id="C")
        anchor = await service.record(conversation, {"role": "user", "name": "Alice", "content": "first"})
        await service.record(conversation, {"role": "user", "name": "Bob", "content": "later"})

        await service.respond(ReplyRequest(conversation=conversation, author=_author(),
                                           content="first", history_until=anchor))
        self.assertEqual([m["content"] for m in client.calls[0]["messages"]], ["first"])

    async def test_tool_loop_runs_tools_and_returns_text(self):
        state = FakeState(tools=True)
        tool_call = {"id": "t1", "function": {"name": "roll_dice", "arguments": '{"notation": "1d1"}'}}
        client = RecordingClient(replies=[ChatResult(tool_calls=[tool_call]), "You rolled a 1."])
        service = self.make_service(state, client)
        conversation = Conversation(channel=_channel(1), channel_id="C")
        message = types.SimpleNamespace(author=_author(), channel=conversation.channel, mentions=[])

        result = await service.respond(ReplyRequest(conversation=conversation, author=_author(),
                                                    content="roll", message=message, recorded=False))
        self.assertEqual(result.text, "You rolled a 1.")
        self.assertIn("tools", client.calls[0])
        second_call = client.calls[1]["messages"]
        self.assertEqual(second_call[-1]["role"], "tool")
        self.assertEqual(self.sent, [(1, "You rolled a 1.")])

    async def test_legacy_clients_still_work(self):
        class LegacyClient:
            async def send_message_with_history(self, messages, model, system_prompt, images):
                return "legacy reply"

        state = FakeState()
        service = self.make_service(state, LegacyClient())
        conversation = Conversation(channel=_channel(1), channel_id="C")
        result = await service.respond(ReplyRequest(conversation=conversation, author=_author(),
                                                    content="hi", recorded=False))
        self.assertEqual(result.text, "legacy reply")

    async def test_missing_client_reports_unavailable(self):
        state = FakeState()
        service = self.make_service(state, None)
        channel = _channel(1)
        result = await service.respond(ReplyRequest(conversation=Conversation(channel=channel, channel_id="C"),
                                                    author=_author(), content="hi"))
        self.assertEqual(result.error_kind, llm_result.UNAVAILABLE)
        channel.send.assert_awaited()

    def test_message_like_from_interaction(self):
        ctx = types.SimpleNamespace(author=_author(), channel=_channel(1), guild="g",
                                    interaction=types.SimpleNamespace(id=42))
        message = MessageLike(ctx, "hi")
        self.assertEqual((message.id, message.content, message.guild), (42, "hi", "g"))
        self.assertEqual(message.mentions, [])


class UsageScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_scope_reaches_the_sink(self):
        rows = []
        usage.set_usage_sink(lambda scope, result: rows.append((scope, result.usage)))
        self.addCleanup(usage.set_usage_sink, None)
        with usage.usage_scope("chat", user_id=1, channel_id=2):
            with usage.usage_scope("tool"):
                usage.record_usage(ChatResult(content="x", usage={"prompt_tokens": 5}))
        scope, recorded = rows[0]
        self.assertEqual((scope.feature, scope.user_id, scope.channel_id), ("tool", "1", "2"))
        self.assertEqual(recorded, {"prompt_tokens": 5})
        self.assertEqual(usage.current_scope().feature, "other")


if __name__ == "__main__":
    unittest.main()
