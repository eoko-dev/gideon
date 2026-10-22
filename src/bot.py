"""Bot wiring and lifecycle.

Setup (state manager, cogs, background tasks) happens once in main() before
the gateway connection is opened. on_ready only performs the pieces that
require a live connection (command sync), guarded so gateway reconnects
don't repeat them.
"""
import asyncio
import logging
import sys
import time

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

from .config import (
    DISCORD_TOKEN, OPENROUTER_API_KEY, SYSTEM_PROMPT, DEFAULT_MODEL,
    DATA_DIRECTORY, OPENAI_API_KEY, AI_HORDE_API_KEY, DASHBOARD_ENABLED,
    ENCRYPTION_MASTER_KEY,
)
from .utils.state_manager import BotStateManager
from .utils.webhook_sender import WebhookSender
from .utils.conversation_service import ConversationService
from .utils.budget import make_usage_sink
from .utils.usage import set_usage_sink
from .utils.reminder_service import deliver_due_reminders
from .utils.memory_service import archive_and_prune, cancel_background_tasks, llm_clients
from .utils.openrouter_client import OpenRouterClient
from .utils.openai_client import OpenAIClient
from .utils.ai_horde_client import AIHordeClient
from .utils.model_manager import ProviderManager
from .utils.discord_fmt import SAFE_ALLOWED_MENTIONS

logger = logging.getLogger(__name__)

load_dotenv()

# Create intents
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="unused!",
    intents=intents,
    sync_commands=False,
    allowed_mentions=SAFE_ALLOWED_MENTIONS,
)
bot.start_time = time.time()

# ─── Provider clients ────────────────────────────────────────────────────────

openrouter_client = OpenRouterClient(
    api_key=OPENROUTER_API_KEY,
    system_prompt=SYSTEM_PROMPT,
    default_model=DEFAULT_MODEL
)

openai_client = None
if OPENAI_API_KEY:
    openai_client = OpenAIClient(api_key=OPENAI_API_KEY)
    logger.info("OpenAI client initialized.")
else:
    logger.warning("OPENAI_API_KEY not found. OpenAI provider will not be available.")

ai_horde_client = None
if AI_HORDE_API_KEY:
    ai_horde_client = AIHordeClient(api_key=AI_HORDE_API_KEY)
    logger.info("AI Horde client initialized.")
else:
    logger.warning("AI_HORDE_API_KEY not found. AI Horde provider will not be available.")

provider_manager = ProviderManager(openrouter_client, openai_client, ai_horde_client, DATA_DIRECTORY)

bot.model_manager = provider_manager
bot.openrouter_client = openrouter_client
bot.openai_client = openai_client
bot.ai_horde_client = ai_horde_client

# ─── Cogs ────────────────────────────────────────────────────────────────────

COGS = [
    "src.cogs.chat_commands",
    "src.cogs.thread_commands",
    "src.cogs.config_commands",
    "src.cogs.mention_commands",
    "src.cogs.unified_image_commands",  # /dream (user) + /dream_manage (admin)
    "src.cogs.video_commands",          # /video (user) + /video_manage (admin)
    "src.cogs.url_commands",
    "src.cogs.reminder_commands",
    "src.cogs.trivia_commands",
    "src.cogs.help_commands",           # Interactive help system
    "src.cogs.dashboard_commands",      # Web admin dashboard
    "src.cogs.persona_commands",        # Per-channel personas with webhooks
    "src.cogs.context_menu_commands",   # /catchup + message context menus
]

# These cogs provide slash commands that duplicate dashboard functionality.
# When the dashboard is enabled, skip them to reduce Discord command clutter.
ADMIN_COGS = [
    "src.cogs.settings_commands",   # /settings (model, provider, system, memory, etc.)
    "src.cogs.channel_commands",    # /channel (model, provider, system, reset, list)
    "src.cogs.admin_commands",      # /admin (state, diagnostic, prune, vision_models)
]


async def _setup_state() -> BotStateManager:
    """Initializes the state manager, webhook sender, and API key service."""
    state = BotStateManager()
    await state.initialize_state()
    state.set_model_manager(bot.model_manager)
    # Lets the state manager map a thread to its parent's memory scope
    state.set_channel_resolver(bot.get_channel)
    bot.state_manager = state
    logger.info(f"State manager initialized. Global model: {state.get_global_model()}")

    bot.webhook_sender = WebhookSender(bot)
    bot.conversation_service = ConversationService(bot)
    set_usage_sink(make_usage_sink(state))

    # Seed built-in persona templates
    from .utils.persona_templates import BUILTIN_TEMPLATES
    seeded = state.db_manager.seed_builtin_persona_templates(BUILTIN_TEMPLATES)
    existing = len(state.db_manager.get_all_persona_templates())
    logger.info(f"Persona templates: {seeded} new seeded, {existing} total available")

    _setup_api_key_service(state)
    return state


def _setup_api_key_service(state: BotStateManager) -> None:
    """Initializes encrypted API key management and hot-swaps DB-stored keys."""
    bot.api_key_service = None
    if not ENCRYPTION_MASTER_KEY:
        logger.info("ENCRYPTION_MASTER_KEY not set — API key management disabled")
        return

    try:
        from .utils.encryption import EncryptionManager
        from .utils.api_key_service import APIKeyService
        encryption_mgr = EncryptionManager(ENCRYPTION_MASTER_KEY)
        api_key_service = APIKeyService(state.db_manager, encryption_mgr)
        bot.api_key_service = api_key_service
        logger.info("API Key Service initialized.")

        # Hot-swap provider keys from DB if available
        for provider, client_attr in [
            ('openrouter', 'openrouter_client'),
            ('openai', 'openai_client'),
            ('ai_horde', 'ai_horde_client'),
        ]:
            db_key = api_key_service.get_key_for_provider(provider)
            client = getattr(bot, client_attr, None)
            if client and db_key:
                if provider == 'openai':
                    # OpenAI SDK caches key internally — recreate client
                    bot.openai_client = OpenAIClient(api_key=db_key)
                    bot.model_manager.providers['openai'] = bot.openai_client
                    logger.info("OpenAI client re-initialized with DB key")
                else:
                    client.api_key = db_key
                    logger.info(f"{provider} client key updated from DB")
            elif not client and db_key and provider == 'openai':
                # Client wasn't initialized because .env had no key, but DB has one
                bot.openai_client = OpenAIClient(api_key=db_key)
                bot.model_manager.providers['openai'] = bot.openai_client
                logger.info("OpenAI client initialized with DB key (was missing from .env)")
            elif not client and db_key and provider == 'ai_horde':
                bot.ai_horde_client = AIHordeClient(api_key=db_key)
                logger.info("AI Horde client initialized with DB key (was missing from .env)")
    except Exception:
        logger.exception("API Key Service initialization failed")


def _load_cogs() -> None:
    """Loads all cogs once, before the gateway connection opens."""
    cogs = list(COGS)
    if not DASHBOARD_ENABLED:
        cogs.extend(ADMIN_COGS)
    else:
        logger.info("Dashboard is enabled - hiding admin slash commands (/settings, /channel, /admin) from Discord.")

    for cog in cogs:
        try:
            bot.load_extension(cog)
            logger.info(f"{cog} loaded successfully.")
        except Exception:
            logger.exception(f"Error loading {cog}")


def _start_background_tasks() -> None:
    """Starts the pruning and reminder loops (they wait for readiness)."""
    if not prune_data_task.is_running():
        try:
            prune_hours = bot.state_manager.get_prune_frequency_hours()
            prune_data_task.change_interval(hours=prune_hours)
            prune_data_task.start()
            logger.info(f"Started data pruning task to run every {prune_hours} hours.")
        except Exception:
            logger.exception("Error starting pruning task")

    if not check_reminders_task.is_running():
        try:
            check_reminders_task.start()
            logger.info("Started reminder checking task to run every minute.")
        except Exception:
            logger.exception("Error starting reminder task")


# ─── One-time setup (idempotent, safe from any entry path) ──────────────────

_setup_lock = asyncio.Lock()
_setup_done = False


def _ensure_logging():
    """Configures logging once, if nothing else has."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s:%(levelname)s:%(name)s: %(message)s')
        logging.getLogger('discord').setLevel(logging.WARNING)
        logging.getLogger('websockets').setLevel(logging.WARNING)


async def _ensure_setup():
    """Runs the one-time setup (state, cogs, background tasks) exactly once.

    Called from main() on the normal path, and again from on_ready as a
    safety net so a legacy entry point that calls bot.run() directly still
    gets a fully initialized bot.
    """
    global _setup_done
    async with _setup_lock:
        if _setup_done:
            return
        _ensure_logging()
        await _setup_state()
        _load_cogs()
        _start_background_tasks()
        _setup_done = True


# ─── Gateway events ──────────────────────────────────────────────────────────

_gateway_setup_done = False


@bot.event
async def on_ready():
    global _gateway_setup_done
    logger.info(f"Logged in as {bot.user.name} ({bot.user.id})")

    # Safety net: if the bot was started without main() (e.g. a direct
    # bot.run() entry point), perform the one-time setup now, before
    # command sync, so cogs and state exist.
    if not _setup_done:
        logger.warning("Bot was started without main(); running setup from on_ready. "
                       "Prefer starting via 'python -m src'.")
        try:
            await _ensure_setup()
        except Exception:
            logger.exception("FATAL: setup failed during on_ready")
            await bot.close()
            return

    if _gateway_setup_done:
        logger.info("Reconnected to gateway; skipping one-time setup.")
        return
    _gateway_setup_done = True

    # Sync slash commands (requires a live connection, so it can't run in main)
    try:
        existing_commands = await bot.http.get_global_commands(bot.user.id)
        existing_command_names = {cmd['name'] for cmd in existing_commands}

        current_command_names = {cmd.name for cmd in bot.application_commands}
        new_commands = current_command_names - existing_command_names
        removed_commands = existing_command_names - current_command_names
        if new_commands:
            logger.info(f"Adding new commands: {', '.join(new_commands)}")
        if removed_commands:
            logger.info(f"Removing commands: {', '.join(removed_commands)}")

        await bot.sync_commands()
        logger.info("Synced commands globally. They may take up to an hour to appear across all servers.")
    except Exception:
        logger.exception("Error syncing commands")

    # Preload models for the global provider (uses cached data if available)
    try:
        global_provider = bot.state_manager.global_provider
        await bot.model_manager.get_models(global_provider)
        logger.info(f"Loaded models for global provider: {global_provider}")
    except Exception:
        logger.exception("Error preloading models")

    # Start the admin dashboard if enabled (idempotent)
    dashboard_cog = bot.get_cog("DashboardCommands")
    if dashboard_cog:
        try:
            await dashboard_cog.start_dashboard()
        except Exception:
            logger.exception("Error starting dashboard")

    logger.info("Ready to serve!")


@bot.event
async def on_application_command_error(ctx, error):
    """Last-resort handler: every failed slash command gets a short, safe reply.

    Permission and cooldown failures are explained; anything else is logged
    with its traceback and the user sees a generic message, never the raw
    exception text.
    """
    original = getattr(error, "original", error)
    if isinstance(error, (commands.MissingPermissions, commands.NotOwner, commands.CheckFailure)):
        message = "⛔ You don't have permission to use this command."
    elif isinstance(error, commands.CommandOnCooldown):
        message = f"⏳ Slow down — try again in {error.retry_after:.0f}s."
    elif isinstance(original, discord.Forbidden):
        message = "⚠️ I'm missing a Discord permission needed for that."
    else:
        command = getattr(getattr(ctx, "command", None), "qualified_name", "?")
        logger.error(f"Unhandled error in /{command}", exc_info=(type(original), original, original.__traceback__))
        message = "⚠️ Something went wrong running that command. The error has been logged."
    try:
        if ctx.response.is_done():
            await ctx.followup.send(message, ephemeral=True)
        else:
            await ctx.respond(message, ephemeral=True)
    except Exception as send_error:
        logger.debug(f"Could not report command error to the user: {send_error}")


async def _shutdown_cleanup():
    """Stops background work and closes HTTP sessions before the DB closes."""
    for task in (prune_data_task, check_reminders_task):
        if task.is_running():
            task.cancel()
    await cancel_background_tasks()
    server = getattr(bot, "dashboard_server", None)
    if server is not None:
        try:
            await server.stop()
        except Exception as e:
            logger.warning(f"Error stopping dashboard: {e}")

    # Provider clients on the bot and on cogs hold aiohttp sessions
    holders = [getattr(bot, name, None) for name in ("openrouter_client", "openai_client", "ai_horde_client")]
    for cog in list(bot.cogs.values()):
        holders.extend(value for name, value in vars(cog).items() if name.endswith("_client"))
        if hasattr(cog, "_fetch_session"):
            holders.append(getattr(cog, "_fetch_session"))
    seen = set()
    for holder in holders:
        if holder is None or id(holder) in seen:
            continue
        seen.add(id(holder))
        close = getattr(holder, "close_session", None) or getattr(holder, "close", None)
        if close is None:
            continue
        try:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.debug(f"Error closing {type(holder).__name__}: {e}")


# ─── Background tasks ────────────────────────────────────────────────────────

@tasks.loop(hours=24)  # Default interval, adjusted in _start_background_tasks
async def prune_data_task():
    """Periodically prunes old data from the database."""
    if not bot.is_ready() or not hasattr(bot, 'state_manager'):
        return

    logger.info("Running periodic data pruning...")
    try:
        # Summarizes channel history before the time window deletes it
        prune_stats = await archive_and_prune(bot.state_manager, llm_clients(bot))
        logger.info(f"Data pruning finished: {prune_stats}")
    except Exception as e:
        logger.error(f"Error during scheduled data pruning: {e}", exc_info=True)


@prune_data_task.before_loop
async def before_prune_data_task():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def check_reminders_task():
    """Periodically checks for due reminders and sends them."""
    if not bot.is_ready() or not hasattr(bot, 'state_manager'):
        return
    try:
        await deliver_due_reminders(bot)
    except Exception as e:
        logger.error(f"Error during reminder check: {e}", exc_info=True)


@check_reminders_task.before_loop
async def before_check_reminders_task():
    await bot.wait_until_ready()


# Attach the tasks to the bot instance so cogs can access them
bot.prune_data_task = prune_data_task
bot.check_reminders_task = check_reminders_task


# ─── Entry point ─────────────────────────────────────────────────────────────

async def main():
    """Initializes everything, then runs the bot until shutdown."""
    if not DISCORD_TOKEN:
        logger.critical("DISCORD_TOKEN is not set. Exiting.")
        sys.exit(1)

    try:
        # `async with bot` rebinds bot.loop to the running loop. The bot was
        # constructed at import time, so without this py-cord schedules gateway
        # heartbeats onto a loop that never runs and they hang forever.
        async with bot:
            await _ensure_setup()
            await bot.start(DISCORD_TOKEN)
    finally:
        try:
            await _shutdown_cleanup()
        except Exception:
            logger.exception("Error during shutdown cleanup")
        if hasattr(bot, 'state_manager'):
            bot.state_manager.close_db()
        logger.info("Shutdown complete.")


def run():
    """Configures logging and runs the bot (synchronous entry point)."""
    _ensure_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted; shutting down.")


if __name__ == "__main__":
    run()
