"""Dashboard web server for Gideon bot admin interface."""
import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import List, Optional

from aiohttp import web, WSMsgType

from .auth import (
    auth_middleware,
    generate_session_token,
    validate_session_token,
    invalidate_session_token,
    verify_secret,
    cleanup_expired_sessions,
)

logger = logging.getLogger(__name__)


class DashboardServer:
    """Manages the aiohttp web server that serves the admin dashboard."""

    def __init__(self, bot, secret: str, port: int = 8080):
        self.bot = bot
        self.secret = secret
        self.port = port
        self.app: Optional[web.Application] = None
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self._ws_clients: List[web.WebSocketResponse] = []
        self._start_time = time.time()

    async def start(self):
        """Start the dashboard web server."""
        self.app = web.Application(middlewares=[auth_middleware])
        self._register_routes()
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '0.0.0.0', self.port)
        await self.site.start()
        logger.info(f"Dashboard server started on port {self.port}")

    async def stop(self):
        """Stop the dashboard web server."""
        # Close all WebSocket connections
        for ws in list(self._ws_clients):
            await ws.close()
        self._ws_clients.clear()

        if self.site:
            await self.site.stop()
        if self.runner:
            await self.runner.cleanup()
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
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        provided_secret = data.get('secret', '')
        if not verify_secret(provided_secret, self.secret):
            logger.warning(f"Failed login attempt from {request.remote}")
            return web.json_response({'error': 'Invalid secret'}, status=401)

        token = generate_session_token(self.secret)
        response = web.json_response({'status': 'ok', 'token': token})
        response.set_cookie('session_token', token, max_age=86400, httponly=True, samesite='Strict')
        logger.info(f"Successful login from {request.remote}")
        return response

    async def _handle_logout(self, request):
        """Handle logout."""
        token = request.cookies.get('session_token')
        if token:
            invalidate_session_token(token)
        response = web.json_response({'status': 'ok'})
        response.del_cookie('session_token')
        return response

    async def _handle_auth_check(self, request):
        """Check if the current session is valid. Returns the token so JS can use it for WebSocket."""
        token = request.cookies.get('session_token')
        if not token:
            auth_header = request.headers.get('Authorization', '')
            if auth_header.startswith('Bearer '):
                token = auth_header[7:]

        if token and validate_session_token(token):
            return web.json_response({'authenticated': True, 'token': token})
        return web.json_response({'authenticated': False})

    # ── Overview Handler ───────────────────────────────────────────

    async def _handle_overview(self, request):
        """Get dashboard overview data."""
        state = self.bot.state_manager
        uptime_seconds = int(time.time() - self._start_time)

        # Gather stats
        message_count = state.get_message_count()
        thread_count = state.get_thread_count()
        channel_configs = state.get_all_channel_configs()
        guild_count = len(self.bot.guilds) if hasattr(self.bot, 'guilds') else 0

        # Database file info
        db_path = state.db_manager.db_path
        db_size = 0
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)

        return web.json_response({
            'bot_name': str(self.bot.user) if self.bot.user else 'Unknown',
            'bot_id': str(self.bot.user.id) if self.bot.user else 'Unknown',
            'guilds': guild_count,
            'uptime_seconds': uptime_seconds,
            'message_count': message_count,
            'thread_count': thread_count,
            'channel_config_count': len(channel_configs),
            'database_size_bytes': db_size,
            'global_model': state.get_global_model(),
            'global_provider': state.get_global_provider(),
            'latency_ms': round(self.bot.latency * 1000, 1) if hasattr(self.bot, 'latency') else None,
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
        """Update global settings."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        state = self.bot.state_manager
        updated = []

        try:
            if 'global_model' in data:
                await state.set_global_model(data['global_model'])
                updated.append('global_model')

            if 'global_provider' in data:
                await state.set_global_provider(data['global_provider'])
                updated.append('global_provider')

            if 'global_system_prompt' in data:
                await state.set_global_system_prompt(data['global_system_prompt'])
                updated.append('global_system_prompt')

            if 'max_channel_history' in data:
                val = int(data['max_channel_history'])
                if val < 1 or val > 100:
                    return web.json_response({'error': 'max_channel_history must be 1-100'}, status=400)
                await state.set_max_channel_history(val)
                updated.append('max_channel_history')

            if 'time_window_hours' in data:
                val = int(data['time_window_hours'])
                if val < 1 or val > 96:
                    return web.json_response({'error': 'time_window_hours must be 1-96'}, status=400)
                await state.set_time_window_hours(val)
                updated.append('time_window_hours')

            if 'prune_frequency_hours' in data:
                val = int(data['prune_frequency_hours'])
                await state.set_prune_frequency_hours(val)
                updated.append('prune_frequency_hours')

            if 'session_timeout_hours' in data:
                val = int(data['session_timeout_hours'])
                await state.set_session_timeout_hours(val)
                updated.append('session_timeout_hours')

            if 'memory_summary_enabled' in data:
                await state.set_memory_summary_enabled(bool(data['memory_summary_enabled']))
                updated.append('memory_summary_enabled')

            if 'max_memory_summaries' in data:
                val = int(data['max_memory_summaries'])
                await state.set_max_memory_summaries(val)
                updated.append('max_memory_summaries')

            if 'tool_calling_enabled' in data:
                await state.set_tool_calling_enabled(bool(data['tool_calling_enabled']))
                updated.append('tool_calling_enabled')

            if 'tool_calling_max_iterations' in data:
                val = int(data['tool_calling_max_iterations'])
                if val < 1 or val > 10:
                    return web.json_response({'error': 'tool_calling_max_iterations must be 1-10'}, status=400)
                await state.set_tool_calling_max_iterations(val)
                updated.append('tool_calling_max_iterations')

        except ValueError as e:
            return web.json_response({'error': str(e)}, status=400)

        await self._broadcast_ws({'type': 'settings_updated', 'fields': updated})
        return web.json_response({'status': 'ok', 'updated': updated})

    

    # ── Channel Handlers ───────────────────────────────────────────

    async def _handle_get_channels(self, request):
        """Get all channels with custom configurations."""
        state = self.bot.state_manager
        configs = state.get_all_channel_configs()

        # Enrich with Discord channel names if available
        enriched = []
        for cfg in configs:
            channel_id = cfg.get('channel_id', '')
            channel_name = None
            guild_name = None
            try:
                ch = self.bot.get_channel(int(channel_id))
                if ch:
                    channel_name = ch.name
                    if hasattr(ch, 'guild') and ch.guild:
                        guild_name = ch.guild.name
            except (ValueError, AttributeError):
                pass

            enriched.append({
                **cfg,
                'channel_name': channel_name,
                'guild_name': guild_name,
            })

        return web.json_response(enriched)

    async def _handle_get_all_channels(self, request):
        """Get ALL channels the bot is in across all guilds, with config overrides."""
        import discord
        state = self.bot.state_manager

        # Build a map of existing channel configs
        configs = state.get_all_channel_configs()
        config_map = {}
        for cfg in configs:
            config_map[cfg.get('channel_id', '')] = cfg

        channels = []
        for guild in self.bot.guilds:
            for ch in guild.channels:
                # Only include text-based channels where the bot can respond
                if not isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
                    continue

                ch_id = str(ch.id)
                cfg = config_map.get(ch_id, {})
                has_override = bool(cfg.get('model') or cfg.get('provider') or cfg.get('system_prompt'))

                channels.append({
                    'channel_id': ch_id,
                    'channel_name': ch.name,
                    'guild_id': str(guild.id),
                    'guild_name': guild.name,
                    'category': ch.category.name if ch.category else None,
                    'model': cfg.get('model'),
                    'provider': cfg.get('provider'),
                    'system_prompt': cfg.get('system_prompt'),
                    'has_override': has_override,
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
        provider = state.get_channel_provider(channel_id)
        system_prompt = state.get_channel_system_prompt(channel_id)
        effective_model = state.get_effective_model(channel_id)

        channel_name = None
        try:
            ch = self.bot.get_channel(int(channel_id))
            if ch:
                channel_name = ch.name
        except (ValueError, AttributeError):
            pass

        return web.json_response({
            'channel_id': channel_id,
            'channel_name': channel_name,
            'model': model,
            'provider': provider,
            'system_prompt': system_prompt,
            'effective_model': effective_model,
            'session_timeout_hours': state._get_channel_memory_config(channel_id, 'session_timeout_hours'),
            'memory_summary_enabled': state._get_channel_memory_config(channel_id, 'memory_summary_enabled'),
            'max_memory_summaries': state._get_channel_memory_config(channel_id, 'max_memory_summaries'),
            'effective_session_timeout': state.get_effective_session_timeout(channel_id),
            'effective_memory_summary_enabled': state.get_effective_memory_summary_enabled(channel_id),
            'effective_max_memory_summaries': state.get_effective_max_memory_summaries(channel_id),
        })

    async def _handle_update_channel(self, request):
        """Update channel configuration."""
        channel_id = request.match_info['channel_id']
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        state = self.bot.state_manager
        updated = []

        try:
            if 'model' in data:
                await state.set_channel_model(channel_id, data['model'] or None)
                updated.append('model')

            if 'provider' in data:
                await state.set_channel_provider(channel_id, data['provider'])
                updated.append('provider')

            if 'system_prompt' in data:
                state.set_channel_system_prompt(channel_id, data['system_prompt'] or None)
                updated.append('system_prompt')

            memory_kwargs = {}
            if 'session_timeout_hours' in data:
                v = data['session_timeout_hours']
                memory_kwargs['session_timeout_hours'] = int(v) if v is not None else None
                updated.append('session_timeout_hours')
            if 'memory_summary_enabled' in data:
                v = data['memory_summary_enabled']
                memory_kwargs['memory_summary_enabled'] = bool(v) if v is not None else None
                updated.append('memory_summary_enabled')
            if 'max_memory_summaries' in data:
                v = data['max_memory_summaries']
                memory_kwargs['max_memory_summaries'] = int(v) if v is not None else None
                updated.append('max_memory_summaries')
            if memory_kwargs:
                state.set_channel_memory_config(channel_id, **memory_kwargs)

        except ValueError as e:
            return web.json_response({'error': str(e)}, status=400)

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

    async def _handle_get_threads(self, request):
        """Get all threads."""
        state = self.bot.state_manager
        all_threads = []

        # Query all threads from the database directly
        try:
            cursor = state.db_manager._get_cursor()
            cursor.execute("SELECT thread_id, channel_id, name, created_at, model, system_prompt FROM THREADS ORDER BY created_at DESC")
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description]
            for row in rows:
                thread_data = dict(zip(columns, row))
                # Add message count
                thread_data['message_count'] = state.get_thread_message_count(thread_data['thread_id'])
                # Enrich with channel/guild names from Discord
                channel_name = None
                guild_name = None
                if thread_data.get('channel_id'):
                    try:
                        ch = self.bot.get_channel(int(thread_data['channel_id']))
                        if ch:
                            channel_name = ch.name
                            if hasattr(ch, 'guild') and ch.guild:
                                guild_name = ch.guild.name
                    except (ValueError, AttributeError):
                        pass
                thread_data['channel_name'] = channel_name
                thread_data['guild_name'] = guild_name
                all_threads.append(thread_data)
        except Exception as e:
            logger.error(f"Error fetching threads: {e}", exc_info=True)

        return web.json_response(all_threads)

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
        """Update thread configuration."""
        thread_id = request.match_info['thread_id']
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        state = self.bot.state_manager
        updated = []

        if 'name' in data:
            state.rename_discord_thread(thread_id, data['name'])
            updated.append('name')

        if 'model' in data:
            state.set_discord_thread_model(thread_id, data['model'] or None)
            updated.append('model')

        if 'system_prompt' in data:
            state.set_discord_thread_system_prompt(thread_id, data['system_prompt'] or None)
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

    async def _handle_get_messages(self, request):
        """Get paginated messages with optional channel/thread/role filters."""
        state = self.bot.state_manager
        page = int(request.query.get('page', '1'))
        per_page = min(int(request.query.get('per_page', '50')), 100)
        channel_id = request.query.get('channel_id')
        thread_id = request.query.get('thread_id')
        role_filter = request.query.get('role')

        try:
            cursor = state.db_manager._get_cursor()

            # Build query with filters
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

            # Count total
            cursor.execute(f"SELECT COUNT(*) FROM MESSAGES{where_sql}", tuple(params))
            total = cursor.fetchone()[0]

            # Fetch page (newest first)
            offset = (page - 1) * per_page
            cursor.execute(
                f"SELECT message_pk, channel_id, thread_id, user_id, role, content, timestamp "
                f"FROM MESSAGES{where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                tuple(params) + (per_page, offset)
            )
            columns = [desc[0] for desc in cursor.description]
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]

            # Enrich with channel/thread names
            for row in rows:
                if row.get('channel_id'):
                    try:
                        ch = self.bot.get_channel(int(row['channel_id']))
                        row['channel_name'] = ch.name if ch else None
                    except (ValueError, AttributeError):
                        row['channel_name'] = None
                # Convert timestamp to string if it's a datetime
                if row.get('timestamp') and not isinstance(row['timestamp'], str):
                    row['timestamp'] = str(row['timestamp'])

            return web.json_response({
                'messages': rows,
                'total': total,
                'page': page,
                'per_page': per_page,
                'total_pages': max(1, (total + per_page - 1) // per_page),
            })
        except Exception as e:
            logger.error(f"Error fetching messages: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_get_message_sources(self, request):
        """Get list of channels and threads that have stored messages."""
        state = self.bot.state_manager
        try:
            cursor = state.db_manager._get_cursor()

            # Channels with messages
            cursor.execute(
                "SELECT DISTINCT channel_id, COUNT(*) as msg_count "
                "FROM MESSAGES WHERE channel_id IS NOT NULL "
                "GROUP BY channel_id ORDER BY msg_count DESC"
            )
            channels = []
            for row in cursor.fetchall():
                ch_id = row[0]
                ch_name = None
                guild_name = None
                try:
                    ch = self.bot.get_channel(int(ch_id))
                    if ch:
                        ch_name = ch.name
                        if hasattr(ch, 'guild') and ch.guild:
                            guild_name = ch.guild.name
                except (ValueError, AttributeError):
                    pass
                channels.append({
                    'id': ch_id,
                    'name': ch_name,
                    'guild': guild_name,
                    'message_count': row[1],
                })

            # Threads with messages
            cursor.execute(
                "SELECT DISTINCT m.thread_id, COUNT(*) as msg_count, t.name "
                "FROM MESSAGES m LEFT JOIN THREADS t ON m.thread_id = t.thread_id "
                "WHERE m.thread_id IS NOT NULL "
                "GROUP BY m.thread_id ORDER BY msg_count DESC"
            )
            threads = []
            for row in cursor.fetchall():
                threads.append({
                    'id': row[0],
                    'name': row[2],
                    'message_count': row[1],
                })

            return web.json_response({
                'channels': channels,
                'threads': threads,
            })
        except Exception as e:
            logger.error(f"Error fetching message sources: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

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
            # Format as provider/model_id (matching slash command behavior)
            formatted = [f"{provider}/{mid}" for mid in model_ids]
            return web.json_response(formatted)
        except ValueError as e:
            return web.json_response({'error': str(e)}, status=400)
        except Exception as e:
            logger.error(f"Error fetching models for {provider}: {e}", exc_info=True)
            return web.json_response({'error': f'Failed to fetch models: {str(e)}'}, status=500)

    # ── Image Generation Handlers ─────────────────────────────────

    async def _handle_get_image_settings(self, request):
        """Get image generation settings (provider, configs, modalities)."""
        try:
            from ...utils.database import DatabaseManager
            db = DatabaseManager()

            # Get active provider
            active_provider = db.get_global_config('image_active_provider', 'ai_horde')

            # Get all provider configs
            providers = ['ai_horde', 'cloudflare', 'openai', 'comfyui', 'openrouter']
            configs = {}
            default_configs = {
                'ai_horde': {"model": "stable_diffusion_xl", "size": "1024x1024", "steps": 30},
                'cloudflare': {"size": "768x768", "steps": 25, "seed": None},
                'openai': {"model": "dall-e-3", "size": "1024x1024", "quality": "standard", "style": "vivid"},
                'comfyui': {"model": None, "size": "512x512", "steps": 20, "seed": None, "workflow": None},
                'openrouter': {"model": "google/gemini-2.0-flash-exp", "aspect_ratio": "1:1", "image_size": "1K"},
            }

            for provider in providers:
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
        except Exception as e:
            logger.error(f"Error fetching image settings: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_update_image_settings(self, request):
        """Update image generation settings."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        try:
            from ...utils.database import DatabaseManager
            db = DatabaseManager()
            updated = []

            # Update active provider
            if 'active_provider' in data:
                provider = data['active_provider']
                valid_providers = ['ai_horde', 'cloudflare', 'openai', 'comfyui', 'openrouter']
                if provider not in valid_providers:
                    return web.json_response({'error': f'Invalid provider: {provider}'}, status=400)
                db.set_global_config('image_active_provider', provider, 'string')
                updated.append('active_provider')

            # Update provider-specific config
            if 'config' in data and 'provider' in data:
                provider = data['provider']
                config = data['config']
                config_key = f'image_config_{provider}'

                # Merge with existing config
                existing_json = db.get_global_config(config_key)
                if existing_json:
                    try:
                        existing = json.loads(existing_json)
                    except json.JSONDecodeError:
                        existing = {}
                else:
                    existing = {}

                merged = {**existing, **config}
                db.set_global_config(config_key, json.dumps(merged), 'json')
                updated.append(f'config_{provider}')

            # Update OpenRouter modalities
            if 'openrouter_modalities' in data:
                modalities = data['openrouter_modalities']
                if not isinstance(modalities, list):
                    return web.json_response({'error': 'modalities must be an array'}, status=400)
                db.set_global_config('image_openrouter_modalities', json.dumps(modalities), 'json')
                updated.append('openrouter_modalities')

            await self._broadcast_ws({'type': 'image_settings_updated', 'fields': updated})
            return web.json_response({'status': 'ok', 'updated': updated})
        except Exception as e:
            logger.error(f"Error updating image settings: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_get_image_models(self, request):
        """Get the live list of OpenRouter image generation models."""
        try:
            image_cog = self.bot.get_cog("UnifiedImageCommands")
            client = getattr(image_cog, 'openrouter_image_client', None) if image_cog else None
            if not client:
                return web.json_response({'error': 'OpenRouter image generation is not configured'}, status=503)

            force = request.query.get('refresh') in ('1', 'true')
            result = await client.get_image_models(force_refresh=force)
            if not result.get('success'):
                return web.json_response({'error': result.get('error', 'Failed to fetch models')}, status=502)
            return web.json_response(result.get('models', []))
        except Exception as e:
            logger.error(f"Error fetching image models: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    # ── Video Generation Handlers ─────────────────────────────────

    async def _handle_get_video_settings(self, request):
        """Get video generation settings."""
        try:
            from ...utils.database import DatabaseManager
            from ...cogs.video_commands import (
                DB_KEY_ACTIVE_PROVIDER as VIDEO_PROVIDER_KEY,
                DB_KEY_CONFIG_PREFIX as VIDEO_CONFIG_PREFIX,
                DEFAULT_OPENROUTER_CONFIG as VIDEO_DEFAULT_CONFIG,
                DEFAULT_PROVIDER as VIDEO_DEFAULT_PROVIDER,
            )
            db = DatabaseManager()

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
        except Exception as e:
            logger.error(f"Error fetching video settings: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_update_video_settings(self, request):
        """Update video generation settings."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        try:
            from ...utils.database import DatabaseManager
            from ...cogs.video_commands import (
                DB_KEY_ACTIVE_PROVIDER as VIDEO_PROVIDER_KEY,
                DB_KEY_CONFIG_PREFIX as VIDEO_CONFIG_PREFIX,
            )
            db = DatabaseManager()
            updated = []

            if 'active_provider' in data:
                provider = data['active_provider']
                if provider != 'openrouter':
                    return web.json_response({'error': f'Unsupported video provider: {provider}'}, status=400)
                db.set_global_config(VIDEO_PROVIDER_KEY, provider, 'string')
                updated.append('active_provider')

            if 'config' in data:
                config = data['config'] or {}
                if not isinstance(config, dict):
                    return web.json_response({'error': 'config must be an object'}, status=400)
                provider = data.get('provider') or db.get_global_config(VIDEO_PROVIDER_KEY, 'openrouter')
                config_key = f'{VIDEO_CONFIG_PREFIX}{provider}'
                existing_json = db.get_global_config(config_key)
                if existing_json:
                    try:
                        existing = json.loads(existing_json)
                    except json.JSONDecodeError:
                        existing = {}
                else:
                    existing = {}
                merged = {**existing, **config}
                db.set_global_config(config_key, json.dumps(merged), 'json')
                updated.append(f'config_{provider}')

            await self._broadcast_ws({'type': 'video_settings_updated', 'fields': updated})
            return web.json_response({'status': 'ok', 'updated': updated})
        except Exception as e:
            logger.error(f"Error updating video settings: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_get_video_models(self, request):
        """Get available video generation models."""
        try:
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
        except Exception as e:
            logger.error(f"Error fetching video models: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_video_generate(self, request):
        """Submit a video generation job from the dashboard."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        prompt = (data.get('prompt') or '').strip()
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
        job_id = request.match_info.get('job_id')
        if not job_id:
            return web.json_response({'error': 'job_id required'}, status=400)

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
        results = {
            'timestamp': datetime.now().isoformat(),
            'bot_status': 'online' if self.bot.is_ready() else 'starting',
            'latency_ms': round(self.bot.latency * 1000, 1) if hasattr(self.bot, 'latency') else None,
            'guilds': len(self.bot.guilds) if hasattr(self.bot, 'guilds') else 0,
            'uptime_seconds': int(time.time() - self._start_time),
        }

        # Database check
        try:
            msg_count = state.get_message_count()
            thread_count = state.get_thread_count()
            db_path = state.db_manager.db_path
            results['database'] = {
                'status': 'ok' if msg_count >= 0 else 'error',
                'message_count': msg_count,
                'thread_count': thread_count,
                'file_size_bytes': os.path.getsize(db_path) if os.path.exists(db_path) else 0,
            }
        except Exception as e:
            results['database'] = {'status': 'error', 'error': str(e)}

        # Provider checks
        providers = {}
        if self.bot.openrouter_client:
            try:
                models = await self.bot.model_manager.get_models('openrouter')
                providers['openrouter'] = {'status': 'ok', 'model_count': len(models)}
            except Exception as e:
                providers['openrouter'] = {'status': 'error', 'error': str(e)}

        if self.bot.openai_client:
            try:
                models = await self.bot.model_manager.get_models('openai')
                providers['openai'] = {'status': 'ok', 'model_count': len(models)}
            except Exception as e:
                providers['openai'] = {'status': 'error', 'error': str(e)}

        if self.bot.ai_horde_client:
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
        state = self.bot.state_manager
        try:
            stats = state.prune_old_data()
            await self._broadcast_ws({'type': 'prune_completed', 'stats': stats})
            return web.json_response({'status': 'ok', 'stats': stats})
        except Exception as e:
            logger.error(f"Error during manual prune: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    # ── API Key Management Handlers ─────────────────────────────────

    def _get_api_key_service(self):
        """Get the API key service, or None if not configured."""
        return getattr(self.bot, 'api_key_service', None)

    async def _handle_get_keys(self, request):
        """List all API keys (masked, never exposes full keys)."""
        service = self._get_api_key_service()
        if not service:
            return web.json_response(
                {'error': 'API key management not configured. Set ENCRYPTION_MASTER_KEY in .env.'},
                status=501)
        provider_filter = request.query.get('provider')
        keys = service.list_keys(provider=provider_filter)
        return web.json_response(keys)

    async def _handle_add_key(self, request):
        """Add a new API key."""
        service = self._get_api_key_service()
        if not service:
            return web.json_response(
                {'error': 'API key management not configured'}, status=501)
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        provider = data.get('provider', '').strip()
        key_value = data.get('key', '').strip()
        alias = data.get('alias', '').strip() or None

        if not provider or not key_value:
            return web.json_response(
                {'error': 'provider and key are required'}, status=400)
        if provider not in service.PROVIDER_ENV_MAP:
            return web.json_response(
                {'error': f'Unknown provider: {provider}'}, status=400)

        try:
            result = service.add_key(provider, key_value, alias)
            await self._broadcast_ws({'type': 'api_key_added', 'provider': provider})
            return web.json_response(result, status=201)
        except Exception as e:
            logger.error(f"Error adding API key: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_update_key(self, request):
        """Update an existing API key."""
        service = self._get_api_key_service()
        if not service:
            return web.json_response(
                {'error': 'API key management not configured'}, status=501)

        key_id = request.match_info['key_id']
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        key_value = data.get('key', '').strip() or None
        alias = data.get('alias')
        is_active = data.get('is_active')

        try:
            updated = service.update_key(key_id, key_value, alias)
            if is_active is not None:
                service.db.set_api_key_active(key_id, bool(is_active))
                updated = True
            if updated:
                await self._broadcast_ws({'type': 'api_key_updated', 'key_id': key_id})
            return web.json_response({'updated': updated})
        except Exception as e:
            logger.error(f"Error updating API key: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_delete_key(self, request):
        """Delete an API key."""
        service = self._get_api_key_service()
        if not service:
            return web.json_response(
                {'error': 'API key management not configured'}, status=501)

        key_id = request.match_info['key_id']
        try:
            deleted = service.delete_key(key_id)
            if deleted:
                await self._broadcast_ws({'type': 'api_key_deleted', 'key_id': key_id})
            return web.json_response({'deleted': deleted})
        except Exception as e:
            logger.error(f"Error deleting API key: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_validate_key(self, request):
        """Validate a stored key by testing the provider API."""
        service = self._get_api_key_service()
        if not service:
            return web.json_response(
                {'error': 'API key management not configured'}, status=501)

        key_id = request.match_info['key_id']
        try:
            result = await service.validate_and_update_status(key_id)
            await self._broadcast_ws({'type': 'api_key_updated', 'key_id': key_id})
            return web.json_response(result)
        except Exception as e:
            logger.error(f"Error validating API key: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_import_env(self, request):
        """One-time bulk import of keys from .env into encrypted DB storage."""
        service = self._get_api_key_service()
        if not service:
            return web.json_response(
                {'error': 'API key management not configured'}, status=501)

        try:
            result = await service.import_from_env()
            if result['imported']:
                await self._broadcast_ws({'type': 'api_keys_imported',
                                          'imported': result['imported']})
            return web.json_response(result)
        except Exception as e:
            logger.error(f"Error importing keys from .env: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_get_audit_log(self, request):
        """Get the API key audit log."""
        service = self._get_api_key_service()
        if not service:
            return web.json_response(
                {'error': 'API key management not configured'}, status=501)

        key_id = request.query.get('key_id')
        limit = int(request.query.get('limit', '50'))
        entries = service.get_audit_log(key_id, limit)
        return web.json_response(entries)

    async def _handle_get_key_providers(self, request):
        """Get list of supported providers for API key management."""
        service = self._get_api_key_service()
        if not service:
            return web.json_response(
                {'error': 'API key management not configured'}, status=501)

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

    async def _handle_websocket(self, request):
        """Handle WebSocket connections for real-time updates."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        # Authenticate the WebSocket connection
        try:
            msg = await asyncio.wait_for(ws.receive(), timeout=10.0)
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                token = data.get('token', '')
                if not validate_session_token(token):
                    await ws.send_json({'type': 'error', 'message': 'Unauthorized'})
                    await ws.close()
                    return ws
            else:
                await ws.close()
                return ws
        except (asyncio.TimeoutError, json.JSONDecodeError):
            await ws.close()
            return ws

        # Authenticated - add to clients
        self._ws_clients.append(ws)
        await ws.send_json({'type': 'connected', 'message': 'WebSocket connected'})
        logger.info(f"WebSocket client connected (total: {len(self._ws_clients)})")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get('type') == 'ping':
                            await ws.send_json({'type': 'pong'})
                    except json.JSONDecodeError:
                        pass
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            if ws in self._ws_clients:
                self._ws_clients.remove(ws)
            logger.info(f"WebSocket client disconnected (total: {len(self._ws_clients)})")

        return ws

    # ── Memory Handlers ──────────────────────────────────────────

    async def _handle_get_memory_stats(self, request):
        """Get per-channel memory summary statistics."""
        state = self.bot.state_manager
        stats = state.get_all_memory_stats()
        # Enrich with channel names where available
        for entry in stats:
            try:
                ch = self.bot.get_channel(int(entry['channel_id']))
                entry['channel_name'] = ch.name if ch else None
            except (ValueError, AttributeError):
                entry['channel_name'] = None
        return web.json_response(stats)

    async def _handle_get_channel_memories(self, request):
        """Get stored memory summaries for a channel."""
        channel_id = request.match_info['channel_id']
        state = self.bot.state_manager
        limit = int(request.rel_url.query.get('limit', 20))
        memories = state.get_channel_memories(channel_id, limit=limit)
        channel_name = None
        try:
            ch = self.bot.get_channel(int(channel_id))
            if ch:
                channel_name = ch.name
        except (ValueError, AttributeError):
            pass
        return web.json_response({
            'channel_id': channel_id,
            'channel_name': channel_name,
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
            return web.json_response({'error': 'No LLM client available for summarization.'}, status=500)

        from ..memory_service import _summarize_history, _parse_timestamp
        conversation_start = _parse_timestamp(history[0].get('timestamp')) if history else None
        summary = await _summarize_history(history, channel_id, state, clients)
        if not summary:
            return web.json_response({'error': 'Summarization failed or returned empty result.'}, status=500)

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
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        required = ['template_id', 'name', 'display_name']
        for field in required:
            if field not in data or not data[field]:
                return web.json_response({'error': f'{field} is required'}, status=400)

        state = self.bot.state_manager
        try:
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
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

        await self._broadcast_ws({'type': 'persona_template_created', 'template_id': data['template_id']})
        return web.json_response({'status': 'ok', 'template_id': data['template_id']})

    async def _handle_update_persona_template(self, request):
        """Update a persona template."""
        template_id = request.match_info['template_id']
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        state = self.bot.state_manager
        template = state.get_persona_template(template_id)
        if not template:
            return web.json_response({'error': 'Template not found'}, status=404)

        try:
            state.db_manager.update_persona_template(template_id, **data)
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

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
        personas = state.get_all_channel_personas()

        # Enrich with channel names
        for p in personas:
            try:
                ch = self.bot.get_channel(int(p['channel_id']))
                p['channel_name'] = ch.name if ch else None
            except (ValueError, AttributeError):
                p['channel_name'] = None

        return web.json_response(personas)

    async def _handle_get_channel_persona(self, request):
        """Get persona for a specific channel."""
        channel_id = request.match_info['channel_id']
        state = self.bot.state_manager
        persona = state.get_channel_persona(channel_id)

        if not persona:
            return web.json_response({'error': 'No persona configured'}, status=404)

        try:
            ch = self.bot.get_channel(int(channel_id))
            persona['channel_name'] = ch.name if ch else None
        except (ValueError, AttributeError):
            persona['channel_name'] = None

        return web.json_response(persona)

    async def _handle_set_channel_persona(self, request):
        """Set or update a channel's persona."""
        channel_id = request.match_info['channel_id']
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        if 'display_name' not in data or not data['display_name']:
            return web.json_response({'error': 'display_name is required'}, status=400)

        state = self.bot.state_manager
        try:
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
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

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
        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON'}, status=400)

        template_id = data.get('template_id')
        if not template_id:
            return web.json_response({'error': 'template_id is required'}, status=400)

        state = self.bot.state_manager
        template = state.get_persona_template(template_id)
        if not template:
            return web.json_response({'error': 'Template not found'}, status=404)

        try:
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
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

        if hasattr(self.bot, 'webhook_sender'):
            self.bot.webhook_sender.invalidate_cache(channel_id)

        await self._broadcast_ws({'type': 'persona_template_applied', 'channel_id': channel_id, 'template_id': template_id})
        return web.json_response({'status': 'ok'})

    # ── Backup & Restore Handlers ─────────────────────────────────

    async def _handle_export_config(self, request):
        """Export bot configuration as downloadable JSON."""
        try:
            state = self.bot.state_manager
            config_data = state.db_manager.export_config_json()

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'gideon_config_{timestamp}.json'

            return web.json_response(
                config_data,
                headers={
                    'Content-Disposition': f'attachment; filename="{filename}"'
                }
            )
        except Exception as e:
            logger.error(f"Error exporting config: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_export_database(self, request):
        """Export full SQLite database as downloadable file."""
        import tempfile
        try:
            state = self.bot.state_manager

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_filename = f'gideon_backup_{timestamp}.db'
            backup_path = os.path.join(tempfile.gettempdir(), backup_filename)

            state.db_manager.create_sqlite_backup(backup_path)

            return web.FileResponse(
                backup_path,
                headers={
                    'Content-Disposition': f'attachment; filename="{backup_filename}"',
                    'Content-Type': 'application/x-sqlite3',
                }
            )
        except Exception as e:
            logger.error(f"Error exporting database: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_validate_config_import(self, request):
        """Validate an uploaded JSON config file before importing."""
        try:
            reader = await request.multipart()
            field = await reader.next()

            if not field or field.name != 'file':
                return web.json_response({'error': 'No file uploaded'}, status=400)

            content = await field.read(decode=False)
            if len(content) > 10 * 1024 * 1024:
                return web.json_response({'error': 'File too large (max 10 MB)'}, status=400)

            try:
                data = json.loads(content.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return web.json_response({'error': 'Invalid JSON file'}, status=400)

            state = self.bot.state_manager
            validation = state.db_manager.validate_config_json(data)

            return web.json_response(validation)
        except Exception as e:
            logger.error(f"Error validating config import: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_import_config(self, request):
        """Import configuration from uploaded JSON file."""
        try:
            reader = await request.multipart()
            field = await reader.next()

            if not field or field.name != 'file':
                return web.json_response({'error': 'No file uploaded'}, status=400)

            content = await field.read(decode=False)
            if len(content) > 10 * 1024 * 1024:
                return web.json_response({'error': 'File too large (max 10 MB)'}, status=400)

            try:
                data = json.loads(content.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                return web.json_response({'error': 'Invalid JSON file'}, status=400)

            state = self.bot.state_manager

            # Validate first
            validation = state.db_manager.validate_config_json(data)
            if not validation.get('valid'):
                return web.json_response(
                    {'error': 'Validation failed', 'details': validation.get('errors', [])},
                    status=400
                )

            # Apply import
            result = state.db_manager.import_config_json(data)

            # Reload in-memory state from DB
            await state.reinitialize_state()

            await self._broadcast_ws({'type': 'config_imported', 'result': result})
            logger.info(f"Config imported successfully: {result.get('applied', {})}")
            return web.json_response({'status': 'ok', 'result': result})
        except Exception as e:
            logger.error(f"Error importing config: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_validate_db_restore(self, request):
        """Validate an uploaded SQLite database file before restoring."""
        import tempfile
        try:
            reader = await request.multipart()
            field = await reader.next()

            if not field or field.name != 'file':
                return web.json_response({'error': 'No file uploaded'}, status=400)

            content = await field.read(decode=False)
            if len(content) > 100 * 1024 * 1024:
                return web.json_response({'error': 'File too large (max 100 MB)'}, status=400)

            # Save to temp file for validation
            temp_path = os.path.join(tempfile.gettempdir(), 'gideon_validate_temp.db')
            with open(temp_path, 'wb') as f:
                f.write(content)

            state = self.bot.state_manager
            validation = state.db_manager.validate_sqlite_backup(temp_path)

            # Clean up temp file
            try:
                os.unlink(temp_path)
            except OSError:
                pass

            return web.json_response(validation)
        except Exception as e:
            logger.error(f"Error validating database restore: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _handle_restore_database(self, request):
        """Restore database from uploaded SQLite backup file."""
        import tempfile
        try:
            reader = await request.multipart()
            field = await reader.next()

            if not field or field.name != 'file':
                return web.json_response({'error': 'No file uploaded'}, status=400)

            content = await field.read(decode=False)
            if len(content) > 100 * 1024 * 1024:
                return web.json_response({'error': 'File too large (max 100 MB)'}, status=400)

            # Save to temp file
            temp_path = os.path.join(tempfile.gettempdir(), 'gideon_restore_temp.db')
            with open(temp_path, 'wb') as f:
                f.write(content)

            state = self.bot.state_manager

            # Validate first
            validation = state.db_manager.validate_sqlite_backup(temp_path)
            if not validation.get('valid'):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
                return web.json_response(
                    {'error': 'Validation failed', 'details': validation.get('errors', [])},
                    status=400
                )

            # Perform restore
            result = state.db_manager.restore_sqlite_backup(temp_path)

            # Clean up temp file
            try:
                os.unlink(temp_path)
            except OSError:
                pass

            if not result.get('success'):
                return web.json_response(
                    {'error': f"Restore failed: {result.get('error', 'Unknown error')}"},
                    status=500
                )

            # Reinitialize state from the restored database
            await state.reinitialize_state()

            await self._broadcast_ws({'type': 'database_restored'})
            logger.info(f"Database restored successfully. Pre-restore backup at: {result.get('bak_path')}")
            return web.json_response({
                'status': 'ok',
                'bak_path': result.get('bak_path'),
                'message': 'Database restored successfully. A backup of the previous database was saved.'
            })
        except Exception as e:
            logger.error(f"Error restoring database: {e}", exc_info=True)
            return web.json_response({'error': str(e)}, status=500)

    async def _broadcast_ws(self, data: dict):
        """Broadcast a message to all connected WebSocket clients."""
        if not self._ws_clients:
            return

        data['timestamp'] = datetime.now().isoformat()
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)

        for ws in dead:
            self._ws_clients.remove(ws)

    async def broadcast_event(self, event_type: str, data: dict = None):
        """Public method for cogs to broadcast events to dashboard clients."""
        payload = {'type': event_type}
        if data:
            payload.update(data)
        await self._broadcast_ws(payload)
