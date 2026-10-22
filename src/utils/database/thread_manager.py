import sqlite3
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)

class ThreadManager:
    """Manages Discord thread records and configuration."""

    def __init__(self, conn):
        self._conn = conn

    def add_thread(self, thread_id: str, channel_id: str, name: str, created_at: datetime,
                   creator_id: Optional[str] = None):
        """Adds a new thread record. creator_id is the user who asked for it, if known."""
        sql = """
        INSERT OR IGNORE INTO THREADS (thread_id, channel_id, name, created_at, creator_id)
        VALUES (?, ?, ?, ?, ?);
        """
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(sql, (thread_id, channel_id, name, created_at, creator_id))
            logger.debug(f"Added or ignored thread: {thread_id} in channel {channel_id}")
        except sqlite3.Error as e:
            logger.error(f"Error adding thread '{thread_id}': {e}", exc_info=True)
            raise

    def get_thread_info(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Gets information about a specific thread."""
        sql = "SELECT thread_id, channel_id, name, created_at, creator_id FROM THREADS WHERE thread_id = ?;"
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, (thread_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error(f"Error getting info for thread '{thread_id}': {e}", exc_info=True)
            return None

    def prune_old_threads(self, cutoff_timestamp: datetime) -> int:
        """Deletes threads created before the cutoff that have been quiet since.

        A thread with a message newer than the cutoff is still in use and
        keeps its model/prompt settings. Foreign keys aren't enforced, so the
        thread's messages are deleted explicitly.
        """
        stale = """
        SELECT thread_id FROM THREADS t
        WHERE created_at < ?
          AND NOT EXISTS (SELECT 1 FROM MESSAGES m WHERE m.thread_id = t.thread_id AND m.timestamp >= ?)
        """
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(f"DELETE FROM MESSAGES WHERE thread_id IN ({stale});",
                               (cutoff_timestamp, cutoff_timestamp))
                cursor.execute(f"DELETE FROM THREADS WHERE thread_id IN ({stale});",
                               (cutoff_timestamp, cutoff_timestamp))
                deleted_count = cursor.rowcount
            if deleted_count > 0:
                logger.info(f"Pruned {deleted_count} old threads created before {cutoff_timestamp}.")
            return deleted_count
        except sqlite3.Error as e:
            logger.error(f"Error pruning old threads: {e}", exc_info=True)
            raise

    def set_thread_model(self, thread_id: str, model: Optional[str]):
        """Sets the specific model override for a thread."""
        sql = """
        UPDATE THREADS SET model = ? WHERE thread_id = ?;
        """
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(sql, (model, thread_id))
                updated = cursor.rowcount > 0
            if updated:
                logger.debug(f"Set model for thread {thread_id} to {model}")
            return updated
        except sqlite3.Error as e:
            logger.error(f"Error setting model for thread '{thread_id}': {e}", exc_info=True)
            raise

    def set_thread_system_prompt(self, thread_id: str, prompt: Optional[str]):
        """Sets the specific system prompt override for a thread."""
        sql = """
        UPDATE THREADS SET system_prompt = ? WHERE thread_id = ?;
        """
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(sql, (prompt, thread_id))
                updated = cursor.rowcount > 0
            if updated:
                logger.debug(f"Set system prompt for thread {thread_id}")
            return updated
        except sqlite3.Error as e:
            logger.error(f"Error setting system prompt for thread '{thread_id}': {e}", exc_info=True)
            raise

    def get_thread_config(self, thread_id: str) -> Optional[Dict[str, Optional[str]]]:
        """Gets both model and system prompt for a thread."""
        sql = "SELECT model, system_prompt FROM THREADS WHERE thread_id = ?;"
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, (thread_id,))
            row = cursor.fetchone()
            if row:
                return {"model": row["model"], "system_prompt": row["system_prompt"]}
            else:
                return None
        except sqlite3.Error as e:
            logger.error(f"Error getting config for thread '{thread_id}': {e}", exc_info=True)
            return None

    def list_threads_for_channel(self, channel_id: str) -> List[Dict[str, Any]]:
        """Lists all threads associated with a channel."""
        sql = "SELECT thread_id, channel_id, name, created_at, model, system_prompt FROM THREADS WHERE channel_id = ? ORDER BY created_at DESC;"
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, (channel_id,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Error listing threads for channel '{channel_id}': {e}", exc_info=True)
            return []

    def delete_thread(self, thread_id: str) -> bool:
        """Deletes a thread and its messages (foreign keys aren't enforced)."""
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute("DELETE FROM MESSAGES WHERE thread_id = ?;", (thread_id,))
                cursor.execute("DELETE FROM THREADS WHERE thread_id = ?;", (thread_id,))
                deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Deleted thread: {thread_id} and associated messages.")
            return deleted
        except sqlite3.Error as e:
            logger.error(f"Error deleting thread '{thread_id}': {e}", exc_info=True)
            raise

    def rename_thread(self, thread_id: str, new_name: str) -> bool:
        """Renames a thread."""
        sql = "UPDATE THREADS SET name = ? WHERE thread_id = ?;"
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(sql, (new_name, thread_id))
                updated = cursor.rowcount > 0
            if updated:
                logger.debug(f"Renamed thread {thread_id} to '{new_name}'")
            return updated
        except sqlite3.Error as e:
            logger.error(f"Error renaming thread '{thread_id}': {e}", exc_info=True)
            raise

    def get_thread_message_count(self, thread_id: str) -> int:
        """Gets the number of messages in a specific thread."""
        sql = "SELECT COUNT(*) FROM MESSAGES WHERE thread_id = ?;"
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, (thread_id,))
            row = cursor.fetchone()
            return row[0] if row else 0
        except sqlite3.Error as e:
            logger.error(f"Error getting message count for thread '{thread_id}': {e}", exc_info=True)
            return -1

    def get_all_configured_thread_ids(self) -> List[str]:
        """Gets IDs of threads with non-NULL model or system_prompt."""
        sql = """
        SELECT DISTINCT thread_id FROM THREADS
        WHERE model IS NOT NULL OR system_prompt IS NOT NULL;
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            return [row['thread_id'] for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting configured thread IDs: {e}", exc_info=True)
            return []

    def get_all_thread_configs(self) -> List[Dict[str, Any]]:
        """Gets detailed configuration for all threads with overrides."""
        sql = """
        SELECT thread_id, channel_id, name, model, system_prompt
        FROM THREADS
        WHERE model IS NOT NULL OR system_prompt IS NOT NULL;
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting all thread configs: {e}", exc_info=True)
            return []
