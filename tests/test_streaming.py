"""Tests for streamed replies and fence-aware chunking."""
import json
import os
import tempfile
import types
import unittest
from unittest import mock

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("DATA_DIRECTORY", tempfile.mkdtemp())

from src.utils.conversation_service import Conversation, ConversationService, ReplyRequest, tool_status_line
from src.utils.discord_fmt import chunk_message
from src.utils.llm_result import ChatResult
from src.utils.openrouter_client import OpenRouterClient
from src.utils.streaming import TYPING_CURSOR, StreamingReply

from tests.test_conversation_service import FakeState, _author, _channel


class FakeMessage:
    def __init__(self, sender, content):
        self.sender = sender
        self.content = content
        self.deleted = False

    async def edit(self, content):
        if self.sender.fail_edits:
            raise RuntimeError("edit rejected")
        self.content = content
        self.sender.edits += 1

    async def delete(self):
        self.deleted = True


class FakeSender:
    def __init__(self):
        self.messages = []
        self.edits = 0
        self.fail_edits = False
        self.kwargs = []

    async def send_response(self, channel, content, channel_id, **kwargs):
        message = FakeMessage(self, content)
        self.messages.append(message)
        self.kwargs.append(kwargs)
        return message


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ChunkFenceTests(unittest.TestCase):
    def test_code_blocks_are_closed_and_reopened(self):
        code = "intro\n```python\n" + "".join(f"print({i})\n" for i in range(400)) + "```\nafter"
        chunks = chunk_message(code)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 2000)
            self.assertEqual(chunk.count("```") % 2, 0, chunk[:40])
        self.assertTrue(chunks[1].startswith("```python\n"))

    def test_short_text_untouched(self):
        self.assertEqual(chunk_message("hi"), ["hi"])
        self.assertEqual(chunk_message(""), [""])


class StreamingReplyTests(unittest.IsolatedAsyncioTestCase):
    def make(self, **kwargs):
        self.sender = FakeSender()
        self.clock = Clock()
        return StreamingReply(self.sender, object(), "C", clock=self.clock, **kwargs)

    async def test_waits_for_enough_text_then_paces_edits(self):
        stream = self.make(min_first_chars=10, min_interval=1.0)
        await stream.update("Hi")
        self.assertFalse(stream.started)
        await stream.update("Hello there, friend")
        self.assertEqual(len(self.sender.messages), 1)
        self.assertTrue(self.sender.messages[0].content.endswith(TYPING_CURSOR))

        await stream.update("Hello there, friend, how")   # too soon: no edit
        self.assertEqual(self.sender.edits, 0)
        self.clock.now = 1.5
        await stream.update("Hello there, friend, how are you")
        self.assertEqual(self.sender.edits, 1)

        self.assertTrue(await stream.finish("Hello there, friend, how are you?"))
        self.assertEqual(self.sender.messages[0].content, "Hello there, friend, how are you?")

    async def test_long_replies_continue_in_new_messages(self):
        stream = self.make(min_first_chars=1, min_interval=0)
        await stream.finish("line\n" * 900)
        self.assertGreater(len(self.sender.messages), 1)
        self.assertTrue(all(len(m.content) <= 2000 for m in self.sender.messages))

    async def test_status_then_text_replaces_it(self):
        stream = self.make(min_interval=0)
        await stream.status("*🔎 Searching the web…*")
        self.assertEqual(self.sender.messages[0].content, "*🔎 Searching the web…*")
        await stream.finish("Here is what I found.")
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(self.sender.messages[0].content, "Here is what I found.")

    async def test_status_with_no_text_is_removed(self):
        stream = self.make(min_interval=0)
        await stream.status("*🎨 Creating an image…*")
        await stream.finish("")
        self.assertTrue(self.sender.messages[0].deleted)
        self.assertFalse(stream.started)

    async def test_reply_reference_only_on_first_message(self):
        stream = self.make(min_first_chars=1, min_interval=0, reply_to="ORIGINAL")
        await stream.finish("x\n" * 1500)
        self.assertEqual(self.sender.kwargs[0], {"reference": "ORIGINAL"})
        self.assertTrue(all(k == {} for k in self.sender.kwargs[1:]))

    async def test_failed_edit_marks_stream_failed(self):
        stream = self.make(min_first_chars=1, min_interval=0)
        await stream.update("first part")
        self.sender.fail_edits = True
        self.assertFalse(await stream.finish("first part and more"))
        self.assertTrue(stream.failed)

    def test_tool_status_lines(self):
        self.assertIn("Searching the web", tool_status_line(["web_search"]))
        self.assertIn("Working on it", tool_status_line(["unknown_tool"]))


class _FakeStreamResponse:
    def __init__(self, lines, status=200):
        self.status = status
        self.headers = {}
        self._lines = [line.encode() + b"\n" for line in lines]
        self.content = self

    def __aiter__(self):
        self._iter = iter(self._lines)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _sse(obj):
    return "data: " + json.dumps(obj)


class OpenRouterStreamParseTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_tool_calls_and_usage(self):
        lines = [
            ": OPENROUTER PROCESSING",
            _sse({"choices": [{"delta": {"content": "Hel"}}]}),
            _sse({"choices": [{"delta": {"content": "lo"}}]}),
            _sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "t1", "function": {"name": "web_", "arguments": '{"q'}}]}}]}),
            _sse({"choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"name": "search", "arguments": '": "x"}'}}]}}]}),
            _sse({"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2, "cost": 0.01}}),
            "data: [DONE]",
        ]
        client = OpenRouterClient("key", "sys", "openrouter/m")
        session = types.SimpleNamespace(post=lambda *a, **k: _FakeStreamResponse(lines))
        client._get_session = lambda: session
        seen = []

        async def on_delta(text):
            seen.append(text)

        result = await client.chat(messages=[{"role": "user", "content": "hi"}], model="m", on_delta=on_delta)
        self.assertTrue(result.ok)
        self.assertEqual(seen, ["Hel", "Hello"])
        self.assertEqual(result.content, "Hello")
        self.assertEqual(result.tool_calls[0]["function"], {"name": "web_search", "arguments": '{"q": "x"}'})
        self.assertEqual(result.usage["cost"], 0.01)

    async def test_mid_stream_error_is_not_retried(self):
        lines = [_sse({"choices": [{"delta": {"content": "par"}}]}),
                 _sse({"error": {"code": 502, "message": "upstream died"}})]
        client = OpenRouterClient("key", "sys", "openrouter/m")
        calls = []

        def post(*a, **k):
            calls.append(1)
            return _FakeStreamResponse(lines)

        client._get_session = lambda: types.SimpleNamespace(post=post)

        async def on_delta(text):
            pass

        result = await client.chat(messages=[{"role": "user", "content": "hi"}], model="m", on_delta=on_delta)
        self.assertFalse(result.ok)
        self.assertEqual(len(calls), 1)


class StreamingClient:
    supports_chat_result = True
    supports_streaming = True

    def __init__(self, text):
        self.text = text
        self.model = "default"

    async def model_supports_vision(self, model):
        return True

    async def chat(self, messages, model=None, system_prompt=None, on_delta=None, **kwargs):
        if on_delta:
            for end in range(10, len(self.text) + 10, 10):
                await on_delta(self.text[:end])
        return ChatResult(content=self.text, model=model)


class StreamingServiceTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self, client, stream=True):
        state = FakeState()
        state.get_stream_replies = lambda: stream
        self.sender = FakeSender()
        bot = types.SimpleNamespace(state_manager=state, openrouter_client=client, openai_client=None,
                                    ai_horde_client=None, webhook_sender=self.sender, get_cog=lambda n: None)
        return ConversationService(bot), state

    async def test_streamed_reply_is_shown_once_and_recorded(self):
        text = "This is a streamed answer that is long enough to show early."
        service, state = self.make_service(StreamingClient(text))
        conversation = Conversation(channel=_channel(1), channel_id="C")
        await service.respond(ReplyRequest(conversation=conversation, author=_author(), content="q",
                                           recorded=False))
        self.assertEqual(len(self.sender.messages), 1)
        self.assertEqual(self.sender.messages[0].content, text)
        self.assertEqual([r["content"] for r in state.channel_rows if r["role"] == "assistant"], [text])

    async def test_streaming_off_sends_once(self):
        text = "Plain answer that is also long enough to stream if it were on."
        service, _ = self.make_service(StreamingClient(text), stream=False)
        conversation = Conversation(channel=_channel(1), channel_id="C")
        await service.respond(ReplyRequest(conversation=conversation, author=_author(), content="q",
                                           recorded=False))
        self.assertEqual([m.content for m in self.sender.messages], [text])
        self.assertEqual(self.sender.edits, 0)


if __name__ == "__main__":
    unittest.main()
