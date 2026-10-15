"""In-memory ring buffer of recent error log records for the dashboard."""
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List

MAX_RECORDS = 100
MAX_MESSAGE_LENGTH = 500


class ErrorLogBuffer(logging.Handler):
    """Keeps the most recent ERROR+ records (time, level, logger, first message line)."""

    def __init__(self, maxlen: int = MAX_RECORDS):
        super().__init__(level=logging.ERROR)
        self._records: deque = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord):
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        lines = message.splitlines()
        first_line = lines[0] if lines else ''
        self._records.append({
            'ts': record.created,
            'time': datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': first_line[:MAX_MESSAGE_LENGTH],
        })

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Newest-first records without internal fields."""
        items = list(self._records)[::-1][:limit]
        return [{k: v for k, v in r.items() if k != 'ts'} for r in items]

    def count_since(self, seconds: float) -> int:
        cutoff = time.time() - seconds
        return sum(1 for r in list(self._records) if r['ts'] >= cutoff)

    def install(self):
        root = logging.getLogger()
        if self not in root.handlers:
            root.addHandler(self)

    def uninstall(self):
        logging.getLogger().removeHandler(self)
