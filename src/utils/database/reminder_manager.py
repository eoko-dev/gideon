import sqlite3
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class ReminderManager:
    """Manages reminder storage and retrieval."""

    def __init__(self, conn):
        self._conn = conn

    def add_reminder(self, user_id: str, channel_id: str, message: str,
                     due_timestamp: datetime, recurrence: Optional[str] = None,
                     deliver_dm: bool = False, timezone: Optional[str] = None) -> int:
        """
        Adds a new reminder to the database.

        Args:
            user_id (str): Discord user ID who created the reminder
            channel_id (str): Channel ID where reminder should be sent
            message (str): The reminder message text
            due_timestamp (datetime): When the reminder should fire

        Returns:
            int: The reminder_id of the inserted reminder

        Raises:
            sqlite3.Error: If database operation fails
        """
        sql = """
        INSERT INTO REMINDERS (user_id, channel_id, message, due_timestamp, created_at, sent,
                               recurrence, deliver_dm, timezone)
        VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?);
        """
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(sql, (user_id, channel_id, message, due_timestamp, datetime.now(),
                                     recurrence, 1 if deliver_dm else 0, timezone))
                last_id = cursor.lastrowid
            logger.debug(f"Added reminder (ID: {last_id}) for user {user_id} due at {due_timestamp}")
            return last_id
        except sqlite3.Error as e:
            logger.error(f"Error adding reminder for user '{user_id}': {e}", exc_info=True)
            raise

    def get_due_reminders(self, current_time: datetime) -> List[Dict[str, Any]]:
        """
        Retrieves all unsent reminders that are due (due_timestamp <= current_time).

        Args:
            current_time (datetime): The current time to compare against

        Returns:
            List[Dict[str, Any]]: List of reminder dictionaries with keys:
                reminder_id, user_id, channel_id, message, due_timestamp, created_at
        """
        sql = """
        SELECT reminder_id, user_id, channel_id, message, due_timestamp, created_at,
               recurrence, deliver_dm, timezone
        FROM REMINDERS
        WHERE sent = 0 AND due_timestamp <= ?
        ORDER BY due_timestamp ASC;
        """
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, (current_time,))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting due reminders: {e}", exc_info=True)
            return []

    def mark_reminder_sent(self, reminder_id: int) -> bool:
        """
        Marks a reminder as sent by setting sent = 1.

        Args:
            reminder_id (int): The ID of the reminder to mark as sent

        Returns:
            bool: True if a reminder was marked, False otherwise
        """
        sql = "UPDATE REMINDERS SET sent = 1 WHERE reminder_id = ?;"
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(sql, (reminder_id,))
                updated = cursor.rowcount > 0
            if updated:
                logger.debug(f"Marked reminder {reminder_id} as sent")
            return updated
        except sqlite3.Error as e:
            logger.error(f"Error marking reminder '{reminder_id}' as sent: {e}", exc_info=True)
            raise

    def get_user_reminders(self, user_id: str, include_sent: bool = False) -> List[Dict[str, Any]]:
        """
        Gets all reminders for a specific user.

        Args:
            user_id (str): Discord user ID
            include_sent (bool): If True, includes sent reminders; if False, only pending

        Returns:
            List[Dict[str, Any]]: List of reminder dictionaries ordered by due_timestamp
        """
        sql = """
        SELECT reminder_id, user_id, channel_id, message, due_timestamp, created_at, sent,
               recurrence, deliver_dm, timezone
        FROM REMINDERS
        WHERE user_id = ?
        """
        params = [user_id]

        if not include_sent:
            sql += " AND sent = 0"

        sql += " ORDER BY due_timestamp ASC;"

        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, tuple(params))
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting reminders for user '{user_id}': {e}", exc_info=True)
            return []

    def get_reminder(self, reminder_id: int) -> Optional[Dict[str, Any]]:
        """One reminder by id (sent or not), or None."""
        row = self._conn.execute(
            "SELECT reminder_id, user_id, channel_id, message, due_timestamp, created_at, sent, "
            "recurrence, deliver_dm, timezone FROM REMINDERS WHERE reminder_id = ?;", (reminder_id,)).fetchone()
        return dict(row) if row else None

    def reschedule(self, reminder_id: int, due_timestamp: datetime) -> bool:
        """Moves a reminder to a new time and makes it pending again (snooze, next repeat)."""
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE REMINDERS SET due_timestamp = ?, sent = 0 WHERE reminder_id = ?;",
                (due_timestamp, reminder_id))
            return cursor.rowcount > 0

    def delete_reminder(self, reminder_id: int, user_id: str) -> bool:
        """
        Deletes a reminder by ID, ensuring it belongs to the specified user.

        Args:
            reminder_id (int): The reminder ID to delete
            user_id (str): Discord user ID (for ownership verification)

        Returns:
            bool: True if a reminder was deleted, False otherwise
        """
        sql = "DELETE FROM REMINDERS WHERE reminder_id = ? AND user_id = ?;"
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(sql, (reminder_id, user_id))
                deleted = cursor.rowcount > 0
            if deleted:
                logger.info(f"Deleted reminder {reminder_id} for user {user_id}")
            return deleted
        except sqlite3.Error as e:
            logger.error(f"Error deleting reminder '{reminder_id}': {e}", exc_info=True)
            raise

    def prune_old_reminders(self, cutoff_timestamp: datetime) -> int:
        """
        Deletes sent reminders older than the cutoff timestamp.

        Args:
            cutoff_timestamp (datetime): Sent reminders older than this will be deleted

        Returns:
            int: The number of reminders deleted
        """
        sql = "DELETE FROM REMINDERS WHERE sent = 1 AND due_timestamp < ?;"
        try:
            with self._conn:
                cursor = self._conn.cursor()
                cursor.execute(sql, (cutoff_timestamp,))
                deleted_count = cursor.rowcount
            if deleted_count > 0:
                logger.info(f"Pruned {deleted_count} old sent reminders before {cutoff_timestamp}.")
            return deleted_count
        except sqlite3.Error as e:
            logger.error(f"Error pruning old reminders: {e}", exc_info=True)
            raise
