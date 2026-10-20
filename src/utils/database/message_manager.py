import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class MessageManager:
    """Manages message storage and history retrieval."""

    def __init__(self, conn):
        self._conn = conn

    def add_message(self, role: str, content: str, timestamp: datetime,
                    channel_id: Optional[str] = None,
                    thread_id: Optional[str] = None,
                    user_id: Optional[str] = None,
                    user_name: Optional[str] = None) -> int:
        """
        Adds a message to the database, associated with either a channel or a thread.

        Args:
            role (str): 'user', 'assistant', 'system', or 'tool'.
            content (str): The message content.
            timestamp (datetime): The time the message was created/received.
            channel_id (Optional[str]): The ID of the channel if it's a channel message.
            thread_id (Optional[str]): The ID of the thread if it's a thread message.
            user_id (Optional[str]): The ID of the user who sent the message.
            user_name (Optional[str]): The display name of the user who sent the message.

        Returns:
            int: The primary key (message_pk) of the inserted message.

        Raises:
            ValueError: If neither channel_id nor thread_id is provided.
        """
        if not channel_id and not thread_id:
            raise ValueError("Either channel_id or thread_id must be provided for a message.")
        if channel_id and thread_id:
            logger.warning(f"Message provided with both channel_id ({channel_id}) and thread_id ({thread_id}). Associating with thread.")
            channel_id = None  # Prioritize thread_id if both given

        sql = """
        INSERT INTO MESSAGES (channel_id, thread_id, user_id, user_name, role, content, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?);
        """
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(sql, (channel_id, thread_id, user_id, user_name, role, content, timestamp))
                last_id = cursor.lastrowid
            logger.debug(f"Added message (PK: {last_id}) for {'channel ' + channel_id if channel_id else 'thread ' + thread_id}")
            return last_id
        except sqlite3.Error as e:
            logger.error(f"Error adding message for {'channel ' + str(channel_id) if channel_id else 'thread ' + str(thread_id)}: {e}", exc_info=True)
            raise

    def _get_history(self, entity_id: str, id_column: str, limit: Optional[int] = None,
                     hours_limit: Optional[int] = None, until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Internal helper to get message history for a channel or thread.

        ``until`` caps the history at a given timestamp (inclusive) so a reply
        never sees messages that arrived after the one it is answering.
        """
        base_sql = f"SELECT role, content, timestamp, user_id, user_name AS name FROM MESSAGES WHERE {id_column} = ?"
        params: List[Any] = [entity_id]

        if hours_limit is not None:
            cutoff_time = datetime.now() - timedelta(hours=hours_limit)
            base_sql += " AND timestamp >= ?"
            params.append(cutoff_time)

        if until is not None:
            base_sql += " AND timestamp <= ?"
            params.append(until)

        base_sql += " ORDER BY timestamp DESC"  # Get latest first

        if limit is not None:
            base_sql += " LIMIT ?"
            params.append(limit)

        try:
            cursor = self._conn.cursor()
            cursor.execute(base_sql, tuple(params))
            # Fetch all and reverse to get chronological order (oldest first)
            rows = cursor.fetchall()

            # Debug timestamp data
            debug_rows = []
            for row in rows:
                debug_row = dict(row)
                timestamp_value = debug_row.get("timestamp")
                timestamp_type = type(timestamp_value).__name__
                logger.debug(f"Raw timestamp value: {timestamp_value}, Type: {timestamp_type}")
                debug_rows.append(debug_row)
            rows = debug_rows

            return [dict(row) for row in reversed(rows)]
        except sqlite3.Error as e:
            logger.error(f"Error getting history for {id_column} '{entity_id}': {e}", exc_info=True)
            return []

    def get_channel_history(self, channel_id: str, limit: Optional[int] = None, hours_limit: Optional[int] = None,
                            until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Gets message history for a specific channel."""
        return self._get_history(channel_id, "channel_id", limit, hours_limit, until)

    def get_thread_history(self, thread_id: str, limit: Optional[int] = None, hours_limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Gets message history for a specific thread."""
        return self._get_history(thread_id, "thread_id", limit, hours_limit)

    def get_channel_message_count(self, channel_id: str) -> int:
        """Counts stored messages for a channel."""
        try:
            cursor = self._conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM MESSAGES WHERE channel_id = ?;", (channel_id,))
            return cursor.fetchone()[0]
        except sqlite3.Error as e:
            logger.error(f"Error counting messages for channel '{channel_id}': {e}", exc_info=True)
            return 0

    def get_distinct_channel_ids(self) -> List[str]:
        """Returns the IDs of all channels that have stored messages."""
        try:
            cursor = self._conn.cursor()
            cursor.execute("SELECT DISTINCT channel_id FROM MESSAGES WHERE channel_id IS NOT NULL;")
            return [row[0] for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error getting distinct channel ids: {e}", exc_info=True)
            return []

    def delete_channel_messages(self, channel_id: str, before_timestamp: Optional[datetime] = None) -> int:
        """Deletes messages for a channel, optionally only those before a timestamp."""
        sql = "DELETE FROM MESSAGES WHERE channel_id = ?"
        params: List[Any] = [channel_id]
        if before_timestamp is not None:
            sql += " AND timestamp < ?"
            params.append(before_timestamp)
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(sql + ";", tuple(params))
                deleted = cursor.rowcount
            logger.info(f"Deleted {deleted} messages for channel {channel_id}"
                        + (f" before {before_timestamp}" if before_timestamp else ""))
            return deleted
        except sqlite3.Error as e:
            logger.error(f"Error deleting messages for channel '{channel_id}': {e}", exc_info=True)
            return 0

    def prune_old_messages(self, cutoff_timestamp: datetime) -> int:
        """
        Deletes messages older than the specified cutoff timestamp.

        Args:
            cutoff_timestamp (datetime): Messages older than this will be deleted.

        Returns:
            int: The number of messages deleted.
        """
        sql = "DELETE FROM MESSAGES WHERE timestamp < ?;"
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(sql, (cutoff_timestamp,))
                deleted_count = cursor.rowcount
            if deleted_count > 0:
                logger.info(f"Pruned {deleted_count} old messages before {cutoff_timestamp}.")
            return deleted_count
        except sqlite3.Error as e:
            logger.error(f"Error pruning old messages: {e}", exc_info=True)
            raise
