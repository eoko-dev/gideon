import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RSVP_STATUSES = ("going", "maybe", "no")


class EventManager:
    """Events Gideon scheduled (EVENTS) and their RSVPs (EVENT_RSVPS)."""

    def __init__(self, conn):
        self._conn = conn

    def add_event(self, *, guild_id: Optional[str], channel_id: str, name: str, description: str,
                  start_time: datetime, duration_minutes: int, discord_event_id: Optional[str],
                  created_by: Optional[str]) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "INSERT INTO EVENTS (guild_id, channel_id, name, description, start_time, duration_minutes, "
                "discord_event_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (guild_id, channel_id, name, description, start_time, duration_minutes,
                 discord_event_id, created_by, datetime.now()))
            return cursor.lastrowid

    def set_card_message(self, event_id: int, message_id: str):
        with self._conn:
            self._conn.execute("UPDATE EVENTS SET card_message_id = ? WHERE event_id = ?;",
                               (message_id, event_id))

    def get_event(self, event_id: int) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM EVENTS WHERE event_id = ?;", (event_id,)).fetchone()
        return dict(row) if row else None

    def get_rsvp(self, event_id: int, user_id: str) -> Optional[Dict[str, Any]]:
        row = self._conn.execute("SELECT * FROM EVENT_RSVPS WHERE event_id = ? AND user_id = ?;",
                                 (event_id, user_id)).fetchone()
        return dict(row) if row else None

    def set_rsvp(self, event_id: int, user_id: str, status: str, reminder_id: Optional[int]):
        if status not in RSVP_STATUSES:
            raise ValueError(f"Unknown RSVP status '{status}'")
        with self._conn:
            self._conn.execute(
                "INSERT INTO EVENT_RSVPS (event_id, user_id, status, reminder_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(event_id, user_id) DO UPDATE SET "
                "status = excluded.status, reminder_id = excluded.reminder_id, updated_at = excluded.updated_at;",
                (event_id, user_id, status, reminder_id, datetime.now()))

    def rsvp_counts(self, event_id: int) -> Dict[str, List[str]]:
        """User ids per status, in RSVP order."""
        counts: Dict[str, List[str]] = {status: [] for status in RSVP_STATUSES}
        for row in self._conn.execute(
                "SELECT user_id, status FROM EVENT_RSVPS WHERE event_id = ? ORDER BY updated_at;",
                (event_id,)).fetchall():
            counts.setdefault(row["status"], []).append(row["user_id"])
        return counts
