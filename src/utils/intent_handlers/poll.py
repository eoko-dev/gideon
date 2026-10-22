"""Handler for poll creation intent."""

import logging
from datetime import datetime
import discord

from ..permissions import has_channel_permission

logger = logging.getLogger('poll_handler')


async def handle_poll_creation(cog, message, channel_id, question, options, duration=24):
    """
    Handle poll creation requests.

    Args:
        cog: The MentionCommands cog instance
        message: Discord message object
        channel_id: Channel ID as string
        question: The poll question
        options: List of poll options (2-10 options)
        duration: Poll duration in hours (default: 24)
    """
    # Validate inputs
    if not question or not question.strip():
        await message.channel.send(
            "❌ I need a poll question. "
            "Try something like: '@Gideon create a poll: Pizza or Tacos?'"
        )
        logger.warning(f"[Poll] Missing question for user {message.author.id}")
        return

    if not options or len(options) < 2:
        await message.channel.send(
            "❌ I need at least 2 poll options. "
            "Try something like: '@Gideon create a poll: Pizza or Tacos?'"
        )
        logger.warning(f"[Poll] Insufficient options for user {message.author.id}")
        return

    if len(options) > 10:
        await message.channel.send(
            "❌ Maximum 10 poll options allowed. Please reduce the number of options."
        )
        logger.warning(f"[Poll] Too many options ({len(options)}) for user {message.author.id}")
        return

    # Gideon posts the poll with its own permissions, so the author must be
    # allowed to post one themselves
    if getattr(message, 'guild', None) and not has_channel_permission(
            message.author, message.channel, send_polls=True):
        await message.channel.send("⛔ You don't have permission to create polls in this channel.")
        logger.info(f"[Poll] User {message.author.id} lacks Send Polls in channel {channel_id}")
        return

    try:
        # Check if Discord native polls are available (discord.py >= 2.3.0)
        # Try to use native polls if available
        if hasattr(discord, 'Poll') and hasattr(message.channel, 'send_poll'):
            # Native Discord polls available
            try:
                poll = discord.Poll(
                    question=question,
                    duration=duration,  # duration in hours
                    multiple=False
                )

                # Add poll options
                for option in options:
                    poll.add_answer(text=option[:55])  # Discord limit is 55 chars per option

                # Send poll
                await message.channel.send(poll=poll)

                # Add to conversation history
                await cog.state.add_to_channel_history(channel_id, {
                    "role": "assistant",
                    "content": f"Created poll: {question} with {len(options)} options",
                    "timestamp": datetime.now()
                })

                logger.info(f"[Poll] User {message.author.id} created native poll: {question}")
                return

            except Exception as e:
                logger.warning(f"[Poll] Native poll creation failed, falling back to reactions: {e}")
                # Fall through to reaction-based polls

        # Fallback: Create reaction-based poll
        # Build poll message
        poll_message = f"**📊 Poll: {question}**\n\n"

        # Emoji numbers for options (1-10)
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

        for i, option in enumerate(options):
            poll_message += f"{number_emojis[i]} {option}\n"

        poll_message += f"\n*React with the corresponding number to vote!*"

        if duration and duration > 0:
            poll_message += f"\n*Poll duration: {duration} hour{'s' if duration != 1 else ''}*"

        # Send poll message
        sent_message = await message.channel.send(poll_message)

        # Add reactions
        for i in range(len(options)):
            await sent_message.add_reaction(number_emojis[i])

        # Add to conversation history
        await cog.state.add_to_channel_history(channel_id, {
            "role": "assistant",
            "content": f"Created poll: {question} with {len(options)} options (reaction-based)",
            "timestamp": datetime.now()
        })

        logger.info(f"[Poll] User {message.author.id} created reaction-based poll: {question}")

    except Exception as e:
        logger.exception(f"[Poll] Error for user {message.author.id}: {e}")
        await message.channel.send(f"❌ Poll creation failed: {str(e)}")
