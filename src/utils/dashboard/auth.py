"""Authentication, login throttling and security headers for the dashboard."""
import hmac
import logging
import math
import re
import secrets
import time
from typing import Callable, Dict, Optional, Tuple

from aiohttp import web

logger = logging.getLogger(__name__)

SESSION_COOKIE = 'session_token'
SESSION_DURATION = 86400  # 24 hours
_CLEANUP_INTERVAL = 60

# Session tokens: maps token -> expiry timestamp
_active_sessions: Dict[str, float] = {}
_last_cleanup = 0.0
_now: Callable[[], float] = time.time

_HOST_RE = re.compile(r'^[A-Za-z0-9.\-:\[\]]+$')


def generate_session_token() -> str:
    """Create and register a random session token (independent of the secret)."""
    token = secrets.token_urlsafe(32)
    _active_sessions[token] = _now() + SESSION_DURATION
    return token


def validate_session_token(token: Optional[str]) -> bool:
    """Check if a session token is valid and not expired."""
    if not token or token not in _active_sessions:
        return False
    if _now() > _active_sessions[token]:
        del _active_sessions[token]
        return False
    return True


def invalidate_session_token(token: str):
    """Remove a session token."""
    _active_sessions.pop(token, None)


def cleanup_expired_sessions():
    """Remove all expired session tokens."""
    now = _now()
    expired = [t for t, exp in _active_sessions.items() if now > exp]
    for t in expired:
        del _active_sessions[t]


def _maybe_cleanup_sessions():
    global _last_cleanup
    now = _now()
    if now - _last_cleanup >= _CLEANUP_INTERVAL:
        _last_cleanup = now
        cleanup_expired_sessions()


def verify_secret(provided: str, actual: str) -> bool:
    """Constant-time comparison of the provided secret against the actual."""
    return hmac.compare_digest(provided.encode(), actual.encode())


class _Entry:
    __slots__ = ('failures', 'lockouts', 'locked_until', 'last_failure')

    def __init__(self):
        self.failures = 0
        self.lockouts = 0
        self.locked_until = 0.0
        self.last_failure = 0.0


class LoginThrottle:
    """Per-client login lockout with exponentially growing cool-downs."""

    MAX_ATTEMPTS = 5
    BASE_LOCKOUT = 90
    MAX_LOCKOUT = 86400
    FORGET_AFTER = 86400
    PRUNE_INTERVAL = 60

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._entries: Dict[str, _Entry] = {}
        self._last_prune = clock()

    def _prune(self, now: float):
        if now - self._last_prune < self.PRUNE_INTERVAL:
            return
        self._last_prune = now
        stale = [k for k, e in self._entries.items() if self._is_stale(e, now)]
        for k in stale:
            del self._entries[k]

    def _is_stale(self, entry: _Entry, now: float) -> bool:
        return now >= max(entry.last_failure, entry.locked_until) + self.FORGET_AFTER

    def _get(self, key: str, now: float) -> Optional[_Entry]:
        entry = self._entries.get(key)
        if entry and self._is_stale(entry, now):
            del self._entries[key]
            return None
        return entry

    def retry_after(self, key: str) -> int:
        """Seconds until the client may try again, or 0 when not locked."""
        now = self._clock()
        self._prune(now)
        entry = self._get(key, now)
        if entry and now < entry.locked_until:
            return max(1, math.ceil(entry.locked_until - now))
        return 0

    def record_failure(self, key: str) -> Tuple[int, int]:
        """Register a failed attempt; returns (attempts_remaining, retry_after)."""
        now = self._clock()
        self._prune(now)
        entry = self._get(key, now)
        if entry is None:
            entry = self._entries[key] = _Entry()
        entry.last_failure = now
        entry.failures += 1
        if entry.failures >= self.MAX_ATTEMPTS:
            entry.failures = 0
            duration = min(self.BASE_LOCKOUT * (2 ** entry.lockouts), self.MAX_LOCKOUT)
            entry.lockouts += 1
            entry.locked_until = now + duration
            return 0, duration
        return self.MAX_ATTEMPTS - entry.failures, 0

    def record_success(self, key: str):
        self._entries.pop(key, None)

    def __len__(self):
        return len(self._entries)


def get_session_token(request) -> Optional[str]:
    return request.cookies.get(SESSION_COOKIE)


@web.middleware
async def auth_middleware(request, handler):
    """Middleware that checks authentication for API routes."""
    path = request.path
    if path in ('/api/auth/login', '/api/auth/check') or not path.startswith('/api/'):
        return await handler(request)

    _maybe_cleanup_sessions()
    if not validate_session_token(get_session_token(request)):
        return web.json_response({'error': 'Unauthorized'}, status=401)

    return await handler(request)


def content_security_policy(host: Optional[str]) -> str:
    connect = "'self'"
    if host and _HOST_RE.match(host):
        connect += f" ws://{host} wss://{host}"
    return (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https: http:; media-src 'self' https: http:; "
        f"connect-src {connect}; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    )


async def apply_security_headers(request, response):
    """on_response_prepare hook: runs for every response, including streamed ones."""
    response.headers['Content-Security-Policy'] = content_security_policy(request.headers.get('Host'))
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
