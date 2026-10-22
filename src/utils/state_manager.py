"""Centralized state management for the bot, using SQLite backend."""
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import logging
import asyncio # Keep for potential async operations like model validation

import discord

# Import the new DatabaseManager
from .database import DatabaseManager
from . import user_memory
# Keep config import for default values if DB is empty
from ..config import (
    DEFAULT_MODEL as CONFIG_DEFAULT_MODEL,
    CONFIG_TOOL_CALLING_ENABLED,
    TOOL_CALLING_MAX_ITERATIONS as CONFIG_TOOL_CALLING_MAX_ITERATIONS,
    SYSTEM_PROMPT as CONFIG_SYSTEM_PROMPT,
)

logger = logging.getLogger('state_manager')

# Appended to every system prompt (persona, channel, or global) so the model
# knows it is in a multi-user room and that user turns are name-labelled.
MULTI_USER_FRAMING = """--- Conversation Format ---
You are chatting in a multi-user Discord channel. Each user message is prefixed
with the sender's display name, like "Alice: hello". Different names are
different people:
- Reply to the person who sent the most recent message, addressing them by name
  when it feels natural.
- Never merge or confuse different users, even when they discuss the same topic.
  Keep track of who said what.
- Text like @Name refers to a user being mentioned.
- Do NOT prefix your own replies with any name or "Name:" label — just respond."""

# --- Constants for DB keys ---
# These help avoid typos when accessing global config in the DB
CONFIG_KEY_MAX_HISTORY = "max_channel_history"
CONFIG_KEY_TIME_WINDOW = "time_window_hours"
CONFIG_KEY_GLOBAL_MODEL = "global_model"
CONFIG_KEY_GLOBAL_PROVIDER = "global_provider"
CONFIG_KEY_GLOBAL_SYSTEM_PROMPT = "global_system_prompt"
CONFIG_KEY_PRUNE_FREQUENCY_HOURS = "prune_frequency_hours"
CONFIG_KEY_INTENT_ENABLED = "intent_enabled"  # Legacy DB key, now stores the tool-calling toggle
CONFIG_KEY_SESSION_TIMEOUT = "session_timeout_hours"
CONFIG_KEY_MEMORY_SUMMARY_ENABLED = "memory_summary_enabled"
CONFIG_KEY_MAX_MEMORY_SUMMARIES = "max_memory_summaries"
CONFIG_KEY_USER_MEMORY_ENABLED = "user_memory_enabled"
CONFIG_KEY_TOOL_CALLING_MAX_ITERATIONS = "tool_calling_max_iterations"
CONFIG_KEY_UTILITY_MODEL = "utility_model"
CONFIG_KEY_DAILY_COST_BUDGET = "daily_user_cost_budget"
CONFIG_KEY_DAILY_IMAGE_LIMIT = "daily_user_image_limit"
CONFIG_KEY_DAILY_VIDEO_LIMIT = "daily_user_video_limit"

class BotStateManager:
    """Singleton class to manage shared state via DatabaseManager."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(BotStateManager, cls).__new__(cls)
            # Initialization moved to a separate async method to handle potential async loads
            # cls._instance._initialize() # Removed sync initialization
            logger.info(f"Created new BotStateManager instance placeholder with id: {id(cls._instance)}")
        else:
            logger.info(f"Reusing existing BotStateManager instance with id: {id(cls._instance)}")
        return cls._instance

    async def initialize_state(self):
        """Asynchronously initializes the state manager and database connection."""
        if hasattr(self, '_initialized') and self._initialized:
            logger.info("StateManager already initialized.")
            return

        logger.info(f"Initializing BotStateManager instance with id: {id(self)}")
        self.db_manager = DatabaseManager() # Initialize DB connection and schema
        self.model_manager = None # Placeholder for ModelManager instance

        # Load configuration from DB or set defaults
        self.max_channel_history = await self._load_or_set_config(CONFIG_KEY_MAX_HISTORY, 35, 'int')
        self.time_window_hours = await self._load_or_set_config(CONFIG_KEY_TIME_WINDOW, 48, 'int')
        self.global_model = await self._load_or_set_config(CONFIG_KEY_GLOBAL_MODEL, CONFIG_DEFAULT_MODEL, 'string')
        self.global_provider = await self._load_or_set_config(CONFIG_KEY_GLOBAL_PROVIDER, "openrouter", 'string')
        self.global_system_prompt = await self._load_or_set_config(CONFIG_KEY_GLOBAL_SYSTEM_PROMPT, None, 'string')
        self.prune_frequency_hours = await self._load_or_set_config(CONFIG_KEY_PRUNE_FREQUENCY_HOURS, 24, 'int')

        # Tool calling settings - load from DB or use .env defaults
        # Uses CONFIG_TOOL_CALLING_ENABLED which combines TOOL_CALLING_ENABLED
        # and INTENT_DISCOVERY for backward compatibility
        self.tool_calling_enabled = await self._load_or_set_config(CONFIG_KEY_INTENT_ENABLED, CONFIG_TOOL_CALLING_ENABLED, 'bool')
        self.tool_calling_max_iterations = await self._load_or_set_config(CONFIG_KEY_TOOL_CALLING_MAX_ITERATIONS, CONFIG_TOOL_CALLING_MAX_ITERATIONS, 'int')

        # Memory system settings
        self.session_timeout_hours = await self._load_or_set_config(CONFIG_KEY_SESSION_TIMEOUT, 24, 'int')
        self.memory_summary_enabled = await self._load_or_set_config(CONFIG_KEY_MEMORY_SUMMARY_ENABLED, True, 'bool')
        self.max_memory_summaries = await self._load_or_set_config(CONFIG_KEY_MAX_MEMORY_SUMMARIES, 10, 'int')
        self.user_memory_enabled = await self._load_or_set_config(CONFIG_KEY_USER_MEMORY_ENABLED, True, 'bool')

        # Cheap model for background work (summaries, notes, time parsing);
        # None means "use the channel's own model"
        self.utility_model = await self._load_or_set_config(CONFIG_KEY_UTILITY_MODEL, None, 'string')

        # Per-user daily limits (0 = unlimited). Admins are exempt.
        self.daily_user_cost_budget = await self._load_or_set_config(CONFIG_KEY_DAILY_COST_BUDGET, 0.0, 'float')
        self.daily_user_image_limit = await self._load_or_set_config(CONFIG_KEY_DAILY_IMAGE_LIMIT, 25, 'int')
        self.daily_user_video_limit = await self._load_or_set_config(CONFIG_KEY_DAILY_VIDEO_LIMIT, 3, 'int')

        self._initialized = True
        logger.info("BotStateManager initialized successfully.")

    async def reinitialize_state(self):
        """Re-reads all configuration from the database into memory.

        Called after config import or database restore to pick up new values
        without requiring a bot restart.
        """
        logger.info("Reinitializing BotStateManager from database...")
        self._initialized = False
        await self.initialize_state()

    async def _load_or_set_config(self, key: str, default_value: Any, value_type: str) -> Any:
        """Loads config from DB or sets default if not found."""
        value = self.db_manager.get_global_config(key)
        if value is None:
            logger.info(f"Config key '{key}' not found in DB, setting default: {default_value}")
            # Handle None default value correctly
            if default_value is not None:
                 self.db_manager.set_global_config(key, default_value, value_type)
                 return default_value
            else:
                 # If default is None, we don't necessarily want to store 'None' string unless type is string
                 if value_type == 'string':
                     self.db_manager.set_global_config(key, None, value_type)
                 # For other types, maybe don't store None? Or store a specific representation?
                 # For now, just return None without storing if default is None and type isn't string.
                 return None
        else:
            # Ensure the type matches what we expect, log warning if not?
            # For now, assume the type in DB is correct.
            return value

    def set_model_manager(self, model_manager):
        """Sets the ModelManager instance, used for model validation."""
        self.model_manager = model_manager
        logger.info(f"ModelManager instance set for BotStateManager: {id(model_manager)}")

    def close_db(self):
        """Closes the database connection."""
        if hasattr(self, 'db_manager') and self.db_manager:
            self.db_manager.close()

    # --- Configuration Methods ---

    def get_max_channel_history(self) -> int:
        return self.max_channel_history

    async def set_max_channel_history(self, value: int):
        self.max_channel_history = value
        await self._save_config(CONFIG_KEY_MAX_HISTORY, value, 'int')

    def get_time_window_hours(self) -> int:
        return self.time_window_hours

    async def set_time_window_hours(self, value: int):
        self.time_window_hours = value
        await self._save_config(CONFIG_KEY_TIME_WINDOW, value, 'int')

    def get_prune_frequency_hours(self) -> int:
        return self.prune_frequency_hours

    async def set_prune_frequency_hours(self, value: int):
        if value < 1:
            raise ValueError("Pruning frequency must be at least 1 hour.")
        self.prune_frequency_hours = value
        await self._save_config(CONFIG_KEY_PRUNE_FREQUENCY_HOURS, value, 'int')

    # --- Tool Calling Methods ---

    def get_tool_calling_enabled(self) -> bool:
        """Gets whether tool calling (native) is enabled."""
        return self.tool_calling_enabled

    async def set_tool_calling_enabled(self, value: bool):
        """Sets whether tool calling is enabled."""
        self.tool_calling_enabled = value
        await self._save_config(CONFIG_KEY_INTENT_ENABLED, value, 'bool')
        logger.info(f"Tool calling {'enabled' if value else 'disabled'}")

    def get_tool_calling_max_iterations(self) -> int:
        """Gets the max tool-calling iterations."""
        return self.tool_calling_max_iterations

    async def set_tool_calling_max_iterations(self, value: int):
        """Sets the max tool-calling iterations (1-10)."""
        if value < 1 or value > 10:
            raise ValueError("Tool calling max iterations must be between 1 and 10.")
        self.tool_calling_max_iterations = value
        await self._save_config(CONFIG_KEY_TOOL_CALLING_MAX_ITERATIONS, value, 'int')
        logger.info(f"Tool calling max iterations set to: {value}")

    # --- Memory System Config ---

    def get_session_timeout_hours(self) -> int:
        return self.session_timeout_hours

    async def set_session_timeout_hours(self, value: int):
        if value < 1 or value > 168:
            raise ValueError("Session timeout must be between 1 and 168 hours.")
        self.session_timeout_hours = value
        await self._save_config(CONFIG_KEY_SESSION_TIMEOUT, value, 'int')

    def get_memory_summary_enabled(self) -> bool:
        return self.memory_summary_enabled

    async def set_memory_summary_enabled(self, value: bool):
        self.memory_summary_enabled = value
        await self._save_config(CONFIG_KEY_MEMORY_SUMMARY_ENABLED, value, 'bool')

    def get_max_memory_summaries(self) -> int:
        return self.max_memory_summaries

    async def set_max_memory_summaries(self, value: int):
        if value < 1 or value > 50:
            raise ValueError("Max memory summaries must be between 1 and 50.")
        self.max_memory_summaries = value
        await self._save_config(CONFIG_KEY_MAX_MEMORY_SUMMARIES, value, 'int')

    def get_user_memory_enabled(self) -> bool:
        return self.user_memory_enabled

    async def set_user_memory_enabled(self, value: bool):
        self.user_memory_enabled = value
        await self._save_config(CONFIG_KEY_USER_MEMORY_ENABLED, value, 'bool')

    # --- Utility model & usage limits ---

    def get_utility_model(self) -> Optional[str]:
        """The provider/model used for background work, or None to use the channel's model."""
        return self.utility_model or None

    async def set_utility_model(self, value: Optional[str]):
        self.utility_model = (value or "").strip() or None
        await self._save_config(CONFIG_KEY_UTILITY_MODEL, self.utility_model, 'string')

    def resolve_utility_model(self, channel_id: Optional[str] = None) -> tuple:
        """(provider, model_name) for background LLM work.

        The configured utility model wins; otherwise the channel's model (or the
        global model when there is no channel) is used, as before.
        """
        utility = self.get_utility_model()
        if utility and "/" in utility:
            provider, name = utility.split("/", 1)
            return provider, name
        if channel_id is not None:
            return self.resolve_model(channel_id)
        model = self.get_global_model()
        if "/" in model:
            provider, name = model.split("/", 1)
            return provider, name
        return self.global_provider, model

    def get_daily_user_cost_budget(self) -> float:
        return float(self.daily_user_cost_budget or 0)

    async def set_daily_user_cost_budget(self, value: float):
        self.daily_user_cost_budget = float(value)
        await self._save_config(CONFIG_KEY_DAILY_COST_BUDGET, self.daily_user_cost_budget, 'float')

    def get_daily_user_image_limit(self) -> int:
        return int(self.daily_user_image_limit or 0)

    async def set_daily_user_image_limit(self, value: int):
        self.daily_user_image_limit = int(value)
        await self._save_config(CONFIG_KEY_DAILY_IMAGE_LIMIT, self.daily_user_image_limit, 'int')

    def get_daily_user_video_limit(self) -> int:
        return int(self.daily_user_video_limit or 0)

    async def set_daily_user_video_limit(self, value: int):
        self.daily_user_video_limit = int(value)
        await self._save_config(CONFIG_KEY_DAILY_VIDEO_LIMIT, self.daily_user_video_limit, 'int')

    def get_effective_session_timeout(self, channel_id: str) -> int:
        """Returns channel override if set, otherwise the global value."""
        val = self._get_channel_memory_config(channel_id, 'session_timeout_hours')
        return int(val) if val is not None else self.session_timeout_hours

    def get_effective_memory_summary_enabled(self, channel_id: str) -> bool:
        """Returns channel override if set, otherwise the global value."""
        val = self._get_channel_memory_config(channel_id, 'memory_summary_enabled')
        if val is not None:
            return bool(int(val))
        return self.memory_summary_enabled

    def get_effective_max_memory_summaries(self, channel_id: str) -> int:
        """Returns channel override if set, otherwise the global value."""
        val = self._get_channel_memory_config(channel_id, 'max_memory_summaries')
        return int(val) if val is not None else self.max_memory_summaries

    def get_effective_user_memory_enabled(self, channel_id: str) -> bool:
        """Whether per-user notes are kept in this channel's memory scope.

        A thread follows its parent channel's setting.
        """
        val = self._get_channel_memory_config(self.get_memory_scope(channel_id), 'user_memory_enabled')
        if val is not None:
            return bool(int(val))
        return self.user_memory_enabled

    def _get_channel_memory_config(self, channel_id: str, column: str):
        """Reads a single memory config column from CHANNEL_CONFIG, returns None if not set."""
        import sqlite3
        sql = f"SELECT {column} FROM CHANNEL_CONFIG WHERE channel_id = ?;"
        try:
            cursor = self.db_manager._conn.cursor()
            cursor.execute(sql, (str(channel_id),))
            row = cursor.fetchone()
            if row:
                return row[column]
            return None
        except sqlite3.Error as e:
            logger.error(f"Error reading {column} for channel {channel_id}: {e}", exc_info=True)
            return None

    _CHANNEL_MEMORY_FIELDS = ('session_timeout_hours', 'memory_summary_enabled', 'max_memory_summaries',
                              'user_memory_enabled')
    _CHANNEL_MEMORY_BOOL_FIELDS = ('memory_summary_enabled', 'user_memory_enabled')

    def set_channel_memory_config(self, channel_id: str, **overrides):
        """Updates the given per-channel memory overrides; a value of None clears it (falls back to global)."""
        import sqlite3
        unknown = set(overrides) - set(self._CHANNEL_MEMORY_FIELDS)
        if unknown:
            raise ValueError(f"Unknown memory config fields: {', '.join(sorted(unknown))}")
        self.db_manager._ensure_channel_exists(str(channel_id))
        updates = []
        params = []
        for field in self._CHANNEL_MEMORY_FIELDS:
            if field not in overrides:
                continue
            value = overrides[field]
            if field in self._CHANNEL_MEMORY_BOOL_FIELDS and value is not None:
                value = 1 if value else 0
            updates.append(f"{field} = ?")
            params.append(value)
        if not updates:
            return
        params.append(str(channel_id))
        sql = f"UPDATE CHANNEL_CONFIG SET {', '.join(updates)} WHERE channel_id = ?;"
        try:
            with self.db_manager._conn:
                cursor = self.db_manager._conn.cursor()
                cursor.execute(sql, params)
        except sqlite3.Error as e:
            logger.error(f"Error updating memory config for channel {channel_id}: {e}", exc_info=True)
            raise

    def reset_channel_memory_config(self, channel_id: str):
        """Clears per-channel memory overrides so the channel falls back to global."""
        import sqlite3
        sql = """UPDATE CHANNEL_CONFIG
                 SET session_timeout_hours = NULL,
                     memory_summary_enabled = NULL,
                     max_memory_summaries = NULL,
                     user_memory_enabled = NULL
                 WHERE channel_id = ?;"""
        try:
            with self.db_manager._conn:
                cursor = self.db_manager._conn.cursor()
                cursor.execute(sql, (str(channel_id),))
        except sqlite3.Error as e:
            logger.error(f"Error resetting memory config for channel {channel_id}: {e}", exc_info=True)
            raise

    # --- Channel Memory Delegation ---

    def get_channel_memories(self, channel_id: str, limit: int = 10):
        return self.db_manager.get_channel_memories(str(channel_id), limit)

    def add_channel_memory(self, channel_id: str, summary: str, message_count: int,
                           conversation_start=None) -> int:
        return self.db_manager.add_channel_memory(str(channel_id), summary, message_count, conversation_start)

    def delete_channel_memories(self, channel_id: str) -> int:
        return self.db_manager.delete_channel_memories(str(channel_id))

    def prune_channel_memories(self, channel_id: str, max_count: int) -> int:
        return self.db_manager.prune_channel_memories(str(channel_id), max_count)

    def get_all_memory_stats(self):
        return self.db_manager.get_all_memory_stats()

    # --- Per-User Memory (scoped to one channel) ---
    #
    # Notes are always keyed by (memory scope, user_id). Callers pass the
    # channel the conversation happens in and these resolve its scope, so
    # nothing outside this section decides which channel a note belongs to.

    def set_channel_resolver(self, resolver):
        """Registers a callable mapping an int channel ID to a Discord channel (e.g. bot.get_channel)."""
        self._channel_resolver = resolver

    def get_memory_scope(self, channel_id: str) -> str:
        """Returns the channel ID whose per-user memory a conversation belongs to.

        Threads share their parent channel's memory; every other channel is
        its own scope.
        """
        channel_id = str(channel_id)
        thread = self.db_manager.get_thread_info(channel_id)
        if thread and thread.get('channel_id'):
            return str(thread['channel_id'])
        resolver = getattr(self, '_channel_resolver', None)
        if resolver is not None:
            try:
                channel = resolver(int(channel_id))
            except (TypeError, ValueError):
                channel = None
            if isinstance(channel, discord.Thread) and channel.parent_id:
                return str(channel.parent_id)
        return channel_id

    def get_user_notes(self, channel_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        return self.db_manager.user_memory.get_notes(self.get_memory_scope(channel_id), str(user_id))

    def get_user_notes_for(self, channel_id: str, user_ids) -> Dict[str, Dict[str, Any]]:
        return self.db_manager.user_memory.get_notes_for_users(self.get_memory_scope(channel_id), user_ids)

    def set_user_notes(self, channel_id: str, user_id: str, notes: str,
                       display_name: Optional[str] = None, added_messages: int = 0):
        scope = self.get_memory_scope(channel_id)
        self.db_manager._ensure_channel_exists(scope)
        self.db_manager.user_memory.set_notes(scope, str(user_id), notes, display_name, added_messages)

    def delete_user_notes(self, channel_id: str, user_id: str) -> int:
        return self.db_manager.user_memory.delete_notes(self.get_memory_scope(channel_id), str(user_id))

    def delete_user_notes_everywhere(self, user_id: str) -> int:
        return self.db_manager.user_memory.delete_user_everywhere(str(user_id))

    def list_channel_user_notes(self, channel_id: str) -> List[Dict[str, Any]]:
        return self.db_manager.user_memory.list_channel_notes(self.get_memory_scope(channel_id))

    def delete_channel_user_notes(self, channel_id: str) -> int:
        return self.db_manager.user_memory.delete_channel_notes(self.get_memory_scope(channel_id))

    def get_user_memory_stats(self) -> Dict[str, int]:
        return self.db_manager.user_memory.get_stats()

    def is_user_memory_opted_out(self, user_id: str) -> bool:
        """True if the user asked Gideon not to keep notes about them in any channel."""
        prefs = self.db_manager.get_user_preferences(str(user_id)) or {}
        return bool(prefs.get('memory_opt_out'))

    def set_user_memory_opt_out(self, user_id: str, opted_out: bool) -> int:
        """Sets the opt-out flag; opting out also deletes the user's notes everywhere.

        Returns the number of notes deleted.
        """
        prefs = self.db_manager.get_user_preferences(str(user_id)) or {}
        prefs['memory_opt_out'] = bool(opted_out)
        self.db_manager.set_user_preferences(str(user_id), prefs)
        return self.delete_user_notes_everywhere(user_id) if opted_out else 0

    async def reload_all_from_env(self, system_prompt: str) -> dict:
        """Reloads all settings from environment variables. Returns dict of reloaded values."""
        # Reimport config to get fresh values (in case .env was modified)
        from ..config import (
            DEFAULT_MODEL,
            CONFIG_TOOL_CALLING_ENABLED,
            TOOL_CALLING_MAX_ITERATIONS,
        )

        # Reload global model
        self.global_model = DEFAULT_MODEL
        await self._save_config(CONFIG_KEY_GLOBAL_MODEL, self.global_model, 'string')

        # Reload memory and time window to defaults from initialization
        self.max_channel_history = 35
        self.time_window_hours = 48
        await self._save_config(CONFIG_KEY_MAX_HISTORY, self.max_channel_history, 'int')
        await self._save_config(CONFIG_KEY_TIME_WINDOW, self.time_window_hours, 'int')

        # Reload system prompt (passed in since it comes from config)
        await self.set_global_system_prompt(system_prompt)

        # Reload tool calling settings
        self.tool_calling_enabled = CONFIG_TOOL_CALLING_ENABLED
        self.tool_calling_max_iterations = TOOL_CALLING_MAX_ITERATIONS
        await self._save_config(CONFIG_KEY_INTENT_ENABLED, self.tool_calling_enabled, 'bool')
        await self._save_config(CONFIG_KEY_TOOL_CALLING_MAX_ITERATIONS, self.tool_calling_max_iterations, 'int')

        logger.info("All settings reloaded from .env")

        return {
            "global_model": self.global_model,
            "max_channel_history": self.max_channel_history,
            "time_window_hours": self.time_window_hours,
            "tool_calling_enabled": self.tool_calling_enabled,
            "tool_calling_max_iterations": self.tool_calling_max_iterations,
        }


    async def _save_config(self, key: str, value: Any, value_type: str):
        """Saves a configuration value to the database."""
        # This can remain synchronous if db_manager methods are sync
        self.db_manager.set_global_config(key, value, value_type)

    def get_global_config(self, key: str, default: Any = None) -> Any:
        """Gets a global configuration value from the database."""
        return self.db_manager.get_global_config(key, default)

    def set_global_config(self, key: str, value: Any, value_type: str):
        """Sets a global configuration value in the database."""
        self.db_manager.set_global_config(key, value, value_type)

    # --- Channel History Methods ---

    def get_channel_history(self, channel_id: str, limit: Optional[int] = None,
                            until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Gets message history for a channel from the database.

        Pass ``until`` (the timestamp returned by add_to_channel_history) to
        exclude messages recorded after the one being answered.
        """
        # Use configured history limit if no specific limit is passed
        effective_limit = limit if limit is not None else self.max_channel_history
        return self.db_manager.get_channel_history(str(channel_id), limit=effective_limit, until=until)

    async def add_to_channel_history(self, channel_id: str, message: Dict[str, Any]) -> Optional[datetime]:
        """Adds a message to the channel history in the database.

        Returns the timestamp the row was stored with, or None if it was skipped.
        """
        # Extract relevant fields from the message dict
        role = message.get("role")
        content = message.get("content")
        user_id = message.get("user_id") # Assuming user_id might be in the message dict
        user_name = message.get("name") # Speaker's display name, used to label turns for the LLM
        is_bot = bool(message.get("is_bot")) # Bots and webhooks are never given per-user notes
        timestamp = datetime.now() # Use current time for DB timestamp

        if not role or not content:
             logger.error(f"Message missing role or content: {message}")
             return None # Don't add incomplete messages

        # The DB call is synchronous — run it off the event loop
        await asyncio.to_thread(
            self.db_manager.add_message,
            role=role,
            content=content,
            timestamp=timestamp,
            channel_id=str(channel_id),
            user_id=user_id,
            user_name=user_name,
            is_bot=is_bot
        )
        return timestamp

    def clear_channel_history(self, channel_id: str) -> bool:
        """Clears history for a channel by deleting messages from the database."""
        deleted_count = self.db_manager.delete_channel_messages(str(channel_id))
        return deleted_count > 0

    def get_channel_message_count(self, channel_id: str) -> int:
        """Counts stored messages for a channel."""
        return self.db_manager.messages.get_channel_message_count(str(channel_id))

    def delete_channel_messages_before(self, channel_id: str, before_timestamp) -> int:
        """Deletes a channel's messages older than the given timestamp."""
        return self.db_manager.delete_channel_messages(str(channel_id), before_timestamp)

    def get_full_channel_history(self, channel_id: str,
                                 until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Gets every stored message for a channel, ignoring the history window.

        Memory archiving needs this: summarizing only the window while
        deleting everything would drop the overflow unsummarized.
        """
        return self.db_manager.get_channel_history(str(channel_id), limit=None, until=until)

    def delete_channel_messages_through(self, channel_id: str, message_pk: int,
                                        before_timestamp: Optional[datetime] = None) -> int:
        """Deletes a channel's messages up to and including ``message_pk``.

        Bounding by primary key deletes exactly the rows that were read and
        summarized, never ones recorded while the summary call was in flight.
        """
        return self.db_manager.delete_channel_messages(str(channel_id), before_timestamp, message_pk)

    def get_channel_ids_with_messages_before(self, cutoff_timestamp: datetime) -> List[str]:
        """Returns the IDs of channels holding messages older than the cutoff."""
        return self.db_manager.messages.get_channel_ids_with_messages_before(cutoff_timestamp)

    def get_thread_ids_with_messages_before(self, cutoff_timestamp: datetime) -> List[str]:
        """Returns the IDs of threads holding messages older than the cutoff."""
        return self.db_manager.messages.get_thread_ids_with_messages_before(cutoff_timestamp)

    def get_full_thread_history(self, thread_id: str,
                                until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Gets every stored message for a thread, ignoring the history window."""
        return self.db_manager.get_thread_history(str(thread_id), limit=None, until=until)

    def delete_thread_messages_through(self, thread_id: str, message_pk: int,
                                       before_timestamp: Optional[datetime] = None) -> int:
        """Deletes a thread's messages up to and including ``message_pk``."""
        return self.db_manager.messages.delete_thread_messages(str(thread_id), before_timestamp, message_pk)

    # --- Discord Thread Methods ---

    async def add_discord_thread(self, thread_id: str, channel_id: str, name: str,
                                 creator_id: Optional[str] = None):
        """Adds a thread record to the database."""
        # Database call is synchronous
        self.db_manager.add_thread(
            thread_id=str(thread_id),
            channel_id=str(channel_id),
            name=name,
            created_at=datetime.now(),
            creator_id=str(creator_id) if creator_id else None,
        )

    async def add_discord_thread_message(self, thread_id: str, message: Dict[str, Any]) -> Optional[datetime]:
        """Adds a message associated with a thread to the database.

        Returns the timestamp the row was stored with, or None if it was skipped.
        """
        role = message.get("role")
        content = message.get("content")
        timestamp = datetime.now()

        if not role or not content:
             logger.error(f"Thread message missing role or content: {message}")
             return None

        # The DB call is synchronous — run it off the event loop
        await asyncio.to_thread(
            self.db_manager.add_message,
            role=role,
            content=content,
            timestamp=timestamp,
            thread_id=str(thread_id),
            user_id=message.get("user_id"),
            user_name=message.get("name"),
            is_bot=bool(message.get("is_bot"))
        )
        return timestamp

    def get_discord_thread_history(self, thread_id: str, limit: Optional[int] = None,
                                   hours_limit: Optional[int] = None,
                                   until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Gets message history for a Discord thread from the database.

        Pass ``until`` (the timestamp returned by add_discord_thread_message) to
        exclude messages recorded after the one being answered.
        """
        effective_limit = limit if limit is not None else self.max_channel_history
        return self.db_manager.get_thread_history(str(thread_id), limit=effective_limit,
                                                  hours_limit=hours_limit, until=until)

    def get_discord_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Gets thread data by Discord thread ID from the database."""
        return self.db_manager.get_thread_info(str(thread_id))

    def list_discord_threads_for_channel(self, channel_id: str) -> List[Dict[str, Any]]:
        """Lists all threads associated with a channel from the database."""
        return self.db_manager.list_threads_for_channel(str(channel_id))

    def delete_discord_thread(self, thread_id: str) -> bool:
        """Deletes a thread by its ID from the database."""
        return self.db_manager.delete_thread(str(thread_id))

    def rename_discord_thread(self, thread_id: str, new_name: str) -> bool:
        """Renames a thread in the database."""
        return self.db_manager.rename_thread(str(thread_id), new_name)

    def set_discord_thread_model(self, thread_id: str, model: Optional[str]):
        """Sets the specific model override for a thread in the database."""
        # Database call is synchronous
        self.db_manager.set_thread_model(str(thread_id), model)

    def set_discord_thread_system_prompt(self, thread_id: str, prompt: Optional[str]):
        """Sets the specific system prompt override for a thread in the database."""
        # Database call is synchronous
        self.db_manager.set_thread_system_prompt(str(thread_id), prompt)

    def get_discord_thread_config(self, thread_id: str) -> Optional[Dict[str, Optional[str]]]:
        """Gets model and system prompt for a thread from the database."""
        return self.db_manager.get_thread_config(str(thread_id))

    def get_thread_message_count(self, thread_id: str) -> int:
        """Gets the number of messages in a specific thread from the database."""
        return self.db_manager.get_thread_message_count(str(thread_id))


    # --- Pruning Method ---

    def prune_old_data(self, retain_channel_ids=(), message_cutoff: Optional[datetime] = None,
                       retain_thread_ids=()) -> Dict[str, int]:
        """Prunes old messages, threads, sent reminders, and trivia sessions from the database.

        Channels in ``retain_channel_ids`` (and threads in ``retain_thread_ids``)
        keep their messages: their pre-prune memory archive failed and is
        retried on the next run.
        Prefer ``memory_service.archive_and_prune``, which summarizes channel
        history before it is deleted.
        """
        logger.info("Starting data pruning...")
        if message_cutoff is None:
            message_cutoff = datetime.now() - timedelta(hours=self.time_window_hours)
        thread_cutoff = datetime.now() - timedelta(days=14)
        reminder_cutoff = datetime.now() - timedelta(days=30)
        trivia_session_days = 30  # Keep trivia sessions for 30 days

        try:
            messages_pruned = self.db_manager.prune_old_messages(message_cutoff, retain_channel_ids,
                                                                 retain_thread_ids)
            threads_pruned = self.db_manager.prune_old_threads(thread_cutoff)
            reminders_pruned = self.db_manager.prune_old_reminders(reminder_cutoff)
            trivia_sessions_pruned = self.db_manager.prune_old_trivia_sessions(trivia_session_days)

            prune_stats = {
                "messages_pruned": messages_pruned,
                "threads_pruned": threads_pruned,
                "reminders_pruned": reminders_pruned,
                "trivia_sessions_pruned": trivia_sessions_pruned,
                "channels_pruned": 0
            }
            logger.info(f"Pruning complete: {prune_stats}")
            return prune_stats
        except Exception as e:
            logger.error(f"Error during data pruning: {e}", exc_info=True)
            return {
                "messages_pruned": 0,
                "threads_pruned": 0,
                "reminders_pruned": 0,
                "trivia_sessions_pruned": 0,
                "channels_pruned": 0
            }


    # --- Model and System Prompt Methods ---

    def get_channel_system_prompt(self, channel_id: str) -> Optional[str]:
        """Gets the system prompt for a specific channel from the database."""
        return self.db_manager.get_channel_system_prompt(str(channel_id))

    def set_channel_system_prompt(self, channel_id: str, prompt: Optional[str]):
        """Sets a custom system prompt for a channel in the database."""
        # Database call is synchronous
        self.db_manager.set_channel_system_prompt(str(channel_id), prompt)

    def reset_channel_system_prompt(self, channel_id: str) -> bool:
        """Resets a channel to use the default system prompt in the database."""
        # Database call is synchronous
        return self.db_manager.reset_channel_config(str(channel_id)) # Resets both model and prompt

    def get_global_model(self) -> str:
        """Gets the current global model, ensuring provider/model format."""
        try:
            # Return cached value if available
            model = self.global_model
            # Ensure provider/model format. Add 'openrouter/' only if no separator exists.
            if "/" not in model and ":" not in model:
                 logger.warning(f"Global model '{model}' lacks provider prefix. Assuming 'openrouter'.")
                 model = f"openrouter/{model}"
            # Convert ':' to '/' if necessary (handle older format)
            elif ":" in model and "/" not in model:
                 logger.warning(f"Global model '{model}' uses ':' separator. Converting to '/'.")
                 model = model.replace(":", "/", 1)
            return model
        except AttributeError:
            # Fallback to DB if cache not initialized
            logger.warning("get_global_model called before state fully initialized. Fetching directly from DB.")
            model = self.db_manager.get_global_config(CONFIG_KEY_GLOBAL_MODEL, CONFIG_DEFAULT_MODEL)
            # Ensure provider/model format. Add 'openrouter/' only if no separator exists.
            if "/" not in model and ":" not in model:
                 logger.warning(f"DB Global model '{model}' lacks provider prefix. Assuming 'openrouter'.")
                 model = f"openrouter/{model}"
            # Convert ':' to '/' if necessary (handle older format)
            elif ":" in model and "/" not in model:
                 logger.warning(f"DB Global model '{model}' uses ':' separator. Converting to '/'.")
                 model = model.replace(":", "/", 1)
            return model

    async def set_global_model(self, model: str):
        """Sets the global model after validation (accepts provider:model or model format)."""
        if not self.model_manager:
            raise RuntimeError("ModelManager not set in BotStateManager")

        try:
            # Log the exact input string received by set_global_model
            logger.info(f"set_global_model received model string: '{model}'")
            # Parse and validate the model ID format
            provider, model_name = self.model_manager.parse_model_id(model) # 'model' is the input e.g., "openai/gpt-4-turbo"

            # Use the parsed provider and model_name to create the canonical ID in provider/model_name format
            canonical_model_id = f"{provider}/{model_name}"

            # Force refresh models for the parsed provider before validation
            await self.model_manager.get_models(provider, force_refresh=True)

            # Validate the canonical model ID (using the provider's list)
            is_valid = await self.model_manager.is_valid_model(canonical_model_id) # is_valid_model should handle provider/model format
            if not is_valid:
                # Fetch models for the specific provider for the error message
                provider_models = []
                try:
                    models = await self.model_manager.get_models(provider, force_refresh=False) # Use cache if possible
                    # Use slash separator for error message formatting
                    provider_models = [f"{provider}/{m}" for m in models]
                except Exception as ex:
                    logger.warning(f"Could not fetch models for provider {provider} for error message: {ex}")

                allowed_str = ', '.join(provider_models[:10]) + ('...' if len(provider_models) > 10 else '')
                # Use canonical_model_id in error message
                raise ValueError(f"Model '{canonical_model_id}' not found or is invalid for provider '{provider}'. Allowed for {provider}: {allowed_str}")

            # Store the validated, canonical model ID
            self.global_model = canonical_model_id
            # Save to DB (synchronous DB call)
            self.db_manager.set_global_config(CONFIG_KEY_GLOBAL_MODEL, canonical_model_id, 'string')
            logger.info(f"Global model set to: {canonical_model_id}")
        except ValueError as e:
            # Re-raise the ValueError with the original message
            raise ValueError(str(e))

    async def set_global_provider(self, provider: str):
        """Sets the global AI provider."""
        # Basic validation - could enhance later if needed
        if not isinstance(provider, str) or not provider:
            raise ValueError("Provider must be a non-empty string.")
        self.global_provider = provider
        await self._save_config(CONFIG_KEY_GLOBAL_PROVIDER, provider, 'string')
        logger.info(f"Global provider set to: {provider}")

    def get_global_system_prompt(self) -> Optional[str]:
        """Gets the global system prompt."""
        return self.global_system_prompt

    async def set_global_system_prompt(self, prompt: Optional[str]):
        """Sets the global system prompt."""
        self.global_system_prompt = prompt
        await self._save_config(CONFIG_KEY_GLOBAL_SYSTEM_PROMPT, prompt, 'string')
        logger.info(f"Global system prompt set to: {prompt[:50]}..." if prompt and len(prompt) > 50 else f"Global system prompt set to: {prompt}")

    async def set_channel_model(self, channel_id: str, model: Optional[str]):
        """Sets a channel-specific model after validation, storing in provider/model_name format."""
        canonical_model_id = None # Default to None if model input is None
        if model is not None: # Allow setting back to None to use global
            if not self.model_manager:
                raise RuntimeError("ProviderManager not set in BotStateManager")

            # Parse and create canonical ID *inside* the if block
            provider, model_name = self.model_manager.parse_model_id(model)
            canonical_model_id = f"{provider}/{model_name}"

            # Validate the canonical model ID (Correctly indented)
            is_valid = await self.model_manager.is_valid_model(canonical_model_id)
            if not is_valid:
                # Fetch models for the specific provider for the error message (Correctly indented)
                provider_models = []
                try:
                    models = await self.model_manager.get_models(provider, force_refresh=False) # Use cache if possible
                    provider_models = [f"{provider}/{m}" for m in models]
                except Exception as ex:
                    logger.warning(f"Could not fetch models for provider {provider} for error message: {ex}")

                allowed_str = ', '.join(provider_models[:10]) + ('...' if len(provider_models) > 10 else '')
                raise ValueError(f"Model '{canonical_model_id}' not found or is invalid for provider '{provider}'. Allowed for {provider}: {allowed_str}")

        # Save canonical ID (or None) to DB (synchronous DB call)
        self.db_manager.set_channel_model(str(channel_id), canonical_model_id)
        if canonical_model_id:
            logger.info(f"Channel {channel_id} model set to: {canonical_model_id}")
        else:
            logger.info(f"Channel {channel_id} model reset to global default.")

    async def set_channel_provider(self, channel_id: str, provider: str):
        """Sets the AI provider for a specific channel and refreshes models."""
        if not self.model_manager:
            raise RuntimeError("ModelManager not set in BotStateManager")
            
        providers = await self.model_manager.get_providers()
        if provider not in providers:
            raise ValueError(f"Invalid provider '{provider}'. Available: {', '.join(providers)}")
            
        # Force refresh models for the new provider
        if hasattr(self.model_manager, 'get_models') and callable(self.model_manager.get_models):
            # Handle both ProviderManager and ModelManager cases
            params = {}
            if 'force_refresh' in self.model_manager.get_models.__code__.co_varnames:
                params['force_refresh'] = True
            if 'provider' in self.model_manager.get_models.__code__.co_varnames:
                params['provider'] = provider
            await self.model_manager.get_models(**params)
            
        # Save to DB (synchronous DB call)
        self.db_manager.set_channel_provider(str(channel_id), provider)

    def get_channel_model(self, channel_id: str) -> Optional[str]:
        """Gets the channel-specific model override (returns None if using global)."""
        return self.db_manager.get_channel_model(str(channel_id))

    def get_channel_provider(self, channel_id: str) -> str:
        """Gets the AI provider for a specific channel."""
        provider = self.db_manager.get_channel_provider(str(channel_id))
        return provider if provider is not None else self.global_provider

    def get_global_provider(self) -> str:
        """Gets the global AI provider."""
        return self.global_provider

    def get_effective_model(self, channel_id: str) -> str:
        """Gets the effective model (persona > channel override > global default) in provider/model_name format."""
        logger.debug(f"[get_effective_model] Checking channel ID: {channel_id}")

        # Check persona model first
        persona = self.get_effective_persona(channel_id)
        if persona and persona.get('model'):
            logger.debug(f"[get_effective_model] Using persona model: {persona['model']}")
            model_to_return = persona['model']
            source = f"persona ({channel_id})"
        else:
            channel_model = self.db_manager.get_channel_model(str(channel_id))
            logger.debug(f"[get_effective_model] DB result for channel_model: {channel_model}")

            if channel_model is not None:
                model_to_return = channel_model
                source = f"channel override ({channel_id})"
                logger.debug(f"[get_effective_model] Using channel override: {model_to_return}")
            else:
                model_to_return = self.get_global_model()
                source = "global default"
                logger.debug(f"[get_effective_model] Using global default: {model_to_return}")

        logger.debug(f"[get_effective_model] Model before format check: {model_to_return} (Source: {source})")
        # Final check for format consistency (handle potential old data from channel config)
        if model_to_return and "/" not in model_to_return and ":" not in model_to_return:
             logger.warning(f"Effective model '{model_to_return}' from {source} lacks provider prefix. Assuming 'openrouter'.")
             model_to_return = f"openrouter/{model_to_return}"
        elif model_to_return and ":" in model_to_return and "/" not in model_to_return:
             logger.warning(f"Effective model '{model_to_return}' from {source} uses ':' separator. Converting to '/'.")
             model_to_return = model_to_return.replace(":", "/", 1)
        elif not model_to_return: # Should not happen if get_global_model has fallback
             logger.error(f"Effective model resolution failed for channel {channel_id}. Falling back to absolute default.")
             return CONFIG_DEFAULT_MODEL

        logger.debug(f"[get_effective_model] Final effective model for channel {channel_id}: {model_to_return} (from {source})")
        return model_to_return

    def resolve_model(self, channel_id: str) -> tuple:
        """Resolves the effective model for a channel into (provider, model_name)."""
        model_id_full = self.get_effective_model(channel_id)
        try:
            provider, model_name = model_id_full.split('/', 1)
        except ValueError:
            logger.warning(f"Invalid model format '{model_id_full}' for channel {channel_id}. Using global provider.")
            provider = self.global_provider
            model_name = model_id_full
        return provider, model_name

    def get_all_channel_configs(self) -> List[Dict[str, Any]]:
        """Gets detailed configuration for all channels with overrides."""
        return self.db_manager.get_all_channel_configs()

    def get_all_thread_configs(self) -> List[Dict[str, Any]]:
        """Gets detailed configuration for all threads with overrides."""
        return self.db_manager.get_all_thread_configs()

    def reset_channel_config(self, channel_id: str) -> bool:
        """Resets channel configuration to use global defaults."""
        return self.db_manager.reset_channel_config(str(channel_id))

    # --- Persona Methods ---

    def get_channel_persona(self, channel_id: str) -> Optional[Dict[str, Any]]:
        """Gets the persona configuration for a channel, or None if not configured."""
        return self.db_manager.get_channel_persona(str(channel_id))

    def get_effective_persona(self, channel_id: str) -> Optional[Dict[str, Any]]:
        """Returns the active persona for a channel, or None if no active persona.

        If the persona is linked to a template, merges template values as fallbacks
        for any fields the channel persona hasn't explicitly set. This means edits
        to a template automatically propagate to all linked channel personas.
        """
        persona = self.db_manager.get_channel_persona(str(channel_id))
        if not persona or not persona.get('is_active'):
            return None

        # If linked to a template, merge template fields as fallbacks
        template_id = persona.get('template_id')
        if template_id:
            template = self.db_manager.get_persona_template(template_id)
            if template:
                mergeable = ('display_name', 'avatar_url', 'system_prompt',
                             'model', 'provider', 'response_style')
                for field in mergeable:
                    if not persona.get(field) and template.get(field):
                        persona[field] = template[field]

        return persona

    def get_effective_system_prompt(self, channel_id: str, query: Optional[str] = None,
                                    speaker_id: Optional[str] = None,
                                    history: Optional[List[Dict[str, Any]]] = None,
                                    base_prompt: Optional[str] = None) -> str:
        """Gets the effective system prompt: Persona > Channel Config > Global.

        The multi-user framing is always appended so the model knows how user
        turns are labelled, followed by any channel memory. When `query` (the
        current user message) is given, memory summaries matching it are
        retrieved via full-text search and included alongside the most recent
        summaries.

        When `speaker_id` is given, that user's notes for this channel are
        added, plus short notes for other people speaking in `history` (the
        context the model will see). `base_prompt` replaces the persona /
        channel / global prompt, e.g. for a thread's own system prompt.
        """
        if not base_prompt:
            persona = self.get_effective_persona(channel_id)
            if persona and persona.get('system_prompt'):
                base_prompt = persona['system_prompt']
            else:
                channel_prompt = self.get_channel_system_prompt(channel_id)
                base_prompt = channel_prompt if channel_prompt else self.get_global_system_prompt()

        # No global prompt is configured by default; fall back explicitly so the
        # identity prompt is never replaced by the memory block alone
        if not base_prompt:
            base_prompt = CONFIG_SYSTEM_PROMPT

        sections = [base_prompt, MULTI_USER_FRAMING]

        memory_block = self._build_memory_context(channel_id, query=query)
        if memory_block:
            sections.append(memory_block)

        if speaker_id:
            people_block = self._build_user_memory_context(channel_id, str(speaker_id), history)
            if people_block:
                sections.append(people_block)

        return "\n\n".join(sections)

    def _build_user_memory_context(self, channel_id: str, speaker_id: str,
                                   history: Optional[List[Dict[str, Any]]]) -> str:
        """Builds the notes block for the speaker and other participants in `history`."""
        try:
            if not self.get_effective_user_memory_enabled(channel_id):
                return ""
            others = user_memory.recent_participants(history, exclude_id=speaker_id)
            notes = self.get_user_notes_for(channel_id, [speaker_id] + [uid for uid, _ in others])
            if not notes:
                return ""
            speaker_name = (user_memory.latest_name(history, speaker_id)
                            or (notes.get(speaker_id) or {}).get('display_name') or "Them")
            return user_memory.build_people_block((speaker_id, speaker_name), others, notes)
        except Exception as e:
            logger.error(f"Error building user memory context for channel {channel_id}: {e}", exc_info=True)
            return ""

    def search_channel_memories(self, channel_id: str, query: str, limit: int = 3):
        """Full-text search over a channel's memory summaries."""
        return self.db_manager.search_channel_memories(str(channel_id), query, limit)

    def _build_memory_context(self, channel_id: str, query: Optional[str] = None) -> str:
        """Builds a memory block from recent summaries plus FTS matches for the query."""
        try:
            # Recent summaries always ride along (capped), FTS retrieval pulls
            # in older-but-relevant ones for the current message.
            recent_limit = min(self.get_effective_max_memory_summaries(channel_id), 3)
            memories = self.get_channel_memories(channel_id, limit=recent_limit)
            seen_ids = {m.get("id") for m in memories}

            if query:
                for match in self.search_channel_memories(channel_id, query, limit=3):
                    if match.get("id") not in seen_ids:
                        memories.append(match)
                        seen_ids.add(match.get("id"))

            if not memories:
                return ""

            lines = ["--- Channel Memory ---",
                     "Here is what I remember from previous conversations in this channel:"]
            for mem in memories:
                # Prefer conversation_start (when the chat happened) over created_at (when summary was stored)
                ts = mem.get("conversation_start") or mem.get("created_at", "")
                if hasattr(ts, "strftime"):
                    ts_str = ts.strftime("%Y-%m-%d")
                else:
                    ts_str = str(ts)[:10]
                lines.append(f"[{ts_str}]: {mem['summary']}")
            return "\n".join(lines)
        except Exception as e:
            logger.error(f"Error building memory context for channel {channel_id}: {e}", exc_info=True)
            return ""

    def get_effective_provider(self, channel_id: str) -> str:
        """Gets the effective provider: Persona > Channel Config > Global."""
        persona = self.get_effective_persona(channel_id)
        if persona and persona.get('provider'):
            return persona['provider']
        return self.get_channel_provider(channel_id)

    def set_channel_persona(self, channel_id: str, display_name: str, **kwargs) -> bool:
        """Sets or updates the persona for a channel."""
        return self.db_manager.set_channel_persona(str(channel_id), display_name, **kwargs)

    def remove_channel_persona(self, channel_id: str) -> bool:
        """Removes the persona for a channel."""
        return self.db_manager.remove_channel_persona(str(channel_id))

    def toggle_channel_persona(self, channel_id: str) -> Optional[bool]:
        """Toggles persona active state. Returns new state or None if not found."""
        return self.db_manager.toggle_channel_persona(str(channel_id))

    def get_all_channel_personas(self) -> List[Dict[str, Any]]:
        """Gets all channel persona configurations."""
        return self.db_manager.get_all_channel_personas()

    def get_all_persona_templates(self) -> List[Dict[str, Any]]:
        """Gets all persona templates."""
        return self.db_manager.get_all_persona_templates()

    def get_persona_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        """Gets a persona template by ID."""
        return self.db_manager.get_persona_template(template_id)

    # --- News Feed Methods (Delegation) ---
    # COMMENTED OUT: News feeds feature removed
    # Methods preserved for potential future data export

    # def get_news_feeds(self) -> List[Dict[str, Any]]:
    #     return self.db_manager.get_news_feeds()

    # def add_news_feed(self, feed_id: str, url: str, name: Optional[str] = None, category: Optional[str] = None):
    #     """Adds or updates a news feed in the database, including URL and category."""
    #     # Pass all relevant info to the database manager method
    #     self.db_manager.add_news_feed(feed_id, url, name, category)

    # def delete_news_feed(self, feed_id: str) -> bool:
    #     return self.db_manager.delete_news_feed(feed_id)

    # def add_news_subscription(self, channel_id: str, feed_id: str):
    #     self.db_manager.add_news_subscription(str(channel_id), feed_id)

    # def remove_news_subscription(self, channel_id: str, feed_id: str) -> bool:
    #     return self.db_manager.remove_news_subscription(str(channel_id), feed_id)

    # def get_subscribed_channels(self, feed_id: str) -> List[str]:
    #     return self.db_manager.get_subscribed_channels(feed_id)

    # def get_channel_subscriptions(self, channel_id: str) -> List[str]:
    #     return self.db_manager.get_channel_subscriptions(str(channel_id))

    # def add_article_history(self, article_identifier: str, feed_id: str):
    #     self.db_manager.add_article_history(article_identifier, feed_id, datetime.now())

    # def check_article_history(self, article_identifier: str, feed_id: str) -> bool:
    #     return self.db_manager.check_article_history(article_identifier, feed_id)

    # def update_feed_last_checked(self, feed_id: str):
    #      # Ensure we store timezone-aware UTC timestamp
    #      from datetime import timezone
    #      self.db_manager.update_feed_last_checked(feed_id, datetime.now(timezone.utc))

    # def get_news_article_history_count(self) -> int:
    #     """Gets the total count of news article history records from the database."""
    #     return self.db_manager.get_news_article_history_count()

    # def set_last_digest_content(self, content: Optional[str]):
    #     """Stores the last generated news digest content in the database."""
    #     self.db_manager.set_last_digest_content(content)

    # def get_last_digest_content(self) -> Optional[str]:
    #     """Retrieves the last generated news digest content from the database."""
    #     return self.db_manager.get_last_digest_content()

    # # --- Article Summary Methods (Delegation) ---

    # def add_article_summary(self, article_id: str, feed_id: str, title: str, link: str,
    #                         published_date: Optional[datetime], summary_text: str,
    #                         feed_name: Optional[str], feed_category: Optional[str]):
    #     """Adds or replaces an article summary in the database."""
    #     # Pass data to the database manager method
    #     self.db_manager.add_article_summary(
    #         article_id=article_id,
    #         feed_id=feed_id,
    #         title=title,
    #         link=link,
    #         published_date=published_date,
    #         summary_text=summary_text,
    #         feed_name=feed_name,
    #         feed_category=feed_category,
    #         timestamp_summarized=datetime.now() # Add timestamp here
    #     )

    # def get_article_summaries(self, feed_ids: Optional[List[str]] = None,
    #                           category: Optional[str] = None,
    #                           limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    #     """
    #     Retrieves recent article summaries from the database, optionally filtered.
    #     Uses the configured retention period to limit how far back it looks.
    #     """
    #     since_cutoff = datetime.now() - timedelta(days=self.summary_retention_days)
    #     return self.db_manager.get_article_summaries(
    #         feed_ids=feed_ids,
    #         category=category,
    #         since=since_cutoff,
    #         limit=limit
    #     )


    # # --- User-Specific Article Summary Methods (Delegation) ---

    # def add_user_article_summary(self, user_id: str, article_link: str, feed_url: str,
    #                              title: Optional[str], published_date: Optional[datetime],
    #                              summary_text: str):
    #     """Adds or replaces a user-specific article summary."""
    #     # Add timestamp here before delegating
    #     self.db_manager.add_user_article_summary(
    #         user_id=user_id,
    #         article_link=article_link,
    #         feed_url=feed_url,
    #         title=title,
    #         published_date=published_date,
    #         summary_text=summary_text,
    #         timestamp_summarized=datetime.now()
    #     )

    # def get_user_article_summaries(self, user_id: str, limit: Optional[int] = 50) -> List[Dict[str, Any]]:
    #     """Retrieves recent article summaries for a specific user."""
    #     # Use configured retention period
    #     since_cutoff = datetime.now() - timedelta(days=self.summary_retention_days)
    #     return self.db_manager.get_user_article_summaries(
    #         user_id=user_id,
    #         since=since_cutoff,
    #         limit=limit
    #     )

    # def get_users_with_personal_feeds(self) -> List[str]:
    #     """Gets user IDs who have configured personal feeds."""
    #     return self.db_manager.get_users_with_personal_feeds()


    # --- Statistics Methods (Example) ---

    def get_message_count(self) -> int:
        """Gets the total number of messages stored."""
        sql = "SELECT COUNT(*) FROM MESSAGES;"
        try:
            cursor = self.db_manager._get_cursor()
            cursor.execute(sql)
            row = cursor.fetchone()
            return row[0] if row else 0
        except sqlite3.Error as e:
            logger.error(f"Error getting message count: {e}", exc_info=True)
            return -1 # Indicate error

    def get_thread_count(self) -> int:
        """Gets the total number of threads stored."""
        sql = "SELECT COUNT(*) FROM THREADS;"
        try:
            cursor = self.db_manager._get_cursor()
            cursor.execute(sql)
            row = cursor.fetchone()
            return row[0] if row else 0
        except sqlite3.Error as e:
            logger.error(f"Error getting thread count: {e}", exc_info=True)
            return -1

    # --- Reminder Methods (Delegation) ---

    def add_reminder(self, user_id: str, channel_id: str, message: str,
                     due_timestamp: datetime) -> int:
        """Adds a reminder to the database."""
        return self.db_manager.add_reminder(
            user_id=str(user_id),
            channel_id=str(channel_id),
            message=message,
            due_timestamp=due_timestamp
        )

    def get_due_reminders(self, current_time: datetime) -> List[Dict[str, Any]]:
        """Gets reminders that are due."""
        return self.db_manager.get_due_reminders(current_time)

    def mark_reminder_sent(self, reminder_id: int) -> bool:
        """Marks a reminder as sent."""
        return self.db_manager.mark_reminder_sent(reminder_id)

    def get_user_reminders(self, user_id: str, include_sent: bool = False) -> List[Dict[str, Any]]:
        """Gets reminders for a specific user."""
        return self.db_manager.get_user_reminders(str(user_id), include_sent)

    def delete_reminder(self, reminder_id: int, user_id: str) -> bool:
        """Deletes a reminder by ID."""
        return self.db_manager.delete_reminder(reminder_id, str(user_id))

    # ===== Trivia Methods =====

    def create_trivia_session(self, thread_id: str, channel_id: str, user_id: Optional[str],
                             game_mode: str, category: Optional[str], difficulty: str,
                             questions_total: int = 10) -> int:
        """Creates a new trivia game session."""
        return self.db_manager.create_trivia_session(
            thread_id=str(thread_id),
            channel_id=str(channel_id),
            user_id=str(user_id) if user_id else None,
            game_mode=game_mode,
            category=category,
            difficulty=difficulty,
            questions_total=questions_total
        )

    def get_active_trivia_session(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Gets the active trivia session for a thread."""
        return self.db_manager.get_active_trivia_session(str(thread_id))

    def update_trivia_questions_answered(self, thread_id: str) -> bool:
        """Increments questions_answered counter for a trivia session."""
        return self.db_manager.update_trivia_questions_answered(str(thread_id))

    def end_trivia_session(self, thread_id: str) -> bool:
        """Ends a trivia session."""
        return self.db_manager.end_trivia_session(str(thread_id))

    def update_trivia_leaderboard(self, user_id: str, server_id: str,
                                  questions_answered: int, correct_answers: int,
                                  points_earned: int, current_streak: int,
                                  response_time: float) -> bool:
        """Updates trivia leaderboard stats for a user."""
        return self.db_manager.update_trivia_leaderboard(
            user_id=str(user_id),
            server_id=str(server_id),
            questions_answered=questions_answered,
            correct_answers=correct_answers,
            points_earned=points_earned,
            current_streak=current_streak,
            response_time=response_time
        )

    def get_trivia_leaderboard(self, server_id: str, timeframe: str = 'all_time',
                              limit: int = 10) -> List[Dict[str, Any]]:
        """Gets trivia leaderboard rankings for a server."""
        return self.db_manager.get_trivia_leaderboard(str(server_id), timeframe, limit)

    def get_trivia_user_stats(self, user_id: str, server_id: str) -> Optional[Dict[str, Any]]:
        """Gets trivia stats for a specific user."""
        return self.db_manager.get_trivia_user_stats(str(user_id), str(server_id))

    def add_trivia_achievement(self, user_id: str, server_id: str,
                              achievement_type: str, achievement_name: str,
                              metadata_json: Optional[str] = None) -> int:
        """Adds a trivia achievement for a user."""
        return self.db_manager.add_trivia_achievement(
            user_id=str(user_id),
            server_id=str(server_id),
            achievement_type=achievement_type,
            achievement_name=achievement_name,
            metadata_json=metadata_json
        )

    def get_trivia_user_achievements(self, user_id: str, server_id: str) -> List[Dict[str, Any]]:
        """Gets all trivia achievements for a user."""
        return self.db_manager.get_trivia_user_achievements(str(user_id), str(server_id))

    def has_trivia_achievement(self, user_id: str, server_id: str, achievement_type: str) -> bool:
        """Checks if user has a specific trivia achievement."""
        return self.db_manager.has_trivia_achievement(str(user_id), str(server_id), achievement_type)

    # def get_news_feeds_count(self) -> int:
    #     """Gets the count of configured news feeds."""
    #     # This could be slightly more efficient than fetching all feeds
    #     sql = "SELECT COUNT(*) FROM NEWS_FEEDS;"
    #     try:
    #         cursor = self.db_manager._get_cursor()
    #         cursor.execute(sql)
    #         row = cursor.fetchone()
    #         return row[0] if row else 0
    #     except sqlite3.Error as e:
    #         logger.error(f"Error getting news feed count: {e}", exc_info=True)
    #         return -1

    # def get_news_channel_config_count(self) -> int:
    #     """Gets the count of channel subscriptions."""
    #     sql = "SELECT COUNT(*) FROM NEWS_CHANNEL_SUBSCRIPTIONS;"
    #     try:
    #         cursor = self.db_manager._get_cursor()
    #         cursor.execute(sql)
    #         row = cursor.fetchone()
    #         return row[0] if row else 0
    #     except sqlite3.Error as e:
    #         logger.error(f"Error getting news subscription count: {e}", exc_info=True)
    #         return -1
