import discord
import logging

logger = logging.getLogger('permissions')

def get_invite_link(application_id, required_scopes=None, permissions=None):
    """
    Generate a Discord bot invite URL with the necessary permissions
    """
    if required_scopes is None:
        required_scopes = ["bot", "applications.commands"]
    
    if permissions is None:
        permissions = discord.Permissions(
            send_messages=True,
            read_messages=True,
            read_message_history=True,
            embed_links=True,
            attach_files=True,
            add_reactions=True,
            use_external_emojis=True,
            create_public_threads=True,
            send_messages_in_threads=True,
            use_slash_commands=True
        )
    
    url = discord.utils.oauth_url(
        application_id, 
        permissions=permissions,
        scopes=required_scopes
    )
    
    return url

def log_bot_permissions(bot, guild):
    """Log the bot's current permissions in a guild"""
    try:
        member = guild.get_member(bot.user.id)
        if not member:
            logger.warning(f"Bot is not a member of {guild.name}")
            return
        
        # Get permissions in guild
        permissions = member.guild_permissions
        logger.info(f"Bot permissions in {guild.name}:")
        
        critical_permissions = {
            "send_messages": permissions.send_messages,
            "read_messages": permissions.read_messages, 
            "read_message_history": permissions.read_message_history,
            "embed_links": permissions.embed_links,
            "attach_files": permissions.attach_files,
            "create_public_threads": permissions.create_public_threads,
            "send_messages_in_threads": permissions.send_messages_in_threads,
            "use_slash_commands": permissions.use_slash_commands,
            "manage_messages": permissions.manage_messages,
            "administrator": permissions.administrator
        }
        
        for perm, value in critical_permissions.items():
            status = "✅" if value else "❌"
            logger.info(f"  {perm}: {status}")
            
        # Check for missing critical permissions
        missing = [perm for perm, value in critical_permissions.items() 
                  if not value and perm != "administrator"]
                  
        if missing:
            logger.warning(f"Bot is missing critical permissions in {guild.name}: {', '.join(missing)}")
            return False
            
        return True
    except Exception as e:
        logger.error(f"Error checking bot permissions: {str(e)}")
        return False

def has_commands_scope(bot, guild_id):
    """
    This is a hypothetical function as Discord doesn't easily allow checking if
    the bot has applications.commands scope, but we can add diagnostics here
    """
    # Unfortunately we can't directly check for applications.commands scope
    # The best we can do is catch failed command registrations in try/except blocks
    logger.info(f"Checking if bot has applications.commands scope in guild {guild_id}")
    return True


def has_channel_permission(member, channel, **perms) -> bool:
    """True if member holds every named permission (or Administrator) in channel.

    Works for threads too: Discord resolves a thread's permissions from its
    parent. Returns False for DMs or unresolved members.
    """
    if member is None or channel is None or not perms:
        return False
    try:
        resolved = channel.permissions_for(member)
    except Exception:
        return False
    if resolved.administrator:
        return True
    return all(getattr(resolved, name, False) for name in perms)


def can_manage_ai_thread(bot, ctx, thread_data: dict) -> bool:
    """Whether ctx.author may rename, delete or reconfigure a tracked AI thread.

    The thread must belong to the invoking server, and the author must have
    created it with /thread new or hold Manage Threads in its parent channel.
    """
    guild = getattr(ctx, "guild", None)
    if guild is None or not thread_data:
        return False
    parent = bot.get_channel(int(thread_data["channel_id"])) if thread_data.get("channel_id") else None
    if parent is None or getattr(parent, "guild", None) != guild:
        return False
    if thread_data.get("creator_id") and str(thread_data["creator_id"]) == str(ctx.author.id):
        return True
    return has_channel_permission(ctx.author, parent, manage_threads=True)


# Discord's CREATE_EVENTS bit (1 << 44); py-cord has no named attribute for it yet
_CREATE_EVENTS_BIT = 1 << 44


def can_create_events(member, channel) -> bool:
    """True if member may create scheduled events (Create or Manage Events, or admin)."""
    if has_channel_permission(member, channel, manage_events=True):
        return True
    try:
        return bool(channel.permissions_for(member).value & _CREATE_EVENTS_BIT)
    except Exception:
        return False
