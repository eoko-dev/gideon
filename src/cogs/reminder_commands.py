"""Commands for setting and managing reminders."""
import discord
import logging
from discord.ext import commands
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
import json
import os
from ..utils.memory_service import llm_clients
from ..utils.usage import usage_scope
from ..utils import time_parse
from ..utils.timeutil import local_tz, now_local, to_epoch

logger = logging.getLogger('reminder_commands')

# custom_id prefix for reminder buttons: gideon-rem:<action>:<reminder_id>[:<minutes>]
BUTTON_PREFIX = "gideon-rem:"

COMMON_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Phoenix",
    "America/Anchorage", "Pacific/Honolulu", "America/Toronto", "America/Vancouver", "America/Sao_Paulo",
    "America/Mexico_City", "Europe/London", "Europe/Dublin", "Europe/Paris", "Europe/Berlin",
    "Europe/Madrid", "Europe/Stockholm", "Europe/Athens", "Africa/Johannesburg", "Asia/Dubai",
    "Asia/Kolkata", "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney", "Pacific/Auckland",
]


def _as_datetime(value) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    return value


def format_confirmation(reminder_id: int, message: str, due: datetime, recurrence: Optional[str],
                        dm: bool) -> str:
    """The "Reminder set!" text shared by /remind, the tool and the context menu."""
    stamp = to_epoch(due)
    lines = ["✅ Reminder set!", f"**Message:** {message}",
             f"**When:** <t:{stamp}:F> (<t:{stamp}:R>)"]
    if recurrence:
        lines.append(f"**Repeats:** {time_parse.describe_rule(recurrence)}")
    if dm:
        lines.append("**Delivery:** by DM")
    lines.append(f"**Reminder ID:** {reminder_id}")
    return "\n".join(lines)


def reminder_buttons(reminder_id: int) -> discord.ui.View:
    """Snooze/Done buttons for a delivered reminder (handled in on_interaction)."""
    view = discord.ui.View(timeout=None)
    for label, minutes in (("10 min", 10), ("1 hour", 60), ("Tomorrow", 24 * 60)):
        view.add_item(discord.ui.Button(label=f"Snooze {label}", style=discord.ButtonStyle.secondary,
                                        emoji="😴", custom_id=f"{BUTTON_PREFIX}snooze:{reminder_id}:{minutes}"))
    view.add_item(discord.ui.Button(label="Done", style=discord.ButtonStyle.success, emoji="✅",
                                    custom_id=f"{BUTTON_PREFIX}done:{reminder_id}"))
    return view

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
        """Parses a one-off natural-language time into a future server-local datetime (or None)."""
        plan = await self.plan_reminder(when, channel_id, user, allow_repeat=False)
        return plan["due"] if plan else None

    async def plan_reminder(self, when: str, channel_id: str, user=None,
                            allow_repeat: bool = True) -> Optional[Dict[str, Any]]:
        """Works out when a reminder fires.

        Returns {"due": naive server-local datetime, "recurrence": RRULE or None,
        "timezone": IANA name or None} or None if the time can't be understood
        or isn't in the future. Times are read in the user's own timezone
        (/me timezone); unusual phrasings fall back to the LLM parser.
        """
        tz_name = self.state.get_user_timezone_name(str(user.id)) if user is not None else None
        tz = self.state.get_user_timezone(str(user.id)) if user is not None else local_tz()
        recurrence, first = time_parse.parse_recurrence(when) if allow_repeat else (None, when)

        candidate = time_parse.parse_when(first, tz)
        if candidate is not None:
            if recurrence:
                candidate = time_parse.first_occurrence(recurrence, candidate)
            return {"due": time_parse.to_server_naive(candidate), "recurrence": recurrence,
                    "timezone": str(tz) if tz_name else None}

        # The LLM parser works in server time and doesn't know the user's zone
        if tz_name and str(tz) != str(local_tz()):
            logger.info(f"[Reminder] Couldn't parse '{first}' locally for a user in {tz}; not guessing")
            return None
        due = await self.parse_time_with_ai(first, channel_id)
        if due is None:
            return None
        if recurrence:
            due = time_parse.to_server_naive(time_parse.first_occurrence(recurrence, due.replace(tzinfo=local_tz())))
        return {"due": due, "recurrence": recurrence, "timezone": None}

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
        description="Set a reminder (e.g. 'in 2 hours', 'Friday 6pm', 'every weekday at 9am')"
    )
    async def remind_slash(
        self,
        ctx,
        message: discord.Option(str, "What should I remind you about?", required=True),
        when: discord.Option(str, "When? 'in 2 hours', 'tomorrow at 3pm', 'every monday at 9am'", required=True),
        dm: discord.Option(bool, "Send it to you privately instead of in this channel", required=False,
                           default=False),
    ):
        """Set a reminder using natural language time."""
        await ctx.defer(ephemeral=bool(dm))
        user_id = str(ctx.author.id)
        logger.info(f"[Reminder] User {user_id} setting reminder in channel {ctx.channel.id}: '{message}' at '{when}'")

        plan = await self.plan_reminder(when, self._scope_id(ctx.channel), ctx.author)
        if plan is None:
            hint = "" if self.state.get_user_timezone_name(user_id) else \
                "\n💡 Times use the server's timezone until you set yours with `/me timezone`."
            await ctx.followup.send(
                f"❌ I couldn't understand '{when}' as a time in the future. Try 'in 2 hours', "
                f"'tomorrow at 3pm' or 'every monday at 9am'.{hint}", ephemeral=True)
            return
        if plan["due"] > datetime.now() + timedelta(days=366):
            await ctx.followup.send("❌ Reminders can be at most a year away.", ephemeral=True)
            return

        try:
            reminder_id = self.state.add_reminder(
                user_id=user_id, channel_id=str(ctx.channel.id), message=message,
                due_timestamp=plan["due"], recurrence=plan["recurrence"], deliver_dm=bool(dm),
                timezone=plan["timezone"])
        except Exception as e:
            logger.exception(f"Error setting reminder: {e}")
            await ctx.followup.send("❌ The reminder couldn't be saved.", ephemeral=True)
            return

        await ctx.followup.send(format_confirmation(reminder_id, message, plan["due"], plan["recurrence"], dm),
                                ephemeral=bool(dm))
        logger.info(f"User {user_id} set reminder {reminder_id} for {plan['due']} ({plan['recurrence']})")

    @staticmethod
    def _scope_id(channel) -> str:
        return str(channel.parent_id) if isinstance(channel, discord.Thread) else str(channel.id)

    @discord.slash_command(
        name="listreminders",
        description="List your pending reminders"
    )
    async def listreminders_slash(self, ctx):
        """Show all pending reminders for the user."""
        await ctx.defer(ephemeral=True)
        reminders = self.state.get_user_reminders(str(ctx.author.id), include_sent=False)
        if not reminders:
            await ctx.followup.send("You have no pending reminders.", ephemeral=True)
            return

        embed = discord.Embed(title=f"📝 Your Reminders ({len(reminders)})", color=discord.Color.blue())
        for reminder in reminders[:25]:  # Discord embed field limit
            stamp = to_epoch(_as_datetime(reminder['due_timestamp']))
            text = reminder['message']
            display_message = text if len(text) <= 50 else text[:47] + "..."
            extras = [time_parse.describe_rule(reminder.get('recurrence'))]
            if reminder.get('deliver_dm'):
                extras.append("by DM")
            extra = " · ".join(e for e in extras if e)
            embed.add_field(
                name=f"ID {reminder['reminder_id']}: {display_message}",
                value=f"Due: <t:{stamp}:F> (<t:{stamp}:R>)" + (f"\n🔁 {extra}" if extra else ""),
                inline=False)
        if len(reminders) > 25:
            embed.set_footer(text=f"Showing first 25 of {len(reminders)} reminders")
        await ctx.followup.send(embed=embed, ephemeral=True)

    # ─── /me ────────────────────────────────────────────────────────────────

    me = discord.SlashCommandGroup("me", "Your personal Gideon settings")

    async def timezone_autocomplete(self, ctx):
        from zoneinfo import available_timezones
        typed = (ctx.value or "").lower().replace(" ", "_")
        zones = sorted(z for z in available_timezones() if "/" in z and not z.startswith(("Etc/", "SystemV/")))
        matches = [z for z in zones if typed in z.lower()] if typed else COMMON_TIMEZONES
        return matches[:25]

    @me.command(name="timezone", description="Set your timezone so reminder times mean your local time")
    async def me_timezone(
        self, ctx,
        timezone: discord.Option(str, "e.g. America/Chicago, Europe/London (leave empty to see yours)",
                                 required=False, default=None, autocomplete=timezone_autocomplete),
    ):
        user_id = str(ctx.author.id)
        if not timezone:
            current = self.state.get_user_timezone_name(user_id)
            await ctx.respond(f"Your timezone is **{current}**." if current else
                              f"You haven't set a timezone; I use the server's (**{local_tz()}**).", ephemeral=True)
            return
        try:
            name = self.state.set_user_timezone(user_id, timezone)
        except ValueError as e:
            await ctx.respond(f"❌ {e}", ephemeral=True)
            return
        local_now = datetime.now(time_parse.resolve_timezone(name))
        await ctx.respond(f"✅ Timezone set to **{name}** (it's {local_now.strftime('%I:%M %p').lstrip('0')} "
                          f"there now). Existing reminders keep their times.", ephemeral=True)

    # ─── Reminder buttons (snooze / done) ───────────────────────────────────

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        """Handles the Snooze/Done buttons on delivered reminders (works across restarts)."""
        if interaction.type != discord.InteractionType.component:
            return
        custom_id = (interaction.data or {}).get("custom_id", "")
        if not custom_id.startswith(BUTTON_PREFIX):
            return
        try:
            _, action, reminder_id, *rest = custom_id.split(":")
            reminder = self.state.get_reminder(int(reminder_id))
        except (ValueError, TypeError):
            return
        if reminder is None:
            await interaction.response.send_message("That reminder no longer exists.", ephemeral=True)
            return
        if str(interaction.user.id) != str(reminder["user_id"]):
            await interaction.response.send_message("Only the person this reminder is for can do that.",
                                                    ephemeral=True)
            return

        if action == "done":
            await interaction.response.edit_message(view=None)
            return
        if action == "snooze":
            minutes = int(rest[0]) if rest else 10
            due = datetime.now() + timedelta(minutes=minutes)
            due = due.replace(second=0, microsecond=0)
            if reminder.get("recurrence"):
                # Keep the repeating schedule; snooze this occurrence as a one-off copy
                new_id = self.state.add_reminder(
                    user_id=reminder["user_id"], channel_id=reminder["channel_id"], message=reminder["message"],
                    due_timestamp=due, deliver_dm=bool(reminder.get("deliver_dm")),
                    timezone=reminder.get("timezone"))
            else:
                self.state.reschedule_reminder(reminder["reminder_id"], due)
                new_id = reminder["reminder_id"]
            stamp = to_epoch(due)
            await interaction.response.edit_message(view=None)
            await interaction.followup.send(f"😴 Snoozed — I'll remind you again <t:{stamp}:R> (#{new_id}).",
                                            ephemeral=True)

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
