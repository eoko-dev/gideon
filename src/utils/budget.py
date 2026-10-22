"""Per-user daily limits on LLM spend and image/video generations.

Limits live in GLOBAL_CONFIG (0 = unlimited) and reset at local midnight.
Server administrators and the bot owner are exempt. Usage rows come from
``usage.record_usage`` (LLM calls) and ``record_generation`` (jobs).
"""

import logging
from datetime import datetime
from typing import Optional

from . import llm_result
from .timeutil import now_local_naive
from .usage import UsageScope

logger = logging.getLogger(__name__)

IMAGE = "image"
VIDEO = "video"


def start_of_day() -> datetime:
    return now_local_naive().replace(hour=0, minute=0, second=0, microsecond=0)


def is_exempt(member, bot=None) -> bool:
    """Admins (and the application owner) are never limited."""
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and getattr(perms, "administrator", False):
        return True
    owner_ids = getattr(bot, "owner_ids", None) or set()
    owner_id = getattr(bot, "owner_id", None)
    member_id = getattr(member, "id", None)
    return member_id is not None and (member_id == owner_id or member_id in owner_ids)


def chat_budget_error(state, member, bot=None) -> Optional[str]:
    """User-facing message when the member has spent today's LLM budget, else None."""
    budget = state.get_daily_user_cost_budget()
    if budget <= 0 or member is None or is_exempt(member, bot):
        return None
    try:
        spent = state.db_manager.usage.user_cost_since(str(member.id), start_of_day())
    except Exception as e:
        logger.error(f"[Budget] Could not read usage for {member.id}: {e}")
        return None
    if spent >= budget:
        logger.info(f"[Budget] User {member.id} reached the daily budget (${spent:.4f} of ${budget:.2f})")
        return llm_result.friendly_error(llm_result.BUDGET)
    return None


def generation_limit_error(state, member, feature: str, bot=None) -> Optional[str]:
    """User-facing message when the member hit today's image/video limit, else None."""
    limit = state.get_daily_user_image_limit() if feature == IMAGE else state.get_daily_user_video_limit()
    if limit <= 0 or member is None or is_exempt(member, bot):
        return None
    try:
        used = state.db_manager.usage.user_units_since(str(member.id), feature, start_of_day())
    except Exception as e:
        logger.error(f"[Budget] Could not read {feature} usage for {member.id}: {e}")
        return None
    if used >= limit:
        noun = "images" if feature == IMAGE else "videos"
        return f"You've generated your {limit} {noun} for today. The limit resets at midnight."
    return None


def record_generation(state, feature: str, user_id, channel_id, provider: str = None,
                      model: str = None, cost: float = 0.0):
    """Counts one image/video job toward the user's daily limit."""
    try:
        state.db_manager.usage.add(
            created_at=now_local_naive(), feature=feature,
            user_id=str(user_id) if user_id is not None else None,
            channel_id=str(channel_id) if channel_id is not None else None,
            provider=provider, model=model, cost=cost, units=1)
    except Exception as e:
        logger.error(f"[Budget] Could not record {feature} generation: {e}")


def make_usage_sink(state):
    """Returns the usage sink that stores one row per LLM call."""
    def sink(scope: UsageScope, result):
        usage = result.usage or {}
        state.db_manager.usage.add(
            created_at=now_local_naive(), feature=scope.feature,
            user_id=scope.user_id, channel_id=scope.channel_id,
            provider=getattr(result, "provider", None), model=getattr(result, "model", None),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cached_tokens=int(usage.get("cached_tokens") or 0),
            cost=float(usage.get("cost") or 0.0))
    return sink
