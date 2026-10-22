"""Functionality for responding to @mentions in messages."""
import discord
import asyncio
import logging
import io
import json
from discord.ext import commands
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from ..utils import budget
from ..utils.conversation_service import Conversation, ConversationService, ReplyRequest
from ..utils.llm_result import call_chat
from ..utils.memory_service import check_and_rotate_session, llm_clients, schedule_compaction
from ..utils.timeutil import to_epoch
from ..utils.web_search import SEARCHING_STATUS, build_search_system_prompt
from ..utils.llm_formatting import describe_message, resolve_discord_mentions
from ..config import DEFAULT_MODEL

logger = logging.getLogger('mention_commands')


class MentionCommands(commands.Cog):
    """Handles responses when the bot is @mentioned in messages."""

    def __init__(self, bot):
        self.bot = bot
        self.state = bot.state_manager
        self.service = getattr(bot, 'conversation_service', None) or ConversationService(bot)
        # Channels the bot participates in — history is only recorded for
        # these, so the bot doesn't log every channel it can merely see.
        self._active_channels: Optional[set] = None
        # Message id -> timestamp its history row was stored with. Replies
        # read history up to that point only, so messages that land while a
        # reply is in flight can't be mistaken for the turn being answered.
        self._history_anchors: Dict[int, datetime] = {}

    @property
    def clients(self) -> Dict[str, Any]:
        """Provider clients, read live so dashboard key rotation applies."""
        return llm_clients(self.bot)

    def get_model_for_channel(self, channel_id):
        """Get the appropriate model for this channel"""
        if not self.state:
            logger.error("[Mention] State manager not available in MentionCommands.")
            return DEFAULT_MODEL
        return self.state.get_effective_model(channel_id)

    def _strip_mentions(self, content: str) -> str:
        """Removes the bot's mention tokens from message content."""
        content = content.replace(f'<@{self.bot.user.id}>', '').replace(f'<@!{self.bot.user.id}>', '')
        return content.strip()

    def _history_for(self, message: discord.Message, channel_id: str) -> List[Dict[str, Any]]:
        """Conversation history (channel or thread) as of the message being answered."""
        return self.service.history(self._conversation(message, channel_id),
                                    until=self._history_anchors.get(message.id))

    async def _send_reply(self, message: discord.Message, channel_id: str,
                          text: str, history: List[Dict[str, Any]]):
        """Sends an LLM reply in chunks, turning '@Name' into real pings."""
        await self.service.send(self._conversation(message, channel_id), text, history,
                                [message.author, *message.mentions])

    def _conversation(self, message: discord.Message, channel_id: str) -> Conversation:
        """The conversation a message belongs to (a thread keeps its own history)."""
        if isinstance(message.channel, discord.Thread):
            return Conversation(channel=message.channel, channel_id=str(message.channel.parent_id),
                                thread_id=str(message.channel.id))
        return Conversation(channel=message.channel, channel_id=channel_id)

    # ─── History recording guard ────────────────────────────────────────────

    def _should_record(self, channel_id: str, is_mentioned: bool) -> bool:
        """Records history only for channels the bot participates in.

        A channel becomes active on the first mention (or if it already has
        stored history or configuration). This avoids logging every message
        in every channel the bot can see.
        """
        if self._active_channels is None:
            self._active_channels = self._load_active_channels()
        if is_mentioned:
            self._active_channels.add(channel_id)
            return True
        return channel_id in self._active_channels

    def _load_active_channels(self) -> set:
        """Seeds the active-channel set from channels with history or config."""
        active = set()
        try:
            db = self.state.db_manager
            active.update(db.messages.get_distinct_channel_ids())
            # User threads and forum posts Gideon has joined
            active.update(db.messages.get_distinct_untracked_thread_ids())
            active.update(db.get_all_configured_channel_ids())
            for persona in db.get_all_channel_personas():
                if persona.get('channel_id'):
                    active.add(persona['channel_id'])
        except Exception as e:
            logger.error(f"[Mention] Failed to load active channels: {e}", exc_info=True)
        logger.info(f"[Mention] Recording history for {len(active)} active channel(s)")
        return active

    # ─── Shared conversational reply ────────────────────────────────────────

    async def respond_conversationally(self, message: discord.Message, channel_id: str,
                                       user_content: Optional[str] = None, prefix: str = ""):
        """Answers a message with a plain LLM reply, optionally prefixed.

        Used as the fallback path when a tool/search flow fails, so the user
        still gets a conversational response with the error context.
        """
        content = user_content if user_content is not None else (self._strip_mentions(message.content) or "Hello!")
        conversation = self._conversation(message, channel_id)

        provider, model_name, client_to_use = self.service.resolve(conversation)
        if client_to_use is None:
            await message.channel.send(f"{prefix}Additionally, the conversation system is unavailable.")
            return

        conversation_context = self._history_for(message, channel_id)
        channel_system_prompt = self.service.system_prompt(
            conversation, content, str(message.author.id), conversation_context)
        conversation_context.append({
            "role": "user",
            "name": message.author.display_name,
            "content": content
        })

        try:
            async with message.channel.typing():
                result = await self.service.generate(
                    client_to_use, model_name, conversation_context, channel_system_prompt)
            if not result.ok:
                await message.channel.send(f"{prefix}Additionally, failed to generate a conversation response.")
                return

            full_response = f"{prefix}{result.text}"

            await self.service.record(conversation, {"role": "assistant", "content": full_response})

            await self._send_reply(message, channel_id, full_response, conversation_context)

        except Exception as e:
            logger.exception(f"[Mention] Error in fallback conversation: {e}")
            await message.channel.send(f"{prefix}Additionally, failed to generate a conversation response.")

    # ─── Side-effect tool handlers ──────────────────────────────────────────

    async def handle_reminder_request(
        self,
        message: discord.Message,
        channel_id: str,
        reminder_message: str,
        time_expression: str
    ):
        """
        Handle a reminder request detected from a mention.

        Args:
            message: Original Discord message object
            channel_id: Channel ID as string
            reminder_message: What to remind about
            time_expression: Natural language time expression
        """
        # Validate inputs
        if not reminder_message or not reminder_message.strip():
            await message.channel.send(
                "❌ I detected you want a reminder, but I'm not sure what to remind you about. "
                "Please try something like: '@Gideon remind me in 2 hours to check the oven'"
            )
            logger.warning(f"[Tool] Missing reminder message for user {message.author.id}")
            return

        if not time_expression or not time_expression.strip():
            await message.channel.send(
                "❌ I detected you want a reminder, but I'm not sure when. "
                "Please specify a time like 'in 2 hours', 'tomorrow at 3pm', or 'at 5pm'"
            )
            logger.warning(f"[Tool] Missing time expression for user {message.author.id}")
            return

        # Get ReminderCommands cog
        reminder_cog = self.bot.get_cog('ReminderCommands')
        if not reminder_cog:
            await message.channel.send("⚠️ Reminder system not available.")
            logger.error("[Tool] ReminderCommands cog not found")
            return

        # Parse time using existing method
        try:
            parsed_time = await reminder_cog.parse_time_with_ai(time_expression, channel_id)
        except Exception as e:
            logger.error(f"[Tool] Error parsing time: {e}", exc_info=True)
            await message.channel.send(
                f"❌ Failed to parse time expression '{time_expression}'. "
                f"Please try something like 'in 2 hours', 'tomorrow at 3pm', or 'at 5pm'"
            )
            return

        if parsed_time is None:
            await message.channel.send(
                f"❌ I couldn't understand the time '{time_expression}'. "
                f"Please try something like 'in 2 hours', 'tomorrow at 3pm', or 'at 5pm'"
            )
            logger.warning(f"[Tool] Failed to parse time expression: {time_expression}")
            return

        # Validate time is not too far in the future (max 1 year)
        max_future = datetime.now() + timedelta(days=365)
        if parsed_time > max_future:
            await message.channel.send(
                "❌ Reminder time is too far in the future (max 1 year)."
            )
            logger.warning(f"[Tool] Reminder time too far in future: {parsed_time}")
            return

        # Save reminder to database
        try:
            user_id = str(message.author.id)
            reminder_id = self.state.add_reminder(
                user_id=user_id,
                channel_id=channel_id,
                message=reminder_message,
                due_timestamp=parsed_time
            )

            # Discord timestamp (shows in each user's own timezone)
            discord_timestamp = to_epoch(parsed_time)

            # Format confirmation matching /remind command
            confirmation_message = (
                f"✅ Reminder set!\n"
                f"**Message:** {reminder_message}\n"
                f"**When:** <t:{discord_timestamp}:F> (<t:{discord_timestamp}:R>)\n"
                f"**Reminder ID:** {reminder_id}"
            )

            # Send confirmation
            await message.channel.send(confirmation_message)

            # Add confirmation to channel history
            await self.state.add_to_channel_history(channel_id, {
                "role": "assistant",
                "content": confirmation_message,
                "timestamp": datetime.now()
            })

            logger.info(f"[Tool] User {user_id} set reminder {reminder_id} for {parsed_time} via mention")

        except Exception as e:
            logger.exception(f"[Tool] Error setting reminder: {e}")
            await message.channel.send(f"❌ Failed to save reminder: {str(e)}")

    async def handle_image_generation_request(
        self,
        message: discord.Message,
        channel_id: str,
        prompt: str,
        negative_prompt: str = "",
        size: str = "",
        quality: str = "",
        style: str = ""
    ):
        """
        Handle an image generation request detected from a mention.

        Args:
            message: Original Discord message object
            channel_id: Channel ID as string
            prompt: Image description/prompt
            negative_prompt: What to exclude from the image
            size: Requested size (e.g., "512x512")
            quality: Quality setting for OpenAI ("hd" or "standard")
            style: Style setting for OpenAI ("vivid" or "natural")
        """
        # Validate prompt
        if not prompt or not prompt.strip():
            await message.channel.send(
                "❌ I detected you want to generate an image, but I'm not sure what to create. "
                "Please try something like: '@Gideon draw a sunset over mountains'"
            )
            logger.warning(f"[Tool] Missing image prompt for user {message.author.id}")
            return

        limit_error = budget.generation_limit_error(self.state, message.author, budget.IMAGE, self.bot)
        if limit_error:
            await message.channel.send(f"⛔ {limit_error}")
            return

        # Get UnifiedImageCommands cog
        image_cog = self.bot.get_cog('UnifiedImageCommands')
        if not image_cog:
            await self.respond_conversationally(
                message, channel_id,
                prefix="Image generation failed. The image generation system is not available. "
            )
            logger.error("[Tool] UnifiedImageCommands cog not found")
            return

        # Get active provider and config from database
        try:
            from ..cogs.unified_image_commands import DEFAULT_CONFIGS

            active_provider = image_cog.db.get_global_config("image_active_provider", "ai_horde")
            config_key = f"image_config_{active_provider}"
            config_json = image_cog.db.get_global_config(config_key)

            # Parse provider config
            if config_json:
                try:
                    provider_config = json.loads(config_json)
                except json.JSONDecodeError:
                    logger.warning(f"[Tool] Failed to parse config for {active_provider}, using defaults")
                    provider_config = DEFAULT_CONFIGS.get(active_provider, {})
            else:
                provider_config = DEFAULT_CONFIGS.get(active_provider, {})

        except Exception as e:
            await self.respond_conversationally(
                message, channel_id,
                prefix="Image generation failed. Could not retrieve image provider configuration. "
            )
            logger.error(f"[Tool] Error getting image provider config: {e}")
            return

        # Select and validate client
        client = None
        provider_display_name = "Unknown"
        params = {"prompt": prompt}

        if active_provider == 'ai_horde':
            client = image_cog.horde_client
            provider_display_name = "AI Horde"
            if client:
                # Parse size or use default
                target_size = size if size else provider_config.get("size", "512x512")
                try:
                    width, height = map(int, target_size.split('x'))
                    width = round(width / 64) * 64  # Ensure multiple of 64
                    height = round(height / 64) * 64
                except (ValueError, AttributeError):
                    width, height = 512, 512
                    logger.warning(f"[Tool] Invalid size '{target_size}', using 512x512")

                params.update({
                    "negative_prompt": negative_prompt,
                    "width": width,
                    "height": height,
                    "steps": provider_config.get("steps", 30),
                    "model": provider_config.get("model", "stable_diffusion_xl")
                })

        elif active_provider == 'cloudflare':
            client = image_cog.cf_client
            provider_display_name = "Cloudflare"
            if client:
                target_size = size if size else provider_config.get("size", "768x768")
                try:
                    width, height = map(int, target_size.split('x'))
                except (ValueError, AttributeError):
                    width, height = 768, 768
                    logger.warning(f"[Tool] Invalid size '{target_size}', using 768x768")

                params.update({
                    "negative_prompt": negative_prompt,
                    "width": width,
                    "height": height,
                    "steps": provider_config.get("steps", 25),
                    "seed": provider_config.get("seed")
                })

        elif active_provider == 'openai':
            client = image_cog.openai_client
            provider_display_name = "OpenAI"
            if client:
                # Map quality variations to OpenAI values
                if quality:
                    quality_lower = quality.lower()
                    if "hd" in quality_lower or "high" in quality_lower:
                        openai_quality = "hd"
                    else:
                        openai_quality = "standard"
                else:
                    openai_quality = provider_config.get("quality", "standard")

                # Map style to OpenAI values
                openai_style = None
                if style:
                    style_lower = style.lower()
                    if "vivid" in style_lower:
                        openai_style = "vivid"
                    elif "natural" in style_lower:
                        openai_style = "natural"
                else:
                    openai_style = provider_config.get("style", "vivid")

                params.update({
                    "model": provider_config.get("model", "dall-e-3"),
                    "size": "1024x1024",  # Fixed for OpenAI
                    "quality": openai_quality if provider_config.get("model", "dall-e-3") == "dall-e-3" else None,
                    "style": openai_style if provider_config.get("model", "dall-e-3") == "dall-e-3" else None
                })
                # Note: OpenAI doesn't support negative_prompt, so we ignore it

        elif active_provider == 'comfyui':
            client = image_cog.comfyui_client
            provider_display_name = "ComfyUI"
            if client:
                target_size = size if size else provider_config.get("size", "512x512")
                try:
                    width, height = map(int, target_size.split('x'))
                except (ValueError, AttributeError):
                    width, height = 512, 512
                    logger.warning(f"[Tool] Invalid size '{target_size}', using 512x512")

                params.update({
                    "negative_prompt": negative_prompt or "",
                    "width": width,
                    "height": height,
                    "steps": provider_config.get("steps", 20),
                    "model": provider_config.get("model"),
                    "seed": provider_config.get("seed"),
                    "workflow_json": provider_config.get("workflow")
                })

        elif active_provider == 'openrouter':
            client = image_cog.openrouter_image_client
            provider_display_name = "OpenRouter"
            if client:
                params.update({
                    "model": provider_config.get("model", "google/gemini-2.0-flash-exp"),
                    "aspect_ratio": provider_config.get("aspect_ratio", "1:1"),
                    "image_size": provider_config.get("image_size", "1K")
                })
                if negative_prompt:
                    params["negative_prompt"] = negative_prompt
                # Load modalities from database config (allows dashboard override)
                modalities_json = image_cog.db.get_global_config('image_openrouter_modalities')
                if modalities_json:
                    try:
                        params["modalities"] = json.loads(modalities_json)
                    except json.JSONDecodeError:
                        pass  # Fall back to client default ["image", "text"]

        if not client:
            await self.respond_conversationally(
                message, channel_id,
                prefix=f"Image generation failed. The {provider_display_name} client is not configured. "
            )
            logger.error(f"[Tool] {provider_display_name} client not available")
            return

        # Send "generating" message with typing indicator
        async with message.channel.typing():
            thinking_msg = await message.channel.send(
                f"🎨 Generating image with **{provider_display_name}**: `{prompt}`\n*Please wait...*"
            )

        # Call generate_image
        try:
            result = await client.generate_image(**params)
        except Exception as e:
            logger.exception(f"[Tool] Exception during image generation with {active_provider}: {e}")
            await thinking_msg.delete()
            await self.respond_conversationally(
                message, channel_id,
                prefix=f"Image generation failed. An unexpected error occurred: {str(e)}. "
            )
            return

        # Handle result
        if result.get("success"):
            budget.record_generation(self.state, budget.IMAGE, message.author.id, channel_id,
                                     provider=active_provider)
            # Build embed
            embed = discord.Embed(
                title="Generated Image",
                description=f"**Prompt:** {prompt}",
                color=discord.Color.blue()
            )

            if negative_prompt and active_provider != 'openai':
                embed.add_field(name="Negative Prompt", value=negative_prompt, inline=False)

            # Footer with metadata
            footer_parts = [f"Provider: {provider_display_name}"]
            if result.get("model_used"):
                footer_parts.append(f"Model: {result.get('model_used')}")
            if result.get("seed"):
                footer_parts.append(f"Seed: {result.get('seed')}")
            if params.get("steps"):
                footer_parts.append(f"Steps: {params.get('steps')}")
            if params.get("size"):
                footer_parts.append(f"Size: {params.get('size')}")
            elif params.get("width"):
                footer_parts.append(f"Size: {params.get('width')}x{params.get('height')}")

            embed.set_footer(text=" | ".join(footer_parts))

            if result.get("revised_prompt"):
                embed.add_field(name="Revised Prompt (DALL-E 3)", value=result["revised_prompt"], inline=False)

            # Send image
            if "image_url" in result:
                embed.set_image(url=result["image_url"])
                await thinking_msg.edit(content=None, embed=embed)
            elif "image_data" in result:
                # ComfyUI returns raw bytes - create Discord file from memory
                file = discord.File(
                    io.BytesIO(result["image_data"]),
                    filename="generated_image.png"
                )
                embed.set_image(url="attachment://generated_image.png")
                await thinking_msg.delete()
                await message.channel.send(embed=embed, file=file)
            elif "local_path" in result:
                file = discord.File(result["local_path"], filename="generated_image.png")
                embed.set_image(url="attachment://generated_image.png")
                await thinking_msg.delete()
                await message.channel.send(embed=embed, file=file)
            else:
                await thinking_msg.delete()
                await self.respond_conversationally(
                    message, channel_id,
                    prefix="Image generation failed. No image data returned. "
                )
                return

            # Add to conversation history (assistant response)
            await self.state.add_to_channel_history(channel_id, {
                "role": "assistant",
                "content": f"[Generated image: {prompt}]",
                "timestamp": datetime.now()
            })

            logger.info(f"[Tool] User {message.author.id} generated image via mention: '{prompt}' using {provider_display_name}")

        else:
            # Generation failed
            error_msg = result.get("error", "Unknown error")
            await thinking_msg.delete()
            await self.respond_conversationally(
                message, channel_id,
                prefix=f"Image generation failed. {error_msg}. "
            )
            logger.warning(f"[Tool] Image generation failed for user {message.author.id}: {error_msg}")

    async def handle_search_request(
        self,
        message: discord.Message,
        channel_id: str,
        query: str,
        history: Optional[List[Dict[str, Any]]] = None
    ) -> str:
        """
        Run a web search for the native tool-calling path.

        Unlike /search (which posts a results embed), this posts nothing
        except a temporary status message: the results are returned to the
        tool loop so the model can weave them into its single, normal reply.
        Errors are likewise returned for the model to relay.

        Args:
            message: Original Discord message object
            channel_id: Channel ID as string
            query: The search query

        Returns:
            The tool-result text to feed back to the model: the actual
            search results on success (so the model can synthesize its
            reply without searching again), or an explanation of why the
            search did not run.
        """
        # Validate query
        if not query or not query.strip():
            logger.warning(f"[Tool] Missing search query for user {message.author.id}")
            return "Error: no search query was provided. Ask the user what to search for."

        # Get current provider and model
        provider, model_name = self.state.resolve_model(channel_id)

        # Check if provider supports web search (OpenRouter only)
        if provider != "openrouter":
            logger.info(f"[Tool] Search requested but provider {provider} doesn't support it")
            return (f"Web search requires OpenRouter (current provider: {provider}), "
                    f"so no search was performed. Answer from your own knowledge and "
                    f"mention that live search was unavailable.")

        client_to_use = self.clients.get("openrouter")
        if not client_to_use:
            logger.error("[Tool] OpenRouter client not found")
            return ("Error: the OpenRouter client is not available, so the search "
                    "did not run.")

        # The conversation the tool was called from (a thread's own history in
        # threads), else this channel's history up to the message
        conversation_context = list(history) if history is not None else self._history_for(message, channel_id)

        channel_system_prompt = self.state.get_effective_system_prompt(
            channel_id, query=query, speaker_id=str(message.author.id), history=conversation_context)
        search_system_prompt = build_search_system_prompt(channel_system_prompt)
        conversation_context.append({
            "role": "user",
            "name": message.author.display_name,
            "content": query
        })

        # Temporary status message so the channel sees search activity
        search_msg = await message.channel.send(SEARCHING_STATUS.format(query=query))

        try:
            result = await call_chat(
                client_to_use,
                messages=conversation_context,
                model=model_name,
                system_prompt=search_system_prompt,
                web_search=True
            )
        except Exception as e:
            logger.exception(f"[Tool] Error during web search: {e}")
            return ("Error: the web search failed. Do not retry the same search; "
                    "apologise briefly and answer from your own knowledge.")
        finally:
            try:
                await search_msg.delete()
            except discord.HTTPException:
                pass

        if not result.ok:
            logger.warning(f"[Tool] Web search failed ({result.error_kind}): {result.detail}")
            return (f"Error: the web search failed ({result.error}). Do not retry the "
                    f"same search; apologise briefly and answer from your own knowledge.")
        response = result.text
        if not response.strip():
            logger.warning("[Tool] Web search returned no usable results")
            return ("Error: the web search returned no results. Do not retry the "
                    "same search; apologise briefly and answer from your own knowledge.")

        logger.info(f"[Tool] User {message.author.id} performed web search via mention: '{query}'")

        return (f"Web search results for '{query}'. The user has NOT seen these — "
                f"use them to write your reply, keeping any source citations that "
                f"matter:\n\n{response}")

    # ─── Message listener ───────────────────────────────────────────────────

    def _is_bot_mentioned(self, message: discord.Message, channel_id: Optional[str] = None) -> bool:
        """Checks @everyone/@here, user mentions, raw content, and same-named role mentions."""
        # Gideon is part of everyone. Discord only sets this when the author
        # was allowed to ping everyone, so typed-but-inert "@everyone" is
        # ignored. Admins can switch this off globally or per channel.
        if message.mention_everyone and self._responds_to_everyone(channel_id or str(message.channel.id)):
            return True

        for mention in message.mentions:
            if mention.id == self.bot.user.id:
                return True

        if f'<@{self.bot.user.id}>' in message.content or f'<@!{self.bot.user.id}>' in message.content:
            return True

        # Role mentions matching the bot's name (common confusion)
        if message.role_mentions:
            bot_name_lower = self.bot.user.name.lower() if self.bot.user.name else ""
            for role in message.role_mentions:
                if role.name.lower() == bot_name_lower:
                    logger.info(f"[Mention] Bot triggered via role mention '{role.name}' by {message.author.id}")
                    return True

        return False

    def _responds_to_everyone(self, channel_id: str) -> bool:
        getter = getattr(self.state, "get_effective_respond_to_everyone", None)
        if getter is None:
            return True
        try:
            return bool(getter(channel_id))
        except Exception as e:
            logger.error(f"[Mention] Could not read respond_to_everyone for {channel_id}: {e}")
            return True

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
            logger.error(f"[Mention] Error checking persona webhook for channel {channel_id}: {e}", exc_info=True)
            return False

        return bool(persona) and str(message.webhook_id) == str(persona.get('webhook_id') or '')

    def _is_gideon_message(self, message, channel_id: str) -> bool:
        """True for a message Gideon wrote (bot account or its persona webhook)."""
        author = getattr(message, "author", None)
        if author is not None and getattr(author, "id", None) == self.bot.user.id:
            return True
        return bool(getattr(message, "webhook_id", None)) and self._is_own_persona_webhook(message, channel_id)

    async def _referenced_message(self, message, fetch: bool):
        """The message this one replies to, if any (fetched only when ``fetch``)."""
        reference = getattr(message, "reference", None)
        if reference is None or getattr(reference, "message_id", None) is None:
            return None
        # Forwards carry their content in message.snapshots instead
        if "forward" in str(getattr(reference, "type", "")).lower():
            return None
        resolved = getattr(reference, "resolved", None) or getattr(reference, "cached_message", None)
        if isinstance(resolved, discord.DeletedReferencedMessage):
            return None
        if resolved is None and fetch:
            try:
                resolved = await message.channel.fetch_message(reference.message_id)
            except (discord.HTTPException, AttributeError):
                return None
        return resolved

    def _thread_is_managed(self, thread) -> bool:
        """AI threads (and trivia threads) are answered by their own cogs."""
        thread_id = str(thread.id)
        try:
            if self.state.get_discord_thread(thread_id):
                return True
            if self.state.get_active_trivia_session(thread_id):
                return True
        except Exception as e:
            logger.error(f"[Mention] Could not check thread {thread_id}: {e}")
            return True
        return getattr(thread, "owner_id", None) == self.bot.user.id

    @commands.Cog.listener()
    async def on_message(self, message):
        """Records conversation in active channels/threads and answers when addressed."""
        # Ignore messages from the bot itself
        if message.author == self.bot.user:
            return

        # User-created threads and forum posts are handled here (on mention,
        # in the thread's own history); AI threads belong to ThreadCommands
        thread_id = None
        if isinstance(message.channel, discord.Thread):
            if self._thread_is_managed(message.channel):
                return
            thread_id = str(message.channel.id)
            channel_id = str(message.channel.parent_id)
        else:
            channel_id = str(message.channel.id)

        # Persona replies go out through a webhook, so they aren't authored by
        # bot.user and would otherwise be recorded as if a human had said them
        if self._is_own_persona_webhook(message, channel_id):
            return

        is_mentioned = self._is_bot_mentioned(message, channel_id)
        referenced = await self._referenced_message(message, fetch=is_mentioned)
        # Replying to Gideon addresses it, even with the reply ping turned off
        if not is_mentioned and referenced is not None and self._is_gideon_message(referenced, channel_id):
            is_mentioned = True

        conversation = Conversation(channel=message.channel, channel_id=channel_id, thread_id=thread_id)
        recorded = False
        recorded_at = None

        # Only record history for channels (and threads) the bot participates in
        if self.state and self._should_record(thread_id or channel_id, is_mentioned):
            entry = {
                "role": "user",
                "name": message.author.display_name,
                "user_id": str(message.author.id),
                "is_bot": message.author.bot,
                "content": describe_message(message, referenced),
            }
            if thread_id:
                try:
                    recorded_at = await self.service.record(conversation, entry)
                except Exception as e:
                    logger.error(f"[Mention] Error adding message to thread {thread_id}: {e}", exc_info=True)
            else:
                # Check for session expiry and rotate before recording the new message
                try:
                    await check_and_rotate_session(channel_id, self.state, self.clients)
                except Exception as e:
                    logger.error(f"[Mention] Error during session rotation for channel {channel_id}: {e}", exc_info=True)

                try:
                    recorded_at = await self.state.add_to_channel_history(
                        channel_id, {**entry, "timestamp": datetime.now()})
                except Exception as e:
                    logger.error(f"[Mention] Error adding message to history for channel {channel_id}: {e}", exc_info=True)

                # Compact oversized histories into a memory summary so long-running
                # sessions don't silently lose context off the end of the window.
                # Runs in the background so the summarization call never delays a reply.
                schedule_compaction(channel_id, self.state, self.clients)
            recorded = recorded_at is not None

        # Other bots and webhooks are kept as context but never answered,
        # so two bots can't trigger each other in an endless loop
        if not is_mentioned or message.author.bot or message.webhook_id:
            return

        if recorded:
            self._history_anchors[message.id] = recorded_at
        try:
            await self._respond_to_mention(message, conversation, recorded, referenced)
        finally:
            self._history_anchors.pop(message.id, None)

    async def _respond_to_mention(self, message: discord.Message, conversation: Conversation,
                                  recorded: bool, referenced=None):
        """Generates and sends the reply to a message that mentioned the bot."""
        # Ensure state manager is available before proceeding
        if not self.state:
            logger.error("[Mention] State manager not available when processing mention.")
            await message.channel.send("⚠️ Internal error: State manager not available.")
            return

        logger.info(f"[Mention] Detected mention from {message.author.id} in "
                    f"{conversation.thread_id or conversation.channel_id}")

        content = self._strip_mentions(describe_message(message, referenced)) or "Hello!"
        try:
            # Mentions are recorded in on_message, so history already ends
            # with this turn. Anchoring matters: without it, messages from
            # other users that arrive while this reply is in flight end up
            # after the mention, and the model answers them instead of the
            # person who summoned it.
            await self.service.respond(ReplyRequest(
                conversation=conversation,
                author=message.author,
                content=content,
                message=message,
                attachments=list(message.attachments or []),
                history_until=self._history_anchors.get(message.id),
                recorded=recorded,
                feature="mention",
            ))
        except Exception as e:
            logger.exception(f"[Mention] Error processing mention in {conversation.channel_id}: {e}")
            try:
                await message.channel.send("⚠️ Something went wrong while I was answering. Please try again.")
            except Exception as followup_e:
                logger.error(f"[Mention] Failed to send error message: {followup_e}")


def setup(bot):
    # Ensure state_manager is available on bot before adding cog
    if not hasattr(bot, 'state_manager'):
        logger.error("State manager not found on bot object. Cannot load MentionCommands cog.")
        return
    if not getattr(bot, 'openrouter_client', None):
        logger.warning("OpenRouter client not found on bot object. MentionCommands might have limited functionality.")

    try:
        bot.add_cog(MentionCommands(bot))
        logger.info("MentionCommands cog loaded successfully.")
    except Exception as e:
        logger.exception(f"Failed to initialize or add MentionCommands cog: {e}")
