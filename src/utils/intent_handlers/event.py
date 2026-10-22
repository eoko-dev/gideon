"""Handler for the schedule_event tool."""

import logging
from datetime import timedelta

import discord

from .. import events_service
from ..permissions import can_create_events
from ..timeutil import to_aware

logger = logging.getLogger('event_handler')


async def handle_event_scheduling(cog, message, channel_id, event_name, date_time, duration=60, description=""):
    """
    Handle event scheduling requests.

    Posts an RSVP card (Going / Maybe / Can't) in the channel and, when the
    author may create server events, also creates the native Discord
    scheduled event. The author is RSVP'd as going. Returns None on success;
    on failure posts nothing and returns the reason for the model to relay.

    Args:
        cog: The MentionCommands cog instance
        message: Discord message object
        channel_id: Channel ID as string (the memory/config scope)
        event_name: Name of the event
        date_time: When the event occurs (natural language, in the author's timezone)
        duration: Event duration in minutes (default: 60)
        description: Event details (optional)
    """
    if not event_name or not event_name.strip():
        return "The event needs a name. Ask the user what it's called."
    if not date_time or not date_time.strip():
        return "It isn't clear when the event is. Ask for a time like 'Friday 8pm'."
    try:
        duration = max(5, min(24 * 60, int(duration or 60)))
    except (TypeError, ValueError):
        duration = 60

    try:
        reminder_cog = cog.bot.get_cog('ReminderCommands')
        if not reminder_cog:
            logger.error("[Event] ReminderCommands cog not found")
            return "Event scheduling isn't available right now."

        start = await reminder_cog.resolve_when(date_time, channel_id, message.author)
        if start is None:
            return (f"'{date_time}' couldn't be understood as a time in the future. "
                    f"Ask for something like 'Friday 8pm' or 'tomorrow at 2pm'.")

        # Gideon creates the server event with its own permissions, so only
        # members who could create one themselves get one; everyone gets the card
        discord_event_id = None
        guild = getattr(message, 'guild', None)
        if guild is not None and can_create_events(message.author, message.channel) \
                and hasattr(guild, 'create_scheduled_event'):
            try:
                aware_start = to_aware(start)
                scheduled = await guild.create_scheduled_event(
                    name=event_name[:100],
                    description=(description or f"Planned in #{getattr(message.channel, 'name', 'Discord')}")[:1000],
                    start_time=aware_start,
                    end_time=aware_start + timedelta(minutes=duration),
                    location=f"#{getattr(message.channel, 'name', 'Discord')}",
                )
                discord_event_id = str(scheduled.id)
            except (discord.HTTPException, TypeError) as e:
                # Missing CREATE_EVENTS on the bot, etc. — the RSVP card still works
                logger.warning(f"[Event] Discord scheduled event not created: {e}")

        state = cog.state
        event_id = state.db_manager.events.add_event(
            guild_id=str(guild.id) if guild is not None else None,
            channel_id=str(message.channel.id), name=event_name[:100], description=description or "",
            start_time=start, duration_minutes=duration, discord_event_id=discord_event_id,
            created_by=str(message.author.id))
        events_service.set_rsvp(state, event_id, str(message.author.id), "going")
        await events_service.post_event_card(state, message.channel, event_id)

        await cog.record_tool_note(message, channel_id,
                                   f"Scheduled event #{event_id}: {event_name} at {start} (RSVP card posted)")
        logger.info(f"[Event] User {message.author.id} scheduled event {event_id}: {event_name} at {start}")
    except Exception as e:
        logger.exception(f"[Event] Error for user {message.author.id}: {e}")
        return "The event could not be scheduled."
