"""Handler for event scheduling intent."""

import logging
from datetime import datetime, timedelta
import discord

from ..permissions import can_create_events
from ..timeutil import to_aware, to_epoch

logger = logging.getLogger('event_handler')


async def handle_event_scheduling(cog, message, channel_id, event_name, date_time, duration=60, description=""):
    """
    Handle event scheduling requests.

    Args:
        cog: The MentionCommands cog instance
        message: Discord message object
        channel_id: Channel ID as string
        event_name: Name of the event
        date_time: When the event occurs (natural language)
        duration: Event duration in minutes (default: 60)
        description: Event details (optional)
    """
    # Validate inputs
    if not event_name or not event_name.strip():
        await message.channel.send(
            "❌ I need an event name. "
            "Try something like: '@Gideon schedule movie night Friday 8pm'"
        )
        logger.warning(f"[Event] Missing event name for user {message.author.id}")
        return

    if not date_time or not date_time.strip():
        await message.channel.send(
            "❌ I need to know when the event is. "
            "Try something like: '@Gideon schedule movie night Friday 8pm'"
        )
        logger.warning(f"[Event] Missing date/time for user {message.author.id}")
        return

    try:
        # Get ReminderCommands cog for time parsing
        reminder_cog = cog.bot.get_cog('ReminderCommands')
        if not reminder_cog:
            await message.channel.send("⚠️ Event scheduling not available (reminder system needed).")
            logger.error("[Event] ReminderCommands cog not found")
            return

        # Parse time using existing AI time parser (similar to reminders)
        # Note: parse_time_with_ai returns None if time is in the past, so no need for additional validation
        parsed_time = await reminder_cog.parse_time_with_ai(date_time, channel_id)

        if parsed_time is None:
            await message.channel.send(
                f"❌ I couldn't understand the time '{date_time}', or the time is in the past. "
                f"Please try something like 'Friday 8pm', 'tomorrow at 2pm', or 'January 15 at 3pm'"
            )
            logger.warning(f"[Event] Failed to parse time expression or time in past: {date_time}")
            return

        # Try to create Discord scheduled event (if supported)
        # Note: py-cord API varies by version, so we try multiple approaches
        # Gideon creates the event with its own permissions, so only members who
        # could create one themselves get a server event; others get a reminder
        discord_event_created = False
        author_can_create = can_create_events(message.author, message.channel)
        if not author_can_create:
            logger.info(f"[Event] User {message.author.id} lacks Create Events; using a reminder instead")
        if author_can_create and getattr(message, 'guild', None) and hasattr(message.guild, 'create_scheduled_event'):
            try:
                # Make parsed_time timezone-aware for Discord API
                # (parsed_time is naive but represents server-local time)
                aware_start_time = to_aware(parsed_time)
                aware_end_time = aware_start_time + timedelta(minutes=duration)

                logger.info(f"[Event] Creating Discord event: start={aware_start_time.isoformat()}, end={aware_end_time.isoformat()}")

                # Try creating an external scheduled event
                # py-cord 2.4+ uses slightly different parameter names
                try:
                    # Use a descriptive location (channel mention or generic)
                    event_location = f"#{message.channel.name}" if message.channel else "Discord"

                    # First try: location-based external event (most compatible)
                    scheduled_event = await message.guild.create_scheduled_event(
                        name=event_name,
                        description=description if description else f"Event created via @mention",
                        start_time=aware_start_time,
                        end_time=aware_end_time,
                        location=event_location
                    )
                    discord_event_created = True
                except TypeError as te:
                    # If that fails, log and fall through to reminder
                    logger.warning(f"[Event] create_scheduled_event TypeError: {te}")

                if discord_event_created:
                    # Use timezone-aware timestamp for correct Discord display
                    event_timestamp = int(aware_start_time.timestamp())

                    # Format confirmation
                    confirmation = (
                        f"✅ **Event Scheduled!**\n"
                        f"**Name:** {event_name}\n"
                        f"**When:** <t:{event_timestamp}:F> (<t:{event_timestamp}:R>)\n"
                        f"**Duration:** {duration} minutes"
                    )

                    if description:
                        confirmation += f"\n**Description:** {description}"

                    await message.channel.send(confirmation)

                    # Add to conversation history
                    await cog.state.add_to_channel_history(channel_id, {
                        "role": "assistant",
                        "content": f"Scheduled event: {event_name} at {parsed_time}",
                        "timestamp": datetime.now()
                    })

                    logger.info(f"[Event] User {message.author.id} scheduled Discord event: {event_name} at {parsed_time}")
                    return

            except Exception as e:
                logger.warning(f"[Event] Discord event creation failed, falling back to reminder: {e}")
                # Fall through to reminder-based fallback

        # Fallback: Create a reminder instead of a scheduled event
        user_id = str(message.author.id)
        reminder_message = f"📅 Event: {event_name}"
        if description:
            reminder_message += f" - {description}"

        reminder_id = cog.state.add_reminder(
            user_id=user_id,
            channel_id=channel_id,
            message=reminder_message,
            due_timestamp=parsed_time
        )

        # Make parsed_time timezone-aware for correct timestamp display
        event_timestamp = to_epoch(parsed_time)

        # Format confirmation
        confirmation = (
            f"✅ **Event Reminder Set!**\n"
            f"*(Note: I couldn't create a server event, so I set a reminder instead)*\n"
            f"**Event:** {event_name}\n"
            f"**When:** <t:{event_timestamp}:F> (<t:{event_timestamp}:R>)\n"
            f"**Duration:** {duration} minutes\n"
            f"**Reminder ID:** {reminder_id}"
        )

        if description:
            confirmation += f"\n**Description:** {description}"

        await message.channel.send(confirmation)

        # Add to conversation history
        await cog.state.add_to_channel_history(channel_id, {
            "role": "assistant",
            "content": f"Set event reminder: {event_name} at {parsed_time}",
            "timestamp": datetime.now()
        })

        logger.info(f"[Event] User {message.author.id} set event reminder: {event_name} at {parsed_time}")

    except Exception as e:
        logger.exception(f"[Event] Error for user {message.author.id}: {e}")
        await message.channel.send(f"❌ Event scheduling failed: {str(e)}")
