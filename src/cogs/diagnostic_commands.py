"""Diagnostic commands for troubleshooting the bot."""
import discord
import socket
import platform
import sys
import os
import json
from datetime import datetime
from discord.ext import commands
from ..utils.state_manager import BotStateManager
from ..utils.openrouter_client import OpenRouterClient
# Removed: from ..utils.model_sync import sync_models
from ..config import OPENROUTER_API_KEY, SYSTEM_PROMPT, ALLOWED_MODELS, DEFAULT_MODEL

class DiagnosticCommands(commands.Cog):
    """Diagnostic and troubleshooting tools."""
    
    def __init__(self, bot):
        self.bot = bot
        self.state = BotStateManager()
        self.openrouter_client = bot.openrouter_client
    
    @discord.slash_command(
        name="diagnostic",
        description="Run diagnostic tests to troubleshoot connection issues"
    )
    async def diagnostic_slash(self, ctx):
        await ctx.defer()
        
        # Create an embed for displaying diagnostics
        embed = discord.Embed(
            title="Gideon Diagnostic Report",
            description="Checking system status and connections...",
            color=discord.Color.blue()
        )
        
        # Check Python version
        embed.add_field(
            name="Python Version",
            value=f"Python {platform.python_version()}",
            inline=False
        )
        
        # Check internet connectivity
        try:
            socket.create_connection(("openrouter.ai", 443), timeout=5)
            embed.add_field(
                name="Internet Connectivity",
                value="✅ Connected to the internet",
                inline=False
            )
        except (socket.timeout, socket.error):
            embed.add_field(
                name="Internet Connectivity",
                value="❌ Failed to connect to the internet",
                inline=False
            )
        
        # Check API connectivity
        dns_resolved = await self.openrouter_client.verify_dns_resolution("openrouter.ai")
        if dns_resolved:
            embed.add_field(
                name="DNS Resolution",
                value="✅ DNS resolving correctly for openrouter.ai",
                inline=False
            )
        else:
            embed.add_field(
                name="DNS Resolution",
                value="❌ Failed to resolve DNS for openrouter.ai",
                inline=False
            )
        
        # Show active model
        global_model = self.state.get_global_model()
        embed.add_field(
            name="Current Global Model",
            value=f"`{global_model}`",
            inline=False
        )
        
        # Show channel-specific model if set
        channel_id = str(ctx.channel.id)
        channel_model = self.state.get_channel_model(channel_id)
        if channel_model:
            embed.add_field(
                name="Channel-Specific Model",
                value=f"`{channel_model}`",
                inline=False
            )

        # Removed model consistency check as it's no longer relevant
        # with the new provider switching logic.

        # Send the report
        await ctx.respond(embed=embed) # Corrected indentation

    # Removed /syncmodels command as it's obsolete
   # @discord.slash_command(
   #     name="syncmodels",
   #     description="Synchronize model settings across all cogs"
   # )
   # @commands.has_permissions(administrator=True)
   # async def sync_models_slash(self, ctx):
   #     await ctx.defer()
   #     # Run the synchronization
   #     # sync_models(self.bot) # Removed call
   #     # Get the global model
   #     global_model = self.state.get_global_model()
   #     # Create an embed for displaying results
   #     embed = discord.Embed(
   #         title="Model Synchronization",
   #         description=f"✅ Model sync is no longer needed. Provider selection is handled dynamically.",
   #         color=discord.Color.orange()
   #     )
   #     await ctx.respond(embed=embed)

    # /visionmodels command removed - use /admin vision_models instead


def setup(bot):
    bot.add_cog(DiagnosticCommands(bot))
