"""Thread-based conversation commands."""
import discord
import logging
from discord.ext import commands
from ..utils.discord_fmt import chunk_message
from ..utils.llm_formatting import resolve_discord_mentions, collect_mention_targets, apply_outbound_mentions
from ..config import SYSTEM_PROMPT, ALLOWED_MODELS, DEFAULT_MODEL
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

# Create logger
logger = logging.getLogger(__name__)

class ThreadCommands(commands.Cog):
    """Commands for managing AI conversation threads."""

    def __init__(self, bot):
        self.bot = bot
        # Use the shared state manager and client from the bot instance so
        # dashboard key rotation and config changes apply here too.
        self.state = bot.state_manager
        self.openrouter_client = bot.openrouter_client

    # Create the thread command group as a class attribute
    thread = discord.SlashCommandGroup(
        "thread",
        "Thread conversation commands"
    )

    async def model_autocomplete(self, ctx):
        """Dynamic model autocomplete using ModelManager with current selection highlighting"""
        current_input = ctx.value.lower() if ctx.value else ""
        # Use the ModelManager instance from the state manager
        if not self.state.model_manager:
             logger.warning("ModelManager not set in StateManager for autocomplete.")
             return [] # Return empty if ModelManager isn't ready

        # Determine the provider for the current context
        try:
            # Use interaction.channel_id for AutocompleteContext
            channel_id = str(ctx.interaction.channel_id)
            provider = self.state.get_channel_provider(channel_id)
            logger.debug(f"Autocomplete using provider '{provider}' for channel {channel_id}")
            all_models = await self.state.model_manager.get_models(provider=provider)
        except Exception as e:
            logger.error(f"Error getting models for autocomplete in channel {ctx.interaction.channel_id}: {e}", exc_info=True)
            return [] # Return empty on error

        # Get current thread model for highlighting
        thread_id = str(ctx.interaction.channel_id) if isinstance(ctx.interaction.channel, discord.Thread) else None
        current_model = None
        if thread_id:
            thread_config = self.state.get_discord_thread_config(thread_id)
            current_model = thread_config.get("model") if thread_config else None

        # Format models with provider prefix and highlight current selection
        formatted_models = []
        for model in all_models:
            full_model = f"{provider}/{model}"
            if full_model == current_model:
                formatted_models.append(f"✓ {full_model} (current)")
            else:
                formatted_models.append(full_model)

        if not current_input:
            # Return formatted models
            return formatted_models[:25]

        # Filter formatted models based on input (which might include the prefix or not)
        matching_models = [
            fm for fm in formatted_models
            if current_input in fm.lower() # Check against the full provider/model string
        ]

        # Return matching formatted models, or the first 25 formatted models if no match
        return matching_models[:25] or formatted_models[:25]

    def get_model_for_channel(self, channel_id):
        """Get the appropriate model for this channel"""
        return self.state.get_effective_model(channel_id)

    @thread.command(name="new", description="Create a new AI conversation thread")
    async def thread_slash(self, ctx,
                          name: str,
                          message: str = None,
                          image: discord.Attachment = None):
        """Create a new thread and optionally start with a message"""
        # Check if the channel supports threads
        if not isinstance(ctx.channel, discord.TextChannel):
            await ctx.respond("⚠️ This command can only be used in text channels that support threads.")
            return

        # Create an initial message that will anchor the thread
        initial_message = await ctx.channel.send(f"**AI Thread: {name}**\n*Starting a new conversation thread...*")

        try:
            # Create actual Discord thread from the message
            thread = await initial_message.create_thread(
                name=name,
                auto_archive_duration=1440  # Auto-archive after 24 hours of inactivity
            )

            # Store thread information in the database
            thread_id = str(thread.id)
            channel_id = str(ctx.channel.id)

            # Add thread to the database
            await self.state.add_discord_thread(
                thread_id=thread_id,
                channel_id=channel_id,
                name=name
                # created_at is set in the state manager/database
            )

            # Welcome message in the thread
            welcome_msg = f"✅ Thread created! You can chat with the AI by just sending regular messages in this thread. I'll respond to everything automatically."
            await thread.send(welcome_msg)

            # If a message was provided, process it immediately in the new thread
            if message:
                # Add user message to thread history in the database
                await self.state.add_discord_thread_message(thread_id, {
                    "role": "user",
                    "name": ctx.author.display_name,
                    "content": message,
                    # timestamp is set in the state manager/database
                    "user_id": str(ctx.author.id) # Store user ID
                })

                # Get thread-specific model (or channel/global default)
                thread_config = self.state.get_discord_thread_config(thread_id)
                thread_model = thread_config.get("model") if thread_config else None
                model_to_use = thread_model if thread_model else self.get_model_for_channel(channel_id)

                # Process image if provided
                model_supports_images = await self.openrouter_client.model_supports_vision(model_to_use)
                images = []

                if image and model_supports_images:
                    if any(image.filename.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                        try:
                            image_data = await image.read()
                            images.append({
                                'data': image_data,
                                'type': image.content_type or 'image/jpeg'
                            })
                        except Exception as e:
                            await thread.send(f"⚠️ Failed to process image {image.filename}: {str(e)}")
                            # Continue without the image if processing fails

                # Send thinking message in the thread
                thinking_msg = await thread.send(f"**{ctx.author.display_name}**: {message}\n\n_Processing response..._")

                # Get thread history for context (should only be the initial message for now)
                # Use the getter method
                conversation_context = self.state.get_discord_thread_history(thread_id)

                # Thread-specific system prompt (or channel default), with the
                # parent channel's memory and notes on the people in the thread
                thread_system_prompt = self.state.get_effective_system_prompt(
                    channel_id, speaker_id=str(ctx.author.id), history=conversation_context,
                    base_prompt=thread_config.get("system_prompt") if thread_config else None)

                # Temporarily set the model for this request
                current_model = self.openrouter_client.model
                self.openrouter_client.model = model_to_use

                try:
                    # Get response from AI
                    response = await self.openrouter_client.send_message_with_history(
                        conversation_context,
                        images=images if model_supports_images else [],
                        system_prompt=thread_system_prompt
                    )

                    # Add AI response to thread history in the database
                    await self.state.add_discord_thread_message(thread_id, {
                        "role": "assistant",
                        "content": response,
                        # timestamp is set in the state manager/database
                    })

                    # Split response into chunks
                    chunks = chunk_message(response)

                    # Update thinking message with first chunk
                    await thinking_msg.edit(content=chunks[0])

                    # Send remaining chunks
                    for chunk in chunks[1:]:
                        await thread.send(chunk)

                    # Update the success message
                    success_msg = f"✅ Created new thread: **{name}** with your initial message. Check the thread for the AI's response!"
                finally:
                    # Restore original model
                    self.openrouter_client.model = current_model
            else:
                # Just confirm thread creation
                success_msg = f"✅ Created new thread: **{name}**\nThe thread is now ready for conversation. All messages in the thread will receive AI responses."

            # Reply to the slash command
            await ctx.respond(success_msg)

        except discord.Forbidden:
            await ctx.respond("⚠️ I don't have permission to create threads in this channel.")
        except discord.HTTPException as e:
            await ctx.respond(f"⚠️ Failed to create thread: {str(e)}")
        except Exception as e:
            logger.error(f"Error creating thread: {e}", exc_info=True)
            await ctx.respond(f"⚠️ An unexpected error occurred while creating the thread: {str(e)}")

    @thread.command(name="message", description="Chat within a specific conversation thread")
    async def thread_chat_slash(self, ctx,
                         id: str,
                         message: str,
                         image: discord.Attachment = None):
        await ctx.defer()

        # Get thread data from the database
        thread_id = id
        thread_data = self.state.get_discord_thread(thread_id)

        # Check if thread exists
        if not thread_data:
            await ctx.respond("⚠️ Thread not found. Use `/thread list` to see available threads.")
            return

        thread_name = thread_data["name"]
        channel_id = thread_data["channel_id"]

        # The reply is posted where the command runs, and it carries the
        # parent channel's memory and people notes — so only allow it from the
        # thread itself or its parent channel, never from another channel
        if str(ctx.channel.id) not in (str(thread_id), str(channel_id)):
            await ctx.respond("⚠️ Run this from the thread or its parent channel.", ephemeral=True)
            return

        # Get thread-specific model and system prompt from DB
        thread_config = self.state.get_discord_thread_config(thread_id)
        thread_model = thread_config.get("model") if thread_config else None
        thread_system_prompt = thread_config.get("system_prompt") if thread_config else None

        # Set model for this request (thread-specific > channel-specific > global)
        current_model = self.openrouter_client.model
        model_to_use = thread_model if thread_model else self.get_model_for_channel(channel_id)
        self.openrouter_client.model = model_to_use

        # Handle image processing
        model_supports_images = await self.openrouter_client.model_supports_vision(model_to_use)
        images = []
        image_embed = None

        if image:
            # Check if it's an image file
            if any(image.filename.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                # Create an embed to display the image
                image_embed = discord.Embed(title=f"Analyzing Image in Thread: {thread_name}", color=discord.Color.blue())
                image_embed.set_image(url=image.url)
                image_embed.add_field(name="File", value=image.filename)

                if model_supports_images:
                    try:
                        image_data = await image.read()
                        images.append({
                            'data': image_data,
                            'type': image.content_type or 'image/jpeg'
                        })
                    except Exception as e:
                        await ctx.respond(f"⚠️ Failed to process image {image.filename}: {str(e)}")
                        return
                else:
                    image_embed.description = "⚠️ Current model doesn't support image analysis. Consider switching to a vision-capable model."

        try:
            # Add user message to thread in the database
            await self.state.add_discord_thread_message(thread_id, {
                "role": "user",
                "name": ctx.author.display_name,
                "content": message,
                # timestamp is set in the state manager/database
                "user_id": str(ctx.author.id) # Store user ID
            })

            # Format conversation context from the database history
            # Use the getter method with the configured time window
            conversation_context = self.state.get_discord_thread_history(thread_id, hours_limit=self.state.get_time_window_hours())

            # First response - show the user's message
            if image_embed:
                await ctx.respond(f"**{ctx.author.display_name}** in **{thread_name}**: {message}", embed=image_embed)
                # Follow up with processing message
                processing_msg = await ctx.followup.send(f"Processing response for thread **{thread_name}**...")
            else:
                # Show user's message before processing for text-only messages too
                await ctx.respond(f"**{ctx.author.display_name}** in **{thread_name}**: {message}\n\n_Processing response..._")
                processing_msg = None

            # Thread prompt, falling back to persona > channel config > global,
            # with the parent channel's memory and people notes appended
            thread_system_prompt = self.state.get_effective_system_prompt(
                channel_id, speaker_id=str(ctx.author.id), history=conversation_context,
                base_prompt=thread_system_prompt)


            response = await self.openrouter_client.send_message_with_history(
                conversation_context,
                images=images if model_supports_images else [],
                system_prompt=thread_system_prompt
            )

            # Add AI response to thread in the database
            await self.state.add_discord_thread_message(thread_id, {
                "role": "assistant",
                "content": response,
                # timestamp is set in the state manager/database
            })

            # Send response in chunks like other commands
            chunks = chunk_message(response)

            # Check if a persona is active for webhook-based sending
            parent_id = str(ctx.channel.parent_id) if isinstance(ctx.channel, discord.Thread) else str(ctx.channel.id)
            thread_persona_active = (hasattr(self.bot, 'webhook_sender')
                                     and self.bot.state_manager.get_effective_persona(parent_id))

            # Process the first chunk differently if we have a processing message to edit
            for i, chunk in enumerate(chunks):
                if i == 0:
                    if thread_persona_active and processing_msg:
                        await processing_msg.delete()
                        await self.bot.webhook_sender.send_response(
                            ctx.channel, f"**Thread: {thread_name}**\n\n{chunk}", parent_id
                        )
                    elif processing_msg:
                        await processing_msg.edit(content=f"**Thread: {thread_name}**\n\n{chunk}")
                    else:
                        await ctx.followup.send(f"**Thread: {thread_name}**\n\n{chunk}")
                else:
                    if thread_persona_active:
                        await self.bot.webhook_sender.send_response(ctx.channel, chunk, parent_id)
                    else:
                        await ctx.channel.send(chunk)
        finally:
            # Restore original model
            self.openrouter_client.model = current_model

    @thread.command(name="list", description="List all active conversation threads in this channel")
    async def list_threads_slash(self, ctx):
        channel_id = str(ctx.channel.id)

        # Get threads for this channel from the database
        channel_threads = self.state.list_discord_threads_for_channel(channel_id)

        if not channel_threads:
            await ctx.respond("No active threads in this channel. Create one with `/thread new`")
            return

        threads_list = []
        for thread_data in channel_threads:
            thread_id = thread_data["thread_id"]
            thread_name = thread_data["name"]
            created_time = thread_data["created_at"].strftime("%Y-%m-%d %H:%M")
            # Get message count from the database
            message_count = self.state.get_thread_message_count(thread_id)
            threads_list.append(f"• **{thread_name}** (ID: `{thread_id}`)\n  Created: {created_time} | Messages: {message_count}")

        await ctx.respond(f"**Active Conversation Threads:**\n\n" + "\n".join(threads_list) +
                          "\n\nUse `/thread message id:<thread_id> message:<your message>` to continue a conversation.")

    @thread.command(name="delete", description="Delete a conversation thread")
    async def delete_thread_slash(self, ctx, id: str):
        # Delete thread from the database
        thread_deleted = self.state.delete_discord_thread(id)

        if thread_deleted:
            # Attempt to delete the Discord thread itself if possible
            try:
                discord_thread = self.bot.get_channel(int(id))
                if discord_thread and isinstance(discord_thread, discord.Thread):
                    await discord_thread.delete()
                    logger.info(f"Deleted Discord thread {id}")
            except Exception as e:
                logger.warning(f"Could not delete Discord thread {id}: {e}")

            await ctx.respond(f"✅ Deleted thread with ID: **{id}**")
        else:
            await ctx.respond("⚠️ Thread not found in the database. Use `/thread list` to see available threads.")

    @thread.command(name="rename", description="Rename a conversation thread")
    async def rename_thread_slash(self, ctx, id: str, name: str):
        # Rename thread in the database
        thread_renamed = self.state.rename_discord_thread(id, name)

        if thread_renamed:
            # Attempt to rename the Discord thread itself if possible
            try:
                discord_thread = self.bot.get_channel(int(id))
                if discord_thread and isinstance(discord_thread, discord.Thread):
                    await discord_thread.edit(name=name)
                    logger.info(f"Renamed Discord thread {id} to '{name}'")
            except Exception as e:
                logger.warning(f"Could not rename Discord thread {id}: {e}")

            await ctx.respond(f"✅ Renamed thread with ID **{id}** to **{name}**")
        else:
            await ctx.respond("⚠️ Thread not found in the database. Use `/thread list` to see available threads.")

    @thread.command(name="show", description="View current thread settings")
    async def show_thread_slash(self, ctx):
        """Display thread-specific settings."""
        # Check if we're in a thread
        if not isinstance(ctx.channel, discord.Thread):
            await ctx.respond("⚠️ This command can only be used within a thread.")
            return

        await ctx.defer()
        thread_id = str(ctx.channel.id)

        # Get thread data
        thread_data = self.state.get_discord_thread(thread_id)
        if not thread_data:
            await ctx.respond("⚠️ This thread is not tracked in the database.")
            return

        # Get thread configuration
        thread_config = self.state.get_discord_thread_config(thread_id)
        thread_model = thread_config.get("model") if thread_config else None
        thread_system = thread_config.get("system_prompt") if thread_config else None

        # Get channel and global settings for comparison
        channel_id = thread_data["channel_id"]
        channel_model = self.state.get_channel_model(channel_id)
        global_model = self.state.get_global_model()
        effective_model = thread_model or channel_model or global_model

        embed = discord.Embed(
            title=f"⚙️ Settings for Thread: {thread_data['name']}",
            description="Thread-specific configuration",
            color=discord.Color.blue()
        )

        # Model info
        if thread_model:
            model_text = f"**Override:** `{thread_model}`\n_(Effective: `{effective_model}`)_"
        elif channel_model:
            model_text = f"**Using Channel:** `{channel_model}`\n_(Global: `{global_model}`)_"
        else:
            model_text = f"**Using Global:** `{global_model}`"

        embed.add_field(
            name="🤖 AI Model",
            value=model_text,
            inline=False
        )

        # System prompt info
        if thread_system:
            prompt_preview = thread_system[:100] + "..." if len(thread_system) > 100 else thread_system
            prompt_text = f"**Custom:** {prompt_preview}"
        else:
            prompt_text = "**Using Channel/Global**"

        embed.add_field(
            name="📝 System Prompt",
            value=prompt_text,
            inline=False
        )

        # Thread info
        message_count = self.state.get_thread_message_count(thread_id)
        created_time = thread_data["created_at"].strftime("%Y-%m-%d %H:%M")

        embed.add_field(
            name="📊 Thread Statistics",
            value=f"• Created: {created_time}\n• Messages: {message_count}",
            inline=False
        )

        embed.set_footer(text="Use /thread model or /thread system to customize this thread")

        await ctx.respond(embed=embed)

    @thread.command(name="model", description="Set AI model for this thread (format: provider/model)")
    async def set_thread_model_slash(
        self,
        ctx,
        model_name: discord.Option(str, "Select the AI model to use for this thread", autocomplete=model_autocomplete)
    ):
        # Check if we're in a thread
        if not isinstance(ctx.channel, discord.Thread):
            await ctx.respond("⚠️ This command can only be used within a thread.")
            return

        await ctx.defer()
        thread_id = str(ctx.channel.id)

        try:
            # Remove the ✓ prefix if present (from autocomplete selection)
            if model_name.startswith("✓ "):
                model_name = model_name[2:].replace(" (current)", "").strip()

            # Use the state manager's method which includes validation (synchronous call)
            self.state.set_discord_thread_model(thread_id, model_name)
            await ctx.respond(f"✅ Model for this thread set to `{model_name}`")

        except ValueError as e:
            error_msg = str(e)
            # Add helpful hints
            if "not found" in error_msg and "/" not in model_name:
                error_msg += "\n\n💡 **Tip:** Use format `provider/model` (e.g., `openrouter/gpt-4`)"
            elif "not found" in error_msg:
                error_msg += "\n\n💡 **Tip:** Use autocomplete to see available models"
            await ctx.respond(f"⚠️ Error: {error_msg}", ephemeral=True)
        except Exception as e:
            logger.error(f"Error setting thread model: {e}", exc_info=True)
            await ctx.respond(
                f"❌ Unexpected error occurred.\n\n"
                f"Please contact an administrator or check `/admin diagnostic`.",
                ephemeral=True
            )

    @thread.command(name="system", description="Set custom system prompt for this thread")
    async def set_thread_system_slash(self, ctx, new_prompt: str):
        # Check if we're in a thread
        if not isinstance(ctx.channel, discord.Thread):
            await ctx.respond("⚠️ This command can only be used within a thread.")
            return

        await ctx.defer()
        thread_id = str(ctx.channel.id)

        try:
            # Use the state manager's method
            self.state.set_discord_thread_system_prompt(thread_id, new_prompt)

            # Handle long prompts by chunking (1950 leaves room for the code fence)
            chunks = chunk_message(new_prompt, limit=1950)

            await ctx.respond(f"✅ System prompt for this thread updated:\n```\n{chunks[0]}\n```")
            for chunk in chunks[1:]:
                await ctx.followup.send(f"```\n{chunk}\n```")

        except Exception as e:
            logger.error(f"Error setting thread system prompt: {e}", exc_info=True)
            await ctx.respond(f"❌ Error: {e}", ephemeral=True)


    def _is_own_persona_webhook(self, message: discord.Message, channel_id: str) -> bool:
        """True if this message is one the bot sent through its persona webhook.

        Persona replies aren't authored by bot.user, so without this check the
        bot's own words get recorded again as a user turn. Deliberately narrow:
        other webhook integrations are still treated as ordinary messages.
        """
        if not message.webhook_id or not self.state:
            return False

        try:
            persona = self.state.get_effective_persona(channel_id)
        except Exception as e:
            logger.error(f"Error checking persona webhook for channel {channel_id}: {e}", exc_info=True)
            return False

        return bool(persona) and str(message.webhook_id) == str(persona.get('webhook_id') or '')

    @commands.Cog.listener()
    async def on_message(self, message):
        """Listen for messages in threads to build context memory and respond."""
        # Ignore messages from the bot itself
        if message.author == self.bot.user:
            return

        # Only process messages in Discord threads
        if isinstance(message.channel, discord.Thread):
            thread_id = str(message.channel.id)
            channel_id = str(message.channel.parent_id) # Get parent channel ID

            # Persona replies are sent through the parent channel's webhook, so
            # they aren't authored by bot.user and would otherwise be recorded
            # as if a human had said them
            if self._is_own_persona_webhook(message, channel_id):
                return

            # Check if this is an active trivia thread - if so, skip it
            # The trivia cog will handle all messages in trivia threads
            trivia_session = self.state.get_active_trivia_session(thread_id)
            if trivia_session:
                logger.debug(f"Skipping message in active trivia thread {thread_id}")
                return

            # Check if this thread is tracked in our database
            thread_data = self.state.get_discord_thread(thread_id)

            # If the thread is not tracked, and it's not a thread created by the bot, ignore it.
            # We assume threads created by the bot via /thread new are tracked.
            is_bot_created_thread = message.channel.owner_id == self.bot.user.id
            if not thread_data and not is_bot_created_thread:
                 logger.debug(f"Ignoring message in untracked thread {thread_id} not created by bot.")
                 return # Ignore messages in threads we don't manage

            # If it's a bot-created thread but not in DB (e.g., bot restarted), add it.
            if is_bot_created_thread and not thread_data:
                 logger.info(f"Bot-created thread {thread_id} found but not in DB. Adding.")
                 # Attempt to get thread name from Discord if possible
                 thread_name = message.channel.name if hasattr(message.channel, 'name') else "Untracked Bot Thread"
                 await self.state.add_discord_thread(thread_id, channel_id, thread_name)
                 thread_data = self.state.get_discord_thread(thread_id) # Re-fetch after adding

            # Add the incoming user message to the database history for this thread
            await self.state.add_discord_thread_message(thread_id, {
                "role": "user",
                "name": message.author.display_name,
                "user_id": str(message.author.id),
                "is_bot": message.author.bot,
                "content": resolve_discord_mentions(message),
                # timestamp is added in add_discord_thread_message
            })

            # Get conversation context from the database history for this thread,
            # bounded by the configured time window
            conversation_context = self.state.get_discord_thread_history(thread_id, hours_limit=self.state.get_time_window_hours())

            # Get thread-specific model and system prompt from DB
            thread_config = self.state.get_discord_thread_config(thread_id)
            thread_model = thread_config.get("model") if thread_config else None
            thread_system_prompt = thread_config.get("system_prompt") if thread_config else None

            # Determine the model to use (thread-specific > channel-specific > global)
            model_to_use = thread_model if thread_model else self.get_model_for_channel(channel_id)

            # Determine the system prompt to use (thread-specific > channel-specific > global).
            # A thread's own prompt still gets the multi-user framing, the parent
            # channel's memory and the notes on the people in this thread.
            system_prompt_to_use = self.state.get_effective_system_prompt(
                channel_id, query=message.content, speaker_id=str(message.author.id),
                history=conversation_context, base_prompt=thread_system_prompt)

            # Temporarily set the model for this request
            current_model = self.openrouter_client.model
            self.openrouter_client.model = model_to_use

            try:
                # Send "thinking" message
                thinking_msg = await message.channel.send(f"Thinking about: '{message.content[:50]}...'") # Truncate for thinking message

                # Process images if any are attached to the user message
                images = []
                model_supports_images = await self.openrouter_client.model_supports_vision(model_to_use)
                if model_supports_images and message.attachments:
                    for attachment in message.attachments:
                        if any(attachment.filename.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                            try:
                                image_data = await attachment.read()
                                images.append({
                                    'data': image_data,
                                    'type': attachment.content_type or 'image/jpeg'
                                })
                            except Exception as e:
                                logger.warning(f"Could not process image attachment {attachment.filename}: {e}")
                                # Continue without the image if processing fails

                # Get response from AI
                response = await self.openrouter_client.send_message_with_history(
                    conversation_context,
                    images=images, # Already empty unless the resolved model supports vision
                    system_prompt=system_prompt_to_use
                )

                # Add AI response to thread history in the database
                await self.state.add_discord_thread_message(thread_id, {
                    "role": "assistant",
                    "content": response,
                    # timestamp is set in the state manager/database
                })

                # Split response into chunks, turning '@Name' into real pings
                targets = collect_mention_targets(conversation_context, [message.author, *message.mentions])
                chunks = chunk_message(apply_outbound_mentions(response, targets))

                # Check if persona is active for the parent channel
                auto_parent_id = str(message.channel.parent_id) if isinstance(message.channel, discord.Thread) else str(message.channel.id)
                auto_persona_active = (hasattr(self.bot, 'webhook_sender')
                                       and self.bot.state_manager.get_effective_persona(auto_parent_id))

                # Update thinking message with first chunk or send via webhook
                if chunks:
                    if auto_persona_active:
                        await thinking_msg.delete()
                        for chunk in chunks:
                            await self.bot.webhook_sender.send_response(
                                message.channel, chunk, auto_parent_id
                            )
                    else:
                        await thinking_msg.edit(content=chunks[0])
                        for chunk in chunks[1:]:
                            await message.channel.send(chunk)
                else:
                    await thinking_msg.edit(content="Received an empty response from the AI.")


            except Exception as e:
                logger.error(f"Error processing thread message in thread {thread_id}: {e}", exc_info=True)
                # Attempt to edit thinking message with error, or send new message
                try:
                    await thinking_msg.edit(content=f"⚠️ Error processing message: {str(e)}")
                except Exception:
                    await message.channel.send(f"⚠️ Error processing message: {str(e)}")
            finally:
                # Restore original model
                self.openrouter_client.model = current_model


def setup(bot):
    bot.add_cog(ThreadCommands(bot))