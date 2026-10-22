"""Typed results for LLM chat calls.

Provider clients used to report failures as ``"⚠️ API Error (429): {raw body}"``
strings, which every caller had to sniff with ``startswith("⚠️")`` and which
leaked raw provider output to Discord. ``ChatResult`` carries content, tool
calls, usage and a *user-facing* error message separately; the raw provider
detail is kept for logs only.

Clients expose ``chat(...) -> ChatResult``. Their older
``send_message_with_history`` keeps its str/dict return shape (via
``to_legacy()``) so existing callers keep working while they migrate.
"""

import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Error kinds. Retryable ones are retried by the clients before giving up.
RATE_LIMIT = "rate_limit"
AUTH = "auth"
CREDITS = "credits"
MODEL = "model"
REFUSED = "refused"
TIMEOUT = "timeout"
NETWORK = "network"
PROVIDER = "provider"
BAD_RESPONSE = "bad_response"
UNAVAILABLE = "unavailable"
BUDGET = "budget"

RETRYABLE_KINDS = {RATE_LIMIT, TIMEOUT, NETWORK, PROVIDER}

_FRIENDLY = {
    RATE_LIMIT: "The AI provider is rate-limiting requests right now. Please try again in a minute.",
    AUTH: "The AI provider rejected Gideon's API key. An admin needs to check it in the dashboard.",
    CREDITS: "The AI provider account is out of credits. An admin needs to top it up.",
    MODEL: "The configured model `{model}` isn't available. An admin can pick another one.",
    REFUSED: "The AI provider declined to answer that request.",
    TIMEOUT: "The AI provider took too long to answer. Please try again.",
    NETWORK: "I couldn't reach the AI provider. Please try again shortly.",
    PROVIDER: "The AI provider had a temporary problem. Please try again.",
    BAD_RESPONSE: "The AI provider sent a response I couldn't read. Please try again.",
    UNAVAILABLE: "No AI provider is configured for this channel. An admin needs to set one up.",
    BUDGET: "You've reached today's usage limit. It resets at midnight.",
}


def friendly_error(kind: str, model: Optional[str] = None) -> str:
    """User-facing message for an error kind (never includes provider output)."""
    template = _FRIENDLY.get(kind, _FRIENDLY[PROVIDER])
    return template.format(model=model or "unknown")


def kind_for_status(status: int, body: str = "") -> str:
    """Maps an HTTP status (and error text) to an error kind."""
    text = (body or "").lower()
    if status == 429 or "rate limit" in text or "ratelimit" in text:
        return RATE_LIMIT
    if status in (401,):
        return AUTH
    if status == 402 or "insufficient credits" in text or (status == 403 and "credit" in text):
        return CREDITS
    if status == 403:
        return REFUSED
    if status in (400, 404) and ("model" in text or "no endpoints" in text):
        return MODEL
    if status in (408, 504, 524):
        return TIMEOUT
    if status >= 500:
        return PROVIDER
    return PROVIDER if status >= 400 else BAD_RESPONSE


def backoff_delay(attempt: int, retry_after: Optional[float] = None,
                  base: float = 1.0, cap: float = 10.0) -> float:
    """Jittered exponential backoff; honours a server Retry-After up to ``cap``."""
    if retry_after is not None and retry_after >= 0:
        return min(retry_after, cap)
    return min(cap, base * (2 ** attempt)) * (0.5 + random.random() / 2)


@dataclass
class ChatResult:
    """Outcome of one chat completion call."""

    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    error: Optional[str] = None          # user-facing, safe to post in Discord
    error_kind: Optional[str] = None
    detail: Optional[str] = None         # raw provider detail, for logs only
    usage: Dict[str, Any] = field(default_factory=dict)
    model: Optional[str] = None
    provider: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def text(self) -> str:
        return self.content or ""

    @classmethod
    def failure(cls, kind: str, model: Optional[str] = None, detail: Optional[str] = None,
                provider: Optional[str] = None) -> "ChatResult":
        return cls(error=friendly_error(kind, model), error_kind=kind, detail=detail,
                   model=model, provider=provider)

    def to_legacy(self):
        """The str/dict shape ``send_message_with_history`` has always returned."""
        if self.error:
            return f"⚠️ {self.error}"
        if self.tool_calls:
            return {"content": self.content, "tool_calls": self.tool_calls}
        return self.content

    @classmethod
    def from_legacy(cls, value, model: Optional[str] = None) -> "ChatResult":
        """Wraps an old-style return value (str, dict or None) from a client."""
        if isinstance(value, dict):
            return cls(content=value.get("content"), tool_calls=value.get("tool_calls") or None,
                       model=model)
        if value is None:
            return cls(content="", model=model)
        if not isinstance(value, str):
            return cls.failure(BAD_RESPONSE, model, detail=repr(value))
        if value.startswith("⚠️"):
            message = value.lstrip("⚠️").strip()
            return cls(error=message or friendly_error(PROVIDER), error_kind=PROVIDER,
                       detail=value, model=model)
        return cls(content=value, model=model)


async def call_chat(client, *, messages, model=None, system_prompt=None, **kwargs) -> ChatResult:
    """Calls ``client.chat`` when available, else adapts ``send_message_with_history``.

    Lets callers use ChatResult with every client, including test doubles and
    providers that haven't been migrated.
    """
    if client is None:
        return ChatResult.failure(UNAVAILABLE, model)
    chat = getattr(client, "chat", None)
    if chat is not None and callable(chat) and getattr(client, "supports_chat_result", False):
        return await chat(messages=messages, model=model, system_prompt=system_prompt, **kwargs)
    if not hasattr(client, "send_message_with_history"):
        return ChatResult.failure(UNAVAILABLE, model)
    images = kwargs.pop("images", None) or []
    kwargs = {k: v for k, v in kwargs.items() if v is not None}
    legacy = await client.send_message_with_history(
        messages=messages, model=model, system_prompt=system_prompt, images=images, **kwargs)
    return ChatResult.from_legacy(legacy, model)
