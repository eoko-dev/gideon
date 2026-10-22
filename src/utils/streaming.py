"""Showing a reply in Discord while the model is still writing it.

``StreamingReply`` receives the text generated so far (from a streaming
client's ``on_delta``) and keeps one or more Discord messages up to date:

- the first message is posted once there's a little text (or a status line,
  e.g. "🔎 Searching the web…" while a tool runs)
- edits are paced (about one per ``min_interval`` seconds) to stay well inside
  Discord's per-channel rate limits
- when the text outgrows a message, that message is finalized and a new one
  continues it, using the same fence-aware chunking as non-streamed replies

Messages go through ``WebhookSender.send_response``, so persona channels
stream through the webhook (webhook messages support edits too). Edits don't
notify anyone, so '@Name' pings written by the model render as mentions but
only notify when they're in the first message's initial text.
"""

import asyncio
import logging
import time
from typing import Any, Callable, List, Optional

from .discord_fmt import chunk_message

logger = logging.getLogger(__name__)

# Text shown at the end of a message that's still being written
TYPING_CURSOR = " ▌"


class StreamingReply:
    def __init__(self, webhook_sender, channel, channel_id: str,
                 transform: Callable[[str], str] = lambda text: text,
                 reply_to: Any = None, min_interval: float = 1.2, min_first_chars: int = 40,
                 limit: int = 2000, clock: Callable[[], float] = time.monotonic):
        self._sender = webhook_sender
        self._channel = channel
        self._channel_id = channel_id
        self._transform = transform
        self._reply_to = reply_to
        self._min_interval = min_interval
        self._min_first_chars = min_first_chars
        self._limit = limit
        self._clock = clock

        self.messages: List[Any] = []        # Discord messages, in order
        self._shown: List[str] = []          # content currently shown in each message
        self._text = ""                      # latest full text from the model
        self._status: Optional[str] = None
        self._last_edit = 0.0
        self._lock = asyncio.Lock()
        self._failed = False

    @property
    def started(self) -> bool:
        return bool(self.messages)

    @property
    def failed(self) -> bool:
        """True if Discord rejected a send/edit; the caller should send normally."""
        return self._failed

    async def update(self, text: str):
        """Called with the full text so far."""
        self._text = text
        if self._failed:
            return
        if not self.started and len(text.strip()) < self._min_first_chars:
            return
        if self.started and self._clock() - self._last_edit < self._min_interval:
            return
        await self._render(final=False)

    async def status(self, line: str):
        """Shows a status line (e.g. a tool running) until text arrives."""
        self._status = line
        # A new model call starts after the tool runs; its text replaces this
        self._text = ""
        if not self._failed:
            await self._render(final=False, force=True)

    async def finish(self, text: str) -> bool:
        """Shows the final text. Returns False if streaming failed and nothing usable is shown."""
        self._text = text
        self._status = None
        if self._failed:
            return False
        await self._render(final=True, force=True)
        return not self._failed

    async def fail(self, error_line: str):
        """Ends the stream with an error note after whatever was shown."""
        self._status = None
        partial = self._text.strip()
        await self.finish(f"{partial}\n\n{error_line}" if partial else error_line)

    async def _render(self, final: bool, force: bool = False):
        async with self._lock:
            if not force and self.started and self._clock() - self._last_edit < self._min_interval:
                return
            text = self._text.strip()
            if not text:
                if final:
                    # Tools did all the talking: drop the status message
                    await self._delete_status()
                    return
                if not self._status:
                    return
                chunks = [self._status]
            else:
                chunks = chunk_message(self._transform(text), self._limit - len(TYPING_CURSOR))
                if not final:
                    chunks[-1] = chunks[-1] + TYPING_CURSOR
            try:
                for index, content in enumerate(chunks):
                    if index < len(self.messages):
                        if self._shown[index] != content:
                            await self.messages[index].edit(content=content)
                            self._shown[index] = content
                    else:
                        kwargs = {"reference": self._reply_to} if index == 0 and self._reply_to is not None else {}
                        message = await self._sender.send_response(
                            self._channel, content, self._channel_id, **kwargs)
                        if message is None:
                            raise RuntimeError("send_response returned no message")
                        self.messages.append(message)
                        self._shown.append(content)
                # The final text can need fewer messages than a status line did
                while final and len(self.messages) > len(chunks):
                    await self.messages.pop().delete()
                    self._shown.pop()
                self._last_edit = self._clock()
            except Exception as e:
                logger.warning(f"[Streaming] Falling back to a normal reply: {e}")
                self._failed = True

    async def discard(self):
        """Deletes everything streamed so far (before falling back to a normal send)."""
        await self._delete_status()

    async def _delete_status(self):
        """Removes a status-only message once tools have finished with no text."""
        for message in self.messages:
            try:
                await message.delete()
            except Exception:
                pass
        self.messages.clear()
        self._shown.clear()
