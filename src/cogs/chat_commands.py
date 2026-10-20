"""Core chat commands for the AI assistant."""
import discord
import asyncio
import socket
import re  # Add this import here
import logging
from discord.ext import commands
from ..utils.state_manager import BotStateManager
from ..utils.memory_service import check_and_rotate_session, schedule_compaction
from ..utils.discord_fmt import chunk_message
from ..utils.llm_formatting import collect_mention_targets, apply_outbound_mentions
# Changed to absolute import: from ..utils.conversation import get_channel_context
# Removed import for conversation, now handled by state_manager
from ..utils.openrouter_client import OpenRouterClient
from ..utils.web_search import SEARCHING_STATUS, build_search_system_prompt
from ..config import OPENROUTER_API_KEY, SYSTEM_PROMPT, ALLOWED_MODELS, DEFAULT_MODEL
from datetime import datetime

# Set up logging
logger = logging.getLogger('chat_commands')

class ChatCommands(commands.Cog):
    """Commands for basic AI chat functionality."""

    # Ensure __init__ accepts the clients passed by setup()
    def __init__(self, bot, openrouter_client, openai_client, ai_horde_client):
        self.bot = bot
        # Use the shared state manager from the bot instance
        self.state = bot.state_manager
        # Store references to all provider clients
        self.clients = {
            "openrouter": openrouter_client,
            "openai": openai_client,
            "ai_horde": ai_horde_client
        }
        # Keep a direct reference for potential backward compatibility or specific uses
        self.openrouter_client = openrouter_client
        # TODO: Consider if openai_client and ai_horde_client need direct references too

    async def check_internet_connection(self):
        """Check if the bot has an internet connection."""
        try:
            # Try to resolve a well-known domain
            await asyncio.get_event_loop().getaddrinfo('google.com', 443)
            return True
        except socket.gaierror:
            return False

    def get_model_for_channel(self, channel_id):
        """Get the appropriate model for this channel"""
        return self.state.get_effective_model(channel_id)

    def format_perplexity_response(self, response_text):
        """
        Formats model responses into embeds with pagination.
        Returns a list of embed objects ready to be displayed.
        """
        # Create embeds with pagination
        embeds = []

        # Split main content into chunks of ~4000 characters (embed description limit is 4096)
        # Try to split on paragraph boundaries
        paragraphs = response_text.split('\n\n')
        current_embed = discord.Embed(
            title="AI Response",
            color=discord.Color.blue()
        )
        current_content = ""

        for paragraph in paragraphs:
            # If adding this paragraph would exceed the limit, create a new embed
            if len(current_content) + len(paragraph) + 2 > 4000:  # +2 for the \n\n
                current_embed.description = current_content
                embeds.append(current_embed)
                current_embed = discord.Embed(
                    title="AI Response (continued)",
                    color=discord.Color.blue()
                )
                current_content = paragraph
            else:
                if current_content:
                    current_content += "\n\n" + paragraph
                else:
                    current_content = paragraph

        # Don't forget the last embed
        if current_content:
            current_embed.description = current_content
            embeds.append(current_embed)

        # If no embeds were created (unlikely), create a default one
        if not embeds:
            embeds.append(discord.Embed(
                title="AI Response",
                description=response_text[:4000] if len(response_text) > 4000 else response_text,
                color=discord.Color.blue()
            ))

        # Add page numbers
        for i, embed in enumerate(embeds):
            embed.set_footer(text=f"Page {i+1}/{len(embeds)}")

        return embeds

    def is_citation_based_model(self, model_name):
        """
        Determines if a model uses citations/footnotes in its responses.

        Args:
            model_name (str): The model identifier

        Returns:
            bool: True if the model typically uses citations
        """
        if not model_name:
            return False

        # Common identifiers for citation-based models, using more standardized patterns
        citation_identifiers = [
            "sonar",
            "perplexity",
            "pplx",
            "claude-3",  # Covers all Claude 3 models
            "fireworks/mixtral",
            "anthropic/claude"
        ]

        model_lower = str(model_name).lower()

        # More detailed logging about the model name
        print(f"Checking citation model: '{model_name}' (lower: '{model_lower}')")

        # Check if model contains any of the citation identifiers
        for identifier in citation_identifiers:
            if identifier in model_lower:
                print(f"✅ Model '{model_name}' identified as citation-based (matches '{identifier}')")
                return True

        print(f"❌ Model '{model_name}' is NOT identified as citation-based")
        return False

    def should_format_citations(self, model_name, response_text):
        """
        Determines if a response should be formatted for citations.
        Checks both the model name and if the response appears to contain citations.

        Args:
            model_name (str): The model identifier
            response_text (str): The model's response

        Returns:
            bool: True if the response should be formatted for citations
        """
        import re

        # Explicitly check for Sonar first
        if "sonar" in str(model_name).lower() or "perplexity" in str(model_name).lower():
            print(f"✅ Direct match for Sonar/Perplexity model: {model_name}")
            return True

        # More robust pattern to detect footnote-style citations
        # Looks for [number] patterns that likely indicate footnotes
        footnote_pattern = r'\[\d+\](?:\s+|\n|$)'
        has_citation_format = bool(re.search(footnote_pattern, response_text))

        # Check for citation section markers
        has_reference_section = any(marker in response_text.lower() for marker in
                                 ["sources:", "references:", "citations:", "footnotes:"])

        # Is this a known citation model?
        is_citation_model = self.is_citation_based_model(model_name)

        # Log detailed detection info
        print(f"Citation detection for '{model_name}':")
        print(f"- Is known citation model: {is_citation_model}")
        print(f"- Has citation format: {has_citation_format}")
        print(f"- Has reference section: {has_reference_section}")

        # More permissive logic:
        # 1. It's a known citation model OR
        # 2. It has citation format OR
        # 3. It has a reference section
        should_format = is_citation_model or has_citation_format or has_reference_section
        print(f"Citation formatting decision: {should_format}")

        return should_format

    @discord.slash_command(
        name="chat",
        description="Send a message to the AI assistant"
    )
    async def chat_slash(self, ctx,
                         message: str,
                         image: discord.Attachment = None):
        await ctx.defer()
        logger.info(f"[Chat Entry] Command invoked by {ctx.author.id} in channel {ctx.channel.id}") # ADDED LOG

        # Check internet connection first
        if not await self.check_internet_connection():
            await ctx.respond("⚠️ Network issue: Unable to connect to the internet. Please check your connection and try again.")
            return

        # Get channel ID to track conversation per channel
        channel_id = str(ctx.channel.id)

        # Determine which provider and model to use for this channel
        model_id_full = self.get_model_for_channel(channel_id) # e.g., "openai/gpt-4o"
        logger.info(f"[Chat Start] Channel {channel_id}: Effective model ID from state = '{model_id_full}'") # Enhanced log

        # Parse provider and model name
        provider, model_name = self.state.resolve_model(channel_id)
        model_id_full = f"{provider}/{model_name}"

        # Select the appropriate client
        client_to_use = self.clients.get(provider)
        logger.info(f"[Chat Client Select] Parsed Provider='{provider}', Model='{model_name}'. Found client object: {client_to_use is not None}") # Enhanced log

        # Log which provider and model are being used
        logger.info(f"Using provider '{provider}' and model '{model_name}' (resolved from '{model_id_full}') for channel {channel_id}")

        # Process image if provided (vision check happens later)
        images = []
        image_embed = None

        if image:
            # Check if it's an image file
            if any(image.filename.lower().endswith(ext) for ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp']):
                # Create an embed to display the image
                image_embed = discord.Embed(title="Analyzing Image", color=discord.Color.blue())
                image_embed.set_image(url=image.url)
                image_embed.add_field(name="File", value=image.filename)

                # Check if the selected client and model support vision *before* downloading
                client_supports_vision = False
                if client_to_use and hasattr(client_to_use, 'model_supports_vision'):
                    # Check if the specific model within the provider supports vision
                    if asyncio.iscoroutinefunction(client_to_use.model_supports_vision):
                        client_supports_vision = await client_to_use.model_supports_vision(model_name)
                    else:
                        client_supports_vision = client_to_use.model_supports_vision(model_name)
                    logger.info(f"Client '{provider}' model '{model_name}' vision support: {client_supports_vision}")
                else:
                    logger.warning(f"Client for provider '{provider}' missing 'model_supports_vision' method.")

                if client_supports_vision:
                    try:
                        # Download the image data only if supported
                        image_data = await image.read()
                        images.append({
                            'data': image_data,
                            'type': image.content_type or 'image/jpeg'  # Default to jpeg if not specified
                        })
                    except Exception as e:
                        await ctx.respond(f"⚠️ Failed to process image {image.filename}: {str(e)}")
                        return
                elif client_to_use: # Only add warning if client exists but doesn't support vision
                    image_embed.description = f"⚠️ Provider '{provider}' model '{model_name}' doesn't support image analysis."
                # If client_to_use is None, the error will be handled later

        # Check for session expiry and rotate if needed (before fetching history)
        await check_and_rotate_session(channel_id, self.state, self.clients)

        # Get effective system prompt (persona > channel config > global, with
        # memory summaries relevant to the current message injected)
        channel_system_prompt = self.state.get_effective_system_prompt(channel_id, query=message)

        try:
            # Get recent channel context from state manager
            conversation_context = self.state.get_channel_history(channel_id)

            # Add this new message
            await self.state.add_to_channel_history(channel_id, {
                "role": "user",
                "name": ctx.author.display_name,
                "user_id": str(ctx.author.id),
                "content": message,
                "timestamp": datetime.now()
            })

            # Compact oversized histories into a memory summary (in the
            # background, so summarization never delays this reply)
            schedule_compaction(channel_id, self.state, self.clients)

            # Format the final query with the current user's question
            conversation_context.append({
                "role": "user",
                "name": ctx.author.display_name,
                "content": message
            })

            # First response - show the user's message
            if image_embed:
                await ctx.respond(f"**{ctx.author.display_name}**: {message}", embed=image_embed)
            else:
                # Show user's message (without processing note)
                await ctx.respond(f"**{ctx.author.display_name}**: {message}")

            # Always send a separate processing message that we'll edit
            processing_msg = await ctx.followup.send("Processing response...")

            # --- Select and Call Correct Client ---
            logger.debug(f"Attempting to select client. Provider='{provider}', Client Object='{client_to_use}', Has Send Method='{hasattr(client_to_use, 'send_message_with_history') if client_to_use else 'N/A'}'") # DETAILED LOG
            if client_to_use and hasattr(client_to_use, 'send_message_with_history'):
                logger.info(f"✅ Calling send_message_with_history on client for provider '{provider}' ({type(client_to_use).__name__}) with model '{model_name}'") # DETAILED LOG
                response = await client_to_use.send_message_with_history(
                    messages=conversation_context,
                    model=model_name, # Pass only the model name part
                    system_prompt=channel_system_prompt,
                    # Pass images only if an image was provided AND the selected client/model supports vision
                    images=images if image and client_supports_vision else []
                )
            elif client_to_use:
                 logger.error(f"❌ Client for provider '{provider}' exists but does not have 'send_message_with_history' method.") # DETAILED LOG
                 response = f"⚠️ Error: Client for provider '{provider}' does not support chat ('send_message_with_history' missing)."
            else:
                 logger.error(f"❌ Client for provider '{provider}' not found or not initialized in self.clients.") # DETAILED LOG
                 response = f"⚠️ Error: Client for provider '{provider}' not available or not initialized."
            # --- End Client Call ---

            # Check if response is an error
            if response.startswith("⚠️"):
                # If it's an error, don't split chunks and don't add to history
                await processing_msg.edit(content=response)
            else:
                # Add assistant's response to history
                await self.state.add_to_channel_history(channel_id, {
                    "role": "assistant",
                    "content": response,
                    "timestamp": datetime.now()
                })

                # Debug logs using the actual model ID used
                is_citation_model = self.is_citation_based_model(model_id_full) # Check full ID for citation patterns
                has_citation_format = bool(re.search(r'\[\d+\]', response))
                logger.debug(f"Model: {model_id_full}, Is citation model: {is_citation_model}, Has citation format: {has_citation_format}")

                # Check if a persona is active for webhook-based sending
                persona_active = (hasattr(self.bot, 'webhook_sender')
                                  and self.bot.state_manager.get_effective_persona(channel_id))

                # Format responses from models that use citations as paginated embeds
                if self.should_format_citations(model_id_full, response):
                    logger.info(f"Formatting response from {model_id_full} with citations")
                    embeds = self.format_perplexity_response(response)

                    if embeds:
                        if persona_active:
                            # Delete processing message, send all embeds via webhook
                            await processing_msg.delete()
                            for embed in embeds:
                                await self.bot.webhook_sender.send_response(
                                    ctx.channel, "", channel_id, embeds=[embed]
                                )
                        else:
                            await processing_msg.edit(content=None, embed=embeds[0])
                            for embed in embeds[1:]:
                                await ctx.channel.send(embed=embed)
                else:
                    logger.info(f"Using standard formatting for model {model_id_full}")
                    targets = collect_mention_targets(conversation_context, [ctx.author])
                    chunks = chunk_message(apply_outbound_mentions(response, targets))

                    if persona_active:
                        # Delete processing message, send all chunks via webhook
                        await processing_msg.delete()
                        for chunk in chunks:
                            await self.bot.webhook_sender.send_response(
                                ctx.channel, chunk, channel_id
                            )
                    else:
                        for i, chunk in enumerate(chunks):
                            if i == 0:
                                await processing_msg.edit(content=chunk)
                            else:
                                await ctx.channel.send(chunk)
        # Correctly indented except block for the try starting at line 269
        except Exception as e:
            logger.exception(f"Error during chat processing for channel {channel_id}: {e}")
            # Attempt to edit the processing message if it exists
            try:
                if processing_msg:
                    await processing_msg.edit(content=f"⚠️ An unexpected error occurred: {str(e)}")
                else:
                    # If processing_msg doesn't exist for some reason, send a new message
                    await ctx.followup.send(f"⚠️ An unexpected error occurred: {str(e)}")
            except discord.NotFound:
                 # If the processing message was deleted, send a new one
                 await ctx.followup.send(f"⚠️ An unexpected error occurred: {str(e)}")
            except Exception as followup_e:
                 # Log if sending the error message itself fails
                 logger.error(f"Failed to send error message to user: {followup_e}")
        # Removed finally block that restored openrouter_client.model

    @discord.slash_command(
        name="reset",
        description="Reset the conversation history for this channel"
    )
    async def reset_slash(self, ctx):
        channel_id = str(ctx.channel.id)
        # clear_channel_history now handles the DB deletion
        if self.state.clear_channel_history(channel_id):
            await ctx.respond("The conversation history for this channel has been reset.")
        else:
            # clear_channel_history returns False if no messages were deleted
            await ctx.respond("No conversation history found for this channel.")

    @discord.slash_command(
        name="memory",
        description="Show conversation memory status for this channel"
    )
    async def channel_memory_slash(self, ctx):
        channel_id = str(ctx.channel.id)
        history = self.state.get_channel_history(channel_id)
        memories = self.state.get_channel_memories(channel_id)
        timeout = self.state.get_effective_session_timeout(channel_id)
        summary_enabled = self.state.get_effective_memory_summary_enabled(channel_id)

        lines = []

        if history:
            lines.append(f"**Active session:** {len(history)} message(s) in current context window.")
            from ..utils.memory_service import _parse_timestamp
            last_ts = _parse_timestamp(history[-1].get("timestamp"))
            if last_ts:
                from datetime import datetime
                age_hours = (datetime.now() - last_ts).total_seconds() / 3600
                lines.append(f"**Last activity:** {age_hours:.1f}h ago (session resets after {timeout}h of silence).")
        else:
            lines.append("**Active session:** No messages in current context window.")
            lines.append(f"Session resets after **{timeout}h** of silence.")

        if memories:
            lines.append(f"**Long-term memories:** {len(memories)} stored summary(ies) for this channel.")
        else:
            lines.append("**Long-term memories:** None stored yet.")

        lines.append(f"**Auto-summarize on reset:** {'Enabled' if summary_enabled else 'Disabled'}")

        await ctx.respond("\n".join(lines))

    @discord.slash_command(
        name="summarize",
        description="Summarize the current conversation history"
    )
    async def summarize_slash(self, ctx):
        await ctx.defer()
        channel_id = str(ctx.channel.id)
        history = self.state.get_channel_history(channel_id)
        if not history:
            await ctx.respond("No conversation history to summarize.")
            return

        await ctx.respond("Generating conversation summary...")

        conversation_context = self.state.get_channel_history(channel_id)
        summary_request = [
            {"role": "system", "content": "Summarize the following conversation in 3-5 bullet points:"},
            {"role": "user", "content": "\n".join([msg["content"] for msg in conversation_context])}
        ]

        # Summarization might still use a default client/model for simplicity, or could be updated
        # For now, let's keep it using the cog's default openrouter_client
        # TODO: Consider if summarize should use the channel's effective model/provider
        summary = await self.openrouter_client.send_message_with_history(summary_request)
        await ctx.respond(f"**Conversation Summary:**\n{summary}")

    @discord.slash_command(
        name="search",
        description="Search the web for current information"
    )
    async def search_slash(self, ctx,
                         query: discord.Option(str, "What would you like to search for?"),
                         model: discord.Option(str, "AI model to use (optional)", required=False) = None):
        """Search the web for current information using AI."""
        await ctx.defer()

        # Get the channel ID and set the model
        channel_id = str(ctx.channel.id)

        # Determine provider and model
        model_id_full = model if model else self.state.get_effective_model(channel_id)

        # Parse provider and model name
        try:
            provider, model_name = model_id_full.split('/', 1)
        except ValueError:
            logger.warning(f"Invalid model format '{model_id_full}' for search command. Using global provider.")
            provider = self.state.global_provider
            model_name = model_id_full
            model_id_full = f"{provider}/{model_name}"

        # Select the appropriate client (primarily for web search capability check)
        client_to_use = self.clients.get(provider)
        logger.info(f"Search using provider '{provider}' and model '{model_name}' (resolved from '{model_id_full}')")

        # Check if the selected client supports web search (currently only OpenRouter does)
        web_search_supported = provider == "openrouter" and client_to_use and hasattr(client_to_use, 'send_message_with_history')
        if not web_search_supported:
             await ctx.respond(f"⚠️ Provider '{provider}' does not support the web search command.")
             return

        try:
            # Create a thinking message
            processing_msg = await ctx.respond(SEARCHING_STATUS.format(query=query))

            # Prepare the user message
            user_message = {
                "role": "user",
                "content": query
            }

            # Get effective system prompt (persona > channel config > global)
            channel_system_prompt = self.state.get_effective_system_prompt(channel_id, query=query)

            # Add a search-focused wrapper to the system prompt
            search_system_prompt = build_search_system_prompt(channel_system_prompt)

            # Send to the OpenRouter client (as only it supports web_search flag currently)
            response = await client_to_use.send_message_with_history(
                messages=[user_message],
                model=model_name, # Pass only model name
                system_prompt=search_system_prompt,
                web_search=True # Enable web search
            )

            # Check if response is an error
            if response.startswith("⚠️"):
                # If it's an error, don't format and just show the error
                await processing_msg.edit(content=response)
            else:
                # Format responses from models that use citations as paginated embeds
                # Check if a persona is active for webhook-based sending
                search_persona_active = (hasattr(self.bot, 'webhook_sender')
                                         and self.bot.state_manager.get_effective_persona(channel_id))

                if self.should_format_citations(model_id_full, response): # Use full ID for citation check
                    logger.info(f"Formatting search response from {model_id_full} with citations")
                    embeds = self.format_perplexity_response(response)

                    # Customize the first embed for search results
                    if embeds:
                        embeds[0].title = f"🔍 Search Results: {query}"
                        embeds[0].set_footer(text=f"Using {model_id_full} • Web search enabled") # Show full ID
                        for embed in embeds[1:]:
                            embed.set_footer(text=f"Using {model_id_full} • Web search enabled") # Show full ID

                        if search_persona_active:
                            await processing_msg.delete()
                            for embed in embeds:
                                await self.bot.webhook_sender.send_response(
                                    ctx.channel, "", channel_id, embeds=[embed]
                                )
                        else:
                            await processing_msg.edit(content=None, embed=embeds[0])
                            for embed in embeds[1:]:
                                await ctx.channel.send(embed=embed)
                else:
                    # For non-citation models, use a single embed
                    embed = discord.Embed(
                        title=f"🔍 Search Results: {query}",
                        description=response,
                        color=discord.Color.blue()
                    )
                    embed.set_footer(text=f"Using {model_id_full} • Web search enabled") # Show full ID
                    if search_persona_active:
                        await processing_msg.delete()
                        await self.bot.webhook_sender.send_response(
                            ctx.channel, "", channel_id, embeds=[embed]
                        )
                    else:
                        await processing_msg.edit(content=None, embed=embed)

        except Exception as e:
            logger.error(f"Error in search command for provider '{provider}', model '{model_name}': {str(e)}", exc_info=True)
            # Edit the original message if possible
            try:
                await processing_msg.edit(content=f"⚠️ Error during search: {str(e)}")
            except discord.NotFound:
                 await ctx.followup.send(f"⚠️ Error during search: {str(e)}", ephemeral=True)
            except Exception as edit_e:
                 logger.error(f"Failed to edit processing message for search error: {edit_e}")
                 await ctx.followup.send(f"⚠️ Error during search: {str(e)}", ephemeral=True)
        # Removed finally block that restored openrouter_client.model

    @discord.slash_command(
        name="setprovider",
        description="Set the AI provider for this channel"
    )
    async def setprovider_slash(self, ctx):
        """Set the AI provider for this channel with interactive selection."""
        await ctx.defer(ephemeral=True)

        try:
            # Get available providers
            providers = await self.state.model_manager.get_providers()
            if not providers:
                await ctx.respond("⚠️ No AI providers available")
                return

            # Create select menu with provider options
            select = discord.ui.Select(
                placeholder="Choose a provider...",
                options=[
                    discord.SelectOption(
                        label=provider.capitalize(),
                        value=provider
                    ) for provider in providers
                ]
            )

            # Create view to hold the select menu
            view = discord.ui.View()
            view.add_item(select)

            # Function to handle the selection
            async def on_select(interaction):
                if interaction.user != ctx.author:
                    await interaction.response.send_message("You didn't initiate this command!", ephemeral=True)
                    return

                selected_provider = select.values[0]
                await self.state.set_channel_provider(str(ctx.channel.id), selected_provider)
                await interaction.response.edit_message(
                    content=f"✅ Provider set to {selected_provider.capitalize()}",
                    view=None
                )

            select.callback = on_select

            # Send the select menu
            await ctx.followup.send(
                "Select an AI provider for this channel:",
                view=view,
                ephemeral=True
            )

        except Exception as e:
            logger.error(f"Error in setprovider command: {str(e)}", exc_info=True)
            await ctx.followup.send(
                f"⚠️ Error setting provider: {str(e)}",
                ephemeral=True
            )

def setup(bot):
    # Pass the required client instances from the bot object
    logger.info("Setting up ChatCommands cog...")
    openrouter_client = getattr(bot, 'openrouter_client', None)
    openai_client = getattr(bot, 'openai_client', None)
    ai_horde_client = getattr(bot, 'ai_horde_client', None)

    # Check if essential clients are available
    if not openrouter_client:
        logger.error("OpenRouter client not found on bot object. Cannot load ChatCommands cog.")
        # Optionally raise an exception to clearly indicate failure
        # raise TypeError("OpenRouter client is required for ChatCommands cog.")
        return # Prevent loading if essential client is missing

    # Log if optional clients are missing (they might be None if API keys are missing)
    if not openai_client:
        logger.warning("OpenAI client not found or initialized. OpenAI provider unavailable in ChatCommands.")
    if not ai_horde_client:
        logger.warning("AI Horde client not found or initialized. AI Horde provider unavailable in ChatCommands.")

    # Add the cog if essential clients are present
    try:
        cog_instance = ChatCommands(bot, openrouter_client, openai_client, ai_horde_client)
        bot.add_cog(cog_instance)
        logger.info("ChatCommands cog loaded successfully.")
    except Exception as e:
        logger.exception(f"Failed to initialize or add ChatCommands cog: {e}")
