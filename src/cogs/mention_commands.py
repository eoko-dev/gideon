"""Functionality for responding to @mentions in messages."""
import discord
import asyncio
import logging
import io
import json
from discord.ext import commands
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from ..utils.memory_service import check_and_rotate_session, schedule_compaction
from ..utils.discord_fmt import chunk_message
from ..utils.timeutil import to_epoch
from ..utils.tool_registry import get_tool_definitions, execute_tool, is_no_repeat_tool
from ..utils.web_search import SEARCHING_STATUS, build_search_system_prompt
from ..utils.llm_formatting import resolve_discord_mentions, collect_mention_targets, apply_outbound_mentions
from ..config import DEFAULT_MODEL

logger = logging.getLogger('mention_commands')


class MentionCommands(commands.Cog):
    """Handles responses when the bot is @mentioned in messages."""

    def __init__(self, bot):
        self.bot = bot
        self.state = bot.state_manager
        self.clients = {
            "openrouter": getattr(bot, 'openrouter_client', None),
            "openai": getattr(bot, 'openai_client', None),
            "ai_horde": getattr(bot, 'ai_horde_client', None)
        }
        # Channels the bot participates in — history is only recorded for
        # these, so the bot doesn't log every channel it can merely see.
        self._active_channels: Optional[set] = None
        # Message id -> timestamp its history row was stored with. Replies
        # read history up to that point only, so messages that land while a
        # reply is in flight can't be mistaken for the turn being answered.
        self._history_anchors: Dict[int, datetime] = {}

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
        """Channel history as of the message being answered."""
        return self.state.get_channel_history(channel_id, until=self._history_anchors.get(message.id))

    async def _send_reply(self, message: discord.Message, channel_id: str,
                          text: str, history: List[Dict[str, Any]]):
        """Sends an LLM reply in chunks, turning '@Name' into real pings."""
        text = apply_outbound_mentions(text, collect_mention_targets(history, [message.author, *message.mentions]))
        for chunk in chunk_message(text):
            await self.bot.webhook_sender.send_response(message.channel, chunk, channel_id)

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

        provider, model_name = self.state.resolve_model(channel_id)
        client_to_use = self.clients.get(provider)
        if not client_to_use or not hasattr(client_to_use, 'send_message_with_history'):
            await message.channel.send(f"{prefix}Additionally, the conversation system is unavailable.")
            return

        conversation_context = self._history_for(message, channel_id)
        channel_system_prompt = self.state.get_effective_system_prompt(
            channel_id, query=content, speaker_id=str(message.author.id), history=conversation_context)
        conversation_context.append({
            "role": "user",
            "name": message.author.display_name,
            "content": content
        })

        try:
            async with message.channel.typing():
                response = await client_to_use.send_message_with_history(
                    messages=conversation_context,
                    model=model_name,
                    system_prompt=channel_system_prompt
                )

            full_response = f"{prefix}{response}"

            await self.state.add_to_channel_history(channel_id, {
                "role": "assistant",
                "content": full_response,
                "timestamp": datetime.now()
            })

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
        query: str
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

        # Provider is OpenRouter, proceed with web search
        client_to_use = self.clients.get("openrouter")
        if not client_to_use:
            logger.error("[Tool] OpenRouter client not found")
            return ("Error: the OpenRouter client is not available, so the search "
                    "did not run.")

        # Get conversation context
        conversation_context = self._history_for(message, channel_id)

        # Enhance system prompt for search
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

        # Perform search
        try:
            response = await client_to_use.send_message_with_history(
                messages=conversation_context,
                model=model_name,
                system_prompt=search_system_prompt,
                web_search=True
            )
        except Exception as e:
            logger.exception(f"[Tool] Error during web search: {e}")
            return (f"Error: the web search failed ({e}). Do not retry the same "
                    f"search; apologise briefly and answer from your own knowledge.")
        finally:
            try:
                await search_msg.delete()
            except discord.HTTPException:
                pass

        # The client reports provider failures (rate limits, API errors) as
        # "⚠️ ..." strings rather than raising, and can return None for a
        # null-content completion — neither must be dressed up as results.
        if not isinstance(response, str) or not response.strip():
            logger.warning(f"[Tool] Web search returned no usable results: {response!r}")
            return (f"Error: the web search returned no results. Do not retry the "
                    f"same search; apologise briefly and answer from your own knowledge.")
        if response.startswith("⚠️"):
            logger.warning(f"[Tool] Web search returned a provider error: {response}")
            return (f"Error: the web search failed with a provider error: {response}\n"
                    f"Do not retry the same search; apologise briefly and answer "
                    f"from your own knowledge.")

        logger.info(f"[Tool] User {message.author.id} performed web search via mention: '{query}'")

        return (f"Web search results for '{query}'. The user has NOT seen these — "
                f"use them to write your reply, keeping any source citations that "
                f"matter:\n\n{response}")

    # ─── Native tool-calling loop ───────────────────────────────────────────

    async def _run_tool_loop(
        self,
        client,
        conversation_context: List[Dict[str, Any]],
        model_name: str,
        system_prompt: Optional[str],
        images: Optional[List[Dict[str, Any]]],
        tool_context: Dict[str, Any]
    ) -> Optional[str]:
        """Runs the model with tools, feeding results back until it produces text.

        Iterates up to the configured budget; each round's tool results are
        appended to the conversation so the model can synthesize an answer,
        chain tools, or recover from a tool error. Returns the final text
        response (possibly an error string starting with ⚠️).
        """
        tools = get_tool_definitions()
        max_iterations = self.state.get_tool_calling_max_iterations()
        executed_calls: set = set()

        for iteration in range(max_iterations):
            response = await client.send_message_with_history(
                messages=conversation_context,
                model=model_name,
                system_prompt=system_prompt,
                images=images if iteration == 0 else None,
                tools=tools,
                tool_choice="auto"
            )

            # Plain text (normal reply or provider error string)
            if isinstance(response, str):
                return response

            if not isinstance(response, dict):
                logger.error(f"[Tool] Unexpected response type from client: {type(response)}")
                return None

            tool_calls = response.get("tool_calls")
            if not tool_calls:
                return response.get("content") or ""

            logger.info(f"[Tool] Iteration {iteration + 1}/{max_iterations}: model requested {len(tool_calls)} tool call(s)")

            # Record the assistant's tool-call turn
            conversation_context.append({
                "role": "assistant",
                "content": response.get("content") or "",
                "tool_calls": tool_calls
            })

            # Execute each tool call and feed the result back
            for tc in tool_calls:
                func_name = tc.get("function", {}).get("name", "")
                func_args_str = tc.get("function", {}).get("arguments", "{}")
                tool_call_id = tc.get("id", "")

                try:
                    func_args = json.loads(func_args_str) if func_args_str else {}
                except json.JSONDecodeError as e:
                    logger.error(f"[Tool] Failed to parse tool arguments for '{func_name}': {e}")
                    func_args = {}

                # Side-effect tools (search, image, reminder, poll, event)
                # must not run twice with identical arguments in one reply —
                # a repeat means the model ignored the earlier result, so
                # point it back at that instead of re-posting to Discord.
                call_key = ((func_name, json.dumps(func_args, sort_keys=True))
                            if is_no_repeat_tool(func_name) else None)
                if call_key and call_key in executed_calls:
                    logger.warning(f"[Tool] Skipping duplicate '{func_name}' call with identical arguments")
                    tool_result = (f"Duplicate call skipped: '{func_name}' already ran with these exact "
                                   f"arguments during this reply. Use its earlier result above to answer "
                                   f"the user now — do not call it again.")
                else:
                    tool_result = await execute_tool(func_name, func_args, tool_context)
                    # Record only successful runs so a transient failure can
                    # still be retried within the same reply
                    if call_key and not tool_result.startswith("Error"):
                        executed_calls.add(call_key)

                conversation_context.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": tool_result
                })

        # Iteration budget exhausted — force a text answer (no tools offered)
        logger.warning(f"[Tool] Iteration budget ({max_iterations}) exhausted; forcing text response")
        response = await client.send_message_with_history(
            messages=conversation_context,
            model=model_name,
            system_prompt=system_prompt
        )
        if isinstance(response, dict):
            return response.get("content") or ""
        return response

    # ─── Message listener ───────────────────────────────────────────────────

    def _is_bot_mentioned(self, message: discord.Message) -> bool:
        """Checks user mentions, raw content, and same-named role mentions."""
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

    @commands.Cog.listener()
    async def on_message(self, message):
        """Listen for messages in channels and respond to @mentions."""
        # Ignore messages from the bot itself
        if message.author == self.bot.user:
            return

        # Ignore messages in threads as they're handled by ThreadCommands
        if isinstance(message.channel, discord.Thread):
            return

        channel_id = str(message.channel.id)

        # Persona replies go out through a webhook, so they aren't authored by
        # bot.user and would otherwise be recorded as if a human had said them
        if self._is_own_persona_webhook(message, channel_id):
            return

        is_mentioned = self._is_bot_mentioned(message)
        recorded = False

        # Only record history for channels the bot participates in
        if self.state and self._should_record(channel_id, is_mentioned):
            # Check for session expiry and rotate before recording the new message
            try:
                await check_and_rotate_session(channel_id, self.state, self.clients)
            except Exception as e:
                logger.error(f"[Mention] Error during session rotation for channel {channel_id}: {e}", exc_info=True)

            try:
                recorded_at = await self.state.add_to_channel_history(channel_id, {
                    "role": "user",
                    "name": message.author.display_name,
                    "user_id": str(message.author.id),
                    "is_bot": message.author.bot,
                    "content": resolve_discord_mentions(message),
                    "timestamp": datetime.now()
                })
                recorded = recorded_at is not None
            except Exception as e:
                logger.error(f"[Mention] Error adding message to history for channel {channel_id}: {e}", exc_info=True)

            # Compact oversized histories into a memory summary so long-running
            # sessions don't silently lose context off the end of the window.
            # Runs in the background so the summarization call never delays a reply.
            schedule_compaction(channel_id, self.state, self.clients)

        if not is_mentioned or message.mention_everyone:
            return

        if recorded:
            self._history_anchors[message.id] = recorded_at
        try:
            await self._respond_to_mention(message, channel_id, recorded)
        finally:
            self._history_anchors.pop(message.id, None)

    async def _respond_to_mention(self, message: discord.Message, channel_id: str, recorded: bool):
        """Generates and sends the reply to a message that mentioned the bot."""
        # Ensure state manager is available before proceeding
        if not self.state:
            logger.error("[Mention] State manager not available when processing mention.")
            await message.channel.send("⚠️ Internal error: State manager not available.")
            return

        logger.info(f"[Mention] Detected mention from {message.author.id} in channel {channel_id}")

        # Determine which provider and model to use for this channel
        provider, model_name = self.state.resolve_model(channel_id)
        client_to_use = self.clients.get(provider)
        logger.info(f"[Mention] Provider='{provider}', Model='{model_name}'. Client available: {client_to_use is not None}")

        try:
            # Get the message content without the mention
            content = self._strip_mentions(message.content) or "Hello!"

            tool_calling_enabled = self.state.get_tool_calling_enabled()

            # Process images if any are attached
            images = []
            if client_to_use and hasattr(client_to_use, 'model_supports_vision') and message.attachments:
                if asyncio.iscoroutinefunction(client_to_use.model_supports_vision):
                    client_supports_vision = await client_to_use.model_supports_vision(model_name)
                else:
                    client_supports_vision = client_to_use.model_supports_vision(model_name)
                logger.info(f"[Mention] Client '{provider}' model '{model_name}' vision support: {client_supports_vision}")

                if client_supports_vision:
                    for attachment in message.attachments:
                        if any(attachment.filename.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                            try:
                                image_data = await attachment.read()
                                images.append({
                                    'data': image_data,
                                    'type': attachment.content_type or 'image/jpeg'
                                })
                            except Exception as e:
                                await message.channel.send(f"⚠️ Failed to process image {attachment.filename}: {str(e)}")

            # Channel context up to and including this mention. Anchoring
            # matters: without it, messages from other users that arrive while
            # this reply is in flight end up after the mention, and the model
            # answers them instead of the person who summoned it.
            conversation_context = self._history_for(message, channel_id)

            # Get effective system prompt (persona > channel config > global),
            # with memory summaries relevant to the current message and this
            # channel's notes on the speaker and other participants appended
            channel_system_prompt = self.state.get_effective_system_prompt(
                channel_id, query=content, speaker_id=str(message.author.id), history=conversation_context)

            # Mentions are recorded above, so history already ends with this
            # turn, labelled with the speaker's name by the client. Only
            # append it here if that write failed, so the model still sees it.
            if not recorded:
                conversation_context.append({
                    "role": "user",
                    "name": message.author.display_name,
                    "content": content
                })

            async with message.channel.typing():
                if not client_to_use or not hasattr(client_to_use, 'send_message_with_history'):
                    if client_to_use:
                        logger.error(f"[Mention] Client for provider '{provider}' does not support chat.")
                        response = f"⚠️ Error: Client for provider '{provider}' does not support chat ('send_message_with_history' missing)."
                    else:
                        logger.error(f"[Mention] Client for provider '{provider}' not available.")
                        response = f"⚠️ Error: Client for provider '{provider}' not available or not initialized."
                elif tool_calling_enabled:
                    # Tool-calling path: the model decides whether to call
                    # tools; results are fed back until it produces text.
                    tool_context = {
                        "bot": self.bot,
                        "state": self.state,
                        "message": message,
                        "channel_id": channel_id,
                        "clients": self.clients,
                        "cog": self  # For side-effect handlers (reminder, image, search)
                    }
                    response = await self._run_tool_loop(
                        client_to_use, conversation_context, model_name,
                        channel_system_prompt, images, tool_context
                    )
                else:
                    # Plain conversation path (tool calling disabled)
                    response = await client_to_use.send_message_with_history(
                        messages=conversation_context,
                        model=model_name,
                        system_prompt=channel_system_prompt,
                        images=images
                    )
                    if isinstance(response, dict):
                        response = response.get("content") or ""

            if response is None:
                logger.error("[Mention] No response produced for mention.")
                await message.channel.send("⚠️ An unexpected response format was received.")
            elif isinstance(response, str) and response.startswith("⚠️"):
                # Error string — don't split into chunks, don't add to history
                await message.channel.send(response)
            elif isinstance(response, str):
                # Add assistant's response to history
                await self.state.add_to_channel_history(channel_id, {
                    "role": "assistant",
                    "content": response,
                    "timestamp": datetime.now()
                })

                # Send in Discord-sized chunks via persona webhook if configured
                await self._send_reply(message, channel_id, response, conversation_context)
            else:
                logger.error(f"[Mention] Unexpected response type: {type(response)}")
                await message.channel.send("⚠️ An unexpected response format was received.")

        except Exception as e:
            logger.exception(f"[Mention] Error processing mention in channel {channel_id}: {e}")
            try:
                await message.channel.send(f"⚠️ An unexpected error occurred while processing your mention: {str(e)}")
            except Exception as followup_e:
                logger.error(f"[Mention] Failed to send error message to channel {channel_id}: {followup_e}")


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
