"""Background refresh of live model lists for Discord autocomplete."""
import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger('model_list_refresher')

# Discord drops autocomplete responses after ~3s; leave headroom for the reply.
AUTOCOMPLETE_WAIT_SECONDS = 2.0


class ModelListRefresher:
    """Runs at most one model-list fetch at a time without blocking autocomplete.

    The fetch callable owns the cache (the API clients cache their own lists);
    this class only schedules refreshes and bounds how long a caller waits.
    """

    def __init__(self, fetch: Callable[[], Awaitable[Any]], name: str):
        self._fetch = fetch
        self._name = name
        self._task: Optional[asyncio.Task] = None

    def start(self) -> Optional[asyncio.Task]:
        if self._task and not self._task.done():
            return self._task
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        self._task = loop.create_task(self._run())
        return self._task

    async def _run(self) -> None:
        try:
            result = await self._fetch()
            if isinstance(result, dict) and not result.get("success"):
                logger.warning("%s model refresh failed: %s", self._name, result.get("error"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("%s model refresh failed: %s", self._name, exc)

    async def refresh(self, wait: bool, timeout: float = AUTOCOMPLETE_WAIT_SECONDS) -> None:
        """Start a refresh; when ``wait`` is set, block up to ``timeout`` for it."""
        task = self.start()
        if not wait or task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except asyncio.TimeoutError:
            pass

    def cancel(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
