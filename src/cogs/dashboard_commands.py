"""Dashboard cog - manages the web-based admin dashboard server."""
import asyncio
import discord
import logging
import socket
from typing import Optional
from discord.ext import commands
from ..config import DASHBOARD_ENABLED, DASHBOARD_PORT, DASHBOARD_SECRET
from ..utils.dashboard import DashboardServer

logger = logging.getLogger(__name__)


class DashboardCommands(commands.Cog, name="DashboardCommands"):
    """Manages the web admin dashboard lifecycle."""

    def __init__(self, bot):
        self.bot = bot
        self.server: Optional[DashboardServer] = None
        self._started = False

    async def start_dashboard(self):
        """Start the dashboard server if configured. Called explicitly from bot.py."""
        if self._started:
            return

        if not DASHBOARD_ENABLED:
            logger.info("Dashboard is disabled (DASHBOARD_ENABLED=FALSE)")
            return

        if not DASHBOARD_SECRET:
            logger.warning("Dashboard enabled but DASHBOARD_SECRET is not set. Dashboard will NOT start.")
            return

        try:
            self.server = DashboardServer(self.bot, DASHBOARD_SECRET, DASHBOARD_PORT)
            await self.server.start()
            self._started = True
            # Attach to bot so other cogs can broadcast events
            self.bot.dashboard_server = self.server
            logger.info(f"Admin dashboard available at http://0.0.0.0:{DASHBOARD_PORT}")
        except Exception as e:
            logger.error(f"Failed to start dashboard server: {e}", exc_info=True)
            self.server = None

    @commands.Cog.listener()
    async def on_message(self, message):
        """Broadcast message events to dashboard WebSocket clients."""
        if message.author.bot:
            return
        if self.server:
            await self.server.broadcast_event('message_activity', {
                'channel_id': str(message.channel.id),
                'channel_name': getattr(message.channel, 'name', 'DM'),
                'author': str(message.author),
                'guild': message.guild.name if message.guild else 'DM',
            })

    def cog_unload(self):
        """Clean up when the cog is unloaded."""
        if self.server:
            asyncio.ensure_future(self.server.stop())

    # Discord command to check dashboard status
    admin_dashboard = discord.SlashCommandGroup(
        "dashboard",
        "Admin dashboard management",
        default_member_permissions=discord.Permissions(administrator=True)
    )

    @admin_dashboard.command(
        name="status",
        description="Check the admin dashboard status"
    )
    @commands.has_permissions(administrator=True)
    async def dashboard_status(self, ctx):
        """Show dashboard server status."""
        await ctx.defer(ephemeral=True)

        if not DASHBOARD_ENABLED:
            await ctx.respond("Dashboard is disabled. Set `DASHBOARD_ENABLED=TRUE` in `.env` to enable.", ephemeral=True)
            return

        if not self.server:
            await ctx.respond("Dashboard server is not running. Check logs for errors.", ephemeral=True)
            return

        ws_count = self.server.ws_client_count
        embed = discord.Embed(
            title="Admin Dashboard Status",
            color=discord.Color.green()
        )
        embed.add_field(name="Status", value="Running", inline=True)
        embed.add_field(name="Port", value=str(DASHBOARD_PORT), inline=True)
        embed.add_field(name="WebSocket Clients", value=str(ws_count), inline=True)
        embed.add_field(
            name="URL",
            value=f"`http://{socket.gethostname()}:{DASHBOARD_PORT}`\n"
                  "Listens on all interfaces; use this host's network address if the name doesn't resolve.",
            inline=False,
        )

        await ctx.respond(embed=embed, ephemeral=True)

    @admin_dashboard.command(
        name="restart",
        description="Restart the admin dashboard server"
    )
    @commands.is_owner()
    async def dashboard_restart(self, ctx):
        """Restart the dashboard server."""
        await ctx.defer(ephemeral=True)

        if self.server:
            await self.server.stop()
            self.server = None
            self._started = False

        await self.start_dashboard()

        if self.server:
            await ctx.respond(f"Dashboard restarted on port {DASHBOARD_PORT}.", ephemeral=True)
        else:
            await ctx.respond("Failed to restart dashboard. Check configuration and logs.", ephemeral=True)


def setup(bot):
    bot.add_cog(DashboardCommands(bot))
