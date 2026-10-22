"""Administrative commands for bot management."""
import asyncio
import platform
import socket
import discord
import logging
import os
from datetime import datetime
from discord.ext import commands
from discord import Option
from ..utils.state_manager import BotStateManager

logger = logging.getLogger(__name__)

class AdminCommands(commands.Cog, name="AdminCommands"):
    """Administrative tools for bot management."""

    def __init__(self, bot):
        self.bot = bot
        self.state = bot.state_manager

    # Create command group with admin-only visibility
    admin = discord.SlashCommandGroup(
        "admin",
        "Administrative tools (admin only)",
        default_member_permissions=discord.Permissions(administrator=True)
    )

    @admin.command(
        name="sync",
        description="Sync slash commands with Discord (owner only)"
    )
    @commands.is_owner()
    async def sync(self, ctx):
        """Manually sync slash commands to Discord."""
        await ctx.defer()

        try:
            # Try to clear existing global commands first
            try:
                existing_commands = await self.bot.http.get_global_commands(self.bot.user.id)
                for cmd in existing_commands:
                    if cmd['name'] != "sync":  # Don't delete the sync command we're using
                        await self.bot.http.delete_global_command(self.bot.user.id, cmd['id'])
                await ctx.followup.send("Existing commands cleared.")
            except Exception as e:
                await ctx.followup.send(f"Warning: Could not clear existing commands: {e}")

            # First to guilds
            if hasattr(self.bot, 'debug_guilds') and self.bot.debug_guilds:
                for guild_id in self.bot.debug_guilds:
                    await self.bot.sync_commands(guild_ids=[guild_id])
                await ctx.followup.send(f"Commands synced to test guilds: {self.bot.debug_guilds}")

            # Then globally
            await self.bot.sync_commands()
            await ctx.followup.send("✅ Commands synced globally")

        except Exception as e:
            await ctx.followup.send(f"❌ Error syncing commands: {str(e)}")

    @admin.command(
        name="debug",
        description="Show registered commands (owner only)"
    )
    @commands.is_owner()
    async def debug(self, ctx):
        """Display debug information about registered commands."""
        await ctx.defer()

        # Build debug information
        debug_info = ["**Registered Application Commands:**"]

        # Get global commands
        try:
            global_commands = await self.bot.http.get_global_commands(self.bot.user.id)
            debug_info.append(f"\n**Global Commands:** {len(global_commands)}")
            for cmd in global_commands[:10]:  # Limit to first 10
                debug_info.append(f"- `/{cmd['name']}`: ID={cmd['id']}")
            if len(global_commands) > 10:
                debug_info.append(f"... and {len(global_commands) - 10} more")
        except Exception as e:
            debug_info.append(f"Error fetching global commands: {str(e)}")

        # Get guild commands for the current guild
        if ctx.guild:
            try:
                guild_commands = await self.bot.http.get_guild_commands(self.bot.user.id, ctx.guild.id)
                debug_info.append(f"\n**Guild Commands ({ctx.guild.name}):** {len(guild_commands)}")
                for cmd in guild_commands[:10]:  # Limit to first 10
                    debug_info.append(f"- `/{cmd['name']}`: ID={cmd['id']}")
                if len(guild_commands) > 10:
                    debug_info.append(f"... and {len(guild_commands) - 10} more")
            except Exception as e:
                debug_info.append(f"Error fetching guild commands: {str(e)}")

        # Send debug info
        await ctx.respond("\n".join(debug_info))

    @admin.command(
        name="state",
        description="Show database statistics and bot state"
    )
    @commands.has_permissions(administrator=True)
    async def state(self, ctx):
        """Display bot state and database information."""
        await ctx.defer()

        embed = discord.Embed(
            title="Bot State Information",
            description="Current database statistics and settings",
            color=discord.Color.blue()
        )

        # Get statistics from the database
        try:
            total_messages = self.state.get_message_count()
            total_threads = self.state.get_thread_count()

            embed.add_field(
                name="Database Statistics",
                value=(f"• Stored Messages: {total_messages if total_messages >= 0 else 'Error'}\n"
                       f"• Stored Threads: {total_threads if total_threads >= 0 else 'Error'}"),
                inline=False
            )

        except Exception as e:
            logger.error(f"Error fetching stats for /admin state: {e}", exc_info=True)
            embed.add_field(name="Statistics Error", value="Could not retrieve database statistics.", inline=False)

        # Add configuration
        embed.add_field(
            name="Current Settings",
            value=(f"• Global model: `{self.state.get_global_model()}`\n"
                   f"• Message history limit: {self.state.get_max_channel_history()}\n"
                   f"• Pruning time window: {self.state.get_time_window_hours()} hours"),
            inline=False
        )

        # Add database file info
        db_path = self.state.db_manager.db_path
        db_size_kb = "N/A"
        db_mod_time = "N/A"
        if os.path.exists(db_path):
            try:
                db_size_kb = f"{os.path.getsize(db_path) / 1024:.1f} KB"
                mod_time = datetime.fromtimestamp(os.path.getmtime(db_path))
                db_mod_time = mod_time.strftime('%Y-%m-%d %H:%M:%S')
            except Exception as e:
                logger.warning(f"Could not get DB file info: {e}")

        embed.add_field(
            name="Storage Information",
            value=(f"• Database File: `{os.path.basename(db_path)}`\n"
                   f"• File Size: {db_size_kb}\n"
                   f"• Last Modified: {db_mod_time}"),
            inline=False
        )

        await ctx.respond(embed=embed)

    @admin.command(
        name="diagnostic",
        description="Run system diagnostics"
    )
    @commands.has_permissions(administrator=True)
    async def diagnostic(self, ctx):
        """Connectivity, DNS and model diagnostics."""
        await ctx.defer(ephemeral=True)
        embed = discord.Embed(title="Gideon Diagnostic Report", color=discord.Color.blue())
        embed.add_field(name="Python Version", value=f"Python {platform.python_version()}", inline=False)

        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("openrouter.ai", 443), timeout=5)
            writer.close()
            connectivity = "✅ Reached openrouter.ai:443"
        except (OSError, asyncio.TimeoutError):
            connectivity = "❌ Could not reach openrouter.ai:443"
        embed.add_field(name="Internet Connectivity", value=connectivity, inline=False)

        try:
            await asyncio.get_running_loop().getaddrinfo("openrouter.ai", 443)
            dns = "✅ DNS resolving correctly for openrouter.ai"
        except socket.gaierror:
            dns = "❌ Failed to resolve DNS for openrouter.ai"
        embed.add_field(name="DNS Resolution", value=dns, inline=False)

        state = self.bot.state_manager
        embed.add_field(name="Current Global Model", value=f"`{state.get_global_model()}`", inline=False)
        channel_model = state.get_channel_model(str(ctx.channel.id))
        if channel_model:
            embed.add_field(name="Channel-Specific Model", value=f"`{channel_model}`", inline=False)
        utility = state.get_utility_model()
        embed.add_field(name="Utility Model", value=f"`{utility}`" if utility else "Channel model", inline=False)
        await ctx.respond(embed=embed, ephemeral=True)

    @admin.command(
        name="vision_models",
        description="List AI models that support vision/image input"
    )
    @commands.has_permissions(administrator=True)
    async def vision_models(self, ctx):
        """Display vision-capable models."""
        await ctx.defer()

        try:
            # Get vision models from the model manager
            vision_models = await self.bot.model_manager.get_vision_models()

            if not vision_models:
                await ctx.respond("No vision-capable models found.")
                return

            embed = discord.Embed(
                title="👁️ Vision-Capable AI Models",
                description=f"Found {len(vision_models)} models that support image input",
                color=discord.Color.purple()
            )

            # Group models by provider
            models_by_provider = {}
            for model in vision_models:
                if "/" in model:
                    provider, model_name = model.split("/", 1)
                    if provider not in models_by_provider:
                        models_by_provider[provider] = []
                    models_by_provider[provider].append(model_name)

            # Add field for each provider
            for provider, models in models_by_provider.items():
                model_list = "\n".join([f"• `{m}`" for m in models[:10]])
                if len(models) > 10:
                    model_list += f"\n... and {len(models) - 10} more"

                embed.add_field(
                    name=f"🔌 {provider.title()}",
                    value=model_list or "None",
                    inline=False
                )

            embed.set_footer(text="Use these models with /chat and attach images")

            await ctx.respond(embed=embed)

        except Exception as e:
            logger.error(f"Error fetching vision models: {e}", exc_info=True)
            await ctx.respond(f"❌ Error: {e}", ephemeral=True)

    @admin.command(
        name="prune",
        description="Set data pruning frequency (in hours)"
    )
    @commands.has_permissions(administrator=True)
    async def prune(self, ctx, hours: int):
        """Set how often old data is pruned."""
        await ctx.defer()

        try:
            if hours < 1 or hours > 168:  # Max 1 week
                await ctx.respond("⚠️ Prune frequency must be between 1 and 168 hours (1 week).", ephemeral=True)
                return

            await self.state.set_prune_frequency_hours(hours)
            await ctx.respond(f"✅ Data pruning frequency set to {hours} hours")

        except Exception as e:
            logger.error(f"Error setting prune frequency: {e}", exc_info=True)
            await ctx.respond(f"❌ Error: {e}", ephemeral=True)


def setup(bot):
    bot.add_cog(AdminCommands(bot))
