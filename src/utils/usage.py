"""Attributing LLM usage to the feature, user and channel that caused it.

Callers wrap work in ``usage_scope(feature, user_id=..., channel_id=...)``;
provider clients call ``record_usage(result)`` after each completion. The
scope travels in a ContextVar, so it follows the call through awaits and into
tasks created inside it without being threaded through every signature.

The sink that persists rows is registered at startup (``set_usage_sink``) so
this module has no database dependency and is a no-op in tests.
"""

import contextlib
import contextvars
import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageScope:
    feature: str = "other"
    user_id: Optional[str] = None
    channel_id: Optional[str] = None


_current_scope: contextvars.ContextVar[UsageScope] = contextvars.ContextVar(
    "gideon_usage_scope", default=UsageScope())

_sink: Optional[Callable[[UsageScope, object], None]] = None


def set_usage_sink(sink: Optional[Callable[[UsageScope, object], None]]):
    """Registers the callable that stores one usage row (scope, ChatResult)."""
    global _sink
    _sink = sink


def current_scope() -> UsageScope:
    return _current_scope.get()


@contextlib.contextmanager
def usage_scope(feature: str, user_id=None, channel_id=None):
    """Attributes LLM calls made inside the block. Unset fields inherit the outer scope."""
    outer = _current_scope.get()
    token = _current_scope.set(UsageScope(
        feature=feature or outer.feature,
        user_id=str(user_id) if user_id is not None else outer.user_id,
        channel_id=str(channel_id) if channel_id is not None else outer.channel_id,
    ))
    try:
        yield
    finally:
        _current_scope.reset(token)


def record_usage(result) -> None:
    """Hands a completed call's usage to the sink. Never raises."""
    if _sink is None or result is None:
        return
    if not getattr(result, "usage", None):
        return
    try:
        _sink(_current_scope.get(), result)
    except Exception as e:
        logger.error(f"[Usage] Failed to record usage: {e}", exc_info=True)
