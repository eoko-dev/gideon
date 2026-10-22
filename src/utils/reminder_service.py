"""Delivery of due reminders (runs from the bot's minutely background task).

Each reminder is phrased in the channel's persona voice on the utility model
(with a plain fallback), sent with Snooze/Done buttons — in the channel, or by
DM when asked — and recorded in the conversation history so Gideon knows it
reminded someone. Repeating reminders are moved to their next occurrence,
computed in the owner's timezone so the wall-clock time survives DST.

When several reminders fall due in one channel at once they go out as a
single message instead of a burst.
"""
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List

import discord

from . import time_parse
from .llm_result import call_chat
from .memory_service import llm_clients
from .restraint import get_budget
from .timeutil import local_tz, now_local_naive, to_epoch
from .usage import usage_scope

logger = logging.getLogger(__name__)

_DELIVERY_SYSTEM_PROMPT = """You are Gideon, a helpful AI assistant delivering a reminder someone asked for.
Be friendly and brief (one or two sentences) and state the reminder clearly."""

# More than this many reminders due in one channel in the same minute are combined
BATCH_THRESHOLD = 2


def _as_datetime(value) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    return value


async def deliver_due_reminders(bot) -> None:
    """Finds due reminders and delivers them.

    Reminders are stored as naive datetimes in the server-local timezone,
    so the comparison uses the same convention.
    """
    from ..cogs.reminder_commands import reminder_buttons

    state = bot.state_manager
    due_reminders = state.get_due_reminders(now_local_naive())
    if not due_reminders:
        return
    logger.info(f"Found {len(due_reminders)} due reminder(s)")

    by_channel: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for reminder in due_reminders:
        if reminder.get("deliver_dm"):
            await _deliver_one(bot, state, reminder, reminder_buttons)
        else:
            by_channel[str(reminder["channel_id"])].append(reminder)

    budget = get_budget(state)
    for channel_id, reminders in by_channel.items():
        if len(reminders) > BATCH_THRESHOLD:
            budget.allow(channel_id)
            await _deliver_batch(bot, state, channel_id, reminders)
            continue
        # Over the channel's hourly budget for unprompted posts: one combined message
        individual = [r for r in reminders if budget.allow(channel_id)]
        held = reminders[len(individual):]
        for reminder in individual:
            await _deliver_one(bot, state, reminder, reminder_buttons)
        if held:
            await _deliver_batch(bot, state, channel_id, held)


async def _resolve_channel(bot, channel_id: str):
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(channel_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
    return channel


async def _deliver_one(bot, state, reminder: Dict[str, Any], reminder_buttons) -> None:
    reminder_id = reminder["reminder_id"]
    user_id = str(reminder["user_id"])
    channel_id = str(reminder["channel_id"])
    try:
        channel = await _resolve_channel(bot, channel_id)
        stamp = to_epoch(_as_datetime(reminder["due_timestamp"]))
        scope_id = str(channel.parent_id) if isinstance(channel, discord.Thread) else channel_id
        text = await _compose(bot, state, scope_id, user_id, reminder["message"], stamp)
        view = reminder_buttons(reminder_id)

        sent = None
        if reminder.get("deliver_dm"):
            sent = await _send_dm(bot, user_id, text, view)
        if sent is None:
            if channel is None:
                logger.warning(f"Channel {channel_id} not found for reminder {reminder_id}; dropping it")
                state.mark_reminder_sent(reminder_id)
                return
            sent = await channel.send(content=f"<@{user_id}> {text}", view=view)
            await _record(state, channel, channel_id, f"⏰ Reminded <@{user_id}>: {reminder['message']}")

        _advance(state, reminder)
        logger.info(f"Sent reminder {reminder_id} to user {user_id}")
    except discord.Forbidden:
        logger.warning(f"No permission to send reminder {reminder_id} in channel {channel_id}. Marking as sent.")
        state.mark_reminder_sent(reminder_id)
    except discord.HTTPException as e:
        logger.error(f"Discord API error sending reminder {reminder_id}: {e}")  # retried next cycle
    except Exception as e:
        logger.error(f"Error sending reminder {reminder_id}: {e}", exc_info=True)  # retried next cycle


async def _deliver_batch(bot, state, channel_id: str, reminders: List[Dict[str, Any]]) -> None:
    """One message for several reminders due in the same channel."""
    channel = await _resolve_channel(bot, channel_id)
    if channel is None:
        for reminder in reminders:
            state.mark_reminder_sent(reminder["reminder_id"])
        return
    lines = ["⏰ **Reminders:**"]
    for reminder in reminders:
        lines.append(f"• <@{reminder['user_id']}> {reminder['message']} (#{reminder['reminder_id']})")
    try:
        await channel.send("\n".join(lines)[:2000])
        await _record(state, channel, channel_id, "\n".join(lines))
        for reminder in reminders:
            _advance(state, reminder)
    except discord.Forbidden:
        for reminder in reminders:
            state.mark_reminder_sent(reminder["reminder_id"])
    except discord.HTTPException as e:
        logger.error(f"Discord API error sending batched reminders in {channel_id}: {e}")


async def _send_dm(bot, user_id: str, text: str, view):
    try:
        user = bot.get_user(int(user_id)) or await bot.fetch_user(int(user_id))
        return await user.send(content=f"⏰ {text}", view=view)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound) as e:
        logger.info(f"Couldn't DM reminder to {user_id} ({e}); posting in the channel instead")
        return None


def _advance(state, reminder: Dict[str, Any]) -> None:
    """Moves a repeating reminder to its next time, or marks a one-off as sent."""
    rule = reminder.get("recurrence")
    if not rule:
        state.mark_reminder_sent(reminder["reminder_id"])
        return
    tz = time_parse.resolve_timezone(reminder.get("timezone")) or local_tz()
    previous = time_parse.from_server_naive(_as_datetime(reminder["due_timestamp"]), tz)
    upcoming = time_parse.next_occurrence(rule, previous, tz)
    if upcoming is None:
        state.mark_reminder_sent(reminder["reminder_id"])
        return
    state.reschedule_reminder(reminder["reminder_id"], time_parse.to_server_naive(upcoming))


async def _record(state, channel, channel_id: str, content: str) -> None:
    try:
        if isinstance(channel, discord.Thread):
            await state.add_discord_thread_message(str(channel.id), {"role": "assistant", "content": content})
        else:
            await state.add_to_channel_history(channel_id, {"role": "assistant", "content": content,
                                                            "timestamp": datetime.now()})
    except Exception as e:
        logger.error(f"Could not record reminder delivery: {e}")


async def _compose(bot, state, scope_id: str, user_id: str, message: str, stamp: int) -> str:
    """The reminder text, in the channel persona's voice when the LLM is available."""
    fallback = f"⏰ **Reminder:** {message}"
    try:
        provider, model_name = state.resolve_utility_model(scope_id)
        client = llm_clients(bot).get(provider)
        if client is None:
            return fallback
        persona = state.get_effective_persona(scope_id)
        system = _DELIVERY_SYSTEM_PROMPT
        if persona and persona.get("system_prompt"):
            system = (f"{persona['system_prompt']}\n\nYou are delivering a reminder someone asked for. "
                      f"Stay in character, be brief (one or two sentences) and state the reminder clearly.")
        prompt = (f'The reminder is: "{message}" (set for <t:{stamp}:F>). Write the reminder message. '
                  f"Don't mention the user by name or ping anyone; that's added for you.")
        with usage_scope("reminder", user_id=user_id, channel_id=scope_id):
            result = await call_chat(client, messages=[{"role": "user", "content": prompt}],
                                     model=model_name, system_prompt=system)
        text = result.text.strip() if result.ok else ""
        return text[:1800] if text else fallback
    except Exception as e:
        logger.error(f"AI error generating reminder message: {e}")
        return fallback
