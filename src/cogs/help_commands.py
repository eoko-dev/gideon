"""Interactive help command with category navigation."""
import discord
import logging
from discord.ext import commands
from typing import Dict

logger = logging.getLogger(__name__)

# Categories group commands by their top-level name. The commands themselves
# (names, descriptions, subcommands) are read from the bot's registered
# application commands, so /help can't drift from what actually exists.
CATEGORY_DEFS = [
    ("chat", "💬", "Chat & AI", "Talk to Gideon and manage what it remembers",
     ["chat", "search", "reset", "summarize", "memory", "setprovider"]),
    ("threads", "🧵", "Threads", "AI conversation threads", ["thread"]),
    ("tools", "🔗", "Tools", "Summaries, reminders and catch-ups",
     ["summarize_url", "remind", "listreminders", "cancelreminder", "catchup", "me"]),
    ("trivia", "🎮", "Trivia", "AI-generated trivia games", ["trivia"]),
    ("images", "🎨", "Images & video", "Generate images and videos", ["dream", "dream_manage", "video", "video_manage"]),
    ("personas", "🎭", "Personas", "Per-channel personas (admin)", ["persona"]),
    ("settings", "⚙️", "Settings", "Bot and channel configuration (admin)", ["settings", "channel"]),
    ("admin", "🔐", "Admin", "Administrative tools", ["admin", "dashboard"]),
]

# Top-level commands that are admin-only even if Discord doesn't gate them
ADMIN_ROOTS = {"settings", "channel", "admin", "dream_manage", "video_manage", "dashboard", "persona"}


def _is_admin_root(root) -> bool:
    if root.name in ADMIN_ROOTS:
        return True
    perms = getattr(root, "default_member_permissions", None)
    return bool(perms is not None and getattr(perms, "value", 0))


def iter_slash_commands(commands_list, root=None, prefix=""):
    """Yields (command, root, full name) for every invocable slash command."""
    for command in commands_list:
        top = root or command
        name = f"{prefix}{command.name}"
        if isinstance(command, discord.SlashCommandGroup):
            yield from iter_slash_commands(command.subcommands, top, f"{name} ")
        elif isinstance(command, discord.SlashCommand):
            yield command, top, name


def build_categories(bot) -> Dict[str, Dict]:
    """Builds the help categories from the bot's registered commands."""
    categories = {key: {"emoji": emoji, "name": name, "description": desc, "commands": [],
                        "admin_commands": [], "roots": set(roots)}
                  for key, emoji, name, desc, roots in CATEGORY_DEFS}
    other = {"emoji": "📦", "name": "Other", "description": "Everything else", "commands": [],
             "admin_commands": [], "roots": set()}
    menus = {"emoji": "🖱️", "name": "Message menu",
             "description": "Right-click (or long-press) a message → Apps", "commands": [],
             "admin_commands": [], "roots": set()}

    # pending_application_commands is everything the cogs registered;
    # application_commands is only filled once Discord has synced them
    registered = list(getattr(bot, "pending_application_commands", None) or bot.application_commands)
    for command, root, full_name in sorted(iter_slash_commands(registered), key=lambda t: t[2]):
        target = next((c for c in categories.values() if root.name in c["roots"]), other)
        entry = {"name": f"/{full_name}", "description": command.description or ""}
        target["admin_commands" if _is_admin_root(root) else "commands"].append(entry)

    for command in registered:
        if isinstance(command, discord.MessageCommand):
            menus["commands"].append({"name": command.name, "description": "Message context menu"})

    result = {}
    for key, category in list(categories.items()) + [("menus", menus), ("other", other)]:
        if category["commands"] or category["admin_commands"]:
            category["admin_only"] = not category["commands"]
            result[key] = category
    return result


class CategorySelect(discord.ui.Select):
    """Dropdown menu for selecting help categories."""

    def __init__(self, categories: Dict[str, Dict], is_admin: bool = False):
        self.is_admin = is_admin
        self.categories = categories

        # Build options based on available categories
        options = []
        for key, category in categories.items():
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
        category = self.categories.get(selected_key)

        if not category:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return

        embed = build_category_embed(category, is_admin=self.is_admin)
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    """View containing the help navigation components."""

    def __init__(self, categories: Dict[str, Dict], is_admin: bool = False, timeout: float = 180):
        super().__init__(timeout=timeout)
        self.add_item(CategorySelect(categories, is_admin=is_admin))

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


def build_overview_embed(categories: Dict[str, Dict], is_admin: bool = False,
                         tools_enabled: bool = True) -> discord.Embed:
    """Build the main help overview embed."""
    description = "Select a category below to view available commands.\n\n"
    if tools_enabled:
        description += ("You can also @mention me in plain language, for example:\n"
                        "• \"remind me to check the logs tomorrow at 3pm\"\n"
                        "• \"draw a sunset over mountains\"\n"
                        "• \"what's the latest on the Artemis mission?\" (web search)\n"
                        "• \"make a poll: pizza or tacos?\" · \"roll 2d20\" · \"what's 15% of 80?\"\n"
                        "• \"what did Sam say about the raid?\" · \"what's pinned here?\"\n"
                        "• \"remember that I'm vegetarian\" · \"what are my reminders?\"\n"
                        "Replying to one of my messages works too.")
    else:
        description += "You can also @mention me to chat. (Natural-language tools are turned off.)"
    embed = discord.Embed(title="Gideon Help", description=description, color=discord.Color.blurple())

    # Add category summary
    categories_text = []
    for key, category in categories.items():
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

        categories = build_categories(self.bot)
        state = getattr(self.bot, "state_manager", None)
        tools_enabled = bool(state.get_tool_calling_enabled()) if state else False
        embed = build_overview_embed(categories, is_admin=is_admin, tools_enabled=tools_enabled)
        view = HelpView(categories, is_admin=is_admin)

        await ctx.respond(embed=embed, view=view, ephemeral=True)


def setup(bot):
    bot.add_cog(HelpCommands(bot))
