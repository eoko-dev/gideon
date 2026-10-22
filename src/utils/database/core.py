import sqlite3
import os
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

from ...config import DATA_DIRECTORY
from .schema import SchemaManager
from .config_manager import ConfigManager
from .channel_manager import ChannelManager
from .thread_manager import ThreadManager
from .message_manager import MessageManager
from .user_manager import UserManager
from .reminder_manager import ReminderManager
from .trivia_manager import TriviaManager
from .api_key_manager import APIKeyManager
from .persona_manager import PersonaManager
from .backup_manager import BackupManager
from .memory_manager import MemoryManager
from .user_memory_manager import UserMemoryManager

logger = logging.getLogger(__name__)

class DatabaseManager:
    """Handles all interactions with the SQLite database."""

    def __init__(self, db_name="gideon_state.db"):
        """
        Initializes the database connection and ensures the schema exists.

        Args:
            db_name (str): The name of the database file within the data directory.
        """
        self.db_path = os.path.join(DATA_DIRECTORY, db_name)
        # Ensure the data directory exists
        os.makedirs(DATA_DIRECTORY, exist_ok=True)
        self._conn = None
        try:
            # Use check_same_thread=False for potential async usage
            self._conn = sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
            # Use Row factory for dictionary-like access to rows
            self._conn.row_factory = sqlite3.Row
            # WAL keeps readers from blocking behind writers; NORMAL sync is
            # safe with WAL and avoids an fsync per transaction.
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            logger.info(f"Connected to database: {self.db_path}")

            # Initialize schema
            SchemaManager.initialize(self._conn)

            # Initialize managers with shared connection.
            # These are public — prefer db.messages.add_message(...) over the
            # legacy flat delegate methods below, which are kept for
            # existing call sites.
            self.config = ConfigManager(self._conn)
            self.channels = ChannelManager(self._conn)
            self.threads = ThreadManager(self._conn)
            self.messages = MessageManager(self._conn)
            self.users = UserManager(self._conn)
            self.reminders = ReminderManager(self._conn)
            self.trivia = TriviaManager(self._conn)
            self.api_keys = APIKeyManager(self._conn)
            self.personas = PersonaManager(self._conn)
            self.backup = BackupManager(self._conn, self.db_path)
            self.memory = MemoryManager(self._conn)
            self.user_memory = UserMemoryManager(self._conn)

            # Legacy private aliases (older call sites reference these)
            self._config = self.config
            self._channels = self.channels
            self._threads = self.threads
            self._messages = self.messages
            self._users = self.users
            self._reminders = self.reminders
            self._trivia = self.trivia
            self._api_keys = self.api_keys
            self._personas = self.personas
            self._backup = self.backup
            self._memory = self.memory

        except sqlite3.Error as e:
            logger.error(f"Database connection error to {self.db_path}: {e}", exc_info=True)
            raise

    def _get_cursor(self):
        """Returns a cursor for database operations."""
        if not self._conn:
            raise sqlite3.Error("Database connection is not established.")
        return self._conn.cursor()

    def _commit(self):
        """Commits the current transaction."""
        if self._conn:
            self._conn.commit()

    def close(self):
        """Closes the database connection."""
        if self._conn:
            self._conn.close()
            logger.info("Database connection closed.")
            self._conn = None

    # --- Global Config Methods (delegate to ConfigManager) ---

    def set_global_config(self, key: str, value: Any, value_type: str):
        """Sets or updates a global configuration value."""
        return self._config.set_global_config(key, value, value_type)

    def get_global_config(self, key: str, default: Any = None) -> Any:
        """Retrieves a global configuration value."""
        return self._config.get_global_config(key, default)

    def delete_global_config(self, key: str) -> bool:
        """Deletes a global configuration key."""
        return self._config.delete_global_config(key)

    # --- Channel Methods (delegate to ChannelManager) ---

    def _ensure_channel_exists(self, channel_id: str, name: Optional[str] = None):
        """Ensures a channel record exists in the CHANNELS table."""
        return self._channels._ensure_channel_exists(channel_id, name)

    def set_channel_model(self, channel_id: str, model: Optional[str]):
        """Sets the specific model override for a channel."""
        return self._channels.set_channel_model(channel_id, model)

    def get_channel_model(self, channel_id: str) -> Optional[str]:
        """Gets the specific model override for a channel."""
        return self._channels.get_channel_model(channel_id)

    def set_channel_system_prompt(self, channel_id: str, prompt: Optional[str]):
        """Sets the specific system prompt override for a channel."""
        return self._channels.set_channel_system_prompt(channel_id, prompt)

    def get_channel_system_prompt(self, channel_id: str) -> Optional[str]:
        """Gets the specific system prompt override for a channel."""
        return self._channels.get_channel_system_prompt(channel_id)

    def set_channel_provider(self, channel_id: str, provider: Optional[str]):
        """Sets the AI provider for a channel."""
        return self._channels.set_channel_provider(channel_id, provider)

    def get_channel_provider(self, channel_id: str) -> Optional[str]:
        """Gets the AI provider for a channel."""
        return self._channels.get_channel_provider(channel_id)

    def get_channel_config(self, channel_id: str) -> Optional[Dict[str, Optional[str]]]:
        """Gets model, provider and system prompt for a channel."""
        return self._channels.get_channel_config(channel_id)

    def reset_channel_config(self, channel_id: str) -> bool:
        """Resets a channel's model and system prompt to NULL."""
        return self._channels.reset_channel_config(channel_id)

    def get_all_configured_channel_ids(self) -> List[str]:
        """Gets IDs of channels with non-NULL model, provider, or system_prompt."""
        return self._channels.get_all_configured_channel_ids()

    def get_all_channel_configs(self) -> List[Dict[str, Any]]:
        """Gets detailed configuration for all channels with overrides."""
        return self._channels.get_all_channel_configs()

    # --- Thread Methods (delegate to ThreadManager) ---

    def add_thread(self, thread_id: str, channel_id: str, name: str, created_at: datetime,
                   creator_id: Optional[str] = None):
        """Adds a new thread record."""
        # Ensure the parent channel exists first
        self._ensure_channel_exists(channel_id)
        return self._threads.add_thread(thread_id, channel_id, name, created_at, creator_id)

    def get_thread_info(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Gets information about a specific thread."""
        return self._threads.get_thread_info(thread_id)

    def prune_old_threads(self, cutoff_timestamp: datetime) -> int:
        """Deletes threads older than the cutoff timestamp."""
        return self._threads.prune_old_threads(cutoff_timestamp)

    def set_thread_model(self, thread_id: str, model: Optional[str]):
        """Sets the specific model override for a thread."""
        return self._threads.set_thread_model(thread_id, model)

    def set_thread_system_prompt(self, thread_id: str, prompt: Optional[str]):
        """Sets the specific system prompt override for a thread."""
        return self._threads.set_thread_system_prompt(thread_id, prompt)

    def get_thread_config(self, thread_id: str) -> Optional[Dict[str, Optional[str]]]:
        """Gets both model and system prompt for a thread."""
        return self._threads.get_thread_config(thread_id)

    def list_threads_for_channel(self, channel_id: str) -> List[Dict[str, Any]]:
        """Lists all threads associated with a channel."""
        return self._threads.list_threads_for_channel(channel_id)

    def delete_thread(self, thread_id: str) -> bool:
        """Deletes a thread by its ID."""
        return self._threads.delete_thread(thread_id)

    def rename_thread(self, thread_id: str, new_name: str) -> bool:
        """Renames a thread."""
        return self._threads.rename_thread(thread_id, new_name)

    def get_thread_message_count(self, thread_id: str) -> int:
        """Gets the number of messages in a specific thread."""
        return self._threads.get_thread_message_count(thread_id)

    def get_all_configured_thread_ids(self) -> List[str]:
        """Gets IDs of threads with non-NULL model or system_prompt."""
        return self._threads.get_all_configured_thread_ids()

    def get_all_thread_configs(self) -> List[Dict[str, Any]]:
        """Gets detailed configuration for all threads with overrides."""
        return self._threads.get_all_thread_configs()

    # --- Message Methods (delegate to MessageManager) ---

    def add_message(self, role: str, content: str, timestamp: datetime,
                    channel_id: Optional[str] = None,
                    thread_id: Optional[str] = None,
                    user_id: Optional[str] = None,
                    user_name: Optional[str] = None,
                    is_bot: bool = False) -> int:
        """Adds a message to the database."""
        # Ensure parent channel/thread exists if provided
        if channel_id:
            self._ensure_channel_exists(channel_id)
        return self._messages.add_message(role, content, timestamp, channel_id, thread_id, user_id, user_name,
                                          is_bot)

    def get_channel_history(self, channel_id: str, limit: Optional[int] = None, hours_limit: Optional[int] = None,
                            until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Gets message history for a specific channel."""
        return self._messages.get_channel_history(channel_id, limit, hours_limit, until)

    def get_thread_history(self, thread_id: str, limit: Optional[int] = None, hours_limit: Optional[int] = None,
                           until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Gets message history for a specific thread."""
        return self._messages.get_thread_history(thread_id, limit, hours_limit, until)

    def prune_old_messages(self, cutoff_timestamp: datetime, exclude_channel_ids=(), exclude_thread_ids=()) -> int:
        """Deletes messages older than the cutoff, keeping those of excluded channels/threads."""
        return self._messages.prune_old_messages(cutoff_timestamp, exclude_channel_ids, exclude_thread_ids)

    def delete_channel_messages(self, channel_id: str, before_timestamp: Optional[datetime] = None,
                                through_pk: Optional[int] = None) -> int:
        """Deletes messages for a channel, optionally bounded by timestamp and/or primary key."""
        return self._messages.delete_channel_messages(channel_id, before_timestamp, through_pk)

    # --- User Methods (delegate to UserManager) ---

    def _ensure_user_exists(self, user_id: str):
        """Ensures a user record exists."""
        return self._users._ensure_user_exists(user_id)

    def set_user_preferences(self, user_id: str, preferences: Dict[str, Any]):
        """Sets the preferences JSON for a user."""
        return self._users.set_user_preferences(user_id, preferences)

    def get_user_preferences(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Gets the preferences JSON for a user."""
        return self._users.get_user_preferences(user_id)

    # --- Reminder Methods (delegate to ReminderManager) ---

    def add_reminder(self, user_id: str, channel_id: str, message: str,
                     due_timestamp: datetime) -> int:
        """Adds a new reminder to the database."""
        # Ensure parent channel exists
        self._ensure_channel_exists(channel_id)
        return self._reminders.add_reminder(user_id, channel_id, message, due_timestamp)

    def get_due_reminders(self, current_time: datetime) -> List[Dict[str, Any]]:
        """Gets reminders that are due."""
        return self._reminders.get_due_reminders(current_time)

    def mark_reminder_sent(self, reminder_id: int) -> bool:
        """Marks a reminder as sent."""
        return self._reminders.mark_reminder_sent(reminder_id)

    def get_user_reminders(self, user_id: str, include_sent: bool = False) -> List[Dict[str, Any]]:
        """Gets reminders for a specific user."""
        return self._reminders.get_user_reminders(user_id, include_sent)

    def delete_reminder(self, reminder_id: int, user_id: str) -> bool:
        """Deletes a reminder by ID."""
        return self._reminders.delete_reminder(reminder_id, user_id)

    def prune_old_reminders(self, cutoff_timestamp: datetime) -> int:
        """Deletes sent reminders older than cutoff."""
        return self._reminders.prune_old_reminders(cutoff_timestamp)

    # --- Trivia Methods (delegate to TriviaManager) ---

    def create_trivia_session(self, thread_id: str, channel_id: str, user_id: Optional[str],
                             game_mode: str, category: Optional[str], difficulty: str,
                             questions_total: int = 10) -> int:
        """Creates a new trivia game session."""
        self._ensure_channel_exists(channel_id)
        return self._trivia.create_session(thread_id, channel_id, user_id, game_mode,
                                          category, difficulty, questions_total)

    def get_active_trivia_session(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Gets the active trivia session for a thread."""
        return self._trivia.get_active_session_by_thread(thread_id)

    def update_trivia_questions_answered(self, thread_id: str) -> bool:
        """Increments the questions_answered counter."""
        return self._trivia.update_questions_answered(thread_id)

    def end_trivia_session(self, thread_id: str) -> bool:
        """Ends a trivia session."""
        return self._trivia.end_session(thread_id)

    def update_trivia_leaderboard(self, user_id: str, server_id: str,
                                  questions_answered: int, correct_answers: int,
                                  points_earned: int, current_streak: int,
                                  response_time: float) -> bool:
        """Updates leaderboard stats for a user."""
        return self._trivia.update_leaderboard(user_id, server_id, questions_answered,
                                              correct_answers, points_earned,
                                              current_streak, response_time)

    def get_trivia_leaderboard(self, server_id: str, timeframe: str = 'all_time',
                              limit: int = 10) -> List[Dict[str, Any]]:
        """Gets the leaderboard rankings for a server."""
        return self._trivia.get_leaderboard(server_id, timeframe, limit)

    def get_trivia_user_stats(self, user_id: str, server_id: str) -> Optional[Dict[str, Any]]:
        """Gets trivia stats for a specific user."""
        return self._trivia.get_user_stats(user_id, server_id)

    def add_trivia_achievement(self, user_id: str, server_id: str,
                              achievement_type: str, achievement_name: str,
                              metadata_json: Optional[str] = None) -> int:
        """Adds an achievement for a user."""
        return self._trivia.add_achievement(user_id, server_id, achievement_type,
                                           achievement_name, metadata_json)

    def get_trivia_user_achievements(self, user_id: str, server_id: str) -> List[Dict[str, Any]]:
        """Gets all achievements for a user."""
        return self._trivia.get_user_achievements(user_id, server_id)

    def has_trivia_achievement(self, user_id: str, server_id: str, achievement_type: str) -> bool:
        """Checks if user has a specific achievement."""
        return self._trivia.has_achievement(user_id, server_id, achievement_type)

    def prune_old_trivia_sessions(self, days_old: int = 30) -> int:
        """Deletes inactive trivia sessions older than specified days."""
        return self._trivia.prune_old_sessions(days_old)

    # --- API Key Methods (delegate to APIKeyManager) ---

    def add_api_key(self, key_id: str, provider: str, encrypted_key: str,
                    key_alias: Optional[str] = None) -> bool:
        """Adds a new encrypted API key."""
        return self._api_keys.add_key(key_id, provider, encrypted_key, key_alias)

    def update_api_key(self, key_id: str, encrypted_key: Optional[str] = None,
                       key_alias: Optional[str] = None) -> bool:
        """Updates an existing API key."""
        return self._api_keys.update_key(key_id, encrypted_key, key_alias)

    def delete_api_key(self, key_id: str) -> bool:
        """Deletes an API key."""
        return self._api_keys.delete_key(key_id)

    def get_api_key(self, key_id: str) -> Optional[Dict[str, Any]]:
        """Gets an API key by ID."""
        return self._api_keys.get_key(key_id)

    def get_api_keys_by_provider(self, provider: str) -> List[Dict[str, Any]]:
        """Gets all API keys for a provider."""
        return self._api_keys.get_keys_by_provider(provider)

    def get_active_api_key_for_provider(self, provider: str) -> Optional[Dict[str, Any]]:
        """Gets the active API key for a provider."""
        return self._api_keys.get_active_key_for_provider(provider)

    def get_all_api_keys(self) -> List[Dict[str, Any]]:
        """Gets all API keys."""
        return self._api_keys.get_all_keys()

    def set_api_key_active(self, key_id: str, is_active: bool) -> bool:
        """Sets the active state of an API key."""
        return self._api_keys.set_key_active(key_id, is_active)

    def update_api_key_last_used(self, key_id: str) -> bool:
        """Updates the last_used timestamp for an API key."""
        return self._api_keys.update_last_used(key_id)

    def update_api_key_validation_status(self, key_id: str, status: str,
                                          timestamp: Optional[str] = None) -> bool:
        """Updates the validation status of an API key."""
        return self._api_keys.update_validation_status(key_id, status, timestamp)

    def add_api_key_audit_entry(self, key_id: str, action: str,
                                user_identifier: Optional[str] = None,
                                details: Optional[str] = None) -> int:
        """Adds an audit log entry for an API key operation."""
        return self._api_keys.add_audit_entry(key_id, action, user_identifier, details)

    def get_api_key_audit_log(self, key_id: Optional[str] = None,
                              limit: int = 50) -> List[Dict[str, Any]]:
        """Gets the API key audit log."""
        return self._api_keys.get_audit_log(key_id, limit)

    def prune_old_api_key_audit_entries(self, cutoff_timestamp: datetime) -> int:
        """Deletes old API key audit entries."""
        return self._api_keys.prune_old_audit_entries(cutoff_timestamp)

    # --- Persona Template Methods (delegate to PersonaManager) ---

    def add_persona_template(self, template_id: str, name: str, display_name: str, **kwargs) -> bool:
        """Adds or replaces a persona template."""
        return self._personas.add_persona_template(template_id, name, display_name, **kwargs)

    def get_persona_template(self, template_id: str) -> Optional[Dict[str, Any]]:
        """Gets a persona template by ID."""
        return self._personas.get_persona_template(template_id)

    def get_all_persona_templates(self) -> List[Dict[str, Any]]:
        """Gets all persona templates."""
        return self._personas.get_all_persona_templates()

    def update_persona_template(self, template_id: str, **fields) -> bool:
        """Updates specific fields of a persona template."""
        return self._personas.update_persona_template(template_id, **fields)

    def delete_persona_template(self, template_id: str) -> bool:
        """Deletes a persona template (rejects built-ins)."""
        return self._personas.delete_persona_template(template_id)

    def seed_builtin_persona_templates(self, templates: List[Dict[str, Any]]) -> int:
        """Seeds built-in persona templates (idempotent)."""
        return self._personas.seed_builtin_templates(templates)

    # --- Channel Persona Methods (delegate to PersonaManager) ---

    def set_channel_persona(self, channel_id: str, display_name: str, **kwargs) -> bool:
        """Sets or replaces the persona for a channel."""
        self._ensure_channel_exists(channel_id)
        return self._personas.set_channel_persona(channel_id, display_name, **kwargs)

    def get_channel_persona(self, channel_id: str) -> Optional[Dict[str, Any]]:
        """Gets the persona configuration for a channel."""
        return self._personas.get_channel_persona(channel_id)

    def get_all_channel_personas(self) -> List[Dict[str, Any]]:
        """Gets all channel persona configurations."""
        return self._personas.get_all_channel_personas()

    def update_channel_persona_webhook(self, channel_id: str,
                                       webhook_id: Optional[str],
                                       webhook_token: Optional[str]) -> bool:
        """Updates the webhook credentials for a channel persona."""
        return self._personas.update_channel_persona_webhook(channel_id, webhook_id, webhook_token)

    def toggle_channel_persona(self, channel_id: str) -> Optional[bool]:
        """Toggles the is_active flag for a channel persona."""
        return self._personas.toggle_channel_persona(channel_id)

    def remove_channel_persona(self, channel_id: str) -> bool:
        """Removes the persona for a channel."""
        return self._personas.remove_channel_persona(channel_id)

    # --- Channel Memory Methods (delegate to MemoryManager) ---

    def get_channel_memories(self, channel_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Gets stored memory summaries for a channel (oldest first)."""
        return self._memory.get_memories(channel_id, limit)

    def add_channel_memory(self, channel_id: str, summary: str, message_count: int,
                           conversation_start=None) -> int:
        """Stores a new memory summary for a channel."""
        self._ensure_channel_exists(channel_id)
        return self._memory.add_memory(channel_id, summary, message_count, conversation_start)

    def delete_channel_memories(self, channel_id: str) -> int:
        """Deletes all memory summaries for a channel."""
        return self._memory.delete_memories(channel_id)

    def prune_channel_memories(self, channel_id: str, max_count: int) -> int:
        """Keeps only the most recent `max_count` summaries for a channel."""
        return self._memory.prune_memories(channel_id, max_count)

    def search_channel_memories(self, channel_id: str, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        """Full-text search over a channel's memory summaries."""
        return self._memory.search_memories(channel_id, query, limit)

    def get_all_memory_stats(self) -> List[Dict[str, Any]]:
        """Returns per-channel memory stats (channel_id, count, latest timestamp)."""
        return self._memory.get_all_memory_stats()

    # --- Backup & Restore Methods (delegate to BackupManager) ---

    def export_config_json(self) -> Dict[str, Any]:
        """Exports all configuration tables as a JSON-serializable dict."""
        return self._backup.export_config_json()

    def validate_config_json(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Validates imported JSON config structure."""
        return self._backup.validate_config_json(data)

    def import_config_json(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Applies imported JSON config to the database."""
        return self._backup.import_config_json(data)

    def create_sqlite_backup(self, backup_path: str) -> str:
        """Creates a consistent SQLite backup using the online backup API."""
        return self._backup.create_sqlite_backup(backup_path)

    def validate_sqlite_backup(self, backup_path: str) -> Dict[str, Any]:
        """Validates an uploaded SQLite backup file."""
        return self._backup.validate_sqlite_backup(backup_path)

    def restore_sqlite_backup(self, backup_path: str) -> Dict[str, Any]:
        """Restores the database from a SQLite backup file."""
        return self._backup.restore_sqlite_backup(backup_path)
