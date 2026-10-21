"""Interactive help command with category navigation."""
import discord
import logging
from discord.ext import commands
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Command categories with their commands and descriptions
COMMAND_CATEGORIES: Dict[str, Dict] = {
    "chat": {
        "emoji": "💬",
        "name": "Chat & AI",
        "description": "Interact with the AI assistant",
        "commands": [
            {"name": "/chat", "description": "Send a message to the AI (supports image uploads for vision models)"},
            {"name": "/search", "description": "Search the web for current information using AI"},
            {"name": "/reset", "description": "Clear the conversation history for the current channel"},
            {"name": "/summarize", "description": "Summarize the current conversation history"},
            {"name": "/memory status", "description": "Show this channel's memory status"},
            {"name": "/memory me", "description": "See what Gideon remembers about you in this channel"},
            {"name": "/memory forget", "description": "Delete Gideon's notes about you (this channel or everywhere)"},
            {"name": "/memory optout / optin", "description": "Stop or resume per-user memory for you, everywhere"},
        ],
        "admin_only": False
    },
    "url": {
        "emoji": "🔗",
        "name": "URL Tools",
        "description": "Summarize and analyze web content",
        "commands": [
            {"name": "/summarizeurl", "description": "Fetch and summarize the content of a given URL"},
        ],
        "admin_only": False
    },
    "threads": {
        "emoji": "🧵",
        "name": "Threads",
        "description": "Manage AI conversation threads",
        "commands": [
            {"name": "/thread new", "description": "Create a new AI conversation thread"},
            {"name": "/thread message", "description": "Send a message to a specific thread"},
            {"name": "/thread list", "description": "View all active AI threads in the channel"},
            {"name": "/thread show", "description": "View thread configuration settings"},
            {"name": "/thread model", "description": "Set the AI model for this thread"},
            {"name": "/thread system", "description": "Set the system prompt for this thread"},
            {"name": "/thread rename", "description": "Change the name of an AI thread"},
            {"name": "/thread delete", "description": "Remove an AI thread and its history"},
        ],
        "admin_only": False
    },
    "trivia": {
        "emoji": "🎮",
        "name": "Trivia",
        "description": "Play AI-generated trivia games",
        "commands": [
            {"name": "/trivia start", "description": "Start a new trivia game (solo or competitive mode)"},
            {"name": "/trivia stop", "description": "End the current trivia game"},
            {"name": "/trivia stats", "description": "View your trivia statistics or another player's stats"},
            {"name": "/trivia leaderboard", "description": "View server rankings (daily/weekly/monthly/all-time)"},
            {"name": "/trivia achievements", "description": "Display your earned achievement badges"},
        ],
        "admin_only": False
    },
    "images": {
        "emoji": "🎨",
        "name": "Images",
        "description": "Generate AI images with multiple providers",
        "commands": [
            {"name": "/dream", "description": "Generate an image using the configured AI backend"},
        ],
        "admin_commands": [
            {"name": "/dream_manage set_provider", "description": "Set the active image provider (AI Horde/Cloudflare/OpenAI/ComfyUI)"},
            {"name": "/dream_manage view_config", "description": "View current image generation configuration"},
            {"name": "/dream_manage configure ai_horde", "description": "Configure AI Horde defaults (model, size, steps)"},
            {"name": "/dream_manage configure cloudflare", "description": "Configure Cloudflare defaults (size, steps, seed)"},
            {"name": "/dream_manage configure openai", "description": "Configure OpenAI/DALL-E defaults (model, quality, style)"},
            {"name": "/dream_manage configure comfyui", "description": "Configure ComfyUI defaults (model, size, steps)"},
            {"name": "/dream_manage comfyui_models", "description": "List available ComfyUI checkpoint models"},
            {"name": "/dream_manage comfyui_test", "description": "Test ComfyUI server connection"},
            {"name": "/dream_manage comfyui_workflow", "description": "Set a custom ComfyUI workflow (JSON)"},
        ],
        "admin_only": False
    },
    "videos": {
        "emoji": "🎬",
        "name": "Videos",
        "description": "Generate AI videos via OpenRouter",
        "commands": [
            {"name": "/video", "description": "Generate a video from a text prompt (model, duration, aspect ratio, resolution, audio, seed, image_url)"},
        ],
        "admin_commands": [
            {"name": "/video_manage view_config", "description": "View current video generation configuration"},
            {"name": "/video_manage models", "description": "List available video generation models"},
            {"name": "/video_manage configure", "description": "Set defaults for /video (model, duration, aspect ratio, resolution, audio)"},
        ],
        "admin_only": False
    },
    "reminders": {
        "emoji": "⏰",
        "name": "Reminders",
        "description": "Set and manage reminders",
        "commands": [
            {"name": "/remind set", "description": "Set a new reminder with natural language time"},
            {"name": "/remind list", "description": "View your active reminders"},
            {"name": "/remind remove", "description": "Remove a reminder"},
        ],
        "admin_only": False
    },
    "settings": {
        "emoji": "⚙️",
        "name": "Settings",
        "description": "Configure bot settings (Admin only)",
        "commands": [
            {"name": "/settings show", "description": "View all current global settings"},
            {"name": "/settings model", "description": "Set global AI model (format: provider/model)"},
            {"name": "/settings system", "description": "Set global system prompt"},
            {"name": "/settings provider", "description": "Set global AI provider (openrouter/openai)"},
            {"name": "/settings memory", "description": "Set message history limit"},
            {"name": "/settings window", "description": "Set time window for history (hours)"},
            {"name": "/settings restore", "description": "Reset all settings to defaults"},
            {"name": "/channel show", "description": "View current channel settings"},
            {"name": "/channel model", "description": "Set AI model for this channel"},
            {"name": "/channel system", "description": "Set system prompt for this channel"},
            {"name": "/channel provider", "description": "Set AI provider for this channel"},
            {"name": "/channel reset", "description": "Clear all channel overrides"},
            {"name": "/channel list", "description": "List all channels with custom settings"},
        ],
        "admin_only": True
    },
    "admin": {
        "emoji": "🔐",
        "name": "Admin",
        "description": "Administrative tools (Admin only)",
        "commands": [
            {"name": "/admin sync", "description": "Sync slash commands with Discord (Owner only)"},
            {"name": "/admin debug", "description": "Show debug information"},
            {"name": "/admin state", "description": "Display database state information"},
            {"name": "/admin diagnostic", "description": "Run system diagnostics"},
            {"name": "/admin vision_models", "description": "List all vision-capable AI models"},
            {"name": "/admin prune", "description": "Set data pruning frequency"},
            {"name": "/admin retention", "description": "Set summary retention period"},
        ],
        "admin_only": True
    }
}


class CategorySelect(discord.ui.Select):
    """Dropdown menu for selecting help categories."""

    def __init__(self, is_admin: bool = False):
        self.is_admin = is_admin

        # Build options based on available categories
        options = []
        for key, category in COMMAND_CATEGORIES.items():
            # Skip admin categories for non-admins
            if category["admin_only"] and not is_admin:
                continue

            options.append(
                discord.SelectOption(
                    label=category["name"],
                    value=key,
                    description=category["description"][:100],  # Discord limit
                    emoji=category["emoji"]
                )
            )

        super().__init__(
            placeholder="Select a category to view commands...",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        """Handle category selection."""
        selected_key = self.values[0]
        category = COMMAND_CATEGORIES.get(selected_key)

        if not category:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return

        embed = build_category_embed(category, is_admin=self.is_admin)
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    """View containing the help navigation components."""

    def __init__(self, is_admin: bool = False, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.add_item(CategorySelect(is_admin=is_admin))

    async def on_timeout(self):
        """Disable components after timeout."""
        for item in self.children:
            item.disabled = True


def build_category_embed(category: Dict, is_admin: bool = False) -> discord.Embed:
    """Build an embed for a command category."""
    embed = discord.Embed(
        title=f"{category['emoji']} {category['name']}",
        description=category["description"],
        color=discord.Color.blue()
    )

    # Add regular commands
    commands_text = []
    for cmd in category["commands"]:
        commands_text.append(f"**{cmd['name']}**\n{cmd['description']}")

    # Split into multiple fields if needed (Discord field limit is 1024 chars)
    current_field = []
    current_length = 0
    field_num = 1

    for text in commands_text:
        if current_length + len(text) + 2 > 1000:  # Leave some margin
            embed.add_field(
                name=f"Commands{f' (Part {field_num})' if field_num > 1 else ''}",
                value="\n\n".join(current_field),
                inline=False
            )
            current_field = [text]
            current_length = len(text)
            field_num += 1
        else:
            current_field.append(text)
            current_length += len(text) + 2

    # Add remaining commands
    if current_field:
        embed.add_field(
            name=f"Commands{f' (Part {field_num})' if field_num > 1 else ''}",
            value="\n\n".join(current_field),
            inline=False
        )

    # Add admin commands if user is admin and category has them
    if is_admin and category.get("admin_commands"):
        admin_commands_text = []
        for cmd in category["admin_commands"]:
            admin_commands_text.append(f"**{cmd['name']}**\n{cmd['description']}")

        # Split admin commands into fields
        current_field = []
        current_length = 0
        field_num = 1

        for text in admin_commands_text:
            if current_length + len(text) + 2 > 1000:
                embed.add_field(
                    name=f"Admin Commands{f' (Part {field_num})' if field_num > 1 else ''}",
                    value="\n\n".join(current_field),
                    inline=False
                )
                current_field = [text]
                current_length = len(text)
                field_num += 1
            else:
                current_field.append(text)
                current_length += len(text) + 2

        if current_field:
            embed.add_field(
                name=f"Admin Commands{f' (Part {field_num})' if field_num > 1 else ''}",
                value="\n\n".join(current_field),
                inline=False
            )

    if category.get("admin_only"):
        embed.set_footer(text="These commands require Administrator permissions")
    elif is_admin and category.get("admin_commands"):
        embed.set_footer(text="Admin commands shown above require Administrator permissions")

    return embed


def build_overview_embed(is_admin: bool = False) -> discord.Embed:
    """Build the main help overview embed."""
    embed = discord.Embed(
        title="Gideon Help",
        description="Select a category below to view available commands.\n\n"
                    "You can also @mention me with natural language for:\n"
                    "• **Reminders**: \"remind me to check logs tomorrow at 3pm\"\n"
                    "• **Images**: \"draw a sunset over mountains\"\n"
                    "• **Search**: \"what are the best games on Game Pass\"\n"
                    "• **Chat**: Any other message for conversation",
        color=discord.Color.blurple()
    )

    # Add category summary
    categories_text = []
    for key, category in COMMAND_CATEGORIES.items():
        if category["admin_only"] and not is_admin:
            continue

        cmd_count = len(category["commands"])
        # Include admin commands in count for admins
        if is_admin and category.get("admin_commands"):
            cmd_count += len(category["admin_commands"])
        categories_text.append(
            f"{category['emoji']} **{category['name']}** - {cmd_count} command{'s' if cmd_count != 1 else ''}"
        )

    embed.add_field(
        name="Categories",
        value="\n".join(categories_text),
        inline=False
    )

    embed.set_footer(text="Use the dropdown menu below to explore each category")

    return embed


class HelpCommands(commands.Cog, name="HelpCommands"):
    """Interactive help command with category navigation."""

    def __init__(self, bot):
        self.bot = bot

    @discord.slash_command(
        name="help",
        description="View available commands with interactive navigation"
    )
    async def help_command(self, ctx: discord.ApplicationContext):
        """Display interactive help menu."""
        # Check if user is admin
        is_admin = False
        if ctx.guild:
            member = ctx.guild.get_member(ctx.author.id)
            if member:
                is_admin = member.guild_permissions.administrator

        embed = build_overview_embed(is_admin=is_admin)
        view = HelpView(is_admin=is_admin)

        await ctx.respond(embed=embed, view=view, ephemeral=True)


def setup(bot):
    bot.add_cog(HelpCommands(bot))
