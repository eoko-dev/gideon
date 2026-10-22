import logging
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class UsageManager:
    """LLM token/cost usage and image/video generation counts (USAGE table).

    One row per completed LLM call or generation job, attributed to the
    feature, user and channel that caused it.
    """

    def __init__(self, conn):
        self._conn = conn

    def add(self, *, created_at: datetime, feature: str, user_id: Optional[str],
            channel_id: Optional[str], provider: Optional[str], model: Optional[str],
            prompt_tokens: int = 0, completion_tokens: int = 0, cached_tokens: int = 0,
            cost: float = 0.0, units: int = 0):
        sql = """
        INSERT INTO USAGE (created_at, feature, user_id, channel_id, provider, model,
                           prompt_tokens, completion_tokens, cached_tokens, cost, units)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """
        try:
            with self._conn:
                self._conn.execute(sql, (created_at, feature, user_id, channel_id, provider, model,
                                         prompt_tokens, completion_tokens, cached_tokens, cost, units))
        except sqlite3.Error as e:
            logger.error(f"Error recording usage: {e}", exc_info=True)

    def user_cost_since(self, user_id: str, since: datetime) -> float:
        """Total LLM cost (USD) attributed to a user since a time."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost), 0) AS total FROM USAGE WHERE user_id = ? AND created_at >= ?;",
            (str(user_id), since)).fetchone()
        return float(row["total"] or 0)

    def user_units_since(self, user_id: str, feature: str, since: datetime) -> int:
        """Generation jobs (images, videos) a user started since a time."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(units), 0) AS total FROM USAGE "
            "WHERE user_id = ? AND feature = ? AND created_at >= ?;",
            (str(user_id), feature, since)).fetchone()
        return int(row["total"] or 0)

    def summary(self, since: datetime) -> Dict[str, Any]:
        """Totals since a time, plus breakdowns by day, feature, model and user."""
        def rows(sql: str) -> List[Dict[str, Any]]:
            return [dict(r) for r in self._conn.execute(sql, (since,)).fetchall()]

        totals_sql = """
            SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                   COALESCE(SUM(cached_tokens), 0) AS cached_tokens,
                   COALESCE(SUM(cost), 0) AS cost, COALESCE(SUM(units), 0) AS units
            FROM USAGE WHERE created_at >= ?"""
        group = lambda col: f"""
            SELECT {col} AS key, COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens + completion_tokens), 0) AS tokens,
                   COALESCE(SUM(cost), 0) AS cost, COALESCE(SUM(units), 0) AS units
            FROM USAGE WHERE created_at >= ? GROUP BY {col} ORDER BY cost DESC, tokens DESC"""
        try:
            return {
                "totals": rows(totals_sql)[0],
                "by_day": rows(group("date(created_at)").replace("ORDER BY cost DESC, tokens DESC",
                                                                  "ORDER BY key")),
                "by_feature": rows(group("feature")),
                "by_model": rows(group("model")),
                "by_user": rows(group("user_id") + " LIMIT 25"),
            }
        except sqlite3.Error as e:
            logger.error(f"Error summarizing usage: {e}", exc_info=True)
            return {"totals": {}, "by_day": [], "by_feature": [], "by_model": [], "by_user": []}

    def prune_before(self, cutoff: datetime) -> int:
        with self._conn:
            return self._conn.execute("DELETE FROM USAGE WHERE created_at < ?;", (cutoff,)).rowcount
