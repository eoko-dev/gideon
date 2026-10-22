"""Keeping Gideon's unprompted posts (reminders, RSVP nudges, welcomes) in check.

``PostBudget`` is a sliding one-hour window per key (a channel or a server).
Callers ask ``allow(key)`` before posting something nobody directly asked for
in that moment; when the budget is spent they batch, defer or skip instead of
flooding the channel.
"""

import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict

WINDOW_SECONDS = 3600


class PostBudget:
    def __init__(self, limit_per_hour: Callable[[], int], clock: Callable[[], float] = time.monotonic):
        self._limit = limit_per_hour
        self._clock = clock
        self._posts: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """Records a post for ``key`` and returns True, or False when the hour's budget is spent."""
        limit = self._limit()
        if limit <= 0:
            return True
        now = self._clock()
        posts = self._posts[key]
        while posts and now - posts[0] > WINDOW_SECONDS:
            posts.popleft()
        if len(posts) >= limit:
            return False
        posts.append(now)
        return True

    def remaining(self, key: str) -> int:
        limit = self._limit()
        if limit <= 0:
            return 1_000_000
        now = self._clock()
        return max(0, limit - sum(1 for t in self._posts[key] if now - t <= WINDOW_SECONDS))


_budget: PostBudget = None


def get_budget(state) -> PostBudget:
    """The process-wide budget, reading its limit from GLOBAL_CONFIG each time."""
    global _budget
    if _budget is None:
        _budget = PostBudget(lambda: int(getattr(state, "get_max_unprompted_posts_per_hour", lambda: 6)()))
    return _budget
