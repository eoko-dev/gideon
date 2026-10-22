"""Commands for setting and managing reminders."""
import discord
import logging
from discord.ext import commands
from datetime import datetime, timedelta
from typing import Optional
import json
import os
from ..utils.memory_service import llm_clients
from ..utils.usage import usage_scope
from ..utils.timeutil import local_tz, now_local, to_epoch

logger = logging.getLogger('reminder_commands')

class ReminderCommands(commands.Cog):
    """Commands for reminder functionality."""

    def __init__(self, bot, openrouter_client, openai_client, ai_horde_client):
        self.bot = bot
        self.state = bot.state_manager
        self.clients = {
            "openrouter": openrouter_client,
            "openai": openai_client,
            "ai_horde": ai_horde_client
        }

    async def resolve_when(self, when: str, channel_id: str, user=None) -> Optional[datetime]:
        """Parses a natural-language time into a future server-local datetime (or None)."""
        return await self.parse_time_with_ai(when, channel_id)

    async def parse_time_with_ai(self, time_str: str, channel_id: str) -> Optional[datetime]:
        """
        Uses AI to parse natural language time into a datetime object.

        Args:
            time_str: Natural language time string (e.g., "in 2 hours", "tomorrow at 3pm")
            channel_id: Channel ID for getting the effective model

        Returns:
            datetime object or None if parsing fails
        """
        # Get current datetime info to provide context.
        # Reminders are stored as naive datetimes in the server-local timezone;
        # Discord <t:...> tags display them in each user's own timezone.
        now = now_local()
        # Convert to naive datetime for consistency with database storage
        now_naive = now.replace(tzinfo=None)
        current_time_str = now_naive.strftime("%Y-%m-%d %H:%M:%S")
        current_day = now_naive.strftime("%A")

        tz_name = now.tzname()
        tz_offset = now.strftime('%z')

        logger.info(f"Time parsing context: now={now_naive}, current_time_str={current_time_str}, tz_name={tz_name}, tz_offset={tz_offset}")

        # System prompt for time parsing - simplified to just parse the time, not calculate dates
        system_prompt = f"""You are a time parsing assistant. Convert natural language time expressions into structured time data.

Current server time: {current_time_str} ({current_day}) - Timezone: {tz_name} (UTC{tz_offset})

Parse the user's time expression and return ONLY a JSON object with this format:
{{"type": "relative|absolute|explicit_day", "hours": H, "minutes": M, "days_offset": D}}

Types:
- "relative": Phrases like "in 2 hours", "in 30 minutes"
  * Set hours/minutes to the offset amount
  * Set days_offset to 0
- "absolute": Just a time like "3:30pm", "4pm", "at 5:15pm"
  * Set hours to 24-hour format (0-23)
  * Set minutes (0-59)
  * Set days_offset to 0 (we'll calculate if it's today/tomorrow)
- "explicit_day": Phrases with day like "tomorrow at 3pm", "next Monday"
  * Set hours to 24-hour format
  * Set minutes
  * Set days_offset (1 for tomorrow, 7 for next week, etc)

Examples:
- "in 2 hours" -> {{"type": "relative", "hours": 2, "minutes": 0, "days_offset": 0}}
- "4:30pm" -> {{"type": "absolute", "hours": 16, "minutes": 30, "days_offset": 0}}
- "tomorrow at 3pm" -> {{"type": "explicit_day", "hours": 15, "minutes": 0, "days_offset": 1}}

Return ONLY the JSON, no other text."""

        logger.info(f"System prompt length: {len(system_prompt)}")

        # Time parsing is background work: the utility model, else the channel's
        provider, model_name = self.state.resolve_utility_model(channel_id)
        client_to_use = llm_clients(self.bot).get(provider)

        if not client_to_use:
            logger.error(f"Client for provider '{provider}' not available for time parsing")
            return None

        try:
            # Prepare message for AI
            messages = [
                {"role": "user", "content": f"Parse this time: {time_str}"}
            ]

            # Call AI with response_format for structured output (if supported)
            response_format = {"type": "json_object"}

            with usage_scope("reminder_parse"):
                response = await client_to_use.send_message_with_history(
                    messages=messages,
                    model=model_name,
                    system_prompt=system_prompt,
                    response_format=response_format
                )

            # Parse the JSON response
            logger.info(f"AI time parsing response for '{time_str}': {response}")

            # Clean response (remove markdown code blocks if present)
            cleaned_response = response.strip()
            if cleaned_response.startswith("```json"):
                cleaned_response = cleaned_response[7:]
            if cleaned_response.startswith("```"):
                cleaned_response = cleaned_response[3:]
            if cleaned_response.endswith("```"):
                cleaned_response = cleaned_response[:-3]
            cleaned_response = cleaned_response.strip()

            result = json.loads(cleaned_response)
            parse_type = result.get("type")
            hours = result.get("hours")
            minutes = result.get("minutes")
            days_offset = result.get("days_offset", 0)

            if parse_type is None or hours is None or minutes is None:
                logger.error(f"AI response missing required fields: {result}")
                return None

            logger.info(f"AI parsed '{time_str}' as type={parse_type}, hours={hours}, minutes={minutes}, days_offset={days_offset}")

            # Calculate the actual datetime based on type
            if parse_type == "relative":
                # Add the relative offset to current time
                parsed_time = now_naive + timedelta(hours=hours, minutes=minutes)
            elif parse_type == "absolute":
                # User gave just a time (e.g., "4pm")
                # Create a datetime for today at that time
                target_time = now_naive.replace(hour=hours, minute=minutes, second=0, microsecond=0)
                # If that time has already passed today, use tomorrow
                if target_time <= now_naive:
                    target_time += timedelta(days=1)
                    logger.info(f"Time {hours}:{minutes:02d} has passed today, using tomorrow")
                parsed_time = target_time
            elif parse_type == "explicit_day":
                # User gave a specific day (e.g., "tomorrow at 3pm")
                parsed_time = (now_naive + timedelta(days=days_offset)).replace(hour=hours, minute=minutes, second=0, microsecond=0)
            else:
                logger.error(f"Unknown parse type: {parse_type}")
                return None

            logger.info(f"Calculated datetime for '{time_str}' as {parsed_time}. Current time: {now_naive}. In future: {parsed_time > now_naive}")

            # Validate it's in the future
            if parsed_time <= now_naive:
                logger.warning(f"Parsed time {parsed_time} is in the past (current: {now_naive})")
                return None

            return parsed_time

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse AI JSON response: {e}. Response: {response}")
            return None
        except Exception as e:
            logger.error(f"Error in AI time parsing: {e}", exc_info=True)
            return None

    @discord.slash_command(
        name="remind",
        description="Set a reminder"
    )
    async def remind_slash(
        self,
        ctx,
        message: discord.Option(str, "What should I remind you about?", required=True),
        when: discord.Option(str, "When? (e.g., 'in 2 hours', 'tomorrow at 3pm')", required=True)
    ):
        """Set a reminder using natural language time."""
        await ctx.defer()

        channel_id = str(ctx.channel.id)
        user_id = str(ctx.author.id)

        # Parse the time using AI
        logger.info(f"[Reminder] User {user_id} setting reminder in channel {channel_id}: '{message}' at '{when}'")

        parsed_time = await self.parse_time_with_ai(when, channel_id)

        if parsed_time is None:
            await ctx.followup.send(
                f"❌ I couldn't understand the time '{when}'. "
                f"Please try something like 'in 2 hours', 'tomorrow at 3pm', or 'next Monday at noon'."
            )
            return

        # Check if time is too far in the future (optional: max 1 year)
        max_future = datetime.now() + timedelta(days=365)
        if parsed_time > max_future:
            await ctx.followup.send(
                "❌ Reminder time is too far in the future (max 1 year)."
            )
            return

        # Save reminder to database
        try:
            reminder_id = self.state.add_reminder(
                user_id=user_id,
                channel_id=channel_id,
                message=message,
                due_timestamp=parsed_time
            )

            # Create Discord timestamp (shows in user's timezone)
            discord_timestamp = to_epoch(parsed_time)

            # Success message with Discord timestamp formatting
            await ctx.followup.send(
                f"✅ Reminder set!\n"
                f"**Message:** {message}\n"
                f"**When:** <t:{discord_timestamp}:F> (<t:{discord_timestamp}:R>)\n"
                f"**Reminder ID:** {reminder_id}"
            )
            logger.info(f"User {user_id} set reminder {reminder_id} for {parsed_time}")

        except Exception as e:
            logger.exception(f"Error setting reminder: {e}")
            await ctx.followup.send(f"❌ Failed to save reminder: {str(e)}")

    @discord.slash_command(
        name="listreminders",
        description="List your pending reminders"
    )
    async def listreminders_slash(self, ctx):
        """Show all pending reminders for the user."""
        await ctx.defer(ephemeral=True)

        user_id = str(ctx.author.id)

        try:
            reminders = self.state.get_user_reminders(user_id, include_sent=False)

            if not reminders:
                await ctx.followup.send("You have no pending reminders.", ephemeral=True)
                return

            # Create embed
            embed = discord.Embed(
                title=f"📝 Your Reminders ({len(reminders)})",
                color=discord.Color.blue(),
                description="Here are your pending reminders:"
            )

            for reminder in reminders[:25]:  # Discord embed field limit
                reminder_id = reminder['reminder_id']
                message = reminder['message']
                due_timestamp = reminder['due_timestamp']

                # Handle datetime conversion if needed
                if isinstance(due_timestamp, str):
                    try:
                        due_timestamp = datetime.fromisoformat(due_timestamp)
                    except ValueError:
                        due_timestamp = datetime.strptime(due_timestamp, '%Y-%m-%d %H:%M:%S')

                # Localize naive datetime to local timezone before converting to timestamp
                discord_timestamp = to_epoch(due_timestamp)

                # Truncate long messages
                display_message = message if len(message) <= 50 else message[:47] + "..."

                embed.add_field(
                    name=f"ID {reminder_id}: {display_message}",
                    value=f"Due: <t:{discord_timestamp}:F> (<t:{discord_timestamp}:R>)",
                    inline=False
                )

            if len(reminders) > 25:
                embed.set_footer(text=f"Showing first 25 of {len(reminders)} reminders")

            await ctx.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.exception(f"Error listing reminders: {e}")
            await ctx.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @discord.slash_command(
        name="cancelreminder",
        description="Cancel a reminder by ID"
    )
    async def cancelreminder_slash(
        self,
        ctx,
        reminder_id: discord.Option(int, "The reminder ID to cancel", required=True)
    ):
        """Cancel a specific reminder."""
        await ctx.defer(ephemeral=True)

        user_id = str(ctx.author.id)

        try:
            deleted = self.state.delete_reminder(reminder_id, user_id)

            if deleted:
                await ctx.followup.send(
                    f"✅ Reminder {reminder_id} has been cancelled.",
                    ephemeral=True
                )
                logger.info(f"User {user_id} cancelled reminder {reminder_id}")
            else:
                await ctx.followup.send(
                    f"❌ Reminder {reminder_id} not found or doesn't belong to you.",
                    ephemeral=True
                )

        except Exception as e:
            logger.exception(f"Error cancelling reminder: {e}")
            await ctx.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

def setup(bot):
    """Setup function called when loading the cog."""
    logger.info("Setting up ReminderCommands cog...")
    openrouter_client = getattr(bot, 'openrouter_client', None)
    openai_client = getattr(bot, 'openai_client', None)
    ai_horde_client = getattr(bot, 'ai_horde_client', None)

    if not openrouter_client:
        logger.error("OpenRouter client not found. Cannot load ReminderCommands cog.")
        return

    try:
        cog_instance = ReminderCommands(bot, openrouter_client, openai_client, ai_horde_client)
        bot.add_cog(cog_instance)
        logger.info("ReminderCommands cog loaded successfully.")
    except Exception as e:
        logger.exception(f"Failed to load ReminderCommands cog: {e}")
