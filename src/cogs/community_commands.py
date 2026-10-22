"""Community features: event RSVP buttons and welcoming new members.

Welcomes are off by default. They need the privileged Server Members intent
(MEMBERS_INTENT=TRUE in .env and enabled in the Developer Portal) and
``welcome_enabled`` in the dashboard. They're written in the welcome
channel's persona voice on the utility model, and limited by the hourly
budget for unprompted posts, so a wave of joins doesn't flood the channel.
"""

import logging

import discord
from discord.ext import commands

from ..config import MEMBERS_INTENT
from ..utils import events_service
from ..utils.llm_result import call_chat
from ..utils.memory_service import llm_clients
from ..utils.restraint import get_budget
from ..utils.usage import usage_scope

logger = logging.getLogger(__name__)

WELCOME_SYSTEM = ("You are Gideon, the friendly AI assistant of a Discord server, welcoming a new member. "
                  "Be warm and brief (two or three sentences).")


class CommunityCommands(commands.Cog):
    """Event RSVPs and welcomes."""

    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        try:
            await events_service.handle_rsvp_interaction(self.bot, interaction)
        except Exception as e:
            logger.error(f"[RSVP] Error handling button: {e}", exc_info=True)

    # ─── Welcome ────────────────────────────────────────────────────────────

    def welcome_channel(self, guild):
        state = self.bot.state_manager
        configured = state.get_welcome_channel_id()
        if configured:
            channel = guild.get_channel(int(configured)) if str(configured).isdigit() else None
            if channel is not None:
                return channel
        return guild.system_channel

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        state = getattr(self.bot, "state_manager", None)
        if state is None or member.bot or not state.get_welcome_enabled():
            return
        channel = self.welcome_channel(member.guild)
        if channel is None:
            logger.info(f"[Welcome] No welcome channel in {member.guild.name}")
            return
        if not get_budget(state).allow(f"welcome:{member.guild.id}"):
            logger.info(f"[Welcome] Hourly budget spent in {member.guild.name}; skipping {member.id}")
            return
        text = await self.compose_welcome(member, channel)
        try:
            await self.bot.webhook_sender.send_response(channel, f"{member.mention} {text}", str(channel.id))
            await state.add_to_channel_history(str(channel.id), {
                "role": "assistant", "content": f"Welcomed {member.display_name} to the server."})
        except discord.HTTPException as e:
            logger.warning(f"[Welcome] Could not post welcome in {channel.id}: {e}")

    async def compose_welcome(self, member, channel) -> str:
        guild = member.guild
        fallback = f"Welcome to **{guild.name}**! 👋 Mention me any time if you have a question."
        state = self.bot.state_manager
        try:
            provider, model_name = state.resolve_utility_model(str(channel.id))
            client = llm_clients(self.bot).get(provider)
            if client is None:
                return fallback
            everyone = guild.default_role
            visible = [c for c in guild.text_channels if c.permissions_for(everyone).view_channel][:12]
            channels = "\n".join(f"- #{c.name}" + (f": {c.topic[:100]}" if c.topic else "") for c in visible)
            note = state.get_welcome_note()
            prompt = (f"Welcome {member.display_name} to the server '{guild.name}'. "
                      f"Point them to one or two channels that suit a newcomer.\n\nChannels:\n{channels}"
                      + (f"\n\nWhat the admins want newcomers to know:\n{note}" if note else "")
                      + "\n\nDon't ping anyone; the mention is added for you.")
            persona = state.get_effective_persona(str(channel.id))
            system = WELCOME_SYSTEM
            if persona and persona.get("system_prompt"):
                system = f"{persona['system_prompt']}\n\n{WELCOME_SYSTEM}"
            with usage_scope("welcome", user_id=None, channel_id=str(channel.id)):
                result = await call_chat(client, messages=[{"role": "user", "content": prompt}],
                                         model=model_name, system_prompt=system)
            return (result.text.strip()[:1800] or fallback) if result.ok else fallback
        except Exception as e:
            logger.error(f"[Welcome] Could not compose welcome: {e}", exc_info=True)
            return fallback


def setup(bot):
    if not MEMBERS_INTENT:
        logger.info("Welcomes need MEMBERS_INTENT=TRUE; event RSVPs still work")
    bot.add_cog(CommunityCommands(bot))
