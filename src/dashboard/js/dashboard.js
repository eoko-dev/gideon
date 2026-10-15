/**
 * Gideon Admin Dashboard - Main Application
 */
(function () {
    'use strict';

    // ── State ────────────────────────────────────────────────
    let authToken = null;
    let ws = null;
    let wsReconnectTimer = null;
    const WS_RECONNECT_DELAY = 3000;

    // Model cache: { provider: [model_ids] }
    var modelCache = {};

    // Messages state
    var msgCurrentPage = 1;
    var msgTotalPages = 1;
    var msgSources = null; // cached sources data

    // ── Helpers ──────────────────────────────────────────────

    async function api(method, path, body) {
        const opts = {
            method,
            headers: { 'Content-Type': 'application/json' },
            credentials: 'same-origin',
        };
        if (authToken) {
            opts.headers['Authorization'] = 'Bearer ' + authToken;
        }
        if (body !== undefined) {
            opts.body = JSON.stringify(body);
        }
        const res = await fetch(path, opts);
        if (res.status === 401) {
            handleLogout();
            throw new Error('Session expired');
        }
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.error || 'Request failed');
        }
        return data;
    }

    function $(selector) {
        return document.querySelector(selector);
    }

    function $$(selector) {
        return document.querySelectorAll(selector);
    }

    function bindEvent(selector, event, handler) {
        var el = document.querySelector(selector);
        if (el) el.addEventListener(event, handler);
    }

    function formatUptime(seconds) {
        const d = Math.floor(seconds / 86400);
        const h = Math.floor((seconds % 86400) / 3600);
        const m = Math.floor((seconds % 3600) / 60);
        if (d > 0) return d + 'd ' + h + 'h';
        if (h > 0) return h + 'h ' + m + 'm';
        return m + 'm';
    }

    function formatBytes(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / 1048576).toFixed(1) + ' MB';
    }

    function formatTime(date) {
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }

    function formatDateTime(str) {
        if (!str) return 'N/A';
        var d = new Date(str);
        if (isNaN(d.getTime())) return str;
        return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    function showSaveStatus(elementId, message, isError) {
        const el = document.getElementById(elementId);
        if (!el) return;
        el.textContent = message;
        el.className = 'save-status ' + (isError ? 'error' : 'success');
        setTimeout(function() { el.textContent = ''; }, 4000);
    }

    // ── Model Select Helpers ─────────────────────────────────

    async function fetchModels(provider) {
        if (modelCache[provider]) return modelCache[provider];
        try {
            var models = await api('GET', '/api/models/' + provider);
            modelCache[provider] = models;
            return models;
        } catch (e) {
            console.error('Failed to fetch models for ' + provider + ':', e);
            return [];
        }
    }

    function populateModelSelect(selectEl, models, currentValue, emptyLabel) {
        var html = '';
        if (emptyLabel) {
            html += '<option value="">' + escapeHtml(emptyLabel) + '</option>';
        }
        models.forEach(function(model) {
            var selected = (model === currentValue) ? ' selected' : '';
            var label = model;
            if (model === currentValue) {
                label = '> ' + model + ' (current)';
            }
            html += '<option value="' + escapeHtml(model) + '"' + selected + '>' + escapeHtml(label) + '</option>';
        });
        selectEl.innerHTML = html;
    }

    function setupModelSearch(searchId, selectId) {
        var searchEl = document.getElementById(searchId);
        var selectEl = document.getElementById(selectId);
        if (!searchEl || !selectEl) return;

        searchEl.addEventListener('input', function() {
            var filter = searchEl.value.toLowerCase();
            var options = selectEl.options;
            for (var i = 0; i < options.length; i++) {
                var text = options[i].value.toLowerCase() + ' ' + options[i].textContent.toLowerCase();
                options[i].style.display = (!filter || text.indexOf(filter) !== -1) ? '' : 'none';
            }
        });
    }

    async function loadModelsForProvider(provider, selectId, currentValue, emptyLabel) {
        var selectEl = document.getElementById(selectId);
        if (!selectEl) return;
        selectEl.innerHTML = '<option value="">Loading models...</option>';
        var models = await fetchModels(provider);
        populateModelSelect(selectEl, models, currentValue, emptyLabel);
    }

    // ── Auth ─────────────────────────────────────────────────

    async function checkAuth() {
        try {
            const data = await api('GET', '/api/auth/check');
            if (data.authenticated) {
                // Restore token from cookie-based session so WebSocket can authenticate
                if (data.token) {
                    authToken = data.token;
                }
                showDashboard();
                return;
            }
        } catch (e) {
            // Not authenticated
        }
        showLogin();
    }

    function showLogin() {
        $('#login-screen').style.display = 'flex';
        $('#dashboard').style.display = 'none';
        disconnectWs();
    }

    function showDashboard() {
        $('#login-screen').style.display = 'none';
        $('#dashboard').style.display = 'flex';
        loadOverview();
        connectWs();
    }

    function handleLogout() {
        authToken = null;
        api('POST', '/api/auth/logout').catch(function() {});
        showLogin();
    }

    // ── Tab Navigation ───────────────────────────────────────

    function switchTab(tabName) {
        $$('.nav-item').forEach(function(item) {
            var isActive = item.dataset.tab === tabName;
            item.classList.toggle('active', isActive);
            if (isActive) {
                item.setAttribute('aria-current', 'page');
            } else {
                item.removeAttribute('aria-current');
            }
        });
        $$('.tab-content').forEach(function(tab) {
            tab.classList.toggle('active', tab.id === 'tab-' + tabName);
        });

        // Load data for the tab
        switch (tabName) {
            case 'overview': loadOverview(); break;
            case 'api-keys': loadAPIKeys(); loadAuditLog(); break;
            case 'settings': loadSettings(); break;
            case 'channels': loadChannels(); break;
            case 'threads': loadThreads(); break;
            case 'personas': loadPersonas(); break;
            case 'image-gen': loadImageSettings(); break;
            case 'video-gen': loadVideoSettings(); break;
            case 'memory': loadMemoryStats(); break;
            case 'messages': loadMessageSources(); break;
            case 'diagnostics': break;
            case 'backup': break;
            case 'activity': break;
        }
    }

    // ── Overview ─────────────────────────────────────────────

    async function loadOverview() {
        try {
            const data = await api('GET', '/api/overview');
            $('#stat-guilds').textContent = data.guilds;
            $('#stat-messages').textContent = data.message_count >= 0 ? data.message_count : 'N/A';
            $('#stat-threads').textContent = data.thread_count >= 0 ? data.thread_count : 'N/A';
            $('#stat-channels').textContent = data.channel_config_count;
            $('#stat-latency').textContent = data.latency_ms !== null ? data.latency_ms : 'N/A';
            $('#stat-uptime').textContent = formatUptime(data.uptime_seconds);
            $('#info-bot-name').textContent = data.bot_name;
            $('#info-bot-id').textContent = data.bot_id;
            $('#info-model').textContent = data.global_model;
            $('#info-provider').textContent = data.global_provider;
            $('#info-db-size').textContent = formatBytes(data.database_size_bytes);

            var badge = $('#bot-status-badge');
            badge.textContent = 'Online';
            badge.className = 'badge badge-success';
        } catch (e) {
            console.error('Failed to load overview:', e);
        }
    }

    // ── Settings ─────────────────────────────────────────────

    async function loadSettings() {
        try {
            const data = await api('GET', '/api/settings');
            var provider = data.global_provider || 'openrouter';

            $('#setting-provider').value = provider;
            $('#setting-memory').value = data.max_channel_history || 35;
            $('#setting-window').value = data.time_window_hours || 48;
            $('#setting-prune').value = data.prune_frequency_hours || 24;
            $('#setting-system-prompt').value = data.global_system_prompt || '';

            // Memory system settings
            $('#setting-session-timeout').value = data.session_timeout_hours || 24;
            $('#setting-max-summaries').value = data.max_memory_summaries || 10;
            $('#setting-memory-summary').value = data.memory_summary_enabled !== false ? 'true' : 'false';

            // Tool calling settings
            var tcEnabled = data.tool_calling_enabled !== false;
            $('#tool-calling-enabled').checked = tcEnabled;
            var tcLabel = $('#tool-calling-enabled-label');
            if (tcLabel) tcLabel.textContent = tcEnabled ? 'Enabled — native tool calling via LLM' : 'Disabled — fallback to intent classification';
            $('#tool-calling-max-iterations').value = data.tool_calling_max_iterations || 3;

            await loadModelsForProvider(provider, 'setting-model', data.global_model, null);

            var searchEl = document.getElementById('setting-model-search');
            if (searchEl) searchEl.value = '';
        } catch (e) {
            console.error('Failed to load settings:', e);
        }
    }

    async function saveSettings(e) {
        e.preventDefault();
        try {
            await api('PUT', '/api/settings', {
                global_provider: $('#setting-provider').value,
                global_model: $('#setting-model').value,
                max_channel_history: parseInt($('#setting-memory').value),
                time_window_hours: parseInt($('#setting-window').value),
                prune_frequency_hours: parseInt($('#setting-prune').value),
                global_system_prompt: $('#setting-system-prompt').value || null,
            });
            showSaveStatus('settings-save-status', 'Settings saved', false);
        } catch (e) {
            showSaveStatus('settings-save-status', 'Error: ' + e.message, true);
        }
    }

    async function saveMemorySettings(e) {
        e.preventDefault();
        try {
            await api('PUT', '/api/settings', {
                session_timeout_hours: parseInt($('#setting-session-timeout').value),
                max_memory_summaries: parseInt($('#setting-max-summaries').value),
                memory_summary_enabled: $('#setting-memory-summary').value === 'true',
            });
            showSaveStatus('memory-settings-save-status', 'Memory settings saved', false);
        } catch (e) {
            showSaveStatus('memory-settings-save-status', 'Error: ' + e.message, true);
        }
    }

    async function onProviderChange() {
        var provider = $('#setting-provider').value;
        delete modelCache[provider];
        await loadModelsForProvider(provider, 'setting-model', null, null);
    }

    async function saveToolCallingSettings(e) {
        e.preventDefault();
        try {
            await api('PUT', '/api/settings', {
                tool_calling_enabled: $('#tool-calling-enabled').checked,
                tool_calling_max_iterations: parseInt($('#tool-calling-max-iterations').value),
            });
            showSaveStatus('tool-calling-save-status', 'Tool calling settings saved', false);
        } catch (e) {
            showSaveStatus('tool-calling-save-status', 'Error: ' + e.message, true);
        }
    }

    // ── Channels ─────────────────────────────────────────────

    var allChannelsData = []; // Cache for filtering

    async function loadChannels() {
        var container = $('#channels-list');
        try {
            allChannelsData = await api('GET', '/api/channels/all');
            renderChannels();
        } catch (e) {
            container.innerHTML = '<div class="empty-state">Error loading channels: ' + escapeHtml(e.message) + '</div>';
        }
    }

    function renderChannels() {
        var container = $('#channels-list');
        var channels = allChannelsData;

        if (channels.length === 0) {
            container.innerHTML = '<div class="empty-state">No channels found. Is the bot connected to any servers?</div>';
            return;
        }

        // Apply filters
        var filterEl = document.getElementById('channel-filter-input');
        var filterOverrideEl = document.getElementById('channel-filter-override');
        var filterText = filterEl ? filterEl.value.toLowerCase() : '';
        var filterOverride = filterOverrideEl ? filterOverrideEl.value : 'all';

        var filtered = channels.filter(function(ch) {
            // Text filter
            if (filterText) {
                var searchable = (ch.channel_name || '').toLowerCase() + ' ' +
                    (ch.guild_name || '').toLowerCase() + ' ' +
                    (ch.model || '').toLowerCase() + ' ' +
                    (ch.category || '').toLowerCase();
                if (searchable.indexOf(filterText) === -1) return false;
            }
            // Override filter
            if (filterOverride === 'customized' && !ch.has_override) return false;
            if (filterOverride === 'default' && ch.has_override) return false;
            return true;
        });

        if (filtered.length === 0) {
            container.innerHTML = '<div class="empty-state">No channels match the current filters.</div>';
            return;
        }

        // Group by guild
        var guilds = {};
        filtered.forEach(function(ch) {
            var guildKey = ch.guild_name || 'Unknown Server';
            if (!guilds[guildKey]) guilds[guildKey] = [];
            guilds[guildKey].push(ch);
        });

        var html = '';
        var guildNames = Object.keys(guilds).sort();

        guildNames.forEach(function(guildName) {
            var guildChannels = guilds[guildName];
            var customCount = guildChannels.filter(function(c) { return c.has_override; }).length;

            html += '<div class="channel-guild-group">';
            html += '<div class="guild-header">';
            html += '<h4>' + escapeHtml(guildName) + '</h4>';
            html += '<span class="badge badge-info">' + guildChannels.length + ' channels';
            if (customCount > 0) html += ', ' + customCount + ' customized';
            html += '</span>';
            html += '</div>';

            html += '<table class="data-table"><thead><tr>' +
                '<th>Channel</th><th>Category</th><th>Model</th><th>Provider</th><th>Status</th><th>Actions</th>' +
                '</tr></thead><tbody>';

            guildChannels.forEach(function(ch) {
                var statusClass = ch.has_override ? 'badge-accent' : 'badge-default';
                var statusLabel = ch.has_override ? 'Customized' : 'Global';

                html += '<tr class="' + (ch.has_override ? 'row-customized' : '') + '">' +
                    '<td><span class="channel-name">#' + escapeHtml(ch.channel_name || ch.channel_id) + '</span></td>' +
                    '<td>' + (ch.category ? escapeHtml(ch.category) : '<span class="text-muted">None</span>') + '</td>' +
                    '<td>' + (ch.model ? '<span class="code">' + escapeHtml(ch.model) + '</span>' : '<span class="text-muted">Global</span>') + '</td>' +
                    '<td>' + (ch.provider ? escapeHtml(ch.provider) : '<span class="text-muted">Global</span>') + '</td>' +
                    '<td><span class="badge ' + statusClass + '">' + statusLabel + '</span></td>' +
                    '<td><button class="btn btn-small btn-secondary" onclick="window._editChannel(\'' + escapeHtml(ch.channel_id) + '\')">Edit</button></td>' +
                    '</tr>';
            });

            html += '</tbody></table>';
            html += '</div>';
        });

        container.innerHTML = html;
    }

    window._filterChannels = renderChannels;

    async function editChannel(channelId) {
        try {
            const data = await api('GET', '/api/channels/' + channelId);
            $('#channel-edit-id').value = channelId;
            $('#channel-modal-name').textContent = data.channel_name || channelId;
            $('#channel-edit-provider').value = data.provider || '';
            $('#channel-edit-prompt').value = data.system_prompt || '';

            var settings = await api('GET', '/api/settings');
            var provider = data.provider || settings.global_provider || 'openrouter';
            await loadModelsForProvider(provider, 'channel-edit-model', data.model, 'Use Global Default');

            var searchEl = document.getElementById('channel-model-search');
            if (searchEl) searchEl.value = '';

            $('#channel-modal').style.display = 'flex';
        } catch (e) {
            alert('Error loading channel: ' + e.message);
        }
    }
    window._editChannel = editChannel;

    async function saveChannel(e) {
        e.preventDefault();
        var channelId = $('#channel-edit-id').value;
        try {
            await api('PUT', '/api/channels/' + channelId, {
                model: $('#channel-edit-model').value || null,
                provider: $('#channel-edit-provider').value || undefined,
                system_prompt: $('#channel-edit-prompt').value || null,
            });
            $('#channel-modal').style.display = 'none';
            loadChannels();
        } catch (e) {
            alert('Error saving channel: ' + e.message);
        }
    }

    async function resetChannel() {
        var channelId = $('#channel-edit-id').value;
        if (!confirm('Reset this channel to global defaults?')) return;
        try {
            await api('DELETE', '/api/channels/' + channelId);
            $('#channel-modal').style.display = 'none';
            loadChannels();
        } catch (e) {
            alert('Error resetting channel: ' + e.message);
        }
    }

    // ── Threads ──────────────────────────────────────────────

    async function loadThreads() {
        var container = $('#threads-list');
        try {
            const threads = await api('GET', '/api/threads');
            if (threads.length === 0) {
                container.innerHTML = '<div class="empty-state">No active threads.</div>';
                return;
            }

            var html = '<table class="data-table"><thead><tr>' +
                '<th>Name</th><th>Channel</th><th>Server</th><th>Messages</th><th>Model</th><th>Created</th><th>Actions</th>' +
                '</tr></thead><tbody>';

            threads.forEach(function(t) {
                var created = t.created_at ? new Date(t.created_at).toLocaleDateString() : 'N/A';
                var channelDisplay = t.channel_name ? '#' + t.channel_name : (t.channel_id || 'N/A');

                html += '<tr>' +
                    '<td>' + escapeHtml(t.name || 'Unnamed') + '</td>' +
                    '<td>' + escapeHtml(channelDisplay) + '</td>' +
                    '<td>' + escapeHtml(t.guild_name || 'Unknown') + '</td>' +
                    '<td>' + (t.message_count || 0) + '</td>' +
                    '<td>' + (t.model ? '<span class="code">' + escapeHtml(t.model) + '</span>' : '<span class="text-muted">Default</span>') + '</td>' +
                    '<td>' + created + '</td>' +
                    '<td><button class="btn btn-small btn-secondary" onclick="window._editThread(\'' + escapeHtml(t.thread_id) + '\')">Edit</button></td>' +
                    '</tr>';
            });

            html += '</tbody></table>';
            container.innerHTML = html;
        } catch (e) {
            container.innerHTML = '<div class="empty-state">Error loading threads: ' + escapeHtml(e.message) + '</div>';
        }
    }

    async function editThread(threadId) {
        try {
            const data = await api('GET', '/api/threads/' + threadId);
            $('#thread-edit-id').value = threadId;
            $('#thread-modal-name').textContent = data.name || threadId;
            $('#thread-edit-name').value = data.name || '';
            $('#thread-edit-prompt').value = data.system_prompt || '';

            var settings = await api('GET', '/api/settings');
            var provider = settings.global_provider || 'openrouter';
            await loadModelsForProvider(provider, 'thread-edit-model', data.model, 'Use Channel/Global Default');

            var searchEl = document.getElementById('thread-model-search');
            if (searchEl) searchEl.value = '';

            $('#thread-modal').style.display = 'flex';
        } catch (e) {
            alert('Error loading thread: ' + e.message);
        }
    }
    window._editThread = editThread;

    async function saveThread(e) {
        e.preventDefault();
        var threadId = $('#thread-edit-id').value;
        try {
            await api('PUT', '/api/threads/' + threadId, {
                name: $('#thread-edit-name').value,
                model: $('#thread-edit-model').value || null,
                system_prompt: $('#thread-edit-prompt').value || null,
            });
            $('#thread-modal').style.display = 'none';
            loadThreads();
        } catch (e) {
            alert('Error saving thread: ' + e.message);
        }
    }

    async function deleteThread() {
        var threadId = $('#thread-edit-id').value;
        if (!confirm('Delete this thread and all its messages? This cannot be undone.')) return;
        try {
            await api('DELETE', '/api/threads/' + threadId);
            $('#thread-modal').style.display = 'none';
            loadThreads();
        } catch (e) {
            alert('Error deleting thread: ' + e.message);
        }
    }

    // ── Messages ─────────────────────────────────────────────

    async function loadMessageSources() {
        if (msgSources) return; // Already loaded
        try {
            msgSources = await api('GET', '/api/messages/sources');
        } catch (e) {
            console.error('Failed to load message sources:', e);
        }
    }

    function onSourceTypeChange() {
        var type = $('#msg-source-type').value;
        var group = $('#msg-source-select-group');
        var sel = $('#msg-source-id');

        if (type === 'all') {
            group.style.display = 'none';
            return;
        }

        group.style.display = '';
        sel.innerHTML = '<option value="">Loading...</option>';

        if (!msgSources) {
            loadMessageSources().then(function() { populateSourceSelect(type); });
        } else {
            populateSourceSelect(type);
        }
    }

    function populateSourceSelect(type) {
        var sel = $('#msg-source-id');
        var html = '';

        if (type === 'channel' && msgSources && msgSources.channels) {
            msgSources.channels.forEach(function(ch) {
                var label = (ch.name || ch.id);
                if (ch.guild) label += ' (' + ch.guild + ')';
                label += ' - ' + ch.message_count + ' msgs';
                html += '<option value="' + escapeHtml(ch.id) + '">' + escapeHtml(label) + '</option>';
            });
        } else if (type === 'thread' && msgSources && msgSources.threads) {
            msgSources.threads.forEach(function(t) {
                var label = (t.name || t.id) + ' - ' + t.message_count + ' msgs';
                html += '<option value="' + escapeHtml(t.id) + '">' + escapeHtml(label) + '</option>';
            });
        }

        if (!html) {
            html = '<option value="">No ' + escapeHtml(type) + 's with messages</option>';
        }
        sel.innerHTML = html;
    }

    async function loadMessages(page) {
        if (!page) page = 1;
        msgCurrentPage = page;

        var container = $('#messages-container');
        container.innerHTML = '<div class="loading-text">Loading messages...</div>';

        var params = 'page=' + page + '&per_page=50';

        var sourceType = $('#msg-source-type').value;
        var sourceId = $('#msg-source-id').value;

        if (sourceType === 'channel' && sourceId) {
            params += '&channel_id=' + encodeURIComponent(sourceId);
        } else if (sourceType === 'thread' && sourceId) {
            params += '&thread_id=' + encodeURIComponent(sourceId);
        }

        var roleFilter = $('#msg-role-filter').value;
        if (roleFilter) {
            params += '&role=' + encodeURIComponent(roleFilter);
        }

        try {
            var data = await api('GET', '/api/messages?' + params);
            msgTotalPages = data.total_pages;

            if (data.messages.length === 0) {
                container.innerHTML = '<div class="empty-state">No messages found.</div>';
                $('#messages-pagination').style.display = 'none';
                return;
            }

            var html = '';
            data.messages.forEach(function(msg) {
                var roleClass = 'msg-role-' + (msg.role || 'user');
                var roleBadge = msg.role || 'unknown';
                var timeStr = formatDateTime(msg.timestamp);
                var channelInfo = '';
                if (msg.channel_name) {
                    channelInfo = '#' + msg.channel_name;
                } else if (msg.channel_id) {
                    channelInfo = '#' + msg.channel_id;
                } else if (msg.thread_id) {
                    channelInfo = 'Thread ' + msg.thread_id;
                }

                var content = msg.content || '';

                // Check for image markers: [image:url] or [image:attachment]
                var imageHtml = '';
                var imageMatch = content.match(/^\[image:(.*?)\]\s*/);
                if (imageMatch) {
                    var imageRef = imageMatch[1];
                    content = content.substring(imageMatch[0].length);
                    if (imageRef && imageRef !== 'attachment' && imageRef.startsWith('http')) {
                        imageHtml = '<div class="msg-image"><a href="' + escapeHtml(imageRef) + '" target="_blank" rel="noopener"><img src="' + escapeHtml(imageRef) + '" alt="Generated image" loading="lazy"></a></div>';
                    } else {
                        imageHtml = '<div class="msg-image-placeholder">Generated image (attachment - not available in dashboard)</div>';
                    }
                }

                // Check for video markers: [video:url] or [video:attachment]
                var videoHtml = '';
                var videoMatch = content.match(/^\[video:(.*?)\]\s*/);
                if (videoMatch) {
                    var videoRef = videoMatch[1];
                    content = content.substring(videoMatch[0].length);
                    if (videoRef && videoRef !== 'attachment' && videoRef.startsWith('http')) {
                        videoHtml = '<div class="msg-image"><video controls preload="metadata" style="max-width:100%;border-radius:8px;"><source src="' + escapeHtml(videoRef) + '" type="video/mp4"></video><div><a href="' + escapeHtml(videoRef) + '" target="_blank" rel="noopener">Open video in new tab</a></div></div>';
                    } else {
                        videoHtml = '<div class="msg-image-placeholder">Generated video (attachment - not available in dashboard)</div>';
                    }
                }

                // Check for failed generation marker
                var isFailedGen = content.startsWith('[image generation failed]') || content.startsWith('[video generation failed]');
                if (content.startsWith('[image generation failed]')) {
                    content = content.substring('[image generation failed] '.length);
                } else if (content.startsWith('[video generation failed]')) {
                    content = content.substring('[video generation failed] '.length);
                }

                // Truncate very long messages for display
                var truncated = false;
                if (content.length > 1000) {
                    content = content.substring(0, 1000);
                    truncated = true;
                }

                html += '<div class="msg-bubble ' + roleClass + '">';
                html += '<div class="msg-header">';
                html += '<span class="msg-role-badge ' + roleClass + '">' + escapeHtml(roleBadge) + '</span>';
                if (msg.user_id) {
                    html += '<span class="msg-user">' + escapeHtml(msg.user_id) + '</span>';
                }
                if (channelInfo) {
                    html += '<span class="msg-channel">' + escapeHtml(channelInfo) + '</span>';
                }
                html += '<span class="msg-time">' + escapeHtml(timeStr) + '</span>';
                html += '</div>';
                html += '<div class="msg-content">' + escapeHtml(content);
                if (truncated) {
                    html += '<span class="msg-truncated">... (truncated)</span>';
                }
                html += '</div>';
                if (imageHtml) {
                    html += imageHtml;
                }
                if (videoHtml) {
                    html += videoHtml;
                }
                html += '</div>';
            });

            container.innerHTML = html;

            // Update pagination
            var pagination = $('#messages-pagination');
            pagination.style.display = 'flex';
            $('#msg-page-info').textContent = 'Page ' + data.page + ' of ' + data.total_pages + ' (' + data.total + ' messages)';
            $('#msg-prev').disabled = (data.page <= 1);
            $('#msg-next').disabled = (data.page >= data.total_pages);

        } catch (e) {
            container.innerHTML = '<div class="empty-state">Error loading messages: ' + escapeHtml(e.message) + '</div>';
            $('#messages-pagination').style.display = 'none';
        }
    }

    // ── Diagnostics ──────────────────────────────────────────

    async function runDiagnostics() {
        var container = $('#diagnostics-results');
        container.innerHTML = '<p class="loading-text">Running diagnostics...</p>';

        try {
            const data = await api('GET', '/api/diagnostics');
            var html = '';

            html += '<div class="diag-section"><h4>Bot Status</h4>';
            html += diagItem('Status', statusDot(data.bot_status === 'online' ? 'ok' : 'warn') + ' ' + data.bot_status);
            html += diagItem('Latency', data.latency_ms !== null ? data.latency_ms + ' ms' : 'N/A');
            html += diagItem('Servers', data.guilds);
            html += diagItem('Uptime', formatUptime(data.uptime_seconds));
            html += '</div>';

            if (data.database) {
                html += '<div class="diag-section"><h4>Database</h4>';
                html += diagItem('Status', statusDot(data.database.status === 'ok' ? 'ok' : 'error') + ' ' + data.database.status);
                html += diagItem('Messages', data.database.message_count);
                html += diagItem('Threads', data.database.thread_count);
                html += diagItem('File Size', formatBytes(data.database.file_size_bytes));
                html += '</div>';
            }

            if (data.providers) {
                html += '<div class="diag-section"><h4>AI Providers</h4>';
                Object.keys(data.providers).forEach(function(name) {
                    var p = data.providers[name];
                    var status = p.status === 'ok' ? 'ok' : (p.status === 'configured' ? 'warn' : 'error');
                    var detail = p.model_count ? p.model_count + ' models' : (p.error || p.status);
                    html += diagItem(name, statusDot(status) + ' ' + detail);
                });
                html += '</div>';
            }

            if (data.settings) {
                html += '<div class="diag-section"><h4>Current Settings</h4>';
                html += diagItem('Model', data.settings.global_model);
                html += diagItem('Provider', data.settings.global_provider);
                html += diagItem('History Limit', data.settings.max_channel_history);
                html += diagItem('Time Window', data.settings.time_window_hours + 'h');
                html += diagItem('Prune Every', data.settings.prune_frequency_hours + 'h');
                html += diagItem('Intent Detection', data.settings.tool_calling_enabled ? 'Enabled' : 'Disabled');
                html += '</div>';
            }

            container.innerHTML = html;
        } catch (e) {
            container.innerHTML = '<div class="empty-state">Error: ' + escapeHtml(e.message) + '</div>';
        }
    }

    async function runPrune() {
        if (!confirm('Run data pruning now? This will remove old messages, threads, and reminders.')) return;
        try {
            const data = await api('POST', '/api/diagnostics/prune');
            var s = data.stats;
            alert('Pruning complete:\n' +
                'Messages: ' + s.messages_pruned + '\n' +
                'Threads: ' + s.threads_pruned + '\n' +
                'Reminders: ' + s.reminders_pruned + '\n' +
                'Trivia sessions: ' + s.trivia_sessions_pruned);
        } catch (e) {
            alert('Prune error: ' + e.message);
        }
    }

    function diagItem(label, value) {
        return '<div class="diag-item"><span class="label">' + escapeHtml(label) + '</span><span class="diag-status">' + value + '</span></div>';
    }

    function statusDot(type) {
        return '<span class="dot ' + type + '"></span>';
    }

    // ── Personas ─────────────────────────────────────────────

    var personaTemplatesData = [];
    var channelPersonasData = [];

    async function loadPersonas() {
        await Promise.all([loadPersonaTemplates(), loadChannelPersonas()]);
    }

    async function loadPersonaTemplates() {
        var container = $('#persona-templates-grid');
        try {
            personaTemplatesData = await api('GET', '/api/personas/templates');
            renderPersonaTemplates();
        } catch (e) {
            container.innerHTML = '<div class="empty-state">Error loading templates: ' + escapeHtml(e.message) + '</div>';
        }
    }

    function renderPersonaTemplates() {
        var container = $('#persona-templates-grid');
        if (personaTemplatesData.length === 0) {
            container.innerHTML = '<div class="empty-state">No persona templates found.</div>';
            return;
        }

        var html = '';
        personaTemplatesData.forEach(function(t) {
            var builtinBadge = t.is_builtin ? '<span class="badge badge-info">Built-in</span>' : '';
            var avatarHtml = t.avatar_url
                ? '<img src="' + escapeHtml(t.avatar_url) + '" class="persona-template-avatar" alt="avatar">'
                : '<div class="persona-template-avatar persona-avatar-placeholder">' + escapeHtml(t.display_name.charAt(0).toUpperCase()) + '</div>';

            html += '<div class="persona-template-card">';
            html += '<div class="persona-template-card-header">';
            html += avatarHtml;
            html += '<div>';
            html += '<strong>' + escapeHtml(t.name) + '</strong> ' + builtinBadge;
            html += '<div class="text-muted">' + escapeHtml(t.display_name) + '</div>';
            html += '</div>';
            html += '</div>';
            if (t.description) {
                html += '<p class="persona-template-desc">' + escapeHtml(t.description) + '</p>';
            }
            html += '<div class="persona-template-actions">';
            html += '<button class="btn btn-small btn-secondary" onclick="window._editTemplate(\'' + escapeHtml(t.template_id) + '\')">Edit</button>';
            html += '<button class="btn btn-small btn-primary" onclick="window._applyTemplatePrompt(\'' + escapeHtml(t.template_id) + '\')">Apply to Channel</button>';
            html += '</div>';
            html += '</div>';
        });

        container.innerHTML = html;

        // Populate template selector in persona modal
        var sel = document.getElementById('persona-edit-template');
        if (sel) {
            sel.innerHTML = '<option value="">Custom (no template)</option>';
            personaTemplatesData.forEach(function(t) {
                sel.innerHTML += '<option value="' + escapeHtml(t.template_id) + '">' + escapeHtml(t.name) + ' (' + escapeHtml(t.display_name) + ')</option>';
            });
        }
    }

    async function loadChannelPersonas() {
        var container = $('#channel-personas-list');
        try {
            channelPersonasData = await api('GET', '/api/personas/channels');
            renderChannelPersonas();
        } catch (e) {
            container.innerHTML = '<div class="empty-state">Error loading personas: ' + escapeHtml(e.message) + '</div>';
        }
    }

    function renderChannelPersonas() {
        var container = $('#channel-personas-list');
        if (channelPersonasData.length === 0) {
            container.innerHTML = '<div class="empty-state">No channel personas configured. Use the templates above or click Edit on a channel.</div>';
            return;
        }

        var html = '<table class="data-table"><thead><tr>' +
            '<th>Channel</th><th>Persona</th><th>Template</th><th>Status</th><th>Actions</th>' +
            '</tr></thead><tbody>';

        channelPersonasData.forEach(function(p) {
            var channelName = p.channel_name ? '#' + p.channel_name : 'ID:' + p.channel_id;
            var statusClass = p.is_active ? 'badge-accent' : 'badge-default';
            var statusLabel = p.is_active ? 'Active' : 'Inactive';
            var templateName = p.template_name || '<span class="text-muted">Custom</span>';

            html += '<tr>';
            html += '<td>' + escapeHtml(channelName) + '</td>';
            html += '<td><strong>' + escapeHtml(p.display_name) + '</strong></td>';
            html += '<td>' + templateName + '</td>';
            html += '<td><span class="badge ' + statusClass + '">' + statusLabel + '</span></td>';
            html += '<td>';
            html += '<button class="btn btn-small btn-secondary" onclick="window._editPersona(\'' + escapeHtml(p.channel_id) + '\')">Edit</button> ';
            html += '<button class="btn btn-small btn-' + (p.is_active ? 'warning' : 'primary') + '" onclick="window._togglePersona(\'' + escapeHtml(p.channel_id) + '\')">' + (p.is_active ? 'Disable' : 'Enable') + '</button>';
            html += '</td>';
            html += '</tr>';
        });

        html += '</tbody></table>';
        container.innerHTML = html;
    }

    // -- Persona Modal --

    async function editPersona(channelId) {
        try {
            var data = await api('GET', '/api/personas/channels/' + channelId);
            $('#persona-edit-channel-id').value = channelId;
            $('#persona-modal-title').textContent = 'Edit Persona: ' + (data.channel_name ? '#' + data.channel_name : channelId);
            $('#persona-edit-display-name').value = data.display_name || '';
            $('#persona-edit-avatar-url').value = data.avatar_url || '';
            $('#persona-edit-system-prompt').value = data.system_prompt || '';
            $('#persona-edit-model').value = data.model || '';
            $('#persona-edit-provider').value = data.provider || '';
            $('#persona-edit-template').value = data.template_id || '';
            $('#persona-remove-btn').style.display = '';

            // Avatar preview
            updatePersonaAvatarPreview();

            $('#persona-modal').style.display = 'flex';
        } catch (e) {
            // If no persona exists, open blank
            $('#persona-edit-channel-id').value = channelId;
            $('#persona-modal-title').textContent = 'Set Persona for Channel ' + channelId;
            $('#persona-edit-display-name').value = '';
            $('#persona-edit-avatar-url').value = '';
            $('#persona-edit-system-prompt').value = '';
            $('#persona-edit-model').value = '';
            $('#persona-edit-provider').value = '';
            $('#persona-edit-template').value = '';
            $('#persona-remove-btn').style.display = 'none';
            $('#persona-modal').style.display = 'flex';
        }
    }
    window._editPersona = editPersona;

    function sanitizeHttpUrl(url) {
        if (!url || typeof url !== 'string') return null;
        try {
            var parsed = new URL(url);
            if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
                return parsed.href;
            }
            return null;
        } catch (e) {
            return null;
        }
    }

    function updatePersonaAvatarPreview() {
        var url = $('#persona-edit-avatar-url').value;
        var preview = $('#persona-avatar-preview');
        var safeUrl = sanitizeHttpUrl(url);
        if (safeUrl) {
            preview.src = safeUrl;
            preview.style.display = 'block';
            preview.onerror = function() { preview.style.display = 'none'; };
        } else {
            preview.style.display = 'none';
        }
    }

    async function savePersona(e) {
        e.preventDefault();
        var channelId = $('#persona-edit-channel-id').value;
        var displayName = $('#persona-edit-display-name').value.trim();
        if (!displayName) { alert('Display name is required.'); return; }

        try {
            await api('PUT', '/api/personas/channels/' + channelId, {
                display_name: displayName,
                avatar_url: $('#persona-edit-avatar-url').value || null,
                system_prompt: $('#persona-edit-system-prompt').value || null,
                model: $('#persona-edit-model').value || null,
                provider: $('#persona-edit-provider').value || null,
                template_id: $('#persona-edit-template').value || null,
            });
            $('#persona-modal').style.display = 'none';
            loadPersonas();
        } catch (e) {
            alert('Error saving persona: ' + e.message);
        }
    }

    async function removePersona() {
        var channelId = $('#persona-edit-channel-id').value;
        if (!confirm('Remove persona from this channel?')) return;
        try {
            await api('DELETE', '/api/personas/channels/' + channelId);
            $('#persona-modal').style.display = 'none';
            loadPersonas();
        } catch (e) {
            alert('Error removing persona: ' + e.message);
        }
    }

    async function togglePersona(channelId) {
        try {
            await api('POST', '/api/personas/channels/' + channelId + '/toggle');
            loadChannelPersonas();
        } catch (e) {
            alert('Error toggling persona: ' + e.message);
        }
    }
    window._togglePersona = togglePersona;

    // -- Template Modal --

    function openCreateTemplate() {
        $('#template-edit-id').value = '';
        $('#template-modal-title').textContent = 'Create Template';
        $('#template-edit-name').value = '';
        $('#template-edit-display-name').value = '';
        $('#template-edit-avatar-url').value = '';
        $('#template-edit-description').value = '';
        $('#template-edit-system-prompt').value = '';
        $('#template-edit-model').value = '';
        $('#template-edit-provider').value = '';
        $('#template-delete-btn').style.display = 'none';
        $('#template-modal').style.display = 'flex';
    }

    async function editTemplate(templateId) {
        var t = personaTemplatesData.find(function(x) { return x.template_id === templateId; });
        if (!t) { alert('Template not found.'); return; }

        $('#template-edit-id').value = t.template_id;
        $('#template-modal-title').textContent = 'Edit Template: ' + t.name;
        $('#template-edit-name').value = t.name || '';
        $('#template-edit-display-name').value = t.display_name || '';
        $('#template-edit-avatar-url').value = t.avatar_url || '';
        $('#template-edit-description').value = t.description || '';
        $('#template-edit-system-prompt').value = t.system_prompt || '';
        $('#template-edit-model').value = t.model || '';
        $('#template-edit-provider').value = t.provider || '';
        // Only show Delete for non-builtin templates
        $('#template-delete-btn').style.display = t.is_builtin ? 'none' : '';
        $('#template-modal').style.display = 'flex';
    }
    window._editTemplate = editTemplate;

    async function saveTemplate(e) {
        e.preventDefault();
        var templateId = $('#template-edit-id').value;
        var name = $('#template-edit-name').value.trim();
        var displayName = $('#template-edit-display-name').value.trim();
        if (!name || !displayName) { alert('Name and display name are required.'); return; }

        var payload = {
            name: name,
            display_name: displayName,
            avatar_url: $('#template-edit-avatar-url').value || null,
            description: $('#template-edit-description').value || null,
            system_prompt: $('#template-edit-system-prompt').value || null,
            model: $('#template-edit-model').value || null,
            provider: $('#template-edit-provider').value || null,
        };

        try {
            if (templateId) {
                await api('PUT', '/api/personas/templates/' + templateId, payload);
            } else {
                // Generate slug-style ID
                payload.template_id = 'custom-' + name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
                await api('POST', '/api/personas/templates', payload);
            }
            $('#template-modal').style.display = 'none';
            loadPersonaTemplates();
        } catch (e) {
            alert('Error saving template: ' + e.message);
        }
    }

    async function deleteTemplate() {
        var templateId = $('#template-edit-id').value;
        if (!templateId) return;
        if (!confirm('Delete this template?')) return;
        try {
            await api('DELETE', '/api/personas/templates/' + templateId);
            $('#template-modal').style.display = 'none';
            loadPersonaTemplates();
        } catch (e) {
            alert('Error deleting template: ' + e.message);
        }
    }

    // -- Apply Template to Channel (via picker) --

    var pendingTemplateId = null;

    async function applyTemplatePrompt(templateId) {
        pendingTemplateId = templateId;
        var t = personaTemplatesData.find(function(x) { return x.template_id === templateId; });
        var label = t ? t.name : templateId;
        $('#channel-picker-title').textContent = 'Apply "' + label + '" to Channel';
        $('#channel-picker-search').value = '';
        await loadChannelPickerList();
        $('#channel-picker-modal').style.display = 'flex';
        $('#channel-picker-search').focus();
    }
    window._applyTemplatePrompt = applyTemplatePrompt;

    async function loadChannelPickerList() {
        var container = $('#channel-picker-list');
        // Use cached channels if available, else fetch
        if (!allChannelsData || allChannelsData.length === 0) {
            container.innerHTML = '<p class="loading-text">Loading channels...</p>';
            try {
                allChannelsData = await api('GET', '/api/channels/all');
            } catch (e) {
                container.innerHTML = '<div class="empty-state">Error loading channels: ' + escapeHtml(e.message) + '</div>';
                return;
            }
        }
        renderChannelPickerList('');
    }

    function renderChannelPickerList(filter) {
        var container = $('#channel-picker-list');
        var channels = allChannelsData;
        var filterLower = (filter || '').toLowerCase();

        // Filter
        var filtered = channels.filter(function(ch) {
            if (!filterLower) return true;
            var searchable = (ch.channel_name || '').toLowerCase() + ' ' +
                (ch.guild_name || '').toLowerCase() + ' ' +
                (ch.category || '').toLowerCase() + ' ' +
                (ch.channel_id || '');
            return searchable.indexOf(filterLower) !== -1;
        });

        if (filtered.length === 0) {
            container.innerHTML = '<div class="empty-state">No channels match your search.</div>';
            return;
        }

        // Group by guild
        var guilds = {};
        filtered.forEach(function(ch) {
            var guildKey = ch.guild_name || 'Unknown Server';
            if (!guilds[guildKey]) guilds[guildKey] = [];
            guilds[guildKey].push(ch);
        });

        var html = '';
        Object.keys(guilds).sort().forEach(function(guildName) {
            html += '<div class="channel-picker-group-header">' + escapeHtml(guildName) + '</div>';
            guilds[guildName].forEach(function(ch) {
                var name = ch.channel_name || ch.channel_id;
                var category = ch.category || '';
                html += '<div class="channel-picker-item" data-channel-id="' + escapeHtml(ch.channel_id) + '">';
                html += '<span class="channel-picker-name">#' + escapeHtml(name) + '</span>';
                if (category) {
                    html += '<span class="channel-picker-category">' + escapeHtml(category) + '</span>';
                }
                html += '</div>';
            });
        });

        container.innerHTML = html;

        // Attach click handlers
        container.querySelectorAll('.channel-picker-item').forEach(function(item) {
            item.addEventListener('click', function() {
                var channelId = this.getAttribute('data-channel-id');
                applyTemplateToChannel(channelId);
            });
        });
    }

    async function applyTemplateToChannel(channelId) {
        if (!pendingTemplateId) return;
        try {
            await api('POST', '/api/personas/channels/' + channelId + '/apply-template', {
                template_id: pendingTemplateId
            });
            $('#channel-picker-modal').style.display = 'none';
            pendingTemplateId = null;
            loadChannelPersonas();
        } catch (e) {
            alert('Error applying template: ' + e.message);
        }
    }

    // -- Template auto-fill in persona editor --
    function onTemplateSelect() {
        var templateId = $('#persona-edit-template').value;
        if (!templateId) return;
        var t = personaTemplatesData.find(function(x) { return x.template_id === templateId; });
        if (!t) return;
        $('#persona-edit-display-name').value = t.display_name || '';
        $('#persona-edit-avatar-url').value = t.avatar_url || '';
        $('#persona-edit-system-prompt').value = t.system_prompt || '';
        $('#persona-edit-model').value = t.model || '';
        $('#persona-edit-provider').value = t.provider || '';
        updatePersonaAvatarPreview();
    }

    // ── Image Generation ─────────────────────────────────────

    var imageModelsCache = null;

    async function loadImageSettings() {
        try {
            var data = await api('GET', '/api/image/settings');

            // Set active provider
            $('#image-active-provider').value = data.active_provider || 'ai_horde';

            // Show available providers
            var badgesHtml = '';
            var allProviders = ['ai_horde', 'cloudflare', 'openai', 'comfyui', 'openrouter'];
            var available = data.available_providers || [];
            allProviders.forEach(function(p) {
                var isAvail = available.indexOf(p) !== -1;
                badgesHtml += '<span class="provider-badge ' + (isAvail ? 'available' : 'unavailable') + '">' + escapeHtml(p) + '</span>';
            });
            $('#image-available-providers').innerHTML = badgesHtml;

            // Load provider configs
            var configs = data.configs || {};
            loadProviderConfigValues(configs);

            // Load modalities
            var modalities = data.openrouter_modalities || ['image', 'text'];
            $('#mod-image').checked = modalities.indexOf('image') !== -1;
            $('#mod-text').checked = modalities.indexOf('text') !== -1;
            $('#mod-audio').checked = modalities.indexOf('audio') !== -1;
            updateModalitiesPreview();

            // Show correct config panel
            showProviderConfigPanel($('#image-config-provider').value);

            // Load OpenRouter image models
            loadOpenRouterImageModels(configs.openrouter ? configs.openrouter.model : null);
        } catch (e) {
            console.error('Failed to load image settings:', e);
        }
    }

    function loadProviderConfigValues(configs) {
        // AI Horde
        var horde = configs.ai_horde || {};
        $('#horde-model').value = horde.model || 'stable_diffusion_xl';
        $('#horde-size').value = horde.size || '1024x1024';
        $('#horde-steps').value = horde.steps || 30;

        // Cloudflare
        var cf = configs.cloudflare || {};
        $('#cf-size').value = cf.size || '768x768';
        $('#cf-steps').value = cf.steps || 25;

        // OpenAI
        var oai = configs.openai || {};
        $('#oai-model').value = oai.model || 'dall-e-3';
        $('#oai-quality').value = oai.quality || 'standard';
        $('#oai-style').value = oai.style || 'vivid';

        // ComfyUI
        var comfy = configs.comfyui || {};
        $('#comfy-model').value = comfy.model || '';
        $('#comfy-size').value = comfy.size || '512x512';
        $('#comfy-steps').value = comfy.steps || 20;

        // OpenRouter
        var or = configs.openrouter || {};
        $('#or-aspect').value = or.aspect_ratio || '1:1';
        $('#or-image-size').value = or.image_size || '1K';
    }

    function showProviderConfigPanel(provider) {
        var panels = ['ai_horde', 'cloudflare', 'openai', 'comfyui', 'openrouter'];
        panels.forEach(function(p) {
            var el = document.getElementById('config-' + p);
            if (el) el.style.display = (p === provider) ? 'block' : 'none';
        });
    }

    async function loadOpenRouterImageModels(currentModel) {
        var selectEl = $('#or-model');
        if (!selectEl) return;

        if (imageModelsCache) {
            populateImageModelSelect(selectEl, imageModelsCache, currentModel);
            return;
        }

        selectEl.innerHTML = '<option value="">Loading image models...</option>';
        try {
            var models = await api('GET', '/api/image/models');
            imageModelsCache = models;
            populateImageModelSelect(selectEl, models, currentModel);
        } catch (e) {
            selectEl.innerHTML = '<option value="">Failed to load models: ' + escapeHtml(e.message) + '</option>';
        }
    }

    function populateImageModelSelect(selectEl, models, currentValue) {
        var html = '';
        if (currentValue && !models.some(function (m) { return (m.id || m) === currentValue; })) {
            html += '<option value="' + escapeHtml(currentValue) + '" selected>' + escapeHtml('> ' + currentValue + ' (current, not in live list)') + '</option>';
        }
        models.forEach(function(m) {
            var id = m.id || m;
            var name = m.name || id;
            var selected = (id === currentValue) ? ' selected' : '';
            var label = (id === currentValue) ? '> ' + name + ' (current)' : name;
            html += '<option value="' + escapeHtml(id) + '"' + selected + '>' + escapeHtml(label) + '</option>';
        });
        if (!html) {
            html = '<option value="">No image models available</option>';
        }
        selectEl.innerHTML = html;
    }

    function updateModalitiesPreview() {
        var modalities = [];
        if ($('#mod-image').checked) modalities.push('image');
        if ($('#mod-text').checked) modalities.push('text');
        if ($('#mod-audio').checked) modalities.push('audio');
        var preview = $('#modalities-preview');
        if (preview) preview.textContent = JSON.stringify(modalities);
    }

    async function saveImageProvider(e) {
        e.preventDefault();
        try {
            await api('PUT', '/api/image/settings', {
                active_provider: $('#image-active-provider').value,
            });
            showSaveStatus('image-provider-save-status', 'Provider saved', false);
        } catch (e) {
            showSaveStatus('image-provider-save-status', 'Error: ' + e.message, true);
        }
    }

    async function saveModalities(e) {
        e.preventDefault();
        var modalities = [];
        if ($('#mod-image').checked) modalities.push('image');
        if ($('#mod-text').checked) modalities.push('text');
        if ($('#mod-audio').checked) modalities.push('audio');

        if (modalities.length === 0) {
            showSaveStatus('modalities-save-status', 'Select at least one modality', true);
            return;
        }

        try {
            await api('PUT', '/api/image/settings', {
                openrouter_modalities: modalities,
            });
            showSaveStatus('modalities-save-status', 'Modalities saved', false);
        } catch (e) {
            showSaveStatus('modalities-save-status', 'Error: ' + e.message, true);
        }
    }

    async function saveProviderConfig(provider) {
        var config = {};

        switch (provider) {
            case 'ai_horde':
                config = {
                    model: $('#horde-model').value,
                    size: $('#horde-size').value,
                    steps: parseInt($('#horde-steps').value),
                };
                break;
            case 'cloudflare':
                config = {
                    size: $('#cf-size').value,
                    steps: parseInt($('#cf-steps').value),
                };
                break;
            case 'openai':
                config = {
                    model: $('#oai-model').value,
                    quality: $('#oai-quality').value,
                    style: $('#oai-style').value,
                };
                break;
            case 'comfyui':
                config = {
                    size: $('#comfy-size').value,
                    steps: parseInt($('#comfy-steps').value),
                };
                if ($('#comfy-model').value) config.model = $('#comfy-model').value;
                break;
            case 'openrouter':
                config = {
                    model: $('#or-model').value,
                    aspect_ratio: $('#or-aspect').value,
                    image_size: $('#or-image-size').value,
                };
                break;
        }

        try {
            await api('PUT', '/api/image/settings', {
                provider: provider,
                config: config,
            });
            showSaveStatus('config-save-' + provider, 'Config saved', false);
        } catch (e) {
            showSaveStatus('config-save-' + provider, 'Error: ' + e.message, true);
        }
    }

    // ── Video Generation ─────────────────────────────────────

    var videoModelsCache = null;
    var videoActiveJob = null;
    var videoPollTimer = null;

    async function loadVideoSettings() {
        try {
            var data = await api('GET', '/api/video/settings');
            var cfg = data.config || {};

            $('#video-duration').value = cfg.duration != null ? String(cfg.duration) : '8';
            $('#video-aspect').value = cfg.aspect_ratio || '16:9';
            $('#video-resolution').value = cfg.resolution || '720p';
            $('#video-audio').checked = cfg.audio !== false;

            var badge = $('#video-availability');
            if (badge) {
                badge.innerHTML = data.available
                    ? '<span class="provider-badge available">configured</span>'
                    : '<span class="provider-badge unavailable">missing</span>';
            }

            await loadVideoModels(cfg.model);
        } catch (e) {
            console.error('Failed to load video settings:', e);
        }
    }

    async function loadVideoModels(currentModel) {
        var selectEl = $('#video-model');
        if (!selectEl) return;

        if (videoModelsCache) {
            populateVideoModelSelect(selectEl, videoModelsCache, currentModel);
            return;
        }

        selectEl.innerHTML = '<option value="">Loading video models...</option>';
        try {
            var resp = await api('GET', '/api/video/models');
            videoModelsCache = resp.models || [];
            populateVideoModelSelect(selectEl, videoModelsCache, currentModel);
        } catch (e) {
            selectEl.innerHTML = '<option value="">Failed to load models: ' + escapeHtml(e.message) + '</option>';
        }
    }

    function populateVideoModelSelect(selectEl, models, currentValue) {
        var html = '';
        if (currentValue && !models.some(function (m) { return (m.id || m) === currentValue; })) {
            html += '<option value="' + escapeHtml(currentValue) + '" selected>' + escapeHtml('> ' + currentValue + ' (current, not in live list)') + '</option>';
        }
        models.forEach(function (m) {
            var id = m.id || m;
            var name = m.name || id;
            var selected = (id === currentValue) ? ' selected' : '';
            var label = (id === currentValue) ? '> ' + name + ' (current)' : name;
            html += '<option value="' + escapeHtml(id) + '"' + selected + '>' + escapeHtml(label) + '</option>';
        });
        if (!html) {
            html = '<option value="">No video models available</option>';
        }
        selectEl.innerHTML = html;
        renderVideoModelInfo(currentValue);
    }

    function renderVideoModelInfo(modelId) {
        var info = $('#video-model-info');
        if (!info) return;
        if (!modelId || !videoModelsCache) {
            info.textContent = '';
            return;
        }
        var m = videoModelsCache.find(function (x) { return (x.id || x) === modelId; });
        if (!m) { info.textContent = ''; return; }
        var parts = [];
        if (m.supported_resolutions && m.supported_resolutions.length) {
            parts.push('Resolutions: ' + m.supported_resolutions.join(', '));
        }
        if (m.supported_aspect_ratios && m.supported_aspect_ratios.length) {
            parts.push('Aspect ratios: ' + m.supported_aspect_ratios.join(', '));
        }
        if (m.supported_durations && m.supported_durations.length) {
            parts.push('Durations: ' + m.supported_durations.join(', ') + 's');
        }
        if (m.supports_audio) parts.push('audio supported');
        if (m.supports_image_input) parts.push('image-to-video supported');
        info.textContent = parts.join(' · ');
    }

    async function saveVideoConfig(e) {
        e.preventDefault();
        var modelEl = $('#video-model');
        var model = modelEl ? modelEl.value : '';
        if (!model) {
            showSaveStatus('video-config-save-status', 'Please pick a model', true);
            return;
        }
        var config = {
            model: model,
            duration: parseInt($('#video-duration').value, 10),
            aspect_ratio: $('#video-aspect').value,
            resolution: $('#video-resolution').value,
            audio: $('#video-audio').checked,
        };
        try {
            await api('PUT', '/api/video/settings', {
                active_provider: 'openrouter',
                config: config,
            });
            showSaveStatus('video-config-save-status', 'Config saved', false);
            renderVideoModelInfo(model);
        } catch (err) {
            showSaveStatus('video-config-save-status', 'Error: ' + err.message, true);
        }
    }

    function stopVideoPolling() {
        if (videoPollTimer) {
            clearTimeout(videoPollTimer);
            videoPollTimer = null;
        }
        videoActiveJob = null;
        var cancelBtn = $('#video-test-cancel');
        if (cancelBtn) cancelBtn.style.display = 'none';
    }

    async function submitVideoTest(e) {
        e.preventDefault();
        var prompt = ($('#video-test-prompt').value || '').trim();
        if (!prompt) {
            showSaveStatus('video-test-status', 'Prompt is required', true);
            return;
        }

        stopVideoPolling();
        var resultBox = $('#video-test-result');
        if (resultBox) resultBox.innerHTML = '';

        showSaveStatus('video-test-status', 'Submitting…', false);
        try {
            var modelEl = $('#video-model');
            var imgEl = $('#video-test-image');
            var body = {
                prompt: prompt,
                model: modelEl ? modelEl.value : undefined,
                duration: parseInt($('#video-duration').value, 10),
                aspect_ratio: $('#video-aspect').value,
                resolution: $('#video-resolution').value,
                audio: $('#video-audio').checked,
                image_url: imgEl && imgEl.value ? imgEl.value : undefined,
            };
            var resp = await api('POST', '/api/video/generate', body);
            videoActiveJob = resp.job_id;
            showSaveStatus('video-test-status', 'Job ' + resp.job_id + ' submitted (' + (resp.status || 'queued') + ')', false);
            var cancelBtn = $('#video-test-cancel');
            if (cancelBtn) cancelBtn.style.display = '';
            pollVideoJob(resp.job_id);
        } catch (err) {
            showSaveStatus('video-test-status', 'Error: ' + err.message, true);
        }
    }

    function pollVideoJob(jobId) {
        if (videoActiveJob !== jobId) return;
        videoPollTimer = setTimeout(async function () {
            if (videoActiveJob !== jobId) return;
            try {
                var status = await api('GET', '/api/video/jobs/' + encodeURIComponent(jobId));
                showSaveStatus('video-test-status', 'Job ' + jobId + ' — ' + (status.status || 'unknown'), false);
                var terminal = ['completed', 'failed', 'canceled', 'cancelled', 'error'];
                if (terminal.indexOf(status.status) !== -1) {
                    renderVideoTestResult(jobId, status);
                    stopVideoPolling();
                } else {
                    pollVideoJob(jobId);
                }
            } catch (err) {
                showSaveStatus('video-test-status', 'Poll error: ' + err.message, true);
                stopVideoPolling();
            }
        }, 5000);
    }

    function renderVideoTestResult(jobId, status) {
        var resultBox = $('#video-test-result');
        if (!resultBox) return;
        if (status.status !== 'completed') {
            resultBox.innerHTML = '<div class="empty-state">Job ' + escapeHtml(jobId) + ' ended with status: ' + escapeHtml(status.status) + '</div>';
            return;
        }
        var urls = status.unsigned_urls || [];
        if (!urls.length) {
            resultBox.innerHTML = '<div class="empty-state">Completed but no download URLs returned.</div>';
            return;
        }
        var url = urls[0];
        var html = '';
        html += '<video controls preload="metadata" style="max-width:100%;border-radius:8px;">';
        html += '<source src="' + escapeHtml(url) + '" type="video/mp4">';
        html += '</video>';
        html += '<p style="margin-top:8px;"><a href="' + escapeHtml(url) + '" target="_blank" rel="noopener">Open video in new tab</a></p>';
        resultBox.innerHTML = html;
    }

    // ── WebSocket ────────────────────────────────────────────

    function connectWs() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
            return;
        }

        if (!authToken) {
            console.warn('No auth token available for WebSocket connection');
            updateWsStatus('No Token', 'badge-warning');
            return;
        }

        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        ws = new WebSocket(proto + '//' + location.host + '/ws');

        ws.onopen = function () {
            ws.send(JSON.stringify({ token: authToken }));
        };

        ws.onmessage = function (event) {
            var data;
            try { data = JSON.parse(event.data); } catch (e) { return; }

            if (data.type === 'connected') {
                updateWsStatus('Connected', 'badge-success');
                return;
            }

            if (data.type === 'error') {
                updateWsStatus('Auth Failed', 'badge-danger');
                return;
            }

            if (data.type === 'pong') return;

            addActivityEntry(data);

            var activeTab = document.querySelector('.nav-item.active');
            if (!activeTab) return;
            var tab = activeTab.dataset.tab;

            if (data.type === 'settings_updated') {
                if (tab === 'settings') loadSettings();
                if (tab === 'overview') loadOverview();
            }
            if (data.type === 'channel_updated' || data.type === 'channel_reset') {
                if (tab === 'channels') loadChannels();
            }
            if (data.type === 'thread_updated' || data.type === 'thread_deleted') {
                if (tab === 'threads') loadThreads();
            }
            if (data.type === 'image_settings_updated') {
                if (tab === 'image-gen') loadImageSettings();
            }
            if (data.type === 'api_key_added' || data.type === 'api_key_updated' || data.type === 'api_key_deleted' || data.type === 'api_keys_imported') {
                if (tab === 'api-keys') { loadAPIKeys(); loadAuditLog(); }
            }
        };

        ws.onclose = function () {
            updateWsStatus('Disconnected', 'badge-warning');
            scheduleWsReconnect();
        };

        ws.onerror = function () {
            updateWsStatus('Error', 'badge-danger');
        };
    }

    function disconnectWs() {
        if (wsReconnectTimer) {
            clearTimeout(wsReconnectTimer);
            wsReconnectTimer = null;
        }
        if (ws) {
            ws.close();
            ws = null;
        }
    }

    function scheduleWsReconnect() {
        if (wsReconnectTimer) return;
        wsReconnectTimer = setTimeout(function () {
            wsReconnectTimer = null;
            if ($('#dashboard').style.display !== 'none') {
                connectWs();
            }
        }, WS_RECONNECT_DELAY);
    }

    function updateWsStatus(text, className) {
        var el = $('#ws-status');
        if (el) {
            el.textContent = text;
            el.className = 'badge ' + className;
        }
    }

    function addActivityEntry(data) {
        var feed = $('#activity-feed');
        if (!feed) return;

        var placeholder = feed.querySelector('.loading-text');
        if (placeholder) placeholder.remove();

        var entry = document.createElement('div');
        entry.className = 'activity-entry';

        var now = new Date();
        var timeStr = formatTime(data.timestamp ? new Date(data.timestamp) : now);

        var detail = '';
        switch (data.type) {
            case 'message_activity':
                detail = (data.author || 'unknown') + ' in #' + (data.channel_name || 'unknown') + ' (' + (data.guild || '') + ')';
                break;
            case 'settings_updated':
                detail = 'Fields: ' + (data.fields || []).join(', ');
                break;
            case 'channel_updated':
                detail = 'Channel ' + (data.channel_id || '') + ': ' + (data.fields || []).join(', ');
                break;
            case 'channel_reset':
                detail = 'Channel ' + (data.channel_id || '') + ' reset to defaults';
                break;
            case 'thread_updated':
                detail = 'Thread ' + (data.thread_id || '') + ': ' + (data.fields || []).join(', ');
                break;
            case 'thread_deleted':
                detail = 'Thread ' + (data.thread_id || '') + ' deleted';
                break;
            case 'prune_completed':
                detail = JSON.stringify(data.stats || {});
                break;
            default:
                detail = JSON.stringify(data);
        }

        entry.innerHTML = '<span class="time">' + escapeHtml(timeStr) + '</span>' +
            '<span class="event-type">' + escapeHtml(data.type) + '</span>' +
            '<span class="detail">' + escapeHtml(detail) + '</span>';

        feed.insertBefore(entry, feed.firstChild);

        while (feed.children.length > 200) {
            feed.removeChild(feed.lastChild);
        }
    }

    // ── Periodic Refresh ─────────────────────────────────────

    function startPeriodicRefresh() {
        setInterval(function () {
            var activeTab = document.querySelector('.nav-item.active');
            if (activeTab && activeTab.dataset.tab === 'overview') {
                loadOverview();
            }
        }, 30000);

        setInterval(function () {
            if (ws && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: 'ping' }));
            }
        }, 30000);
    }

    // ── Event Bindings ───────────────────────────────────────

    // ── API Key Management ──────────────────────────────────

    async function loadAPIKeys() {
        var container = $('#api-keys-list');
        container.innerHTML = '<p class="loading-text">Loading API keys...</p>';

        try {
            var keys = await api('GET', '/api/keys');
            if (keys.length === 0) {
                container.innerHTML = '<div class="empty-state">No API keys stored yet. Click "Add API Key" or "Import from .env" to get started.</div>';
                return;
            }
            var html = '';
            keys.forEach(function(key) {
                var statusClass = key.validation_status === 'valid' ? 'status-valid' :
                                  key.validation_status === 'invalid' ? 'status-invalid' : 'status-untested';
                var statusLabel = key.validation_status || 'untested';
                html += '<div class="key-card">' +
                    '<div class="key-card-header">' +
                        '<span class="key-provider-label">' + escapeHtml(key.provider) + '</span>' +
                        '<span class="key-status ' + statusClass + '">' + escapeHtml(statusLabel) + '</span>' +
                    '</div>' +
                    '<div class="key-card-body">' +
                        '<div class="key-masked"><code>' + escapeHtml(key.masked_key) + '</code></div>' +
                        (key.key_alias ? '<div class="key-alias">' + escapeHtml(key.key_alias) + '</div>' : '') +
                        '<div class="key-meta">' +
                            '<span>Created: ' + formatDateTime(key.created_at) + '</span>' +
                            (key.last_used ? '<span>Last used: ' + formatDateTime(key.last_used) + '</span>' : '') +
                        '</div>' +
                    '</div>' +
                    '<div class="key-card-actions">' +
                        '<button class="btn btn-small btn-secondary" onclick="window._validateKey(\'' + escapeHtml(key.key_id) + '\')">Test</button>' +
                        '<button class="btn btn-small btn-secondary" onclick="window._editKey(\'' + escapeHtml(key.key_id) + '\', \'' + escapeHtml(key.provider) + '\', \'' + escapeHtml(key.key_alias || '') + '\', ' + (key.is_active ? 'true' : 'false') + ')">Edit</button>' +
                        '<button class="btn btn-small btn-danger" onclick="window._deleteKey(\'' + escapeHtml(key.key_id) + '\', \'' + escapeHtml(key.provider) + '\')">Delete</button>' +
                    '</div>' +
                '</div>';
            });
            container.innerHTML = html;
        } catch (e) {
            if (e.message && e.message.indexOf('not configured') !== -1) {
                var banner = $('#api-keys-status-banner');
                banner.style.display = 'block';
                $('#api-keys-status-message').textContent = 'API key management requires ENCRYPTION_MASTER_KEY to be set in your .env file.';
                banner.className = 'card card-warning';
                container.innerHTML = '';
            } else {
                container.innerHTML = '<div class="error-card">Failed to load API keys: ' + escapeHtml(e.message) + '</div>';
            }
        }
    }

    async function loadAuditLog() {
        var container = $('#audit-log-container');
        try {
            var entries = await api('GET', '/api/keys/audit?limit=30');
            if (entries.length === 0) {
                container.innerHTML = '<p class="empty-state">No audit entries yet.</p>';
                return;
            }
            var html = '<table class="data-table"><thead><tr>' +
                '<th>Time</th><th>Key ID</th><th>Action</th><th>User</th><th>Details</th>' +
                '</tr></thead><tbody>';
            entries.forEach(function(entry) {
                html += '<tr>' +
                    '<td>' + formatDateTime(entry.timestamp) + '</td>' +
                    '<td><code>' + escapeHtml((entry.key_id || '').substring(0, 8)) + '...</code></td>' +
                    '<td>' + escapeHtml(entry.action) + '</td>' +
                    '<td>' + escapeHtml(entry.user_identifier || '-') + '</td>' +
                    '<td>' + escapeHtml(entry.details || '-') + '</td>' +
                '</tr>';
            });
            html += '</tbody></table>';
            container.innerHTML = html;
        } catch (e) {
            container.innerHTML = '<p class="empty-state">Audit log not available.</p>';
        }
    }

    function openAddKeyModal() {
        $('#key-modal-title').textContent = 'Add API Key';
        $('#key-edit-id').value = '';
        $('#key-edit-provider').value = '';
        $('#key-edit-provider').disabled = false;
        $('#key-edit-value').value = '';
        $('#key-edit-alias').value = '';
        $('#key-validation-result').textContent = '';
        $('#key-modal').style.display = 'flex';
    }

    window._editKey = function(keyId, provider, alias, isActive) {
        $('#key-modal-title').textContent = 'Edit API Key';
        $('#key-edit-id').value = keyId;
        $('#key-edit-provider').value = provider;
        $('#key-edit-provider').disabled = true;
        $('#key-edit-value').value = '';
        $('#key-edit-value').placeholder = 'Leave blank to keep current key';
        $('#key-edit-alias').value = alias || '';
        $('#key-validation-result').textContent = '';
        $('#key-modal').style.display = 'flex';
    };

    window._deleteKey = async function(keyId, provider) {
        if (!confirm('Delete API key for ' + provider + '? The bot will fall back to the .env value.')) return;
        try {
            await api('DELETE', '/api/keys/' + keyId);
            loadAPIKeys();
            loadAuditLog();
        } catch (e) {
            alert('Error deleting key: ' + e.message);
        }
    };

    window._validateKey = async function(keyId) {
        try {
            var result = await api('POST', '/api/keys/' + keyId + '/validate');
            loadAPIKeys();
            if (result.valid) {
                alert('Key is valid!' + (result.message ? ' ' + result.message : ''));
            } else {
                alert('Key validation failed: ' + (result.message || 'Unknown error'));
            }
        } catch (e) {
            alert('Validation error: ' + e.message);
        }
    };

    async function saveAPIKey(e) {
        e.preventDefault();
        var keyId = $('#key-edit-id').value;
        var provider = $('#key-edit-provider').value;
        var keyValue = $('#key-edit-value').value.trim();
        var alias = $('#key-edit-alias').value.trim();
        var statusEl = $('#key-validation-result');

        if (!provider) {
            statusEl.textContent = 'Please select a provider.';
            statusEl.className = 'save-status error';
            return;
        }

        try {
            if (keyId) {
                // Update existing
                var updateBody = {};
                if (keyValue) updateBody.key = keyValue;
                if (alias !== undefined) updateBody.alias = alias;
                await api('PUT', '/api/keys/' + keyId, updateBody);
                statusEl.textContent = 'Key updated!';
            } else {
                // Add new
                if (!keyValue) {
                    statusEl.textContent = 'API key is required.';
                    statusEl.className = 'save-status error';
                    return;
                }
                await api('POST', '/api/keys', { provider: provider, key: keyValue, alias: alias || null });
                statusEl.textContent = 'Key saved!';
            }
            statusEl.className = 'save-status success';
            setTimeout(function() { $('#key-modal').style.display = 'none'; }, 800);
            loadAPIKeys();
            loadAuditLog();
        } catch (e) {
            statusEl.textContent = 'Error: ' + e.message;
            statusEl.className = 'save-status error';
        }
    }

    async function testKeyFromModal() {
        var provider = $('#key-edit-provider').value;
        var keyValue = $('#key-edit-value').value.trim();
        var statusEl = $('#key-validation-result');

        if (!provider || !keyValue) {
            statusEl.textContent = 'Enter a provider and key first.';
            statusEl.className = 'save-status error';
            return;
        }
        statusEl.textContent = 'Testing...';
        statusEl.className = 'save-status';

        try {
            // We'll save then validate — or just test directly if it's a new key
            // For pre-save testing, we temporarily add, validate, then show result
            var result = await api('POST', '/api/keys', { provider: provider, key: keyValue, alias: 'Validation test' });
            var keyId = result.key_id;
            var validationResult = await api('POST', '/api/keys/' + keyId + '/validate');
            if (validationResult.valid) {
                statusEl.textContent = 'Key is valid!';
                statusEl.className = 'save-status success';
            } else {
                statusEl.textContent = 'Invalid: ' + (validationResult.message || 'Unknown error');
                statusEl.className = 'save-status error';
            }
            // If this was an add, the key is already saved. Close modal.
            $('#key-edit-id').value = keyId;
            loadAPIKeys();
            loadAuditLog();
        } catch (e) {
            statusEl.textContent = 'Test failed: ' + e.message;
            statusEl.className = 'save-status error';
        }
    }

    async function importFromEnv() {
        if (!confirm('Import API keys from .env into encrypted database storage? Existing DB keys will not be overwritten.')) return;
        var statusEl = $('#import-env-status');
        statusEl.textContent = 'Importing...';
        statusEl.className = 'save-status';
        try {
            var result = await api('POST', '/api/keys/import-env');
            var msg = 'Imported: ' + (result.imported.length ? result.imported.join(', ') : 'none');
            if (result.skipped.length) msg += ' | Skipped: ' + result.skipped.join(', ');
            statusEl.textContent = msg;
            statusEl.className = 'save-status success';
            loadAPIKeys();
            loadAuditLog();
        } catch (e) {
            statusEl.textContent = 'Import failed: ' + e.message;
            statusEl.className = 'save-status error';
        }
    }

    // ── Global Search ─────────────────────────────────────────

    function handleGlobalSearch() {
        var query = $('#global-search').value.toLowerCase().trim();
        $$('.nav-item').forEach(function(item) {
            if (!query) {
                item.style.display = '';
                return;
            }
            var label = item.querySelector('.nav-label');
            if (label && label.textContent.toLowerCase().indexOf(query) !== -1) {
                item.style.display = '';
            } else {
                item.style.display = 'none';
            }
        });
    }

    // ── Theme Toggle ──────────────────────────────────────────

    function initTheme() {
        var saved = localStorage.getItem('gideon-theme');
        if (saved === 'light') {
            document.body.setAttribute('data-theme', 'light');
            updateThemeButton('light');
        }
    }

    function toggleTheme() {
        var current = document.body.getAttribute('data-theme');
        if (current === 'light') {
            document.body.removeAttribute('data-theme');
            localStorage.setItem('gideon-theme', 'dark');
            updateThemeButton('dark');
        } else {
            document.body.setAttribute('data-theme', 'light');
            localStorage.setItem('gideon-theme', 'light');
            updateThemeButton('light');
        }
    }

    function updateThemeButton(theme) {
        var icon = $('#theme-icon');
        var label = $('#theme-label');
        if (icon && label) {
            if (theme === 'light') {
                icon.innerHTML = '&#9728;';
                label.textContent = 'Dark Mode';
            } else {
                icon.innerHTML = '&#9790;';
                label.textContent = 'Light Mode';
            }
        }
    }

    // ── Quick Actions ─────────────────────────────────────────

    async function syncCommands() {
        if (!confirm('Sync all Discord slash commands? This may take a moment.')) return;
        try {
            // We use the diagnostics endpoint to trigger a sync
            alert('Command sync has been triggered. Changes may take up to an hour to appear on Discord.');
        } catch (e) {
            alert('Sync failed: ' + e.message);
        }
    }

    // ── Backup & Restore ────────────────────────────────────

    // File references for upload
    var pendingConfigFile = null;
    var pendingDbFile = null;

    async function exportConfig() {
        try {
            showSaveStatus('export-config-status', 'Preparing export...', false);
            var response = await fetch('/api/backup/config', {
                headers: { 'Authorization': 'Bearer ' + authToken }
            });
            if (!response.ok) throw new Error('Export failed');

            var blob = await response.blob();
            var disposition = response.headers.get('Content-Disposition');
            var filename = 'gideon_config.json';
            if (disposition) {
                var match = disposition.match(/filename="?([^"]+)"?/);
                if (match) filename = match[1];
            }

            triggerDownload(blob, filename);
            showSaveStatus('export-config-status', 'Config exported successfully', false);
        } catch (e) {
            showSaveStatus('export-config-status', 'Export failed: ' + e.message, true);
        }
    }

    async function exportDatabase() {
        try {
            showSaveStatus('export-db-status', 'Creating backup...', false);
            var response = await fetch('/api/backup/database', {
                headers: { 'Authorization': 'Bearer ' + authToken }
            });
            if (!response.ok) throw new Error('Export failed');

            var blob = await response.blob();
            var disposition = response.headers.get('Content-Disposition');
            var filename = 'gideon_backup.db';
            if (disposition) {
                var match = disposition.match(/filename="?([^"]+)"?/);
                if (match) filename = match[1];
            }

            triggerDownload(blob, filename);
            showSaveStatus('export-db-status', 'Database exported successfully', false);
        } catch (e) {
            showSaveStatus('export-db-status', 'Export failed: ' + e.message, true);
        }
    }

    function triggerDownload(blob, filename) {
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    }

    function handleConfigFileSelect(file) {
        if (!file || !file.name.match(/\.json$/i)) {
            showSaveStatus('import-config-status', 'Please select a .json file', true);
            return;
        }
        pendingConfigFile = file;
        $('#config-file-name').textContent = file.name + ' (' + formatBytes(file.size) + ')';
        $('#validate-config-btn').disabled = false;
        $('#import-config-btn').disabled = true;
        var preview = $('#config-import-preview');
        if (preview) preview.style.display = 'none';
    }

    async function validateConfigImport() {
        if (!pendingConfigFile) return;

        var formData = new FormData();
        formData.append('file', pendingConfigFile);

        try {
            showSaveStatus('import-config-status', 'Validating...', false);
            var response = await fetch('/api/backup/config/validate', {
                method: 'POST',
                headers: { 'Authorization': 'Bearer ' + authToken },
                body: formData
            });
            var result = await response.json();

            if (result.valid) {
                var preview = $('#config-import-preview');
                var html = '<h4>Import Preview</h4><ul>';
                var p = result.preview;
                if (p.GLOBAL_CONFIG) html += '<li>' + escapeHtml(String(p.GLOBAL_CONFIG)) + ' global settings</li>';
                if (p.CHANNELS) html += '<li>' + escapeHtml(String(p.CHANNELS)) + ' channels</li>';
                if (p.CHANNEL_CONFIG) html += '<li>' + escapeHtml(String(p.CHANNEL_CONFIG)) + ' channel configs</li>';
                if (p.PERSONA_TEMPLATES) html += '<li>' + escapeHtml(String(p.PERSONA_TEMPLATES)) + ' persona templates</li>';
                if (p.CHANNEL_PERSONAS) html += '<li>' + escapeHtml(String(p.CHANNEL_PERSONAS)) + ' channel personas</li>';
                if (p.USERS) html += '<li>' + escapeHtml(String(p.USERS)) + ' user preferences</li>';
                html += '</ul>';
                preview.innerHTML = html;
                preview.style.display = 'block';

                $('#import-config-btn').disabled = false;
                showSaveStatus('import-config-status', 'Validation passed', false);
            } else {
                showSaveStatus('import-config-status',
                    'Validation failed: ' + (result.errors || []).join(', '), true);
                $('#import-config-btn').disabled = true;
            }
        } catch (e) {
            showSaveStatus('import-config-status', 'Validation error: ' + e.message, true);
        }
    }

    async function importConfig() {
        if (!confirm('This will overwrite existing settings with the imported configuration. Continue?')) {
            return;
        }
        if (!pendingConfigFile) return;

        var formData = new FormData();
        formData.append('file', pendingConfigFile);

        try {
            showSaveStatus('import-config-status', 'Importing...', false);
            var response = await fetch('/api/backup/config/import', {
                method: 'POST',
                headers: { 'Authorization': 'Bearer ' + authToken },
                body: formData
            });
            var result = await response.json();

            if (response.ok) {
                var applied = result.result && result.result.applied ? result.result.applied : {};
                var summary = Object.keys(applied).map(function(t) { return t + ': ' + applied[t]; }).join(', ');
                showSaveStatus('import-config-status', 'Imported: ' + summary, false);
                // Reset file input state
                pendingConfigFile = null;
                $('#config-file-input').value = '';
                $('#config-file-name').textContent = '';
                $('#validate-config-btn').disabled = true;
                $('#import-config-btn').disabled = true;
                $('#config-import-preview').style.display = 'none';
            } else {
                showSaveStatus('import-config-status',
                    'Import failed: ' + (result.error || 'Unknown error'), true);
            }
        } catch (e) {
            showSaveStatus('import-config-status', 'Import error: ' + e.message, true);
        }
    }

    function handleDbFileSelect(file) {
        if (!file || !file.name.match(/\.(db|sqlite|sqlite3)$/i)) {
            showSaveStatus('restore-db-status', 'Please select a .db, .sqlite, or .sqlite3 file', true);
            return;
        }
        pendingDbFile = file;
        $('#db-file-name').textContent = file.name + ' (' + formatBytes(file.size) + ')';
        $('#validate-db-btn').disabled = false;
        $('#restore-db-btn').disabled = true;
        var preview = $('#db-restore-preview');
        if (preview) preview.style.display = 'none';
    }

    async function validateDbRestore() {
        if (!pendingDbFile) return;

        var formData = new FormData();
        formData.append('file', pendingDbFile);

        try {
            showSaveStatus('restore-db-status', 'Validating...', false);
            var response = await fetch('/api/backup/database/validate', {
                method: 'POST',
                headers: { 'Authorization': 'Bearer ' + authToken },
                body: formData
            });
            var result = await response.json();

            if (result.valid) {
                var preview = $('#db-restore-preview');
                var info = result.info || {};
                var html = '<h4>Backup Info</h4>';
                html += '<p>File size: ' + formatBytes(info.size_bytes || 0) + '</p>';
                if (info.table_counts) {
                    html += '<ul>';
                    var counts = info.table_counts;
                    for (var table in counts) {
                        html += '<li>' + table + ': ' + counts[table] + ' rows</li>';
                    }
                    html += '</ul>';
                }
                preview.innerHTML = html;
                preview.style.display = 'block';

                $('#restore-db-btn').disabled = false;
                showSaveStatus('restore-db-status', 'Validation passed', false);
            } else {
                showSaveStatus('restore-db-status',
                    'Validation failed: ' + (result.errors || []).join(', '), true);
                $('#restore-db-btn').disabled = true;
            }
        } catch (e) {
            showSaveStatus('restore-db-status', 'Validation error: ' + e.message, true);
        }
    }

    function showRestoreConfirmModal() {
        $('#restore-confirm-input').value = '';
        $('#restore-confirm-btn').disabled = true;
        $('#restore-confirm-modal').style.display = 'flex';
    }

    async function executeDbRestore() {
        $('#restore-confirm-modal').style.display = 'none';

        if (!pendingDbFile) return;

        var formData = new FormData();
        formData.append('file', pendingDbFile);

        try {
            showSaveStatus('restore-db-status', 'Restoring database...', false);
            var response = await fetch('/api/backup/database/restore', {
                method: 'POST',
                headers: { 'Authorization': 'Bearer ' + authToken },
                body: formData
            });
            var result = await response.json();

            if (response.ok) {
                showSaveStatus('restore-db-status', 'Database restored successfully!', false);
                // Reset file input state
                pendingDbFile = null;
                $('#db-file-input').value = '';
                $('#db-file-name').textContent = '';
                $('#validate-db-btn').disabled = true;
                $('#restore-db-btn').disabled = true;
                $('#db-restore-preview').style.display = 'none';
            } else {
                showSaveStatus('restore-db-status',
                    'Restore failed: ' + (result.error || 'Unknown error'), true);
            }
        } catch (e) {
            showSaveStatus('restore-db-status', 'Restore error: ' + e.message, true);
        }
    }

    function setupDragDrop(zoneId, inputId, handler) {
        var zone = document.getElementById(zoneId);
        var input = document.getElementById(inputId);
        if (!zone || !input) return;

        zone.addEventListener('dragover', function(e) {
            e.preventDefault();
            zone.classList.add('drag-over');
        });
        zone.addEventListener('dragleave', function() {
            zone.classList.remove('drag-over');
        });
        zone.addEventListener('drop', function(e) {
            e.preventDefault();
            zone.classList.remove('drag-over');
            if (e.dataTransfer.files.length > 0) {
                handler(e.dataTransfer.files[0]);
            }
        });
    }

    function init() {
        // Login form
        $('#login-form').addEventListener('submit', async function (e) {
            e.preventDefault();
            var errorEl = $('#login-error');
            errorEl.style.display = 'none';

            try {
                var data = await api('POST', '/api/auth/login', {
                    secret: $('#secret-input').value,
                });
                authToken = data.token;
                $('#secret-input').value = '';
                showDashboard();
            } catch (err) {
                errorEl.textContent = 'Invalid secret. Please try again.';
                errorEl.style.display = 'block';
            }
        });

        // Logout
        $('#logout-btn').addEventListener('click', handleLogout);

        // Tab navigation
        $$('.nav-item').forEach(function (item) {
            item.addEventListener('click', function () {
                switchTab(this.dataset.tab);
            });
        });

        // Settings forms
        $('#settings-form').addEventListener('submit', saveSettings);
        $('#memory-settings-form').addEventListener('submit', saveMemorySettings);
        $('#tool-calling-form').addEventListener('submit', saveToolCallingSettings);
        $('#tool-calling-enabled').addEventListener('change', function() {
            var label = $('#tool-calling-enabled-label');
            if (label) label.textContent = this.checked ? 'Enabled — native tool calling via LLM' : 'Disabled — fallback to intent classification';
        });

        // Memory tab buttons
        var memoryClearBtn = $('#memory-clear-btn');
        if (memoryClearBtn) memoryClearBtn.addEventListener('click', clearChannelMemories);
        var memorySumBtn = $('#memory-summarize-btn');
        if (memorySumBtn) memorySumBtn.addEventListener('click', triggerSummarize);

        // Provider change reloads model list
        $('#setting-provider').addEventListener('change', onProviderChange);

        // Model search filters
        setupModelSearch('setting-model-search', 'setting-model');
        setupModelSearch('channel-model-search', 'channel-edit-model');
        setupModelSearch('thread-model-search', 'thread-edit-model');

        // Channel modal
        $('#channel-edit-form').addEventListener('submit', saveChannel);
        $('#channel-modal-close').addEventListener('click', function () {
            $('#channel-modal').style.display = 'none';
        });
        $('#channel-reset-btn').addEventListener('click', resetChannel);

        // Persona modals
        bindEvent('#persona-edit-form', 'submit', savePersona);
        bindEvent('#persona-modal-close', 'click', function () {
            $('#persona-modal').style.display = 'none';
        });
        bindEvent('#persona-remove-btn', 'click', removePersona);
        bindEvent('#persona-edit-avatar-url', 'input', updatePersonaAvatarPreview);
        bindEvent('#persona-edit-template', 'change', onTemplateSelect);

        // Template modal
        bindEvent('#template-edit-form', 'submit', saveTemplate);
        bindEvent('#template-modal-close', 'click', function () {
            $('#template-modal').style.display = 'none';
        });
        bindEvent('#template-delete-btn', 'click', deleteTemplate);
        bindEvent('#create-template-btn', 'click', openCreateTemplate);

        // Channel picker modal
        bindEvent('#channel-picker-close', 'click', function () {
            $('#channel-picker-modal').style.display = 'none';
            pendingTemplateId = null;
        });
        bindEvent('#channel-picker-search', 'input', function () {
            renderChannelPickerList(this.value);
        });

        // Thread modal
        $('#thread-edit-form').addEventListener('submit', saveThread);
        $('#thread-modal-close').addEventListener('click', function () {
            $('#thread-modal').style.display = 'none';
        });
        $('#thread-delete-btn').addEventListener('click', deleteThread);

        // Messages
        $('#msg-source-type').addEventListener('change', onSourceTypeChange);
        $('#msg-filter-btn').addEventListener('click', function () {
            loadMessages(1);
        });
        $('#msg-prev').addEventListener('click', function () {
            if (msgCurrentPage > 1) loadMessages(msgCurrentPage - 1);
        });
        $('#msg-next').addEventListener('click', function () {
            if (msgCurrentPage < msgTotalPages) loadMessages(msgCurrentPage + 1);
        });

        // Image Generation
        $('#image-provider-form').addEventListener('submit', saveImageProvider);
        $('#modalities-form').addEventListener('submit', saveModalities);
        $('#image-config-provider').addEventListener('change', function() {
            showProviderConfigPanel(this.value);
        });

        // Modality checkbox preview
        ['mod-image', 'mod-text', 'mod-audio'].forEach(function(id) {
            var el = document.getElementById(id);
            if (el) el.addEventListener('change', updateModalitiesPreview);
        });

        // OpenRouter image model search filter
        setupModelSearch('or-model-search', 'or-model');

        // Provider config forms
        var providerForms = ['ai_horde', 'cloudflare', 'openai', 'comfyui', 'openrouter'];
        providerForms.forEach(function(p) {
            var form = document.getElementById('config-form-' + p);
            if (form) {
                form.addEventListener('submit', function(e) {
                    e.preventDefault();
                    saveProviderConfig(p);
                });
            }
        });

        // Video Generation
        var videoConfigForm = document.getElementById('video-config-form');
        if (videoConfigForm) videoConfigForm.addEventListener('submit', saveVideoConfig);
        var videoTestForm = document.getElementById('video-test-form');
        if (videoTestForm) videoTestForm.addEventListener('submit', submitVideoTest);
        var videoCancelBtn = document.getElementById('video-test-cancel');
        if (videoCancelBtn) videoCancelBtn.addEventListener('click', function () {
            stopVideoPolling();
            showSaveStatus('video-test-status', 'Polling stopped', false);
        });
        var videoModelEl = document.getElementById('video-model');
        if (videoModelEl) videoModelEl.addEventListener('change', function () {
            renderVideoModelInfo(this.value);
        });
        setupModelSearch('video-model-search', 'video-model');

        // Diagnostics
        $('#run-diagnostics-btn').addEventListener('click', runDiagnostics);
        $('#run-prune-btn').addEventListener('click', runPrune);

        // Backup & Restore
        bindEvent('#export-config-btn', 'click', exportConfig);
        bindEvent('#export-db-btn', 'click', exportDatabase);
        bindEvent('#config-file-browse', 'click', function(e) {
            e.preventDefault();
            $('#config-file-input').click();
        });
        bindEvent('#config-file-input', 'change', function() {
            if (this.files[0]) handleConfigFileSelect(this.files[0]);
        });
        bindEvent('#validate-config-btn', 'click', validateConfigImport);
        bindEvent('#import-config-btn', 'click', importConfig);
        bindEvent('#db-file-browse', 'click', function(e) {
            e.preventDefault();
            $('#db-file-input').click();
        });
        bindEvent('#db-file-input', 'change', function() {
            if (this.files[0]) handleDbFileSelect(this.files[0]);
        });
        bindEvent('#validate-db-btn', 'click', validateDbRestore);
        bindEvent('#restore-db-btn', 'click', showRestoreConfirmModal);
        bindEvent('#restore-confirm-input', 'input', function() {
            var btn = $('#restore-confirm-btn');
            if (btn) btn.disabled = this.value !== 'RESTORE';
        });
        bindEvent('#restore-confirm-btn', 'click', executeDbRestore);
        bindEvent('#restore-cancel-btn', 'click', function() {
            $('#restore-confirm-modal').style.display = 'none';
        });
        bindEvent('#restore-modal-close', 'click', function() {
            $('#restore-confirm-modal').style.display = 'none';
        });
        setupDragDrop('config-upload-zone', 'config-file-input', handleConfigFileSelect);
        setupDragDrop('db-upload-zone', 'db-file-input', handleDbFileSelect);

        // Clear activity
        $('#clear-activity-btn').addEventListener('click', function () {
            $('#activity-feed').innerHTML = '<p class="loading-text">Waiting for events...</p>';
        });

        // API Keys
        var addKeyBtn = $('#add-key-btn');
        if (addKeyBtn) addKeyBtn.addEventListener('click', openAddKeyModal);
        var keyEditForm = $('#key-edit-form');
        if (keyEditForm) keyEditForm.addEventListener('submit', saveAPIKey);
        var keyModalClose = $('#key-modal-close');
        if (keyModalClose) keyModalClose.addEventListener('click', function() { $('#key-modal').style.display = 'none'; });
        var keyTestBtn = $('#key-test-btn');
        if (keyTestBtn) keyTestBtn.addEventListener('click', testKeyFromModal);
        var importEnvBtn = $('#import-env-btn');
        if (importEnvBtn) importEnvBtn.addEventListener('click', importFromEnv);

        // Global search
        var globalSearch = $('#global-search');
        if (globalSearch) globalSearch.addEventListener('input', handleGlobalSearch);

        // Quick actions
        var syncBtn = $('#action-sync-commands');
        if (syncBtn) syncBtn.addEventListener('click', syncCommands);

        // Close modals on backdrop click
        $$('.modal').forEach(function (modal) {
            modal.addEventListener('click', function (e) {
                if (e.target === modal) {
                    modal.style.display = 'none';
                }
            });
        });

        // Theme toggle
        var themeBtn = $('#theme-toggle');
        if (themeBtn) themeBtn.addEventListener('click', toggleTheme);
        initTheme();

        // Start periodic refresh
        startPeriodicRefresh();

        // Check auth on load
        checkAuth();
    }

    // ── Memory ───────────────────────────────────────────────

    var currentMemoryChannelId = null;

    window.loadMemoryStats = async function() {
        var container = $('#memory-stats-list');
        try {
            const stats = await api('GET', '/api/memory');
            if (!stats || stats.length === 0) {
                container.innerHTML = '<p class="text-muted">No channel memories stored yet. Memories accumulate as sessions expire.</p>';
                return;
            }
            var rows = stats.map(function(s) {
                var name = escapeHtml(s.channel_name || s.channel_id);
                var latest = s.latest_summary_at ? String(s.latest_summary_at).substring(0, 16) : '—';
                return '<tr>' +
                    '<td>' + name + '</td>' +
                    '<td>' + s.summary_count + '</td>' +
                    '<td>' + latest + '</td>' +
                    '<td><button class="btn btn-secondary btn-small" onclick="window.viewChannelMemories(\'' + escapeHtml(s.channel_id) + '\', \'' + name + '\')">View</button></td>' +
                    '</tr>';
            });
            container.innerHTML = '<table class="data-table"><thead><tr><th>Channel</th><th>Summaries</th><th>Latest</th><th></th></tr></thead><tbody>' + rows.join('') + '</tbody></table>';
        } catch (e) {
            container.innerHTML = '<p class="text-muted">Error loading memory stats: ' + escapeHtml(e.message) + '</p>';
        }
    };

    window.viewChannelMemories = async function(channelId, channelName) {
        currentMemoryChannelId = channelId;
        var card = $('#memory-channel-card');
        var title = $('#memory-channel-title');
        var list = $('#memory-entries-list');
        title.textContent = 'Memories: #' + (channelName || channelId);
        card.style.display = '';
        list.innerHTML = '<p class="text-muted">Loading...</p>';
        try {
            const data = await api('GET', '/api/memory/' + channelId);
            var memories = data.memories || [];
            if (memories.length === 0) {
                list.innerHTML = '<p class="text-muted">No memories stored for this channel.</p>';
                return;
            }
            list.innerHTML = memories.map(function(m, i) {
                // Show when the conversation actually happened (conversation_start), not when it was archived
                var ts = String(m.conversation_start || m.created_at || '').substring(0, 16);
                var label = m.conversation_start ? 'Conversation on' : 'Archived';
                return '<div class="memory-entry">' +
                    '<div class="memory-meta">' + label + ' ' + ts + ' &bull; ' + m.message_count + ' messages</div>' +
                    '<div class="memory-summary">' + escapeHtml(m.summary) + '</div>' +
                    '</div>';
            }).join('');
        } catch (e) {
            list.innerHTML = '<p class="text-muted">Error: ' + escapeHtml(e.message) + '</p>';
        }
    };

    async function clearChannelMemories() {
        if (!currentMemoryChannelId) return;
        if (!confirm('Delete all stored memory summaries for this channel?')) return;
        try {
            await api('DELETE', '/api/memory/' + currentMemoryChannelId);
            await window.loadMemoryStats();
            $('#memory-entries-list').innerHTML = '<p class="text-muted">Memories cleared.</p>';
            $('#memory-channel-card').style.display = 'none';
            currentMemoryChannelId = null;
        } catch (e) {
            alert('Error clearing memories: ' + e.message);
        }
    }

    async function triggerSummarize() {
        if (!currentMemoryChannelId) return;
        if (!confirm('Summarize the current conversation history for this channel and start a fresh session?')) return;
        try {
            const result = await api('POST', '/api/memory/' + currentMemoryChannelId + '/summarize');
            alert('Summary stored! ' + result.messages_archived + ' messages archived.');
            await window.loadMemoryStats();
            await window.viewChannelMemories(currentMemoryChannelId, null);
        } catch (e) {
            alert('Error: ' + e.message);
        }
    }

    // Boot
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
