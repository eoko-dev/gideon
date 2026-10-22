"""Core chat commands for the AI assistant."""
import discord
import asyncio
import re  # Add this import here
import logging
from discord.ext import commands
from ..utils.state_manager import BotStateManager
from ..utils.conversation_service import Conversation, ConversationService, MessageLike, ReplyRequest
from ..utils.llm_result import call_chat
from ..utils.memory_service import llm_clients
from ..utils.usage import usage_scope
from ..utils.discord_fmt import chunk_message
from ..utils.permissions import has_channel_permission
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
        self.service = getattr(bot, 'conversation_service', None) or ConversationService(bot)

    @property
    def clients(self):
        """Provider clients, read live so dashboard key rotation applies."""
        return llm_clients(self.bot)

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
        logger.debug(f"Checking citation model: '{model_name}' (lower: '{model_lower}')")

        # Check if model contains any of the citation identifiers
        for identifier in citation_identifiers:
            if identifier in model_lower:
                logger.debug(f"✅ Model '{model_name}' identified as citation-based (matches '{identifier}')")
                return True

        logger.debug(f"❌ Model '{model_name}' is NOT identified as citation-based")
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
            logger.debug(f"✅ Direct match for Sonar/Perplexity model: {model_name}")
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
        logger.debug(f"Citation detection for '{model_name}':")
        logger.debug(f"- Is known citation model: {is_citation_model}")
        logger.debug(f"- Has citation format: {has_citation_format}")
        logger.debug(f"- Has reference section: {has_reference_section}")

        # More permissive logic:
        # 1. It's a known citation model OR
        # 2. It has citation format OR
        # 3. It has a reference section
        should_format = is_citation_model or has_citation_format or has_reference_section
        logger.debug(f"Citation formatting decision: {should_format}")

        return should_format

    @discord.slash_command(
        name="chat",
        description="Send a message to the AI assistant"
    )
    async def chat_slash(self, ctx,
                         message: str,
                         image: discord.Attachment = None):
        await ctx.defer()
        channel_id = str(ctx.channel.id)
        logger.info(f"[Chat] /chat by {ctx.author.id} in channel {channel_id}")

        # A thread keeps its own history and settings; anywhere else the
        # channel is the conversation
        if isinstance(ctx.channel, discord.Thread):
            conversation = Conversation(channel=ctx.channel, channel_id=str(ctx.channel.parent_id),
                                        thread_id=channel_id)
        else:
            conversation = Conversation(channel=ctx.channel, channel_id=channel_id)

        # Echo the prompt so the channel sees what was asked
        echo = f"**{ctx.author.display_name}**: {message}"
        if image:
            embed = discord.Embed(color=discord.Color.blue())
            embed.set_image(url=image.url)
            await ctx.respond(echo[:2000], embed=embed)
        else:
            await ctx.respond(echo[:2000])

        try:
            recorded_at = await self.service.record_user_turn(conversation, ctx.author, message)
            await self.service.respond(ReplyRequest(
                conversation=conversation,
                author=ctx.author,
                content=message,
                message=MessageLike(ctx, message),
                attachments=[image] if image else [],
                history_until=recorded_at,
                recorded=recorded_at is not None,
                feature="chat",
            ))
        except Exception as e:
            logger.exception(f"Error during chat processing for channel {channel_id}: {e}")
            await ctx.followup.send("⚠️ Something went wrong while I was answering. Please try again.")

    @discord.slash_command(
        name="reset",
        description="Reset the conversation history for this channel"
    )
    async def reset_slash(self, ctx):
        if not has_channel_permission(ctx.author, ctx.channel, manage_channels=True):
            await ctx.respond("⛔ You need **Manage Channels** to reset this channel's history.", ephemeral=True)
            return
        channel_id = str(ctx.channel.id)
        # clear_channel_history now handles the DB deletion
        if self.state.clear_channel_history(channel_id):
            await ctx.respond("The conversation history for this channel has been reset.")
        else:
            # clear_channel_history returns False if no messages were deleted
            await ctx.respond("No conversation history found for this channel.")

    memory = discord.SlashCommandGroup("memory", "What Gideon remembers in this channel")

    @memory.command(name="status", description="Show conversation memory status for this channel")
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

        if self.state.get_effective_user_memory_enabled(channel_id):
            people = len(self.state.list_channel_user_notes(channel_id))
            lines.append(f"**Per-user memory:** Enabled — notes kept for {people} person(s) in this channel. "
                         f"Use `/memory me` to see yours.")
        else:
            lines.append("**Per-user memory:** Disabled in this channel.")

        await ctx.respond("\n".join(lines))

    @memory.command(name="me", description="Show what Gideon remembers about you in this channel")
    async def memory_me_slash(self, ctx):
        user_id = str(ctx.author.id)
        if self.state.is_user_memory_opted_out(user_id):
            await ctx.respond("You've opted out of per-user memory, so nothing is kept about you. "
                              "Use `/memory optin` to turn it back on.", ephemeral=True)
            return
        if not self.state.get_effective_user_memory_enabled(str(ctx.channel.id)):
            await ctx.respond("Per-user memory is disabled in this channel.", ephemeral=True)
            return

        row = self.state.get_user_notes(str(ctx.channel.id), user_id)
        if not row or not row.get("notes"):
            await ctx.respond("I don't have any notes about you in this channel yet.", ephemeral=True)
            return
        await ctx.respond("**What I remember about you in this channel:**\n" + row["notes"][:1900]
                          + "\n\n-# These notes only apply here. `/memory forget` removes them.",
                          ephemeral=True)

    @memory.command(name="forget", description="Delete what Gideon remembers about you")
    async def memory_forget_slash(
        self, ctx,
        scope: discord.Option(str, "Where to forget you", choices=["this channel", "everywhere"],
                              default="this channel"),
    ):
        user_id = str(ctx.author.id)
        if scope == "everywhere":
            deleted = self.state.delete_user_notes_everywhere(user_id)
            where = "in every channel"
        else:
            deleted = self.state.delete_user_notes(str(ctx.channel.id), user_id)
            where = "in this channel"
        if deleted:
            await ctx.respond(f"Done — I've forgotten my notes about you {where}.", ephemeral=True)
        else:
            await ctx.respond(f"I didn't have any notes about you {where}.", ephemeral=True)

    @memory.command(name="optout", description="Stop Gideon from keeping notes about you, in every channel")
    async def memory_optout_slash(self, ctx):
        deleted = self.state.set_user_memory_opt_out(str(ctx.author.id), True)
        suffix = f" I also deleted my notes about you in {deleted} channel(s)." if deleted else ""
        await ctx.respond("You're opted out: I won't keep notes about you anywhere." + suffix
                          + " Use `/memory optin` to change your mind.", ephemeral=True)

    @memory.command(name="optin", description="Let Gideon keep notes about you again")
    async def memory_optin_slash(self, ctx):
        self.state.set_user_memory_opt_out(str(ctx.author.id), False)
        await ctx.respond("You're opted back in. I'll keep notes about you separately in each channel.",
                          ephemeral=True)

    @memory.command(name="forget_user", description="Delete what Gideon remembers about a member in this channel (admin)")
    async def memory_forget_user_slash(
        self, ctx,
        user: discord.Option(discord.Member, "Whose notes to delete"),
    ):
        permissions = getattr(ctx.author, "guild_permissions", None)
        if not permissions or not permissions.administrator:
            await ctx.respond("⚠️ Only server administrators can delete someone else's notes.", ephemeral=True)
            return
        deleted = self.state.delete_user_notes(str(ctx.channel.id), str(user.id))
        if deleted:
            await ctx.respond(f"Deleted my notes about {user.display_name} in this channel.", ephemeral=True)
        else:
            await ctx.respond(f"I had no notes about {user.display_name} in this channel.", ephemeral=True)

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
            channel_system_prompt = self.state.get_effective_system_prompt(
                channel_id, query=query, speaker_id=str(ctx.author.id))

            # Add a search-focused wrapper to the system prompt
            search_system_prompt = build_search_system_prompt(channel_system_prompt)

            # Send to the OpenRouter client (as only it supports web_search flag currently)
            with usage_scope("search", user_id=ctx.author.id, channel_id=channel_id):
                result = await call_chat(
                    client_to_use,
                    messages=[user_message],
                    model=model_name, # Pass only model name
                    system_prompt=search_system_prompt,
                    web_search=True # Enable web search
                )
            response = result.text

            if not result.ok:
                await processing_msg.edit(content=f"⚠️ {result.error}")
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
            await ctx.followup.send("⚠️ Something went wrong during the search. Please try again.",
                                    ephemeral=True)
        # Removed finally block that restored openrouter_client.model

    @discord.slash_command(
        name="setprovider",
        description="Set the AI provider for this channel"
    )
    async def setprovider_slash(self, ctx):
        """Set the AI provider for this channel with interactive selection."""
        if not has_channel_permission(ctx.author, ctx.channel, manage_channels=True):
            await ctx.respond("⛔ You need **Manage Channels** to change this channel's AI provider.", ephemeral=True)
            return
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
