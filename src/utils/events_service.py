"""Events Gideon schedules: the RSVP card and "starting soon" reminders.

An event gets a card (embed + Going / Maybe / Can't buttons) in the channel
it was planned in, alongside the native Discord scheduled event when the
author is allowed to create one. Everyone who taps Going gets a reminder 30
minutes before it starts; changing the answer removes it. Buttons use stable
custom_ids (gideon-rsvp:<status>:<event_id>), so they keep working after a
restart.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import discord

from .timeutil import now_local_naive, to_epoch

logger = logging.getLogger(__name__)

RSVP_PREFIX = "gideon-rsvp:"
REMIND_BEFORE = timedelta(minutes=30)

_LABELS = {"going": ("Going", "✅", discord.ButtonStyle.success),
           "maybe": ("Maybe", "🤔", discord.ButtonStyle.secondary),
           "no": ("Can't make it", "❌", discord.ButtonStyle.secondary)}


def _as_datetime(value) -> datetime:
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def rsvp_view(event_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for status, (label, emoji, style) in _LABELS.items():
        view.add_item(discord.ui.Button(label=label, emoji=emoji, style=style,
                                        custom_id=f"{RSVP_PREFIX}{status}:{event_id}"))
    return view


def build_card(event: Dict[str, Any], counts: Dict[str, list]) -> discord.Embed:
    start = _as_datetime(event["start_time"])
    stamp = to_epoch(start)
    embed = discord.Embed(title=f"📅 {event['name']}", color=discord.Color.blurple(),
                          description=event.get("description") or None)
    embed.add_field(name="When", value=f"<t:{stamp}:F> (<t:{stamp}:R>)", inline=False)
    embed.add_field(name="Duration", value=f"{event.get('duration_minutes') or 60} minutes", inline=True)
    for status, (label, emoji, _) in _LABELS.items():
        people = counts.get(status) or []
        shown = ", ".join(f"<@{uid}>" for uid in people[:15]) + (" …" if len(people) > 15 else "")
        embed.add_field(name=f"{emoji} {label} ({len(people)})", value=shown or "—", inline=False)
    footer = "Tap a button to RSVP. If you're going, I'll remind you 30 minutes before."
    if event.get("discord_event_id"):
        footer += " Also listed in the server's Events."
    embed.set_footer(text=footer)
    return embed


async def post_event_card(state, channel, event_id: int):
    event = state.db_manager.events.get_event(event_id)
    counts = state.db_manager.events.rsvp_counts(event_id)
    message = await channel.send(embed=build_card(event, counts), view=rsvp_view(event_id))
    state.db_manager.events.set_card_message(event_id, str(message.id))
    return message


def set_rsvp(state, event_id: int, user_id: str, status: str) -> Optional[int]:
    """Records an RSVP and keeps the user's "starting soon" reminder in step.

    Returns the reminder id when one was created.
    """
    events = state.db_manager.events
    event = events.get_event(event_id)
    if event is None:
        raise ValueError("That event no longer exists.")
    previous = events.get_rsvp(event_id, user_id)
    if previous and previous.get("reminder_id"):
        state.delete_reminder(previous["reminder_id"], user_id)

    reminder_id = None
    if status == "going":
        start = _as_datetime(event["start_time"])
        due = start - REMIND_BEFORE
        if due <= now_local_naive():
            due = None if start <= now_local_naive() else now_local_naive() + timedelta(minutes=1)
        if due is not None:
            reminder_id = state.add_reminder(
                user_id=user_id, channel_id=event["channel_id"],
                message=f"📅 **{event['name']}** starts <t:{to_epoch(start)}:R>", due_timestamp=due)
    events.set_rsvp(event_id, user_id, status, reminder_id)
    return reminder_id


async def handle_rsvp_interaction(bot, interaction: discord.Interaction) -> bool:
    """Handles an RSVP button press. Returns False if it wasn't one."""
    custom_id = (interaction.data or {}).get("custom_id", "")
    if not custom_id.startswith(RSVP_PREFIX):
        return False
    try:
        status, event_id = custom_id[len(RSVP_PREFIX):].split(":")
        event_id = int(event_id)
    except ValueError:
        return True
    state = bot.state_manager
    try:
        set_rsvp(state, event_id, str(interaction.user.id), status)
    except ValueError as e:
        await interaction.response.send_message(str(e), ephemeral=True)
        return True
    event = state.db_manager.events.get_event(event_id)
    counts = state.db_manager.events.rsvp_counts(event_id)
    await interaction.response.edit_message(embed=build_card(event, counts), view=rsvp_view(event_id))
    if status == "going":
        await interaction.followup.send("✅ You're in. I'll remind you 30 minutes before it starts.", ephemeral=True)
    return True
