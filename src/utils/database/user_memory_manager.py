import sqlite3
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Iterable

logger = logging.getLogger(__name__)


class UserMemoryManager:
    """Manages per-user notes, scoped to a single channel.

    Every row is keyed by (channel_id, user_id) and every read or write here
    requires both, so a user's notes from one channel can never be loaded
    for another. The only methods that take a bare user_id delete data.
    """

    def __init__(self, conn):
        self._conn = conn

    @staticmethod
    def _require(channel_id, user_id=None, need_user=True):
        if not channel_id:
            raise ValueError("channel_id is required for user memory")
        if need_user and not user_id:
            raise ValueError("user_id is required for user memory")

    def get_notes(self, channel_id: str, user_id: str) -> Optional[Dict[str, Any]]:
        """Returns one user's notes for one channel, or None."""
        self._require(channel_id, user_id)
        sql = """
        SELECT channel_id, user_id, notes, display_name, message_count, created_at, updated_at
        FROM CHANNEL_USER_MEMORY WHERE channel_id = ? AND user_id = ?;
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, (str(channel_id), str(user_id)))
            row = cursor.fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error(f"Error getting user memory for {user_id} in channel '{channel_id}': {e}", exc_info=True)
            return None

    def get_notes_for_users(self, channel_id: str, user_ids: Iterable[str]) -> Dict[str, Dict[str, Any]]:
        """Returns {user_id: row} for the given users' notes in one channel."""
        self._require(channel_id, need_user=False)
        ids = [str(u) for u in user_ids if u]
        if not ids:
            return {}
        sql = f"""
        SELECT channel_id, user_id, notes, display_name, message_count, created_at, updated_at
        FROM CHANNEL_USER_MEMORY
        WHERE channel_id = ? AND user_id IN ({', '.join('?' * len(ids))});
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, (str(channel_id), *ids))
            return {row["user_id"]: dict(row) for row in cursor.fetchall()}
        except sqlite3.Error as e:
            logger.error(f"Error getting user memories in channel '{channel_id}': {e}", exc_info=True)
            return {}

    def list_channel_notes(self, channel_id: str) -> List[Dict[str, Any]]:
        """Returns every user's notes for one channel, most recently updated first."""
        self._require(channel_id, need_user=False)
        sql = """
        SELECT channel_id, user_id, notes, display_name, message_count, created_at, updated_at
        FROM CHANNEL_USER_MEMORY WHERE channel_id = ? ORDER BY updated_at DESC;
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, (str(channel_id),))
            return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error listing user memories for channel '{channel_id}': {e}", exc_info=True)
            return []

    def set_notes(self, channel_id: str, user_id: str, notes: str,
                  display_name: Optional[str] = None, added_messages: int = 0):
        """Creates or replaces one user's notes for one channel."""
        self._require(channel_id, user_id)
        now = datetime.now()
        sql = """
        INSERT INTO CHANNEL_USER_MEMORY
            (channel_id, user_id, notes, display_name, message_count, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel_id, user_id) DO UPDATE SET
            notes = excluded.notes,
            display_name = COALESCE(excluded.display_name, CHANNEL_USER_MEMORY.display_name),
            message_count = CHANNEL_USER_MEMORY.message_count + excluded.message_count,
            updated_at = excluded.updated_at;
        """
        try:
            with self._conn:
                self._conn.execute(sql, (str(channel_id), str(user_id), notes, display_name,
                                         added_messages, now, now))
        except sqlite3.Error as e:
            logger.error(f"Error saving user memory for {user_id} in channel '{channel_id}': {e}", exc_info=True)
            raise

    def delete_notes(self, channel_id: str, user_id: str) -> int:
        """Deletes one user's notes for one channel."""
        self._require(channel_id, user_id)
        return self._delete("channel_id = ? AND user_id = ?", (str(channel_id), str(user_id)))

    def delete_channel_notes(self, channel_id: str) -> int:
        """Deletes every user's notes for one channel."""
        self._require(channel_id, need_user=False)
        return self._delete("channel_id = ?", (str(channel_id),))

    def delete_user_everywhere(self, user_id: str) -> int:
        """Deletes one user's notes in every channel."""
        if not user_id:
            raise ValueError("user_id is required for user memory")
        return self._delete("user_id = ?", (str(user_id),))

    def _delete(self, where: str, params) -> int:
        try:
            with self._conn:
                cursor = self._conn.execute(f"DELETE FROM CHANNEL_USER_MEMORY WHERE {where};", params)
                return cursor.rowcount
        except sqlite3.Error as e:
            logger.error(f"Error deleting user memory ({where}): {e}", exc_info=True)
            raise

    def get_stats(self) -> Dict[str, int]:
        """Returns {channel_id: number of users with notes}."""
        try:
            cursor = self._conn.cursor()
            cursor.execute("SELECT channel_id, COUNT(*) AS n FROM CHANNEL_USER_MEMORY GROUP BY channel_id;")
            return {row["channel_id"]: row["n"] for row in cursor.fetchall()}
        except sqlite3.Error as e:
            logger.error(f"Error getting user memory stats: {e}", exc_info=True)
            return {}
