"""Dashboard web server for Gideon bot admin interface."""
import asyncio
import json
import logging
import math
import os
import re
import tempfile
import time
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from aiohttp import web, WSMsgType, BodyPartReader

from .auth import (
    SESSION_COOKIE,
    SESSION_DURATION,
    LoginThrottle,
    apply_security_headers,
    auth_middleware,
    cleanup_expired_sessions,
    generate_session_token,
    get_session_token,
    invalidate_session_token,
    validate_session_token,
    verify_secret,
)
from .error_log import ErrorLogBuffer

logger = logging.getLogger(__name__)

IMAGE_PROVIDERS = ['ai_horde', 'cloudflare', 'openai', 'comfyui', 'openrouter']
VIDEO_PROVIDERS = ['openrouter']
JOB_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
INT_RE = re.compile(r'^-?[0-9]+$')

CHUNK_SIZE = 64 * 1024
SEARCH_GROUP_LIMIT = 10
WS_CLOSE_SESSION_EXPIRED = 4401

# Bounds mirror the setters in BotStateManager / the slash commands
SETTINGS_INT_RANGES = {
    'max_channel_history': (1, 100),
    'time_window_hours': (1, 96),
    'prune_frequency_hours': (1, 168),
    'session_timeout_hours': (1, 168),
    'max_memory_summaries': (1, 50),
    'tool_calling_max_iterations': (1, 10),
}
SETTINGS_BOOL_FIELDS = ('memory_summary_enabled', 'tool_calling_enabled')
CHANNEL_MEMORY_RANGES = {
    'session_timeout_hours': (1, 168),
    'max_memory_summaries': (1, 50),
}
CHANNEL_OVERRIDE_FIELDS = (
    'model', 'provider', 'system_prompt',
    'session_timeout_hours', 'memory_summary_enabled', 'max_memory_summaries',
)


class ApiError(Exception):
    """Raised by handlers to produce a specific JSON error response."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.payload = {'error': message, **extra}

    def response(self) -> web.Response:
        headers = {}
        if 'retry_after' in self.payload:
            headers['Retry-After'] = str(self.payload['retry_after'])
        return web.json_response(self.payload, status=self.status, headers=headers)


@web.middleware
async def error_middleware(request, handler):
    """Turns ApiError into JSON and hides details of unexpected failures."""
    try:
        return await handler(request)
    except ApiError as e:
        return e.response()
    except web.HTTPException:
        raise
    except Exception:
        logger.error("Unhandled error handling %s %s", request.method, request.path, exc_info=True)
        return web.json_response({'error': 'Internal server error'}, status=500)


def parse_int_param(request, name: str, default: int, minimum: int, maximum: Optional[int] = None) -> int:
    """Parse an integer query parameter, raising a 400 ApiError when invalid."""
    raw = request.query.get(name)
    if raw is None or raw == '':
        return default
    if not INT_RE.fullmatch(raw):
        raise ApiError(400, f'{name} must be an integer')
    value = int(raw)
    if value < minimum or (maximum is not None and value > maximum):
        bounds = f'between {minimum} and {maximum}' if maximum is not None else f'at least {minimum}'
        raise ApiError(400, f'{name} must be {bounds}')
    return value


async def read_json_object(request) -> Dict[str, Any]:
    try:
        data = await request.json()
    except web.HTTPException:
        raise
    except Exception:
        raise ApiError(400, 'Invalid JSON')
    if not isinstance(data, dict):
        raise ApiError(400, 'Request body must be a JSON object')
    return data


def coerce_int(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f'{name} must be an integer between {minimum} and {maximum}')
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, str) and INT_RE.fullmatch(value.strip()):
        value = int(value.strip())
    if not isinstance(value, int) or value < minimum or value > maximum:
        raise ValueError(f'{name} must be an integer between {minimum} and {maximum}')
    return value


def coerce_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f'{name} must be a boolean')
    return value


def too_many_attempts(retry_after: int) -> ApiError:
    return ApiError(429, 'Too many failed attempts', retry_after=retry_after)


def validation_error(details: Dict[str, str]) -> ApiError:
    return ApiError(400, 'Validation failed', details=details)


def remove_sqlite_files(path: str):
    for suffix in ('', '-journal', '-wal', '-shm'):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def latency_ms(bot) -> Optional[float]:
    latency = getattr(bot, 'latency', None)
    if latency is None or not math.isfinite(latency):
        return None
    return round(latency * 1000, 1)


def public_persona(persona: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in persona.items() if k not in ('webhook_id', 'webhook_token')}


class DashboardServer:
    """Manages the aiohttp web server that serves the admin dashboard."""

    MAX_CONFIG_UPLOAD_BYTES = 10 * 1024 * 1024
    MAX_DB_UPLOAD_BYTES = 100 * 1024 * 1024
    SYNC_COOLDOWN_SECONDS = 60

    def __init__(self, bot, secret: str, port: int = 8080):
        self.bot = bot
        self.secret = secret
        self.port = port
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.login_throttle = LoginThrottle()
        self.error_log = ErrorLogBuffer()
        self._ws_clients: Dict[web.WebSocketResponse, str] = {}
        self._start_time = time.time()
        self._db_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self._last_sync: Optional[float] = None
        self._monotonic = time.monotonic

    @property
    def ws_client_count(self) -> int:
        return len(self._ws_clients)

    def create_app(self) -> web.Application:
        """Build the aiohttp application (middlewares, signals, routes)."""
        self.app = web.Application(middlewares=[error_middleware, auth_middleware])
        self.app.on_response_prepare.append(apply_security_headers)
        self.app.on_startup.append(self._on_startup)
        self.app.on_shutdown.append(self._on_shutdown)
        self._register_routes()
        return self.app

    async def _on_startup(self, app):
        self.error_log.install()

    async def _on_shutdown(self, app):
        self.error_log.uninstall()
        for ws in list(self._ws_clients):
            await ws.close()
        self._ws_clients.clear()

    async def start(self):
        """Start the dashboard web server."""
        self.create_app()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await self.site.start()
        logger.info(f"Dashboard server started on port {self.port}")

    async def stop(self):
        """Stop the dashboard web server."""
        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
        self.error_log.uninstall()
        logger.info("Dashboard server stopped")

    def _register_routes(self):
        """Register all HTTP and WebSocket routes."""
        router = self.app.router

        # Static files (dashboard frontend)
        dashboard_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'dashboard')
        css_dir = os.path.join(dashboard_dir, 'css')
        js_dir = os.path.join(dashboard_dir, 'js')

        # Page routes
        router.add_get('/', self._handle_index)
        router.add_get('/login', self._handle_login_page)

        # Static asset routes (append_version busts browser cache on file changes)
        if os.path.isdir(css_dir):
            router.add_static('/css/', css_dir, append_version=True)
        if os.path.isdir(js_dir):
            router.add_static('/js/', js_dir, append_version=True)

        # Auth API
        router.add_post('/api/auth/login', self._handle_login)
        router.add_post('/api/auth/logout', self._handle_logout)
        router.add_get('/api/auth/check', self._handle_auth_check)

        # Overview API
        router.add_get('/api/overview', self._handle_overview)

        # Settings API
        router.add_get('/api/settings', self._handle_get_settings)
        router.add_put('/api/settings', self._handle_update_settings)

        # Channel API
        router.add_get('/api/channels', self._handle_get_channels)
        router.add_get('/api/channels/all', self._handle_get_all_channels)
        router.add_get('/api/channels/{channel_id}', self._handle_get_channel)
        router.add_put('/api/channels/{channel_id}', self._handle_update_channel)
        router.add_delete('/api/channels/{channel_id}', self._handle_reset_channel)

        # Thread API
        router.add_get('/api/threads', self._handle_get_threads)
        router.add_get('/api/threads/{thread_id}', self._handle_get_thread)
        router.add_put('/api/threads/{thread_id}', self._handle_update_thread)
        router.add_delete('/api/threads/{thread_id}', self._handle_delete_thread)

        # Messages API
        router.add_get('/api/messages', self._handle_get_messages)
        router.add_get('/api/messages/sources', self._handle_get_message_sources)

        # Models API
        router.add_get('/api/models/{provider}', self._handle_get_models)
        router.add_get('/api/providers', self._handle_get_providers)

        # Image Generation API
        router.add_get('/api/image/settings', self._handle_get_image_settings)
        router.add_put('/api/image/settings', self._handle_update_image_settings)
        router.add_get('/api/image/models', self._handle_get_image_models)

        # Video Generation API
        router.add_get('/api/video/settings', self._handle_get_video_settings)
        router.add_put('/api/video/settings', self._handle_update_video_settings)
        router.add_get('/api/video/models', self._handle_get_video_models)
        router.add_post('/api/video/generate', self._handle_video_generate)
        router.add_get('/api/video/jobs/{job_id}', self._handle_video_job_status)

        # Diagnostics API
        router.add_get('/api/diagnostics', self._handle_diagnostics)
        router.add_post('/api/diagnostics/prune', self._handle_prune)
        router.add_get('/api/logs/errors', self._handle_get_error_log)

        # Slash command sync and global search
        router.add_post('/api/commands/sync', self._handle_sync_commands)
        router.add_get('/api/search', self._handle_search)

        # Backup & Restore API
        router.add_get('/api/backup/config', self._handle_export_config)
        router.add_get('/api/backup/database', self._handle_export_database)
        router.add_post('/api/backup/config/validate', self._handle_validate_config_import)
        router.add_post('/api/backup/config/import', self._handle_import_config)
        router.add_post('/api/backup/database/validate', self._handle_validate_db_restore)
        router.add_post('/api/backup/database/restore', self._handle_restore_database)

        # API Key Management
        router.add_get('/api/keys', self._handle_get_keys)
        router.add_post('/api/keys', self._handle_add_key)
        router.add_post('/api/keys/test', self._handle_test_key)
        router.add_put('/api/keys/{key_id}', self._handle_update_key)
        router.add_delete('/api/keys/{key_id}', self._handle_delete_key)
        router.add_post('/api/keys/{key_id}/validate', self._handle_validate_key)
        router.add_post('/api/keys/import-env', self._handle_import_env)
        router.add_get('/api/keys/audit', self._handle_get_audit_log)
        router.add_get('/api/keys/providers', self._handle_get_key_providers)

        # Memory API
        router.add_get('/api/memory', self._handle_get_memory_stats)
        router.add_get('/api/memory/{channel_id}', self._handle_get_channel_memories)
        router.add_delete('/api/memory/{channel_id}', self._handle_delete_channel_memories)
        router.add_post('/api/memory/{channel_id}/summarize', self._handle_trigger_summarize)

        # Persona API
        router.add_get('/api/personas/templates', self._handle_get_persona_templates)
        router.add_post('/api/personas/templates', self._handle_create_persona_template)
        router.add_put('/api/personas/templates/{template_id}', self._handle_update_persona_template)
        router.add_delete('/api/personas/templates/{template_id}', self._handle_delete_persona_template)
        router.add_get('/api/personas/channels', self._handle_get_channel_personas)
        router.add_get('/api/personas/channels/{channel_id}', self._handle_get_channel_persona)
        router.add_put('/api/personas/channels/{channel_id}', self._handle_set_channel_persona)
        router.add_delete('/api/personas/channels/{channel_id}', self._handle_remove_channel_persona)
        router.add_post('/api/personas/channels/{channel_id}/toggle', self._handle_toggle_channel_persona)
        router.add_post('/api/personas/channels/{channel_id}/apply-template', self._handle_apply_template)

        # WebSocket for real-time updates
        router.add_get('/ws', self._handle_websocket)

    # ── Helpers ────────────────────────────────────────────────────

    async def _db(self, func, *args):
        """Run blocking database work in a thread, one such job at a time."""
        async with self._db_lock:
            return await asyncio.to_thread(func, *args)

    def _uptime_seconds(self) -> int:
        started = getattr(self.bot, 'start_time', None) or self._start_time
        return max(0, int(time.time() - started))

    def _channel_info(self, channel_id) -> Dict[str, Optional[str]]:
        """Resolve a channel id to its Discord channel and guild names, if known."""
        try:
            ch = self.bot.get_channel(int(channel_id))
        except (ValueError, TypeError, AttributeError):
            ch = None
        if not ch:
            return {'channel_name': None, 'guild_name': None}
        guild = getattr(ch, 'guild', None)
        return {
            'channel_name': getattr(ch, 'name', None),
            'guild_name': guild.name if guild else None,
        }

    async def _check_model(self, model: Any) -> Optional[str]:
        """Return an error message when the model can't be used, else None."""
        if not isinstance(model, str) or not model.strip():
            return 'model must be a non-empty string'
        manager = self.bot.model_manager
        try:
            provider, name = manager.parse_model_id(model.strip())
            models = await manager.get_models(provider)
            if name not in models:
                models = await manager.get_models(provider, force_refresh=True)
            if name not in models:
                return f"Model '{provider}/{name}' not found for provider '{provider}'"
        except ValueError as e:
            return str(e)
        except Exception:
            logger.warning("Could not verify model %r", model, exc_info=True)
            return 'Could not verify the model with its provider'
        return None

    async def _check_provider(self, provider: Any) -> Optional[str]:
        if not isinstance(provider, str) or not provider:
            return 'provider must be a non-empty string'
        manager = self.bot.model_manager
        available = await manager.get_providers()
        clients = getattr(manager, 'providers', None)
        if provider not in available or (clients is not None and not clients.get(provider)):
            configured = [p for p in available if clients is None or clients.get(p)]
            return f"Invalid provider '{provider}'. Available: {', '.join(configured)}"
        return None

    # ── Page Handlers ──────────────────────────────────────────────

    async def _handle_index(self, request):
        """Serve the main dashboard page."""
        dashboard_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'dashboard')
        index_path = os.path.join(dashboard_dir, 'index.html')
        if os.path.exists(index_path):
            return web.FileResponse(index_path)
        return web.Response(text='Dashboard files not found', status=404)

    async def _handle_login_page(self, request):
        """Serve the login page (same index.html, JS handles routing)."""
        return await self._handle_index(request)

    # ── Auth Handlers ──────────────────────────────────────────────

    async def _handle_login(self, request):
        """Handle login with secret key."""
        client = request.remote or 'unknown'

        retry_after = self.login_throttle.retry_after(client)
        if retry_after:
            raise too_many_attempts(retry_after)

        data = await read_json_object(request)
        provided_secret = data.get('secret', '')
        if not isinstance(provided_secret, str) or not verify_secret(provided_secret, self.secret):
            remaining, retry_after = self.login_throttle.record_failure(client)
            logger.warning(f"Failed login attempt from {client}")
            if retry_after:
                logger.warning(f"Login locked for {client} for {retry_after}s")
                raise too_many_attempts(retry_after)
            return web.json_response(
                {'error': 'Invalid secret', 'attempts_remaining': remaining}, status=401)

        self.login_throttle.record_success(client)
        cleanup_expired_sessions()
        token = generate_session_token()
        response = web.json_response({'status': 'ok'})
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DURATION, httponly=True,
                            samesite='Strict', secure=request.secure)
        logger.info(f"Successful login from {client}")
        return response

    async def _handle_logout(self, request):
        """Handle logout."""
        token = get_session_token(request)
        if token:
            invalidate_session_token(token)
            for ws, ws_token in list(self._ws_clients.items()):
                if ws_token == token:
                    await ws.close(code=WS_CLOSE_SESSION_EXPIRED, message=b'Logged out')
        response = web.json_response({'status': 'ok'})
        response.del_cookie(SESSION_COOKIE)
        return response

    async def _handle_auth_check(self, request):
        """Check if the current session is valid."""
        return web.json_response({'authenticated': validate_session_token(get_session_token(request))})

    # ── Overview Handler ───────────────────────────────────────────

    def _overview_counts(self):
        state = self.bot.state_manager
        return (state.get_message_count(), state.get_thread_count(),
                len(state.get_all_channel_configs()))

    def _provider_health(self) -> Dict[str, str]:
        """Provider status from what is already cached; never calls out."""
        health = {}
        manager = self.bot.model_manager
        cache = getattr(manager, 'models_by_provider', {}) or {}
        for name in ('openrouter', 'openai'):
            if not getattr(self.bot, f'{name}_client', None):
                continue
            entry = cache.get(name)
            if entry is None:
                health[name] = 'configured'
            else:
                health[name] = 'ok' if entry.get('success') else 'error'
        if getattr(self.bot, 'ai_horde_client', None):
            health['ai_horde'] = 'configured'
        return health

    async def _handle_overview(self, request):
        """Get dashboard overview data."""
        state = self.bot.state_manager
        message_count, thread_count, channel_config_count = await self._db(self._overview_counts)
        guild_count = len(self.bot.guilds) if hasattr(self.bot, 'guilds') else 0

        db_path = state.db_manager.db_path
        db_size = os.path.getsize(db_path) if os.path.exists(db_path) else 0

        service = self._get_api_key_service()
        api_keys = service.get_status_summary() if service else None
        uptime = self._uptime_seconds()

        return web.json_response({
            'bot_name': str(self.bot.user) if self.bot.user else 'Unknown',
            'bot_id': str(self.bot.user.id) if self.bot.user else 'Unknown',
            'guilds': guild_count,
            'bot_uptime_seconds': uptime,
            'uptime_seconds': uptime,
            'message_count': message_count,
            'thread_count': thread_count,
            'channel_config_count': channel_config_count,
            'database_size_bytes': db_size,
            'global_model': state.get_global_model(),
            'global_provider': state.get_global_provider(),
            'latency_ms': latency_ms(self.bot),
            'health': {
                'providers': self._provider_health(),
                'api_keys': api_keys,
                'recent_error_count': self.error_log.count_since(86400),
            },
        })

    # ── Settings Handlers ──────────────────────────────────────────

    async def _handle_get_settings(self, request):
        """Get all global settings."""
        state = self.bot.state_manager
        return web.json_response({
            'global_model': state.get_global_model(),
            'global_provider': state.get_global_provider(),
            'global_system_prompt': state.get_global_system_prompt(),
            'max_channel_history': state.get_max_channel_history(),
            'time_window_hours': state.get_time_window_hours(),
            'prune_frequency_hours': state.get_prune_frequency_hours(),
            'session_timeout_hours': state.get_session_timeout_hours(),
            'memory_summary_enabled': state.get_memory_summary_enabled(),
            'max_memory_summaries': state.get_max_memory_summaries(),
            'tool_calling_enabled': state.get_tool_calling_enabled(),
            'tool_calling_max_iterations': state.get_tool_calling_max_iterations(),
        })

    async def _handle_update_settings(self, request):
        """Update global settings; every field is validated before any is applied."""
        data = await read_json_object(request)
        state = self.bot.state_manager
        errors: Dict[str, str] = {}
        parsed: Dict[str, Any] = {}

        if 'global_model' in data and data['global_model'] != state.get_global_model():
            error = await self._check_model(data['global_model'])
            if error:
                errors['global_model'] = error
            else:
                parsed['global_model'] = data['global_model'].strip()

        if 'global_provider' in data:
            error = await self._check_provider(data['global_provider'])
            if error:
                errors['global_provider'] = error
            else:
                parsed['global_provider'] = data['global_provider']

        if 'global_system_prompt' in data:
            prompt = data['global_system_prompt']
            if prompt is not None and not isinstance(prompt, str):
                errors['global_system_prompt'] = 'global_system_prompt must be a string or null'
            else:
                parsed['global_system_prompt'] = prompt

        for name, (low, high) in SETTINGS_INT_RANGES.items():
            if name in data:
                try:
                    parsed[name] = coerce_int(data[name], name, low, high)
                except ValueError as e:
                    errors[name] = str(e)

        for name in SETTINGS_BOOL_FIELDS:
            if name in data:
                try:
                    parsed[name] = coerce_bool(data[name], name)
                except ValueError as e:
                    errors[name] = str(e)

        if errors:
            raise validation_error(errors)

        setters = {
            'global_model': state.set_global_model,
            'global_provider': state.set_global_provider,
            'global_system_prompt': state.set_global_system_prompt,
            'max_channel_history': state.set_max_channel_history,
            'time_window_hours': state.set_time_window_hours,
            'prune_frequency_hours': state.set_prune_frequency_hours,
            'session_timeout_hours': state.set_session_timeout_hours,
            'memory_summary_enabled': state.set_memory_summary_enabled,
            'max_memory_summaries': state.set_max_memory_summaries,
            'tool_calling_enabled': state.set_tool_calling_enabled,
            'tool_calling_max_iterations': state.set_tool_calling_max_iterations,
        }
        updated = []
        # The model is the only setter that can still fail (it re-checks the live
        # model list), so it goes first and nothing else is written if it does.
        for name in sorted(parsed, key=lambda n: n != 'global_model'):
            try:
                await setters[name](parsed[name])
            except ValueError as e:
                if not updated:
                    raise validation_error({name: str(e)})
                raise
            updated.append(name)

        await self._broadcast_ws({'type': 'settings_updated', 'fields': updated})
        return web.json_response({'status': 'ok', 'updated': updated})

    # ── Channel Handlers ───────────────────────────────────────────

    async def _handle_get_channels(self, request):
        """Get all channels with custom configurations."""
        state = self.bot.state_manager
        configs = state.get_all_channel_configs()

        enriched = []
        for cfg in configs:
            info = self._channel_info(cfg.get('channel_id', ''))
            enriched.append({**cfg, **info})

        return web.json_response(enriched)

    def _channel_override_rows(self) -> Dict[str, Dict[str, Any]]:
        cursor = self.bot.state_manager.db_manager._get_cursor()
        cursor.execute(
            "SELECT channel_id, model, provider, system_prompt, session_timeout_hours, "
            "memory_summary_enabled, max_memory_summaries FROM CHANNEL_CONFIG"
        )
        return {row['channel_id']: dict(row) for row in cursor.fetchall()}

    async def _handle_get_all_channels(self, request):
        """Get ALL channels the bot is in across all guilds, with config overrides."""
        import discord

        config_map = self._channel_override_rows()

        channels = []
        for guild in self.bot.guilds:
            for ch in guild.channels:
                # Only include text-based channels where the bot can respond
                if not isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
                    continue

                ch_id = str(ch.id)
                cfg = config_map.get(ch_id, {})
                overrides = [f for f in CHANNEL_OVERRIDE_FIELDS if cfg.get(f) not in (None, '')]

                channels.append({
                    'channel_id': ch_id,
                    'channel_name': ch.name,
                    'guild_id': str(guild.id),
                    'guild_name': guild.name,
                    'category': ch.category.name if ch.category else None,
                    'model': cfg.get('model'),
                    'provider': cfg.get('provider'),
                    'system_prompt': cfg.get('system_prompt'),
                    'has_override': bool(overrides),
                    'overrides': overrides,
                    'type': str(ch.type).replace('ChannelType.', ''),
                })

        # Sort: channels with overrides first, then alphabetically by guild then channel name
        channels.sort(key=lambda c: (not c['has_override'], c['guild_name'].lower(), c['channel_name'].lower()))

        return web.json_response(channels)

    async def _handle_get_channel(self, request):
        """Get configuration for a specific channel."""
        channel_id = request.match_info['channel_id']
        state = self.bot.state_manager

        model = state.get_channel_model(channel_id)
        provider = state.db_manager.get_channel_provider(str(channel_id))
        system_prompt = state.get_channel_system_prompt(channel_id)
        effective_model = state.get_effective_model(channel_id)

        memory_config = {
            name: state._get_channel_memory_config(channel_id, name)
            for name in ('session_timeout_hours', 'memory_summary_enabled', 'max_memory_summaries')
        }

        raw_persona = state.get_channel_persona(channel_id)
        persona = None
        if raw_persona:
            persona = {
                'display_name': raw_persona.get('display_name'),
                'is_active': bool(raw_persona.get('is_active')),
                'template_id': raw_persona.get('template_id'),
            }

        active_persona = state.get_effective_persona(channel_id)
        sources = {
            'model': 'persona' if active_persona and active_persona.get('model')
                     else 'channel' if model is not None else 'global',
            'system_prompt': 'persona' if active_persona and active_persona.get('system_prompt')
                             else 'channel' if system_prompt else 'global',
        }
        for name, value in memory_config.items():
            sources[name] = 'channel' if value is not None else 'global'

        return web.json_response({
            'channel_id': channel_id,
            'channel_name': self._channel_info(channel_id)['channel_name'],
            'model': model,
            'provider': provider,
            'effective_provider': provider or state.get_global_provider(),
            'system_prompt': system_prompt,
            'effective_model': effective_model,
            **memory_config,
            'effective_session_timeout': state.get_effective_session_timeout(channel_id),
            'effective_memory_summary_enabled': state.get_effective_memory_summary_enabled(channel_id),
            'effective_max_memory_summaries': state.get_effective_max_memory_summaries(channel_id),
            'persona': persona,
            'sources': sources,
        })

    async def _handle_update_channel(self, request):
        """Update channel configuration; all fields are validated before any is applied."""
        channel_id = request.match_info['channel_id']
        data = await read_json_object(request)
        state = self.bot.state_manager
        errors: Dict[str, str] = {}

        model = None
        if 'model' in data:
            model = data['model'] or None
            if model is not None and model != state.get_channel_model(channel_id):
                error = await self._check_model(model)
                if error:
                    errors['model'] = error
                else:
                    model = model.strip()

        if 'provider' in data:
            error = await self._check_provider(data['provider'])
            if error:
                errors['provider'] = error

        system_prompt = None
        if 'system_prompt' in data:
            system_prompt = data['system_prompt'] or None
            if system_prompt is not None and not isinstance(system_prompt, str):
                errors['system_prompt'] = 'system_prompt must be a string or null'

        memory_kwargs: Dict[str, Any] = {}
        for name, (low, high) in CHANNEL_MEMORY_RANGES.items():
            if name in data:
                try:
                    value = data[name]
                    memory_kwargs[name] = None if value is None else coerce_int(value, name, low, high)
                except ValueError as e:
                    errors[name] = str(e)
        if 'memory_summary_enabled' in data:
            try:
                value = data['memory_summary_enabled']
                memory_kwargs['memory_summary_enabled'] = (
                    None if value is None else coerce_bool(value, 'memory_summary_enabled'))
            except ValueError as e:
                errors['memory_summary_enabled'] = str(e)

        if errors:
            raise validation_error(errors)

        updated = []
        try:
            if 'model' in data:
                await state.set_channel_model(channel_id, model)
                updated.append('model')

            if 'provider' in data:
                await state.set_channel_provider(channel_id, data['provider'])
                updated.append('provider')
        except ValueError as e:
            if not updated:
                raise ApiError(400, str(e))
            raise

        if 'system_prompt' in data:
            state.set_channel_system_prompt(channel_id, system_prompt)
            updated.append('system_prompt')

        if memory_kwargs:
            state.set_channel_memory_config(channel_id, **memory_kwargs)
            updated.extend(memory_kwargs)

        await self._broadcast_ws({'type': 'channel_updated', 'channel_id': channel_id, 'fields': updated})
        return web.json_response({'status': 'ok', 'updated': updated})

    async def _handle_reset_channel(self, request):
        """Reset channel to global defaults."""
        channel_id = request.match_info['channel_id']
        state = self.bot.state_manager
        result = state.reset_channel_config(channel_id)

        await self._broadcast_ws({'type': 'channel_reset', 'channel_id': channel_id})
        return web.json_response({'status': 'ok', 'reset': result})

    # ── Thread Handlers ────────────────────────────────────────────

    def _query_threads(self):
        cursor = self.bot.state_manager.db_manager._get_cursor()
        cursor.execute(
            "SELECT t.thread_id, t.channel_id, t.name, t.created_at, t.model, t.system_prompt, "
            "COUNT(m.message_pk) AS message_count "
            "FROM THREADS t LEFT JOIN MESSAGES m ON m.thread_id = t.thread_id "
            "GROUP BY t.thread_id ORDER BY t.created_at DESC"
        )
        return [dict(row) for row in cursor.fetchall()]

    async def _handle_get_threads(self, request):
        """Get all threads."""
        threads = await self._db(self._query_threads)
        for thread in threads:
            thread.update(self._channel_info(thread.get('channel_id')))
        return web.json_response(threads)

    async def _handle_get_thread(self, request):
        """Get a specific thread's details."""
        thread_id = request.match_info['thread_id']
        state = self.bot.state_manager

        thread_info = state.get_discord_thread(thread_id)
        if not thread_info:
            return web.json_response({'error': 'Thread not found'}, status=404)

        thread_config = state.get_discord_thread_config(thread_id)
        message_count = state.get_thread_message_count(thread_id)

        result = {
            **thread_info,
            'message_count': message_count,
        }
        if thread_config:
            result['model'] = thread_config.get('model')
            result['system_prompt'] = thread_config.get('system_prompt')

        return web.json_response(result)

    async def _handle_update_thread(self, request):
        """Update thread configuration; all fields are validated before any is applied."""
        thread_id = request.match_info['thread_id']
        data = await read_json_object(request)
        state = self.bot.state_manager
        errors: Dict[str, str] = {}

        name = None
        if 'name' in data:
            name = data['name'].strip() if isinstance(data['name'], str) else None
            if not name or len(name) > 100:
                errors['name'] = 'name must be a non-empty string of at most 100 characters'

        model = None
        if 'model' in data:
            model = data['model'] or None
            if model is not None and model != (state.get_discord_thread_config(thread_id) or {}).get('model'):
                error = await self._check_model(model)
                if error:
                    errors['model'] = error
                else:
                    model = model.strip()

        system_prompt = None
        if 'system_prompt' in data:
            system_prompt = data['system_prompt'] or None
            if system_prompt is not None and not isinstance(system_prompt, str):
                errors['system_prompt'] = 'system_prompt must be a string or null'

        if errors:
            raise validation_error(errors)

        if not state.get_discord_thread(thread_id):
            raise ApiError(404, 'Thread not found')

        updated = []
        if 'name' in data:
            state.rename_discord_thread(thread_id, name)
            updated.append('name')

        if 'model' in data:
            state.set_discord_thread_model(thread_id, model)
            updated.append('model')

        if 'system_prompt' in data:
            state.set_discord_thread_system_prompt(thread_id, system_prompt)
            updated.append('system_prompt')

        await self._broadcast_ws({'type': 'thread_updated', 'thread_id': thread_id, 'fields': updated})
        return web.json_response({'status': 'ok', 'updated': updated})

    async def _handle_delete_thread(self, request):
        """Delete a thread."""
        thread_id = request.match_info['thread_id']
        state = self.bot.state_manager
        result = state.delete_discord_thread(thread_id)

        await self._broadcast_ws({'type': 'thread_deleted', 'thread_id': thread_id})
        return web.json_response({'status': 'ok', 'deleted': result})

    # ── Message Handlers ─────────────────────────────────────────────

    def _query_messages(self, channel_id, thread_id, role_filter, per_page, offset):
        cursor = self.bot.state_manager.db_manager._get_cursor()
        where_clauses = []
        params = []

        if channel_id:
            where_clauses.append("channel_id = ?")
            params.append(channel_id)
        if thread_id:
            where_clauses.append("thread_id = ?")
            params.append(thread_id)
        if role_filter:
            where_clauses.append("role = ?")
            params.append(role_filter)

        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        cursor.execute(f"SELECT COUNT(*) FROM MESSAGES{where_sql}", tuple(params))
        total = cursor.fetchone()[0]

        cursor.execute(
            f"SELECT message_pk, channel_id, thread_id, user_id, user_name, role, content, timestamp "
            f"FROM MESSAGES{where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            tuple(params) + (per_page, offset)
        )
        columns = [desc[0] for desc in cursor.description]
        return total, [dict(zip(columns, row)) for row in cursor.fetchall()]

    async def _handle_get_messages(self, request):
        """Get paginated messages with optional channel/thread/role filters."""
        page = parse_int_param(request, 'page', 1, 1, 1_000_000)
        per_page = parse_int_param(request, 'per_page', 50, 1, 100)
        channel_id = request.query.get('channel_id')
        thread_id = request.query.get('thread_id')
        role_filter = request.query.get('role')

        total, rows = await self._db(
            self._query_messages, channel_id, thread_id, role_filter, per_page, (page - 1) * per_page)

        for row in rows:
            if row.get('channel_id'):
                row['channel_name'] = self._channel_info(row['channel_id'])['channel_name']
            if row.get('timestamp') and not isinstance(row['timestamp'], str):
                row['timestamp'] = str(row['timestamp'])

        return web.json_response({
            'messages': rows,
            'total': total,
            'page': page,
            'per_page': per_page,
            'total_pages': max(1, (total + per_page - 1) // per_page),
        })

    def _query_message_sources(self):
        cursor = self.bot.state_manager.db_manager._get_cursor()
        cursor.execute(
            "SELECT DISTINCT channel_id, COUNT(*) as msg_count "
            "FROM MESSAGES WHERE channel_id IS NOT NULL "
            "GROUP BY channel_id ORDER BY msg_count DESC"
        )
        channels = [(row[0], row[1]) for row in cursor.fetchall()]

        cursor.execute(
            "SELECT DISTINCT m.thread_id, COUNT(*) as msg_count, t.name "
            "FROM MESSAGES m LEFT JOIN THREADS t ON m.thread_id = t.thread_id "
            "WHERE m.thread_id IS NOT NULL "
            "GROUP BY m.thread_id ORDER BY msg_count DESC"
        )
        threads = [(row[0], row[1], row[2]) for row in cursor.fetchall()]
        return channels, threads

    async def _handle_get_message_sources(self, request):
        """Get list of channels and threads that have stored messages."""
        channel_rows, thread_rows = await self._db(self._query_message_sources)

        channels = []
        for ch_id, count in channel_rows:
            info = self._channel_info(ch_id)
            channels.append({
                'id': ch_id,
                'name': info['channel_name'],
                'guild': info['guild_name'],
                'message_count': count,
            })
        threads = [{'id': tid, 'name': name, 'message_count': count} for tid, count, name in thread_rows]

        return web.json_response({'channels': channels, 'threads': threads})

    # ── Models Handlers ─────────────────────────────────────────────

    async def _handle_get_providers(self, request):
        """Get list of available AI providers."""
        providers = []
        if self.bot.openrouter_client:
            providers.append('openrouter')
        if self.bot.openai_client:
            providers.append('openai')
        return web.json_response(providers)

    async def _handle_get_models(self, request):
        """Get available models for a specific provider."""
        provider = request.match_info['provider']
        try:
            model_ids = await self.bot.model_manager.get_models(provider)
        except ValueError as e:
            raise ApiError(400, str(e))
        # Format as provider/model_id (matching slash command behavior)
        return web.json_response([f"{provider}/{mid}" for mid in model_ids])

    # ── Image Generation Handlers ─────────────────────────────────

    async def _handle_get_image_settings(self, request):
        """Get image generation settings (provider, configs, modalities)."""
        db = self.bot.state_manager.db_manager

        active_provider = db.get_global_config('image_active_provider', 'ai_horde')

        configs = {}
        default_configs = {
            'ai_horde': {"model": "stable_diffusion_xl", "size": "1024x1024", "steps": 30},
            'cloudflare': {"size": "768x768", "steps": 25, "seed": None},
            'openai': {"model": "dall-e-3", "size": "1024x1024", "quality": "standard", "style": "vivid"},
            'comfyui': {"model": None, "size": "512x512", "steps": 20, "seed": None, "workflow": None},
            'openrouter': {"model": "google/gemini-2.0-flash-exp", "aspect_ratio": "1:1", "image_size": "1K"},
        }

        for provider in IMAGE_PROVIDERS:
            config_json = db.get_global_config(f'image_config_{provider}')
            if config_json:
                try:
                    config = json.loads(config_json)
                except json.JSONDecodeError:
                    config = default_configs.get(provider, {})
            else:
                config = default_configs.get(provider, {})
            configs[provider] = config

        # Get modalities setting for OpenRouter (defaults to ["image", "text"])
        modalities_json = db.get_global_config('image_openrouter_modalities')
        if modalities_json:
            try:
                modalities = json.loads(modalities_json)
            except json.JSONDecodeError:
                modalities = ["image", "text"]
        else:
            modalities = ["image", "text"]

        # Check which clients are available
        image_cog = self.bot.get_cog("UnifiedImageCommands")
        available_providers = []
        if image_cog:
            if image_cog.horde_client: available_providers.append('ai_horde')
            if image_cog.cf_client: available_providers.append('cloudflare')
            if image_cog.openai_client: available_providers.append('openai')
            if image_cog.comfyui_client: available_providers.append('comfyui')
            if image_cog.openrouter_image_client: available_providers.append('openrouter')

        return web.json_response({
            'active_provider': active_provider,
            'configs': configs,
            'available_providers': available_providers,
            'openrouter_modalities': modalities,
        })

    async def _handle_update_image_settings(self, request):
        """Update image generation settings."""
        data = await read_json_object(request)
        db = self.bot.state_manager.db_manager

        if 'active_provider' in data and data['active_provider'] not in IMAGE_PROVIDERS:
            raise ApiError(400, f"Invalid provider: {data['active_provider']}")

        config_update = 'config' in data and 'provider' in data
        if config_update:
            if data['provider'] not in IMAGE_PROVIDERS:
                raise ApiError(400, f"Invalid provider: {data['provider']}")
            if not isinstance(data['config'], dict):
                raise ApiError(400, 'config must be an object')

        if 'openrouter_modalities' in data and not isinstance(data['openrouter_modalities'], list):
            raise ApiError(400, 'modalities must be an array')

        updated = []

        if 'active_provider' in data:
            db.set_global_config('image_active_provider', data['active_provider'], 'string')
            updated.append('active_provider')

        if config_update:
            provider = data['provider']
            config_key = f'image_config_{provider}'
            existing_json = db.get_global_config(config_key)
            try:
                existing = json.loads(existing_json) if existing_json else {}
            except json.JSONDecodeError:
                existing = {}
            merged = {**existing, **data['config']}
            db.set_global_config(config_key, json.dumps(merged), 'json')
            updated.append(f'config_{provider}')

        if 'openrouter_modalities' in data:
            db.set_global_config('image_openrouter_modalities',
                                 json.dumps(data['openrouter_modalities']), 'json')
            updated.append('openrouter_modalities')

        await self._broadcast_ws({'type': 'image_settings_updated', 'fields': updated})
        return web.json_response({'status': 'ok', 'updated': updated})

    async def _handle_get_image_models(self, request):
        """Get the live list of OpenRouter image generation models."""
        image_cog = self.bot.get_cog("UnifiedImageCommands")
        client = getattr(image_cog, 'openrouter_image_client', None) if image_cog else None
        if not client:
            return web.json_response({'error': 'OpenRouter image generation is not configured'}, status=503)

        force = request.query.get('refresh') in ('1', 'true')
        result = await client.get_image_models(force_refresh=force)
        if not result.get('success'):
            return web.json_response({'error': result.get('error', 'Failed to fetch models')}, status=502)
        return web.json_response(result.get('models', []))

    # ── Video Generation Handlers ─────────────────────────────────

    async def _handle_get_video_settings(self, request):
        """Get video generation settings."""
        from ...cogs.video_commands import (
            DB_KEY_ACTIVE_PROVIDER as VIDEO_PROVIDER_KEY,
            DB_KEY_CONFIG_PREFIX as VIDEO_CONFIG_PREFIX,
            DEFAULT_OPENROUTER_CONFIG as VIDEO_DEFAULT_CONFIG,
            DEFAULT_PROVIDER as VIDEO_DEFAULT_PROVIDER,
        )
        db = self.bot.state_manager.db_manager

        active_provider = db.get_global_config(VIDEO_PROVIDER_KEY, VIDEO_DEFAULT_PROVIDER)
        config_json = db.get_global_config(f'{VIDEO_CONFIG_PREFIX}{active_provider}')
        if config_json:
            try:
                stored = json.loads(config_json)
            except json.JSONDecodeError:
                stored = {}
        else:
            stored = {}
        config = {**VIDEO_DEFAULT_CONFIG, **stored}

        video_cog = self.bot.get_cog("VideoCommands")
        available = bool(video_cog and video_cog.video_client and video_cog.video_client.is_configured)

        return web.json_response({
            'active_provider': active_provider,
            'config': config,
            'available': available,
            'available_providers': ['openrouter'] if available else [],
        })

    async def _handle_update_video_settings(self, request):
        """Update video generation settings."""
        from ...cogs.video_commands import (
            DB_KEY_ACTIVE_PROVIDER as VIDEO_PROVIDER_KEY,
            DB_KEY_CONFIG_PREFIX as VIDEO_CONFIG_PREFIX,
        )
        data = await read_json_object(request)
        db = self.bot.state_manager.db_manager

        if 'active_provider' in data and data['active_provider'] not in VIDEO_PROVIDERS:
            raise ApiError(400, f"Unsupported video provider: {data['active_provider']}")

        config = None
        provider = None
        if 'config' in data:
            config = data['config'] or {}
            if not isinstance(config, dict):
                raise ApiError(400, 'config must be an object')
            provider = data.get('provider') or db.get_global_config(VIDEO_PROVIDER_KEY, 'openrouter')
            if provider not in VIDEO_PROVIDERS:
                raise ApiError(400, f'Unsupported video provider: {provider}')

        updated = []

        if 'active_provider' in data:
            db.set_global_config(VIDEO_PROVIDER_KEY, data['active_provider'], 'string')
            updated.append('active_provider')

        if config is not None:
            config_key = f'{VIDEO_CONFIG_PREFIX}{provider}'
            existing_json = db.get_global_config(config_key)
            try:
                existing = json.loads(existing_json) if existing_json else {}
            except json.JSONDecodeError:
                existing = {}
            db.set_global_config(config_key, json.dumps({**existing, **config}), 'json')
            updated.append(f'config_{provider}')

        await self._broadcast_ws({'type': 'video_settings_updated', 'fields': updated})
        return web.json_response({'status': 'ok', 'updated': updated})

    async def _handle_get_video_models(self, request):
        """Get available video generation models."""
        video_cog = self.bot.get_cog("VideoCommands")
        if not video_cog or not video_cog.video_client:
            return web.json_response({'error': 'Video generation is not configured'}, status=503)

        force = request.query.get('refresh') in ('1', 'true')
        result = await video_cog.video_client.list_video_models(force_refresh=force)
        if not result.get('success'):
            return web.json_response({'error': result.get('error', 'Failed to fetch models')}, status=502)
        return web.json_response({
            'models': result.get('models', []),
            'source': result.get('source', 'unknown'),
        })

    async def _handle_video_generate(self, request):
        """Submit a video generation job from the dashboard."""
        data = await read_json_object(request)

        prompt = data.get('prompt')
        prompt = prompt.strip() if isinstance(prompt, str) else ''
        if not prompt:
            return web.json_response({'error': 'prompt is required'}, status=400)

        video_cog = self.bot.get_cog("VideoCommands")
        if not video_cog or not video_cog.video_client or not video_cog.video_client.is_configured:
            return web.json_response({'error': 'Video client is not configured'}, status=400)

        cfg = video_cog._load_config()
        result = await video_cog.video_client.submit_video(
            prompt=prompt,
            model=data.get('model') or cfg.get('model'),
            aspect_ratio=data.get('aspect_ratio') or cfg.get('aspect_ratio'),
            duration=data.get('duration') if data.get('duration') is not None else cfg.get('duration'),
            resolution=data.get('resolution') or cfg.get('resolution'),
            audio=data.get('audio') if data.get('audio') is not None else cfg.get('audio'),
            seed=data.get('seed'),
            image=data.get('image_url'),
        )
        if not result.get('success'):
            return web.json_response({'error': result.get('error', 'Submission failed')}, status=502)
        return web.json_response({
            'job_id': result.get('job_id'),
            'status': result.get('status'),
            'polling_url': result.get('polling_url'),
        })

    async def _handle_video_job_status(self, request):
        """Poll a video generation job's status."""
        job_id = request.match_info.get('job_id', '')
        if not JOB_ID_RE.fullmatch(job_id):
            return web.json_response({'error': 'Invalid job_id'}, status=400)

        video_cog = self.bot.get_cog("VideoCommands")
        if not video_cog or not video_cog.video_client:
            return web.json_response({'error': 'Video client is not configured'}, status=400)

        result = await video_cog.video_client.get_video_status(job_id)
        if not result.get('success'):
            return web.json_response({'error': result.get('error')}, status=502)
        return web.json_response({
            'status': result.get('status'),
            'unsigned_urls': result.get('unsigned_urls', []),
        })

    # ── Diagnostics Handlers ───────────────────────────────────────

    async def _handle_diagnostics(self, request):
        """Run diagnostics and return results."""
        state = self.bot.state_manager
        uptime = self._uptime_seconds()
        results = {
            'timestamp': datetime.now().isoformat(),
            'bot_status': 'online' if self.bot.is_ready() else 'starting',
            'latency_ms': latency_ms(self.bot),
            'guilds': len(self.bot.guilds) if hasattr(self.bot, 'guilds') else 0,
            'bot_uptime_seconds': uptime,
            'uptime_seconds': uptime,
        }

        # Database check
        try:
            msg_count, thread_count, _ = await self._db(self._overview_counts)
            db_path = state.db_manager.db_path
            results['database'] = {
                'status': 'ok' if msg_count >= 0 else 'error',
                'message_count': msg_count,
                'thread_count': thread_count,
                'file_size_bytes': os.path.getsize(db_path) if os.path.exists(db_path) else 0,
            }
        except Exception:
            logger.error("Diagnostics database check failed", exc_info=True)
            results['database'] = {'status': 'error', 'error': 'Database check failed'}

        # Provider checks
        providers = {}
        for name in ('openrouter', 'openai'):
            if not getattr(self.bot, f'{name}_client', None):
                continue
            try:
                models = await self.bot.model_manager.get_models(name)
                providers[name] = {'status': 'ok', 'model_count': len(models)}
            except Exception:
                logger.error("Diagnostics model fetch failed for %s", name, exc_info=True)
                providers[name] = {'status': 'error', 'error': 'Failed to fetch models'}

        if getattr(self.bot, 'ai_horde_client', None):
            providers['ai_horde'] = {'status': 'configured'}

        results['providers'] = providers

        # Settings summary
        results['settings'] = {
            'global_model': state.get_global_model(),
            'global_provider': state.get_global_provider(),
            'max_channel_history': state.get_max_channel_history(),
            'time_window_hours': state.get_time_window_hours(),
            'prune_frequency_hours': state.get_prune_frequency_hours(),
            'tool_calling_enabled': state.get_tool_calling_enabled(),
        }

        return web.json_response(results)

    async def _handle_prune(self, request):
        """Trigger manual data pruning."""
        stats = await self._db(self.bot.state_manager.prune_old_data)
        await self._broadcast_ws({'type': 'prune_completed', 'stats': stats})
        return web.json_response({'status': 'ok', 'stats': stats})

    async def _handle_get_error_log(self, request):
        """Recent ERROR-level log records from the in-memory ring buffer."""
        limit = parse_int_param(request, 'limit', 50, 1, 100)
        return web.json_response(self.error_log.recent(limit))

    # ── Command Sync & Search Handlers ─────────────────────────────

    async def _handle_sync_commands(self, request):
        """Re-register the bot's slash commands with Discord."""
        if self._sync_lock.locked():
            raise ApiError(429, 'A command sync is already in progress', retry_after=5)

        now = self._monotonic()
        if self._last_sync is not None and now - self._last_sync < self.SYNC_COOLDOWN_SECONDS:
            wait = math.ceil(self.SYNC_COOLDOWN_SECONDS - (now - self._last_sync))
            raise ApiError(429, 'Commands were synced recently', retry_after=wait)

        async with self._sync_lock:
            self._last_sync = now
            await self.bot.sync_commands()

        logger.info("Slash commands synced from the dashboard")
        await self._broadcast_ws({'type': 'commands_synced'})
        return web.json_response({'status': 'ok'})

    def _search_rows(self):
        state = self.bot.state_manager
        cursor = state.db_manager._get_cursor()
        cursor.execute("SELECT thread_id, channel_id, name FROM THREADS ORDER BY created_at DESC")
        threads = [dict(row) for row in cursor.fetchall()]
        return threads, state.get_all_persona_templates(), state.get_all_channel_personas()

    async def _handle_search(self, request):
        """Global search over channels, threads and personas."""
        import discord

        result = {'channels': [], 'threads': [], 'persona_templates': [], 'channel_personas': []}
        query = (request.query.get('q') or '').strip()
        if len(query) < 2:
            return web.json_response(result)
        needle = query[:100].casefold()

        def matches(*values) -> bool:
            return any(v and needle in str(v).casefold() for v in values)

        for guild in self.bot.guilds:
            for ch in guild.channels:
                if len(result['channels']) >= SEARCH_GROUP_LIMIT:
                    break
                if isinstance(ch, (discord.TextChannel, discord.ForumChannel)) and matches(ch.name):
                    result['channels'].append({
                        'channel_id': str(ch.id), 'channel_name': ch.name, 'guild_name': guild.name})

        threads, templates, personas = await self._db(self._search_rows)

        for thread in threads:
            if len(result['threads']) >= SEARCH_GROUP_LIMIT:
                break
            if matches(thread['name']):
                result['threads'].append({
                    'thread_id': thread['thread_id'],
                    'name': thread['name'],
                    'channel_name': self._channel_info(thread['channel_id'])['channel_name'],
                })

        for template in templates:
            if len(result['persona_templates']) >= SEARCH_GROUP_LIMIT:
                break
            if matches(template.get('name'), template.get('display_name')):
                result['persona_templates'].append({
                    'template_id': template['template_id'],
                    'name': template['name'],
                    'display_name': template['display_name'],
                })

        for persona in personas:
            if len(result['channel_personas']) >= SEARCH_GROUP_LIMIT:
                break
            channel_name = self._channel_info(persona['channel_id'])['channel_name']
            if matches(persona.get('display_name'), channel_name):
                result['channel_personas'].append({
                    'channel_id': persona['channel_id'],
                    'channel_name': channel_name,
                    'display_name': persona['display_name'],
                })

        return web.json_response(result)

    # ── API Key Management Handlers ─────────────────────────────────

    def _get_api_key_service(self):
        """Get the API key service, or None if not configured."""
        return getattr(self.bot, 'api_key_service', None)

    def _require_api_key_service(self):
        service = self._get_api_key_service()
        if not service:
            raise ApiError(501, 'API key management not configured. Set ENCRYPTION_MASTER_KEY in .env.')
        return service

    @staticmethod
    def _string_field(data: Dict[str, Any], name: str) -> str:
        value = data.get(name, '')
        if value is None:
            return ''
        if not isinstance(value, str):
            raise ApiError(400, f'{name} must be a string')
        return value.strip()

    async def _handle_get_keys(self, request):
        """List all API keys (masked, never exposes full keys)."""
        service = self._require_api_key_service()
        keys = service.list_keys(provider=request.query.get('provider'))
        return web.json_response(keys)

    async def _handle_add_key(self, request):
        """Add a new API key."""
        service = self._require_api_key_service()
        data = await read_json_object(request)

        provider = self._string_field(data, 'provider')
        key_value = self._string_field(data, 'key')
        alias = self._string_field(data, 'alias') or None

        if not provider or not key_value:
            raise ApiError(400, 'provider and key are required')
        if provider not in service.PROVIDER_ENV_MAP:
            raise ApiError(400, f'Unknown provider: {provider}')

        result = service.add_key(provider, key_value, alias)
        await self._broadcast_ws({'type': 'api_key_added', 'provider': provider})
        return web.json_response(result, status=201)

    async def _handle_test_key(self, request):
        """Validate a raw key against its provider without storing it."""
        service = self._require_api_key_service()
        data = await read_json_object(request)

        provider = self._string_field(data, 'provider')
        key_value = self._string_field(data, 'key')
        if not provider or not key_value:
            raise ApiError(400, 'provider and key are required')
        if provider not in service.PROVIDER_ENV_MAP:
            raise ApiError(400, f'Unknown provider: {provider}')

        result = await service.validate_key(provider, key_value)
        return web.json_response({
            'valid': bool(result.get('valid')),
            'message': str(result.get('message', '')),
        })

    async def _handle_update_key(self, request):
        """Update an existing API key."""
        service = self._require_api_key_service()
        key_id = request.match_info['key_id']
        data = await read_json_object(request)

        key_value = self._string_field(data, 'key') or None
        alias = data.get('alias')
        if alias is not None and not isinstance(alias, str):
            raise ApiError(400, 'alias must be a string')
        is_active = data.get('is_active')

        updated = service.update_key(key_id, key_value, alias)
        if is_active is not None:
            service.db.set_api_key_active(key_id, bool(is_active))
            updated = True
        if updated:
            await self._broadcast_ws({'type': 'api_key_updated', 'key_id': key_id})
        return web.json_response({'updated': updated})

    async def _handle_delete_key(self, request):
        """Delete an API key."""
        service = self._require_api_key_service()
        key_id = request.match_info['key_id']
        deleted = service.delete_key(key_id)
        if deleted:
            await self._broadcast_ws({'type': 'api_key_deleted', 'key_id': key_id})
        return web.json_response({'deleted': deleted})

    async def _handle_validate_key(self, request):
        """Validate a stored key by testing the provider API."""
        service = self._require_api_key_service()
        key_id = request.match_info['key_id']
        result = await service.validate_and_update_status(key_id)
        await self._broadcast_ws({'type': 'api_key_updated', 'key_id': key_id})
        return web.json_response(result)

    async def _handle_import_env(self, request):
        """One-time bulk import of keys from .env into encrypted DB storage."""
        service = self._require_api_key_service()
        result = await service.import_from_env()
        if result['imported']:
            await self._broadcast_ws({'type': 'api_keys_imported', 'imported': result['imported']})
        return web.json_response(result)

    async def _handle_get_audit_log(self, request):
        """Get the API key audit log."""
        service = self._require_api_key_service()
        key_id = request.query.get('key_id')
        limit = parse_int_param(request, 'limit', 50, 1, 200)
        return web.json_response(service.get_audit_log(key_id, limit))

    async def _handle_get_key_providers(self, request):
        """Get list of supported providers for API key management."""
        service = self._require_api_key_service()
        providers = []
        for key, label in service.PROVIDER_LABELS.items():
            providers.append({
                'id': key,
                'label': label,
                'usage_link': service.PROVIDER_USAGE_LINKS.get(key),
                'env_var': service.PROVIDER_ENV_MAP.get(key),
            })
        return web.json_response(providers)

    # ── WebSocket Handler ──────────────────────────────────────────

    @staticmethod
    def _origin_allowed(request) -> bool:
        origin = request.headers.get('Origin')
        host = request.headers.get('Host')
        if not origin or not host:
            return False
        return urlparse(origin).netloc.lower() == host.lower()

    async def _handle_websocket(self, request):
        """Handle WebSocket connections for real-time updates."""
        if not self._origin_allowed(request):
            return web.json_response({'error': 'Forbidden'}, status=403)

        token = get_session_token(request)
        if not validate_session_token(token):
            return web.json_response({'error': 'Unauthorized'}, status=401)

        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=65536)
        await ws.prepare(request)

        self._ws_clients[ws] = token
        await ws.send_json({'type': 'connected', 'message': 'WebSocket connected'})
        logger.info(f"WebSocket client connected (total: {self.ws_client_count})")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if not validate_session_token(token):
                        await ws.close(code=WS_CLOSE_SESSION_EXPIRED, message=b'Session expired')
                        break
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict) and data.get('type') == 'ping':
                        await ws.send_json({'type': 'pong'})
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self._ws_clients.pop(ws, None)
            logger.info(f"WebSocket client disconnected (total: {self.ws_client_count})")

        return ws

    # ── Memory Handlers ──────────────────────────────────────────

    async def _handle_get_memory_stats(self, request):
        """Get per-channel memory summary statistics."""
        state = self.bot.state_manager
        stats = state.get_all_memory_stats()
        for entry in stats:
            entry['channel_name'] = self._channel_info(entry['channel_id'])['channel_name']
        return web.json_response(stats)

    async def _handle_get_channel_memories(self, request):
        """Get stored memory summaries for a channel."""
        channel_id = request.match_info['channel_id']
        state = self.bot.state_manager
        limit = parse_int_param(request, 'limit', 20, 1, 100)
        memories = state.get_channel_memories(channel_id, limit=limit)
        return web.json_response({
            'channel_id': channel_id,
            'channel_name': self._channel_info(channel_id)['channel_name'],
            'memories': [
                {**m,
                 'conversation_start': str(m['conversation_start']) if m.get('conversation_start') else None,
                 'created_at': str(m['created_at'])} for m in memories
            ],
        })

    async def _handle_delete_channel_memories(self, request):
        """Delete all memory summaries for a channel."""
        channel_id = request.match_info['channel_id']
        state = self.bot.state_manager
        deleted = state.delete_channel_memories(channel_id)
        await self._broadcast_ws({'type': 'memory_cleared', 'channel_id': channel_id})
        return web.json_response({'status': 'ok', 'deleted': deleted})

    async def _handle_trigger_summarize(self, request):
        """Manually summarize current channel history and rotate the session."""
        channel_id = request.match_info['channel_id']
        state = self.bot.state_manager

        history = state.get_channel_history(channel_id)
        if not history:
            return web.json_response({'error': 'No conversation history to summarize.'}, status=400)

        clients = {
            'openrouter': getattr(self.bot, 'openrouter_client', None),
            'openai': getattr(self.bot, 'openai_client', None),
        }
        if not any(clients.values()):
            return web.json_response({'error': 'No LLM client available for summarization.'}, status=503)

        from ..memory_service import _summarize_history, _parse_timestamp
        conversation_start = _parse_timestamp(history[0].get('timestamp')) if history else None
        summary = await _summarize_history(history, channel_id, state, clients)
        if not summary:
            return web.json_response({'error': 'Summarization failed or returned empty result.'}, status=502)

        state.add_channel_memory(channel_id, summary, len(history), conversation_start=conversation_start)
        max_summaries = state.get_effective_max_memory_summaries(channel_id)
        state.prune_channel_memories(channel_id, max_summaries)
        state.clear_channel_history(channel_id)

        await self._broadcast_ws({'type': 'memory_updated', 'channel_id': channel_id})
        return web.json_response({'status': 'ok', 'summary': summary, 'messages_archived': len(history)})

    # ── Persona Handlers ─────────────────────────────────────────

    async def _handle_get_persona_templates(self, request):
        """Get all persona templates."""
        state = self.bot.state_manager
        templates = state.get_all_persona_templates()
        return web.json_response(templates)

    async def _handle_create_persona_template(self, request):
        """Create a new persona template."""
        data = await read_json_object(request)

        required = ['template_id', 'name', 'display_name']
        for field in required:
            if field not in data or not data[field]:
                return web.json_response({'error': f'{field} is required'}, status=400)

        state = self.bot.state_manager
        state.db_manager.add_persona_template(
            template_id=data['template_id'],
            name=data['name'],
            display_name=data['display_name'],
            avatar_url=data.get('avatar_url'),
            system_prompt=data.get('system_prompt'),
            model=data.get('model'),
            provider=data.get('provider'),
            response_style=data.get('response_style'),
            description=data.get('description'),
            is_builtin=False,
        )

        await self._broadcast_ws({'type': 'persona_template_created', 'template_id': data['template_id']})
        return web.json_response({'status': 'ok', 'template_id': data['template_id']})

    async def _handle_update_persona_template(self, request):
        """Update a persona template."""
        template_id = request.match_info['template_id']
        data = await read_json_object(request)

        state = self.bot.state_manager
        template = state.get_persona_template(template_id)
        if not template:
            return web.json_response({'error': 'Template not found'}, status=404)

        state.db_manager.update_persona_template(template_id, **data)

        await self._broadcast_ws({'type': 'persona_template_updated', 'template_id': template_id})
        return web.json_response({'status': 'ok'})

    async def _handle_delete_persona_template(self, request):
        """Delete a persona template."""
        template_id = request.match_info['template_id']
        state = self.bot.state_manager

        template = state.get_persona_template(template_id)
        if not template:
            return web.json_response({'error': 'Template not found'}, status=404)
        if template.get('is_builtin'):
            return web.json_response({'error': 'Cannot delete built-in templates'}, status=403)

        deleted = state.db_manager.delete_persona_template(template_id)
        await self._broadcast_ws({'type': 'persona_template_deleted', 'template_id': template_id})
        return web.json_response({'status': 'ok', 'deleted': deleted})

    async def _handle_get_channel_personas(self, request):
        """Get all channel personas."""
        state = self.bot.state_manager
        personas = [public_persona(p) for p in state.get_all_channel_personas()]
        for p in personas:
            p['channel_name'] = self._channel_info(p['channel_id'])['channel_name']
        return web.json_response(personas)

    async def _handle_get_channel_persona(self, request):
        """Get persona for a specific channel."""
        channel_id = request.match_info['channel_id']
        state = self.bot.state_manager
        persona = state.get_channel_persona(channel_id)

        if not persona:
            return web.json_response({'error': 'No persona configured'}, status=404)

        persona = public_persona(persona)
        persona['channel_name'] = self._channel_info(channel_id)['channel_name']
        return web.json_response(persona)

    async def _handle_set_channel_persona(self, request):
        """Set or update a channel's persona."""
        channel_id = request.match_info['channel_id']
        data = await read_json_object(request)

        if 'display_name' not in data or not data['display_name']:
            return web.json_response({'error': 'display_name is required'}, status=400)

        state = self.bot.state_manager
        state.set_channel_persona(
            channel_id=channel_id,
            display_name=data['display_name'],
            avatar_url=data.get('avatar_url'),
            system_prompt=data.get('system_prompt'),
            model=data.get('model'),
            provider=data.get('provider'),
            response_style=data.get('response_style'),
            template_id=data.get('template_id'),
        )

        # Invalidate webhook cache
        if hasattr(self.bot, 'webhook_sender'):
            self.bot.webhook_sender.invalidate_cache(channel_id)

        await self._broadcast_ws({'type': 'persona_updated', 'channel_id': channel_id})
        return web.json_response({'status': 'ok'})

    async def _handle_remove_channel_persona(self, request):
        """Remove persona from a channel."""
        channel_id = request.match_info['channel_id']
        state = self.bot.state_manager
        removed = state.remove_channel_persona(channel_id)

        if hasattr(self.bot, 'webhook_sender'):
            self.bot.webhook_sender.invalidate_cache(channel_id)

        await self._broadcast_ws({'type': 'persona_removed', 'channel_id': channel_id})
        return web.json_response({'status': 'ok', 'removed': removed})

    async def _handle_toggle_channel_persona(self, request):
        """Toggle active state of a channel persona."""
        channel_id = request.match_info['channel_id']
        state = self.bot.state_manager
        new_state = state.toggle_channel_persona(channel_id)

        if new_state is None:
            return web.json_response({'error': 'No persona configured for this channel'}, status=404)

        if hasattr(self.bot, 'webhook_sender'):
            self.bot.webhook_sender.invalidate_cache(channel_id)

        await self._broadcast_ws({'type': 'persona_toggled', 'channel_id': channel_id, 'is_active': new_state})
        return web.json_response({'status': 'ok', 'is_active': new_state})

    async def _handle_apply_template(self, request):
        """Apply a persona template to a channel."""
        channel_id = request.match_info['channel_id']
        data = await read_json_object(request)

        template_id = data.get('template_id')
        if not template_id:
            return web.json_response({'error': 'template_id is required'}, status=400)

        state = self.bot.state_manager
        template = state.get_persona_template(template_id)
        if not template:
            return web.json_response({'error': 'Template not found'}, status=404)

        state.set_channel_persona(
            channel_id=channel_id,
            display_name=template['display_name'],
            avatar_url=template.get('avatar_url'),
            system_prompt=template.get('system_prompt'),
            model=template.get('model'),
            provider=template.get('provider'),
            response_style=template.get('response_style'),
            template_id=template_id,
        )

        if hasattr(self.bot, 'webhook_sender'):
            self.bot.webhook_sender.invalidate_cache(channel_id)

        await self._broadcast_ws({'type': 'persona_template_applied', 'channel_id': channel_id, 'template_id': template_id})
        return web.json_response({'status': 'ok'})

    # ── Backup & Restore Handlers ─────────────────────────────────

    @staticmethod
    def _megabytes(limit: int) -> str:
        return f'{limit / (1024 * 1024):g} MB'

    @staticmethod
    async def _upload_field(request) -> BodyPartReader:
        if not request.content_type.startswith('multipart/'):
            raise ApiError(400, 'Expected a multipart/form-data upload')
        try:
            reader = await request.multipart()
            field = await reader.next()
        except (AssertionError, ValueError):
            raise ApiError(400, 'Malformed upload')
        if not isinstance(field, BodyPartReader) or field.name != 'file':
            raise ApiError(400, 'No file uploaded')
        return field

    async def _read_upload_bytes(self, request, limit: int) -> bytes:
        """Buffer an uploaded file in memory, rejecting it as soon as it exceeds `limit`."""
        field = await self._upload_field(request)
        buffer = bytearray()
        while True:
            chunk = await field.read_chunk(CHUNK_SIZE)
            if not chunk:
                return bytes(buffer)
            buffer += chunk
            if len(buffer) > limit:
                raise ApiError(413, f'File too large (max {self._megabytes(limit)})')

    async def _save_upload(self, request, limit: int, suffix: str) -> str:
        """Stream an uploaded file to a unique temp file; the caller must delete it."""
        field = await self._upload_field(request)
        fd, path = tempfile.mkstemp(prefix='gideon_upload_', suffix=suffix)
        try:
            size = 0
            with os.fdopen(fd, 'wb') as f:
                while True:
                    chunk = await field.read_chunk(CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ApiError(413, f'File too large (max {self._megabytes(limit)})')
                    f.write(chunk)
        except BaseException:
            remove_sqlite_files(path)
            raise
        return path

    async def _read_config_upload(self, request) -> Dict[str, Any]:
        content = await self._read_upload_bytes(request, self.MAX_CONFIG_UPLOAD_BYTES)
        try:
            return await asyncio.to_thread(lambda: json.loads(content.decode('utf-8')))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, 'Invalid JSON file')

    async def _handle_export_config(self, request):
        """Export bot configuration as downloadable JSON."""
        db = self.bot.state_manager.db_manager
        config_data = await self._db(db.export_config_json)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f'gideon_config_{timestamp}.json'

        return web.json_response(
            config_data,
            headers={'Content-Disposition': f'attachment; filename="{filename}"'}
        )

    async def _handle_export_database(self, request):
        """Export full SQLite database as downloadable file, streamed from a temp copy."""
        db = self.bot.state_manager.db_manager
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f'gideon_backup_{timestamp}.db'

        fd, backup_path = tempfile.mkstemp(prefix='gideon_backup_', suffix='.db')
        os.close(fd)
        try:
            await self._db(db.create_sqlite_backup, backup_path)

            response = web.StreamResponse(headers={
                'Content-Disposition': f'attachment; filename="{backup_filename}"',
                'Content-Type': 'application/x-sqlite3',
            })
            response.content_length = os.path.getsize(backup_path)
            await response.prepare(request)
            with open(backup_path, 'rb') as f:
                while True:
                    chunk = await asyncio.to_thread(f.read, CHUNK_SIZE * 4)
                    if not chunk:
                        break
                    await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            remove_sqlite_files(backup_path)

    async def _handle_validate_config_import(self, request):
        """Validate an uploaded JSON config file before importing."""
        data = await self._read_config_upload(request)
        db = self.bot.state_manager.db_manager
        validation = await asyncio.to_thread(db.validate_config_json, data)
        return web.json_response(validation)

    async def _handle_import_config(self, request):
        """Import configuration from uploaded JSON file."""
        data = await self._read_config_upload(request)
        state = self.bot.state_manager

        validation = await asyncio.to_thread(state.db_manager.validate_config_json, data)
        if not validation.get('valid'):
            raise ApiError(400, 'Validation failed', details=validation.get('errors', []))

        result = await self._db(state.db_manager.import_config_json, data)

        # Reload in-memory state from DB
        await state.reinitialize_state()

        await self._broadcast_ws({'type': 'config_imported', 'result': result})
        logger.info(f"Config imported successfully: {result.get('applied', {})}")
        return web.json_response({'status': 'ok', 'result': result})

    async def _handle_validate_db_restore(self, request):
        """Validate an uploaded SQLite database file before restoring."""
        temp_path = await self._save_upload(request, self.MAX_DB_UPLOAD_BYTES, '.db')
        try:
            validation = await self._db(
                self.bot.state_manager.db_manager.validate_sqlite_backup, temp_path)
            return web.json_response(validation)
        finally:
            remove_sqlite_files(temp_path)

    async def _handle_restore_database(self, request):
        """Restore database from uploaded SQLite backup file."""
        temp_path = await self._save_upload(request, self.MAX_DB_UPLOAD_BYTES, '.db')
        try:
            state = self.bot.state_manager

            validation = await self._db(state.db_manager.validate_sqlite_backup, temp_path)
            if not validation.get('valid'):
                raise ApiError(400, 'Validation failed', details=validation.get('errors', []))

            result = await self._db(state.db_manager.restore_sqlite_backup, temp_path)
        finally:
            remove_sqlite_files(temp_path)

        if not result.get('success'):
            logger.error(f"Database restore failed: {result.get('error', 'Unknown error')}")
            return web.json_response({'error': 'Internal server error'}, status=500)

        # Reinitialize state from the restored database
        await state.reinitialize_state()

        await self._broadcast_ws({'type': 'database_restored'})
        logger.info(f"Database restored successfully. Pre-restore backup at: {result.get('bak_path')}")
        return web.json_response({
            'status': 'ok',
            'message': 'Database restored successfully. A backup of the previous database was saved.'
        })

    # ── WebSocket Broadcast ───────────────────────────────────────

    async def _broadcast_ws(self, data: dict):
        """Broadcast a message to all connected WebSocket clients."""
        if not self._ws_clients:
            return

        data['timestamp'] = datetime.now().isoformat()
        for ws, token in list(self._ws_clients.items()):
            try:
                if not validate_session_token(token):
                    self._ws_clients.pop(ws, None)
                    await ws.close(code=WS_CLOSE_SESSION_EXPIRED, message=b'Session expired')
                    continue
                await ws.send_json(data)
            except Exception:
                self._ws_clients.pop(ws, None)

    async def broadcast_event(self, event_type: str, data: dict = None):
        """Public method for cogs to broadcast events to dashboard clients."""
        payload = {'type': event_type}
        if data:
            payload.update(data)
        await self._broadcast_ws(payload)
