/**
 * Gideon Admin Dashboard - Main Application
 */
(function () {
    'use strict';

    var G = window.Gideon;
    var $ = G.$;
    var $$ = G.$$;
    var api = G.api;
    var escapeHtml = G.escapeHtml;
    var actionAttrs = G.actionAttrs;
    var sanitizeHttpUrl = G.sanitizeHttpUrl;
    var icon = G.icon;
    var toast = G.toast;
    var Modals = G.Modals;
    var confirmDialog = G.confirmDialog;
    var skeleton = G.skeleton;
    var emptyState = G.emptyState;
    var setBusy = G.setBusy;

    // ── State ────────────────────────────────────────────────

    var ws = null;
    var wsTimer = null;
    var wsAttempts = 0;
    var WS_BACKOFF_BASE_MS = 1000;
    var WS_BACKOFF_MAX_MS = 30000;
    var loggedIn = false;
    var currentTab = null;
    var lastOwnWriteAt = 0;

    var modelCache = {};
    var imageModelsCache = null;
    var videoModelsCache = null;

    var msgCurrentPage = 1;
    var msgTotalPages = 1;
    var msgSources = null;

    var allChannelsData = [];
    var personaTemplatesData = [];
    var channelPersonasData = [];
    var currentMemoryChannelId = null;
    var currentMemoryChannelName = null;
    var settingsModel = null;
    var settingsProvider = null;

    var TABS = [
        { id: 'overview', title: 'Overview', group: 'Core', keywords: 'dashboard home status health errors', load: loadOverview },
        { id: 'settings', title: 'Settings', group: 'Core', keywords: 'global model provider prompt memory tool calling', load: loadSettings },
        { id: 'api-keys', title: 'API Keys', group: 'Core', keywords: 'keys secrets tokens audit encrypted', load: function () { loadAPIKeys(); loadAuditLog(); } },
        { id: 'channels', title: 'Channels', group: 'Conversations', keywords: 'channel overrides servers guilds', load: loadChannels },
        { id: 'threads', title: 'Threads', group: 'Conversations', keywords: 'thread conversations', load: loadThreads },
        { id: 'personas', title: 'Personas', group: 'Conversations', keywords: 'persona templates webhook identity avatar', load: loadPersonas },
        { id: 'memory', title: 'Memory', group: 'Conversations', keywords: 'memory summaries history', load: function () { loadMemoryStats(); } },
        { id: 'messages', title: 'Messages', group: 'Conversations', keywords: 'messages history log stored', load: loadMessageSources },
        { id: 'image-gen', title: 'Image Gen', group: 'Generation', keywords: 'image dream dall-e horde comfyui', load: loadImageSettings },
        { id: 'video-gen', title: 'Video Gen', group: 'Generation', keywords: 'video clips generate', load: loadVideoSettings },
        { id: 'diagnostics', title: 'Diagnostics', group: 'Operations', keywords: 'health check prune database providers', load: function () {} },
        { id: 'backup', title: 'Backup & Restore', group: 'Operations', keywords: 'export import restore database config json sqlite', load: function () {} },
        { id: 'activity', title: 'Live Activity', group: 'Operations', keywords: 'events websocket realtime feed', load: function () {} }
    ];

    var SETTINGS_FIELD_MAP = {
        global_provider: 'setting-provider',
        global_model: 'setting-model',
        max_channel_history: 'setting-memory',
        time_window_hours: 'setting-window',
        prune_frequency_hours: 'setting-prune',
        global_system_prompt: 'setting-system-prompt',
        session_timeout_hours: 'setting-session-timeout',
        max_memory_summaries: 'setting-max-summaries',
        memory_summary_enabled: 'setting-memory-summary',
        tool_calling_enabled: 'tool-calling-enabled',
        tool_calling_max_iterations: 'tool-calling-max-iterations'
    };

    var CHANNEL_FIELD_MAP = {
        model: 'channel-edit-model',
        provider: 'channel-edit-provider',
        system_prompt: 'channel-edit-prompt',
        session_timeout_hours: 'channel-edit-timeout',
        memory_summary_enabled: 'channel-edit-summary',
        max_memory_summaries: 'channel-edit-max-summaries'
    };

    var OVERRIDE_LABELS = {
        model: 'Model',
        provider: 'Provider',
        system_prompt: 'Prompt',
        session_timeout_hours: 'Timeout',
        memory_summary_enabled: 'Summaries',
        max_memory_summaries: 'Max summaries'
    };

    var formTrackers = {};

    // ── Helpers ──────────────────────────────────────────────

    var store = {
        get: function (key) {
            try { return localStorage.getItem(key); } catch (e) { return null; }
        },
        set: function (key, value) {
            try { localStorage.setItem(key, value); } catch (e) { /* storage unavailable */ }
        }
    };

    function bindEvent(selector, event, handler) {
        var el = document.querySelector(selector);
        if (el) el.addEventListener(event, handler);
    }

    function formatUptime(seconds) {
        seconds = Number(seconds) || 0;
        var d = Math.floor(seconds / 86400);
        var h = Math.floor((seconds % 86400) / 3600);
        var m = Math.floor((seconds % 3600) / 60);
        if (d > 0) return d + 'd ' + h + 'h';
        if (h > 0) return h + 'h ' + m + 'm';
        if (m > 0) return m + 'm';
        return Math.floor(seconds) + 's';
    }

    function formatBytes(bytes) {
        bytes = Number(bytes) || 0;
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
        if (isNaN(d.getTime())) return String(str);
        return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }

    function timeAgo(value) {
        var d = new Date(value);
        if (isNaN(d.getTime())) return '';
        var s = Math.round((Date.now() - d.getTime()) / 1000);
        if (s < 45) return 'just now';
        var m = Math.round(s / 60);
        if (m < 60) return m + 'm ago';
        var h = Math.round(m / 60);
        if (h < 48) return h + 'h ago';
        return Math.round(h / 24) + 'd ago';
    }

    function formatCountdown(totalSeconds) {
        var m = Math.floor(totalSeconds / 60);
        var s = totalSeconds % 60;
        return m + ':' + (s < 10 ? '0' : '') + s;
    }

    function truncate(text, max) {
        text = String(text || '');
        return text.length > max ? text.substring(0, max - 1) + '…' : text;
    }

    function showSaveStatus(elementId, message, isError) {
        var el = document.getElementById(elementId);
        if (!el) return;
        el.textContent = message;
        el.className = 'save-status ' + (isError ? 'error' : 'success');
        clearTimeout(el._timer);
        el._timer = setTimeout(function () { el.textContent = ''; el.className = 'save-status'; }, isError ? 10000 : 4000);
    }

    function markOwnWrite() { lastOwnWriteAt = Date.now(); }

    async function reloadKeepingFocus(opener, loader) {
        var restore = G.captureFocus(opener);
        await loader();
        restore();
    }

    function describeError(err) {
        if (err && err.status === 429 && err.retryAfter) {
            return (err.message || 'Too many requests') + ' - try again in ' + err.retryAfter + 's';
        }
        return (err && err.message) || 'Unexpected error';
    }

    function handleFormError(form, statusId, err, fieldMap) {
        if (err && err.status === 401) return;
        if (err && err.status === 400 && err.details && !Array.isArray(err.details)) {
            var unmatched = G.applyFieldErrors(form, err.details, fieldMap);
            var msg = 'Fix the highlighted fields';
            if (unmatched.length) msg += ' (' + unmatched.join('; ') + ')';
            showSaveStatus(statusId, msg, true);
            return;
        }
        showSaveStatus(statusId, 'Error: ' + describeError(err), true);
    }

    function startCountdown(seconds, onTick, onDone) {
        var end = Date.now() + seconds * 1000;
        var timer = null;
        function tick() {
            var remaining = Math.max(0, Math.ceil((end - Date.now()) / 1000));
            onTick(remaining);
            if (remaining <= 0) {
                clearInterval(timer);
                if (onDone) onDone();
            }
        }
        timer = setInterval(tick, 500);
        tick();
        return function cancel() { clearInterval(timer); };
    }

    function trackSettingsForms() {
        formTrackers.settings = G.trackForm($('#settings-form'), 'AI configuration');
        formTrackers.memory = G.trackForm($('#memory-settings-form'), 'Memory system');
        formTrackers.tools = G.trackForm($('#tool-calling-form'), 'Tool calling');
        formTrackers.imageProvider = G.trackForm($('#image-provider-form'), 'Active image provider');
        formTrackers.modalities = G.trackForm($('#modalities-form'), 'OpenRouter modalities');
        formTrackers.video = G.trackForm($('#video-config-form'), 'Video defaults');
        var labels = { ai_horde: 'AI Horde config', cloudflare: 'Cloudflare config', openai: 'OpenAI config', comfyui: 'ComfyUI config', openrouter: 'OpenRouter config' };
        Object.keys(labels).forEach(function (p) {
            formTrackers['config-' + p] = G.trackForm(document.getElementById('config-form-' + p), labels[p]);
        });
    }

    function snapshot() {
        for (var i = 0; i < arguments.length; i++) {
            if (formTrackers[arguments[i]]) formTrackers[arguments[i]].snapshot();
        }
    }

    function clearCaches() {
        modelCache = {};
        imageModelsCache = null;
        videoModelsCache = null;
        msgSources = null;
    }

    // ── Model select helpers ─────────────────────────────────

    async function fetchModels(provider) {
        if (modelCache[provider]) return { models: modelCache[provider] };
        try {
            var models = await api('GET', '/api/models/' + encodeURIComponent(provider));
            if (!Array.isArray(models)) models = [];
            modelCache[provider] = models;
            return { models: models };
        } catch (e) {
            return { models: [], error: e.message || 'Request failed' };
        }
    }

    function setFieldNote(selectEl, message, tone) {
        var host = selectEl.closest('.field') || selectEl.parentNode;
        var note = host.querySelector('.field-note[data-note-for="' + selectEl.id + '"]');
        if (!message) {
            if (note) note.remove();
            return;
        }
        if (!note) {
            note = document.createElement('div');
            note.setAttribute('data-note-for', selectEl.id);
            note.setAttribute('role', 'status');
            host.appendChild(note);
        }
        note.className = 'field-note is-' + (tone || 'warning');
        note.textContent = message;
    }

    function populateModelSelect(selectEl, models, currentValue, emptyLabel, failed) {
        var html = '';
        var inList = !!currentValue && models.indexOf(currentValue) !== -1;
        if (emptyLabel) {
            html += '<option value=""' + (currentValue ? '' : ' selected') + '>' + escapeHtml(emptyLabel) + '</option>';
        }
        if (currentValue && !inList) {
            var why = failed ? 'model list unavailable' : 'not in provider list';
            html += '<option value="' + escapeHtml(currentValue) + '" selected>' + escapeHtml(currentValue + ' (current, ' + why + ')') + '</option>';
        }
        models.forEach(function (model) {
            var isCurrent = model === currentValue;
            html += '<option value="' + escapeHtml(model) + '"' + (isCurrent ? ' selected' : '') + '>' +
                escapeHtml(isCurrent ? model + ' (current)' : model) + '</option>';
        });
        selectEl.innerHTML = html;
        if (currentValue) selectEl.value = currentValue;
        revealSelected(selectEl);
    }

    function revealSelected(selectEl) {
        var opt = selectEl.options[selectEl.selectedIndex];
        if (opt && selectEl.size > 1) {
            selectEl.scrollTop = Math.max(0, opt.offsetTop - selectEl.clientHeight / 2);
        }
    }

    function setupModelSearch(searchId, selectId) {
        var searchEl = document.getElementById(searchId);
        var selectEl = document.getElementById(selectId);
        if (!searchEl || !selectEl) return;
        searchEl.addEventListener('input', function () {
            var filter = searchEl.value.toLowerCase();
            for (var i = 0; i < selectEl.options.length; i++) {
                var opt = selectEl.options[i];
                var text = (opt.value + ' ' + opt.textContent).toLowerCase();
                opt.hidden = !!filter && text.indexOf(filter) === -1 && !opt.selected;
            }
        });
        searchEl.addEventListener('keydown', function (e) {
            if (e.key === 'ArrowDown') { e.preventDefault(); selectEl.focus(); }
        });
    }

    async function loadModelsForProvider(provider, selectId, currentValue, emptyLabel) {
        var selectEl = document.getElementById(selectId);
        if (!selectEl) return { ok: false, models: [] };
        selectEl.innerHTML = '<option value="">Loading models...</option>';
        selectEl.setAttribute('aria-busy', 'true');
        var result = await fetchModels(provider);
        selectEl.removeAttribute('aria-busy');
        var failed = !!result.error;
        populateModelSelect(selectEl, result.models, currentValue, emptyLabel, failed);
        if (failed) {
            setFieldNote(selectEl, 'Could not load the ' + provider + ' model list (' + result.error + '). ' +
                (currentValue ? 'The current model is kept; you can still save.' : 'Try again later.'), 'warning');
        } else if (currentValue && result.models.indexOf(currentValue) === -1) {
            setFieldNote(selectEl, 'The current model is not in the ' + provider + ' list. It may have been retired.', 'warning');
        } else {
            setFieldNote(selectEl, '');
        }
        return { ok: !failed, models: result.models };
    }

    // ── Auth ─────────────────────────────────────────────────

    var lockoutCancel = null;
    var expiredHandled = false;

    async function checkAuth() {
        try {
            var data = await api('GET', '/api/auth/check', undefined, { noAuthRedirect: true });
            if (data && data.authenticated) {
                showDashboard();
                return;
            }
            showLogin();
        } catch (e) {
            showLogin();
            if (e && e.status === 0) setLoginError('Cannot reach the server. Check your connection and reload.', 'danger');
        }
    }

    function setLoginError(message, tone) {
        var box = $('#login-error');
        box.textContent = message || '';
        box.hidden = !message;
        box.className = 'form-alert' + (tone ? ' is-' + tone : '');
    }

    function showLogin(message) {
        loggedIn = false;
        $('#boot').hidden = true;
        $('#dashboard').hidden = true;
        $('#login-screen').hidden = false;
        disconnectWs();
        clearCaches();
        stopVideoPolling();
        if (message) setLoginError(message, 'info');
        setTimeout(function () {
            var input = $('#secret-input');
            if (input && !input.disabled) input.focus();
        }, 0);
    }

    function showDashboard() {
        loggedIn = true;
        expiredHandled = false;
        $('#boot').hidden = true;
        $('#login-screen').hidden = true;
        $('#dashboard').hidden = false;
        setLoginError('');
        activateTab(tabFromHash() || 'overview', { initial: true });
        connectWs();
    }

    async function handleLogout() {
        try { await api('POST', '/api/auth/logout', undefined, { noAuthRedirect: true }); } catch (e) { /* already signed out */ }
        showLogin();
    }

    function handleSessionExpired() {
        if (!loggedIn || expiredHandled) return;
        expiredHandled = true;
        showLogin('Your session expired. Please sign in again.');
    }

    function startLoginLockout(seconds) {
        var submit = $('#login-submit');
        var box = $('#login-error');
        if (lockoutCancel) lockoutCancel();
        submit.disabled = true;
        box.className = 'form-alert is-warning';
        box.hidden = false;
        box.innerHTML = '';
        var lead = document.createElement('span');
        lead.textContent = 'Too many failed attempts. Sign-in is locked for ' + seconds + ' seconds. Try again in ';
        var counter = document.createElement('strong');
        counter.setAttribute('aria-hidden', 'true');
        counter.className = 'countdown';
        box.appendChild(lead);
        box.appendChild(counter);
        lockoutCancel = startCountdown(seconds, function (remaining) {
            counter.textContent = formatCountdown(remaining);
            submit.textContent = remaining > 0 ? 'Try again in ' + formatCountdown(remaining) : 'Sign in';
        }, function () {
            lockoutCancel = null;
            submit.disabled = false;
            submit.textContent = 'Sign in';
            setLoginError('You can try again now.', 'info');
            $('#secret-input').focus();
        });
    }

    async function onLoginSubmit(e) {
        e.preventDefault();
        var submit = $('#login-submit');
        var input = $('#secret-input');
        if (submit.disabled) return;
        if (!input.value) {
            setLoginError('Enter the dashboard secret.', 'danger');
            input.focus();
            return;
        }
        setLoginError('');
        setBusy(submit, true);
        try {
            await api('POST', '/api/auth/login', { secret: input.value }, { noAuthRedirect: true });
            input.value = '';
            setBusy(submit, false);
            showDashboard();
        } catch (err) {
            setBusy(submit, false);
            if (err.status === 429) {
                startLoginLockout(err.retryAfter || Number(err.data.retry_after) || 60);
            } else if (err.status === 401) {
                var left = err.data && err.data.attempts_remaining;
                var msg = 'Invalid secret.';
                if (typeof left === 'number') {
                    msg += left > 0 ? ' ' + left + (left === 1 ? ' attempt' : ' attempts') + ' remaining before a temporary lockout.' : ' No attempts remaining.';
                } else {
                    msg += ' Please try again.';
                }
                setLoginError(msg, 'danger');
                input.select();
            } else {
                setLoginError(describeError(err), 'danger');
            }
        }
    }

    // ── Navigation ───────────────────────────────────────────

    function tabById(id) {
        for (var i = 0; i < TABS.length; i++) if (TABS[i].id === id) return TABS[i];
        return null;
    }

    function tabFromHash() {
        var id = location.hash.replace(/^#/, '').split('/')[0];
        return tabById(id) ? id : null;
    }

    async function navigate(tabId) {
        if (!tabById(tabId)) tabId = 'overview';
        if (currentTab && tabId !== currentTab) {
            var dirty = G.dirtyForms($('#tab-' + currentTab));
            if (dirty.length) {
                var ok = await confirmDialog({
                    title: 'Discard unsaved changes?',
                    message: 'These sections have changes that have not been saved:',
                    details: dirty.map(function (d) { return d.label; }),
                    confirmLabel: 'Discard changes',
                    cancelLabel: 'Stay on this page'
                });
                if (!ok) {
                    if (location.hash !== '#' + currentTab) history.replaceState(null, '', '#' + currentTab);
                    return false;
                }
            }
        }
        activateTab(tabId);
        return true;
    }

    function activateTab(tabId, opts) {
        opts = opts || {};
        var tab = tabById(tabId) || tabById('overview');
        var wasCurrent = currentTab === tab.id;
        currentTab = tab.id;

        $$('.nav-item').forEach(function (item) {
            var active = item.dataset.tab === tab.id;
            item.classList.toggle('active', active);
            if (active) item.setAttribute('aria-current', 'page'); else item.removeAttribute('aria-current');
        });
        $$('.tab-content').forEach(function (section) {
            section.classList.toggle('active', section.id === 'tab-' + tab.id);
        });
        var activeItem = $('.nav-item.active');
        var group = activeItem ? activeItem.closest('.nav-group') : null;
        if (group) setGroupExpanded(group, true);

        $('#page-title').textContent = tab.title;
        document.title = tab.title + ' · Gideon';
        if (location.hash !== '#' + tab.id) {
            if (opts.initial) history.replaceState(null, '', '#' + tab.id); else location.hash = tab.id;
        }
        closeDrawer(false);
        if (!opts.initial) {
            var main = $('#main');
            if (main) main.scrollTop = 0;
            window.scrollTo(0, 0);
        }

        var dirty = wasCurrent ? G.dirtyForms($('#tab-' + tab.id)) : [];
        if (!dirty.length) tab.load();
        if (opts.initial && tab.id !== 'overview') loadOverview();
    }

    function reloadCurrentTab() {
        var tab = tabById(currentTab);
        if (tab) tab.load();
        if (currentTab !== 'overview') loadOverview();
    }

    function onHashChange() {
        if (!loggedIn) return;
        var id = tabFromHash();
        if (id && id !== currentTab) navigate(id);
    }

    function setGroupExpanded(group, expanded) {
        group.classList.toggle('collapsed', !expanded);
        var btn = group.querySelector('.nav-group-toggle');
        if (btn) btn.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    }

    function initNavGroups() {
        var saved = {};
        try { saved = JSON.parse(store.get('gideon-nav-groups') || '{}') || {}; } catch (e) { saved = {}; }
        $$('.nav-group').forEach(function (group) {
            var key = group.dataset.group;
            setGroupExpanded(group, saved[key] !== false);
            group.querySelector('.nav-group-toggle').addEventListener('click', function () {
                var expand = group.classList.contains('collapsed');
                setGroupExpanded(group, expand);
                saved[key] = expand;
                store.set('gideon-nav-groups', JSON.stringify(saved));
            });
        });
    }

    function setSidebarCollapsed(collapsed) {
        $('#dashboard').classList.toggle('sidebar-collapsed', collapsed);
        var btn = $('#sidebar-collapse');
        btn.setAttribute('aria-pressed', collapsed ? 'true' : 'false');
        var label = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
        btn.setAttribute('aria-label', label);
        btn.title = label;
        store.set('gideon-sidebar', collapsed ? 'collapsed' : 'expanded');
    }

    function openDrawer() {
        document.body.classList.add('drawer-open');
        $('#sidebar-scrim').hidden = false;
        $('#sidebar-open').setAttribute('aria-expanded', 'true');
        var target = $('.nav-item.active') || $('.nav-item');
        if (target) setTimeout(function () { target.focus(); }, 30);
    }

    function closeDrawer(returnFocus) {
        if (!document.body.classList.contains('drawer-open')) return;
        document.body.classList.remove('drawer-open');
        $('#sidebar-scrim').hidden = true;
        $('#sidebar-open').setAttribute('aria-expanded', 'false');
        if (returnFocus) $('#sidebar-open').focus();
    }

    function focusField(tabId, targetId) {
        return navigate(tabId).then(function (ok) {
            if (!ok) return;
            var el = document.getElementById(targetId);
            if (!el) return;
            var panel = el.closest('.provider-config-panel');
            if (panel) {
                var select = $('#image-config-provider');
                select.value = panel.id.replace(/^config-/, '');
                showProviderConfigPanel(select.value);
            }
            setTimeout(function () {
                el.scrollIntoView({ block: 'center' });
                el.focus();
                var field = el.closest('.field') || el;
                field.classList.add('flash');
                setTimeout(function () { field.classList.remove('flash'); }, 1800);
            }, 60);
        });
    }

    var paletteOpener = function () { return $('#global-search'); };

    async function openChannelFromPalette(channelId) {
        if (await navigate('channels')) editChannel(channelId, paletteOpener());
    }

    async function openThreadFromPalette(threadId) {
        if (await navigate('threads')) editThread(threadId, paletteOpener());
    }

    async function openTemplateFromPalette(templateId) {
        if (!(await navigate('personas'))) return;
        if (!personaTemplatesData.length) await loadPersonaTemplates();
        editTemplate(templateId, paletteOpener());
    }

    async function openPersonaFromPalette(channelId) {
        if (await navigate('personas')) editPersona(channelId, paletteOpener());
    }

    // ── Theme ────────────────────────────────────────────────

    function currentTheme() {
        return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
    }

    function applyTheme(theme) {
        document.documentElement.setAttribute('data-theme', theme);
        var btn = $('#theme-toggle');
        var label = theme === 'light' ? 'Switch to dark theme' : 'Switch to light theme';
        btn.setAttribute('aria-label', label);
        btn.title = label;
    }

    function toggleTheme() {
        var next = currentTheme() === 'light' ? 'dark' : 'light';
        store.set('gideon-theme', next);
        applyTheme(next);
    }

    // ── Overview ─────────────────────────────────────────────

    function setStat(selector, value) {
        var el = $(selector);
        el.textContent = value;
        el.classList.remove('skeleton-text');
    }

    function healthDot(state) {
        return '<span class="status-dot is-' + escapeHtml(state) + '"></span>';
    }

    function renderHealth(health) {
        var providersBox = $('#health-providers');
        var keysBox = $('#health-keys');
        var errorsBox = $('#health-errors');

        var providers = health && health.providers ? health.providers : {};
        var names = Object.keys(providers);
        var anyProviderError = false;
        if (!names.length) {
            providersBox.innerHTML = '<p class="muted">No providers configured.</p>';
        } else {
            providersBox.innerHTML = '<ul class="health-list">' + names.map(function (name) {
                var status = providers[name];
                var state = status === 'ok' ? 'ok' : (status === 'configured' ? 'warn' : 'danger');
                var label = status === 'ok' ? 'Connected' : (status === 'configured' ? 'Configured' : 'Error');
                if (status !== 'ok' && status !== 'configured') anyProviderError = true;
                var title = status === 'configured' ? ' title="Credentials present, not verified"' : '';
                return '<li' + title + '>' + healthDot(state) + '<span class="health-name">' + escapeHtml(name) + '</span><span class="health-state is-' + state + '">' + label + '</span></li>';
            }).join('') + '</ul>';
        }
        $('#health-providers-card').classList.toggle('has-issue', anyProviderError);

        var keys = health ? health.api_keys : null;
        if (!keys) {
            keysBox.innerHTML = '<p class="muted">Key management is not configured. Set <code class="code">ENCRYPTION_MASTER_KEY</code> to store keys encrypted.</p>';
            $('#health-keys-card').classList.remove('has-issue');
        } else {
            keysBox.innerHTML =
                '<div class="metric-row"><div class="metric"><span class="metric-value">' + escapeHtml(keys.total) + '</span><span class="metric-label">Total</span></div>' +
                '<div class="metric' + (keys.invalid > 0 ? ' is-danger' : '') + '"><span class="metric-value">' + escapeHtml(keys.invalid) + '</span><span class="metric-label">Invalid</span></div>' +
                '<div class="metric' + (keys.untested > 0 ? ' is-warning' : '') + '"><span class="metric-value">' + escapeHtml(keys.untested) + '</span><span class="metric-label">Untested</span></div></div>' +
                '<a class="inline-link" href="#api-keys">Manage keys</a>';
            $('#health-keys-card').classList.toggle('has-issue', keys.invalid > 0);
        }

        var count = health ? Number(health.recent_error_count) || 0 : 0;
        errorsBox.innerHTML =
            '<div class="metric-row"><div class="metric' + (count > 0 ? ' is-danger' : ' is-ok') + '"><span class="metric-value">' + count + '</span><span class="metric-label">' +
            (count === 1 ? 'error' : 'errors') + ' logged</span></div></div>';
        $('#health-errors-card').classList.toggle('has-issue', count > 0);
    }

    async function loadOverview() {
        var errBox = $('#overview-error');
        try {
            var data = await api('GET', '/api/overview');
            errBox.hidden = true;
            setStat('#stat-guilds', data.guilds);
            setStat('#stat-messages', data.message_count >= 0 ? data.message_count : 'N/A');
            setStat('#stat-threads', data.thread_count >= 0 ? data.thread_count : 'N/A');
            setStat('#stat-channels', data.channel_config_count);
            setStat('#stat-latency', data.latency_ms !== null && data.latency_ms !== undefined ? data.latency_ms : 'N/A');
            setStat('#stat-uptime', formatUptime(data.bot_uptime_seconds != null ? data.bot_uptime_seconds : data.uptime_seconds));
            $('#info-bot-name').textContent = data.bot_name;
            $('#info-bot-id').textContent = data.bot_id;
            $('#info-model').textContent = data.global_model;
            $('#info-provider').textContent = data.global_provider;
            $('#info-db-size').textContent = formatBytes(data.database_size_bytes);
            renderHealth(data.health);
            setBotStatus('online', 'Online');
            $('#overview-updated').textContent = 'Updated ' + formatTime(new Date());
        } catch (e) {
            if (e.status === 401) return;
            errBox.textContent = 'Could not load the overview: ' + describeError(e);
            errBox.hidden = false;
            errBox.className = 'form-alert is-danger';
            setBotStatus('offline', 'Unreachable');
            $$('.stat-value.skeleton-text').forEach(function (el) { el.textContent = '--'; el.classList.remove('skeleton-text'); });
        }
        loadRecentErrors();
    }

    function setBotStatus(state, text) {
        var badge = $('#bot-status-badge');
        badge.dataset.state = state;
        badge.querySelector('.bot-status-text').textContent = text;
    }

    async function loadRecentErrors() {
        var box = $('#recent-errors');
        try {
            var rows = await api('GET', '/api/logs/errors?limit=10');
            if (!Array.isArray(rows) || !rows.length) {
                box.innerHTML = emptyState('check-circle', 'No recent errors', 'The bot has not logged any errors since it started.');
                return;
            }
            box.innerHTML = '<ul class="error-list">' + rows.map(function (row) {
                var level = String(row.level || 'ERROR');
                return '<li class="error-item">' +
                    '<div class="error-meta">' +
                    '<span class="badge badge-danger">' + escapeHtml(level) + '</span>' +
                    '<span class="error-logger mono">' + escapeHtml(row.logger || '') + '</span>' +
                    '<time class="error-time muted" datetime="' + escapeHtml(row.time) + '" title="' + escapeHtml(formatDateTime(row.time)) + '">' + escapeHtml(timeAgo(row.time)) + '</time>' +
                    '</div>' +
                    '<div class="error-message">' + escapeHtml(row.message) + '</div>' +
                    '</li>';
            }).join('') + '</ul>';
        } catch (e) {
            if (e.status === 401) return;
            box.innerHTML = '<div class="pad muted">Error log unavailable: ' + escapeHtml(describeError(e)) + '</div>';
        }
    }

    // ── Quick actions ────────────────────────────────────────

    async function syncCommands() {
        var btn = $('#action-sync-commands');
        var original = btn.innerHTML;
        setBusy(btn, true);
        try {
            await api('POST', '/api/commands/sync');
            toast.success('Slash commands synced. Discord can take a few minutes to show changes.', { title: 'Commands synced' });
            showSaveStatus('sync-commands-status', 'Synced at ' + formatTime(new Date()), false);
            setBusy(btn, false);
        } catch (e) {
            if (e.status === 401) return;
            if (e.status === 429) {
                var wait = e.retryAfter || 60;
                toast.warning('A sync ran recently. You can sync again in ' + wait + ' seconds.', { title: 'Sync rate limited' });
                startCountdown(wait, function (remaining) {
                    btn.textContent = 'Sync again in ' + remaining + 's';
                }, function () {
                    btn.innerHTML = original;
                    setBusy(btn, false);
                });
            } else {
                toast.error(describeError(e), { title: 'Sync failed' });
                setBusy(btn, false);
            }
        }
    }

    // ── Settings ─────────────────────────────────────────────

    async function loadSettings() {
        var data;
        try {
            data = await api('GET', '/api/settings');
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Could not load settings' });
            return;
        }
        var provider = data.global_provider || 'openrouter';
        settingsProvider = provider;
        settingsModel = data.global_model || '';

        $('#setting-provider').value = provider;
        $('#setting-memory').value = data.max_channel_history || 35;
        $('#setting-window').value = data.time_window_hours || 48;
        $('#setting-prune').value = data.prune_frequency_hours || 24;
        $('#setting-system-prompt').value = data.global_system_prompt || '';

        $('#setting-session-timeout').value = data.session_timeout_hours || 24;
        $('#setting-max-summaries').value = data.max_memory_summaries || 10;
        $('#setting-memory-summary').value = data.memory_summary_enabled !== false ? 'true' : 'false';

        var tcEnabled = data.tool_calling_enabled !== false;
        $('#tool-calling-enabled').checked = tcEnabled;
        updateToolCallingLabel();
        $('#tool-calling-max-iterations').value = data.tool_calling_max_iterations || 3;

        G.clearFieldErrors($('#tab-settings'));
        var searchEl = $('#setting-model-search');
        if (searchEl) searchEl.value = '';
        await loadModelsForProvider(provider, 'setting-model', settingsModel, null);
        snapshot('settings', 'memory', 'tools');
    }

    function updateToolCallingLabel() {
        var label = $('#tool-calling-enabled-label');
        if (label) label.textContent = $('#tool-calling-enabled').checked ? 'Enabled - native tool calling via LLM' : 'Disabled - fallback to intent classification';
    }

    function readInt(id) {
        var el = document.getElementById(id);
        var raw = String(el.value).trim();
        var value = /^-?\d+$/.test(raw) ? parseInt(raw, 10) : NaN;
        if (isNaN(value)) {
            G.setFieldError(el, 'Enter a whole number');
            return null;
        }
        return value;
    }

    async function saveSettings(e) {
        e.preventDefault();
        var form = e.currentTarget;
        G.clearFieldErrors(form);
        var modelEl = $('#setting-model');
        var model = modelEl.value;
        var body = {
            global_provider: $('#setting-provider').value,
            global_model: model,
            max_channel_history: readInt('setting-memory'),
            time_window_hours: readInt('setting-window'),
            prune_frequency_hours: readInt('setting-prune'),
            global_system_prompt: $('#setting-system-prompt').value || null
        };
        var invalid = false;
        ['max_channel_history', 'time_window_hours', 'prune_frequency_hours'].forEach(function (k) {
            if (body[k] === null) invalid = true;
        });
        if (!model) {
            G.setFieldError(modelEl, 'Select a model');
            invalid = true;
        } else if (body.global_provider !== settingsProvider && (modelCache[body.global_provider] || []).indexOf(model) === -1 && modelCache[body.global_provider]) {
            G.setFieldError(modelEl, 'This model is not available for the selected provider. Choose one from the list.');
            invalid = true;
        }
        if (invalid) {
            showSaveStatus('settings-save-status', 'Fix the highlighted fields', true);
            return;
        }
        var btn = e.submitter;
        setBusy(btn, true);
        try {
            await api('PUT', '/api/settings', body);
            markOwnWrite();
            settingsProvider = body.global_provider;
            settingsModel = model;
            showSaveStatus('settings-save-status', 'Settings saved', false);
            snapshot('settings');
        } catch (err) {
            handleFormError(form, 'settings-save-status', err, SETTINGS_FIELD_MAP);
        }
        setBusy(btn, false);
    }

    async function saveMemorySettings(e) {
        e.preventDefault();
        var form = e.currentTarget;
        G.clearFieldErrors(form);
        var timeout = readInt('setting-session-timeout');
        var maxSummaries = readInt('setting-max-summaries');
        if (timeout === null || maxSummaries === null) {
            showSaveStatus('memory-settings-save-status', 'Fix the highlighted fields', true);
            return;
        }
        var btn = e.submitter;
        setBusy(btn, true);
        try {
            await api('PUT', '/api/settings', {
                session_timeout_hours: timeout,
                max_memory_summaries: maxSummaries,
                memory_summary_enabled: $('#setting-memory-summary').value === 'true'
            });
            markOwnWrite();
            showSaveStatus('memory-settings-save-status', 'Memory settings saved', false);
            snapshot('memory');
        } catch (err) {
            handleFormError(form, 'memory-settings-save-status', err, SETTINGS_FIELD_MAP);
        }
        setBusy(btn, false);
    }

    async function saveToolCallingSettings(e) {
        e.preventDefault();
        var form = e.currentTarget;
        G.clearFieldErrors(form);
        var iterations = readInt('tool-calling-max-iterations');
        if (iterations === null) {
            showSaveStatus('tool-calling-save-status', 'Fix the highlighted fields', true);
            return;
        }
        var btn = e.submitter;
        setBusy(btn, true);
        try {
            await api('PUT', '/api/settings', {
                tool_calling_enabled: $('#tool-calling-enabled').checked,
                tool_calling_max_iterations: iterations
            });
            markOwnWrite();
            showSaveStatus('tool-calling-save-status', 'Tool calling settings saved', false);
            snapshot('tools');
        } catch (err) {
            handleFormError(form, 'tool-calling-save-status', err, SETTINGS_FIELD_MAP);
        }
        setBusy(btn, false);
    }

    async function onProviderChange() {
        var provider = $('#setting-provider').value;
        var current = $('#setting-model').value || settingsModel;
        delete modelCache[provider];
        var result = await loadModelsForProvider(provider, 'setting-model', current, null);
        if (current && result.ok && result.models.indexOf(current) === -1) {
            setFieldNote($('#setting-model'), 'The current model is not available for ' + provider + '. Pick a model from the list before saving.', 'warning');
        }
    }

    // ── Channels ─────────────────────────────────────────────

    async function loadChannels() {
        var container = $('#channels-list');
        if (!allChannelsData.length) container.innerHTML = skeleton('rows', 5);
        try {
            allChannelsData = await api('GET', '/api/channels/all');
            renderChannels();
        } catch (e) {
            if (e.status === 401) return;
            container.innerHTML = emptyState('alert-triangle', 'Could not load channels', describeError(e));
        }
    }

    function renderChannels() {
        var container = $('#channels-list');
        var channels = allChannelsData;

        if (channels.length === 0) {
            container.innerHTML = emptyState('hash', 'No channels found', 'Is the bot connected to any servers?');
            return;
        }

        var filterText = ($('#channel-filter-input').value || '').toLowerCase().trim();
        var filterOverride = $('#channel-filter-override').value;

        var filtered = channels.filter(function (ch) {
            if (filterText) {
                var searchable = (ch.channel_name || '').toLowerCase() + ' ' +
                    (ch.guild_name || '').toLowerCase() + ' ' +
                    (ch.model || '').toLowerCase() + ' ' +
                    (ch.category || '').toLowerCase();
                if (searchable.indexOf(filterText) === -1) return false;
            }
            if (filterOverride === 'customized' && !ch.has_override) return false;
            if (filterOverride === 'default' && ch.has_override) return false;
            return true;
        });

        if (filtered.length === 0) {
            container.innerHTML = emptyState('search', 'No channels match', 'Try a different search or status filter.');
            return;
        }

        var guilds = {};
        filtered.forEach(function (ch) {
            var key = ch.guild_name || 'Unknown server';
            if (!guilds[key]) guilds[key] = [];
            guilds[key].push(ch);
        });

        var html = '';
        Object.keys(guilds).sort().forEach(function (guildName) {
            var list = guilds[guildName];
            var customCount = list.filter(function (c) { return c.has_override; }).length;

            html += '<div class="card"><div class="card-header"><h2 class="card-title">' + escapeHtml(guildName) + '</h2>' +
                '<span class="badge badge-info">' + list.length + (list.length === 1 ? ' channel' : ' channels') + (customCount ? ', ' + customCount + ' customized' : '') + '</span></div>' +
                '<div class="card-body flush"><div class="table-wrap"><table class="data-table"><thead><tr>' +
                '<th scope="col">Channel</th><th scope="col">Category</th><th scope="col">Model</th><th scope="col">Provider</th><th scope="col">Status</th><th scope="col"><span class="sr-only">Actions</span></th>' +
                '</tr></thead><tbody>';

            list.forEach(function (ch) {
                var overrides = Array.isArray(ch.overrides) ? ch.overrides : [];
                var chips = overrides.map(function (name) {
                    return '<span class="chip">' + escapeHtml(OVERRIDE_LABELS[name] || name) + '</span>';
                }).join('');
                html += '<tr>' +
                    '<td><span class="channel-name">#' + escapeHtml(ch.channel_name || ch.channel_id) + '</span></td>' +
                    '<td>' + (ch.category ? escapeHtml(ch.category) : '<span class="muted">None</span>') + '</td>' +
                    '<td>' + (ch.model ? '<span class="code">' + escapeHtml(ch.model) + '</span>' : '<span class="muted">Global</span>') + '</td>' +
                    '<td>' + (ch.provider ? escapeHtml(ch.provider) : '<span class="muted">Global</span>') + '</td>' +
                    '<td><div class="chip-row">' + (ch.has_override
                        ? '<span class="badge badge-accent">Customized</span>' + chips
                        : '<span class="badge badge-default">Global</span>') + '</div></td>' +
                    '<td class="cell-actions"><button type="button" class="btn btn-sm btn-secondary" ' + actionAttrs('editChannel', [ch.channel_id]) + ' aria-label="Edit #' + escapeHtml(ch.channel_name || ch.channel_id) + '">Edit</button></td>' +
                    '</tr>';
            });

            html += '</tbody></table></div></div></div>';
        });

        container.innerHTML = html;
    }

    var channelEdit = null;

    function sourceLabel(source) {
        return source === 'persona' ? 'Persona' : (source === 'channel' ? 'Channel override' : 'Global');
    }

    function formatSummaryFlag(value) {
        if (value === null || value === undefined || value === '') return 'not set';
        return (value === true || value === 'true') ? 'enabled' : 'disabled';
    }

    function channelFieldDefs() {
        var st = channelEdit;
        var d = st.data;
        var g = st.settings;
        return [
            {
                key: 'model', hint: '#channel-model-hint',
                hasValue: function () { return !!$('#channel-edit-model').value; },
                effective: function (source) {
                    return source === 'channel' ? $('#channel-edit-model').value : (source === 'persona' ? d.effective_model : g.global_model);
                },
                global: function () { return g.global_model; }
            },
            {
                key: 'system_prompt', hint: '#channel-prompt-hint',
                hasValue: function () { return !!$('#channel-edit-prompt').value.trim(); },
                effective: function (source) {
                    return source === 'persona' ? (st.personaPrompt || '') : (source === 'channel' ? $('#channel-edit-prompt').value : g.global_system_prompt);
                },
                global: function () { return g.global_system_prompt; },
                textual: true
            },
            {
                key: 'session_timeout_hours', hint: '#channel-timeout-hint', input: '#channel-edit-timeout',
                hasValue: function () { return $('#channel-edit-timeout').value.trim() !== ''; },
                effective: function (source) {
                    return source === 'channel' ? $('#channel-edit-timeout').value : (source === 'persona' ? d.effective_session_timeout : g.session_timeout_hours);
                },
                global: function () { return g.session_timeout_hours; },
                suffix: ' h'
            },
            {
                key: 'memory_summary_enabled', hint: '#channel-summary-hint',
                hasValue: function () { return $('#channel-edit-summary').value !== ''; },
                effective: function (source) {
                    return source === 'channel' ? formatSummaryFlag($('#channel-edit-summary').value)
                        : formatSummaryFlag(source === 'persona' ? d.effective_memory_summary_enabled : g.memory_summary_enabled);
                },
                global: function () { return formatSummaryFlag(g.memory_summary_enabled); }
            },
            {
                key: 'max_memory_summaries', hint: '#channel-max-summaries-hint', input: '#channel-edit-max-summaries',
                hasValue: function () { return $('#channel-edit-max-summaries').value.trim() !== ''; },
                effective: function (source) {
                    return source === 'channel' ? $('#channel-edit-max-summaries').value : (source === 'persona' ? d.effective_max_memory_summaries : g.max_memory_summaries);
                },
                global: function () { return g.max_memory_summaries; }
            }
        ];
    }

    function updateSourceChips() {
        if (!channelEdit) return;
        var personaName = channelEdit.persona ? channelEdit.persona.display_name : '';
        channelFieldDefs().forEach(function (def) {
            var source = channelEdit.sources[def.key] === 'persona' ? 'persona' : (def.hasValue() ? 'channel' : 'global');
            var chip = $('.chip-source[data-source-for="' + def.key + '"]');
            if (chip) {
                chip.hidden = false;
                chip.dataset.source = source;
                chip.textContent = sourceLabel(source);
            }
            var hint = $(def.hint);
            var effective = def.effective(source);
            var shown = def.textual ? truncate(effective, 140) : String(effective == null ? 'not set' : effective) + (def.suffix && effective != null ? def.suffix : '');
            if (hint) {
                if (source === 'persona') {
                    hint.textContent = 'Set by persona "' + personaName + '" while it is active. Effective: ' + (shown || 'persona prompt');
                } else if (source === 'channel') {
                    var base = def.global();
                    hint.textContent = 'Overrides the global value' + (def.textual ? '.' : ' (' + (base == null ? 'not set' : base) + (def.suffix && base != null ? def.suffix : '') + ').');
                } else {
                    hint.textContent = 'Effective: ' + (shown || (def.textual ? 'none (no global prompt)' : 'not set')) + ' (from global settings)';
                }
            }
            if (def.input) {
                var inputEl = $(def.input);
                var base2 = def.global();
                inputEl.placeholder = base2 == null ? '' : String(base2);
            }
        });
        $('#channel-edit-prompt').placeholder = channelEdit.settings.global_system_prompt
            ? 'Inherits: ' + truncate(channelEdit.settings.global_system_prompt, 90)
            : 'Leave blank to inherit';
    }

    async function editChannel(channelId, opener) {
        opener = opener || document.activeElement;
        var data;
        var settings;
        try {
            var results = await Promise.all([api('GET', '/api/channels/' + encodeURIComponent(channelId)), api('GET', '/api/settings')]);
            data = results[0];
            settings = results[1];
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Could not load channel' });
            return;
        }
        var modal = $('#channel-modal');
        var form = $('#channel-edit-form');
        G.clearFieldErrors(form);
        G.setFormAlert(form, '');
        Modals.watchDirty(modal);

        channelEdit = { opener: opener, id: channelId, data: data, settings: settings, sources: data.sources || {}, persona: data.persona || null, personaPrompt: '', provider: data.provider || '' };
        $('#channel-edit-id').value = channelId;
        $('#channel-modal-name').textContent = '#' + (data.channel_name || channelId);
        $('#channel-edit-provider').value = data.provider || '';
        $('#channel-edit-prompt').value = data.system_prompt || '';
        $('#channel-edit-timeout').value = data.session_timeout_hours != null ? data.session_timeout_hours : '';
        $('#channel-edit-summary').value = data.memory_summary_enabled == null ? '' : String(data.memory_summary_enabled);
        $('#channel-edit-max-summaries').value = data.max_memory_summaries != null ? data.max_memory_summaries : '';

        var note = $('#channel-persona-note');
        var persona = data.persona;
        var byPersona = Object.keys(channelEdit.sources).filter(function (k) { return channelEdit.sources[k] === 'persona'; });
        if (persona && persona.is_active) {
            note.hidden = false;
            note.textContent = 'Persona "' + persona.display_name + '" is active in this channel' +
                (byPersona.length ? ' and takes priority for: ' + byPersona.map(function (k) { return OVERRIDE_LABELS[k] || k; }).join(', ') + '.' : '.') +
                ' Channel values for those fields apply only if the persona is removed or disabled.';
        } else {
            note.hidden = true;
            note.textContent = '';
        }

        var provider = data.provider || settings.global_provider || 'openrouter';
        var searchEl = $('#channel-model-search');
        if (searchEl) searchEl.value = '';
        updateSourceChips();
        Modals.markClean(modal);
        Modals.open(modal, { opener: opener, focus: $('#channel-edit-model') });

        loadModelsForProvider(provider, 'channel-edit-model', data.model, 'Use global default' + (settings.global_model ? ' (' + settings.global_model + ')' : '')).then(function () {
            if (channelEdit && channelEdit.id === channelId) updateSourceChips();
        });
        if (byPersona.indexOf('system_prompt') !== -1) {
            api('GET', '/api/personas/channels/' + encodeURIComponent(channelId)).then(function (p) {
                if (channelEdit && channelEdit.id === channelId) {
                    channelEdit.personaPrompt = p.system_prompt || '';
                    updateSourceChips();
                }
            }).catch(function () {});
        }
    }

    async function onChannelProviderChange() {
        if (!channelEdit) return;
        var provider = $('#channel-edit-provider').value || channelEdit.settings.global_provider || 'openrouter';
        var current = $('#channel-edit-model').value;
        await loadModelsForProvider(provider, 'channel-edit-model', current, 'Use global default' + (channelEdit.settings.global_model ? ' (' + channelEdit.settings.global_model + ')' : ''));
        updateSourceChips();
    }

    async function saveChannel(e) {
        e.preventDefault();
        var form = e.currentTarget;
        var modal = $('#channel-modal');
        G.clearFieldErrors(form);
        G.setFormAlert(form, '');
        var channelId = $('#channel-edit-id').value;
        var provider = $('#channel-edit-provider').value;
        var invalid = false;
        if (!provider && channelEdit && channelEdit.provider) {
            G.setFieldError($('#channel-edit-provider'), 'A provider override can only be cleared with "Reset to defaults".');
            invalid = true;
        }
        var timeoutRaw = $('#channel-edit-timeout').value.trim();
        var maxRaw = $('#channel-edit-max-summaries').value.trim();
        var timeout = timeoutRaw === '' ? null : readInt('channel-edit-timeout');
        var maxSummaries = maxRaw === '' ? null : readInt('channel-edit-max-summaries');
        if ((timeoutRaw !== '' && timeout === null) || (maxRaw !== '' && maxSummaries === null)) invalid = true;
        if (invalid) return;

        var summary = $('#channel-edit-summary').value;
        var body = {
            model: $('#channel-edit-model').value || null,
            system_prompt: $('#channel-edit-prompt').value || null,
            session_timeout_hours: timeout,
            memory_summary_enabled: summary === '' ? null : summary === 'true',
            max_memory_summaries: maxSummaries
        };
        if (provider) body.provider = provider;

        var btn = e.submitter;
        setBusy(btn, true);
        try {
            await api('PUT', '/api/channels/' + encodeURIComponent(channelId), body);
            markOwnWrite();
            Modals.markClean(modal);
            Modals.close(modal);
            toast.success('Channel settings saved.');
            reloadKeepingFocus(channelEdit && channelEdit.opener, loadChannels);
        } catch (err) {
            if (err.status !== 401) {
                var unmatched = err.details && !Array.isArray(err.details) ? G.applyFieldErrors(form, err.details, CHANNEL_FIELD_MAP) : [];
                G.setFormAlert(form, unmatched.length || !err.details ? describeError(err) : 'Fix the highlighted fields', 'danger');
            }
        }
        setBusy(btn, false);
    }

    async function resetChannel() {
        var modal = $('#channel-modal');
        var channelId = $('#channel-edit-id').value;
        var ok = await confirmDialog({
            title: 'Reset channel to defaults?',
            message: 'Remove every override for #' + $('#channel-modal-name').textContent.replace(/^#/, '') + '.',
            details: ['Model, provider and system prompt overrides are removed', 'Memory overrides are removed', 'The channel will follow the global settings again'],
            confirmLabel: 'Reset channel'
        });
        if (!ok) return;
        try {
            await api('DELETE', '/api/channels/' + encodeURIComponent(channelId));
            markOwnWrite();
            Modals.markClean(modal);
            Modals.close(modal);
            toast.success('Channel reset to global defaults.');
            reloadKeepingFocus(channelEdit && channelEdit.opener, loadChannels);
        } catch (e) {
            if (e.status !== 401) G.setFormAlert($('#channel-edit-form'), describeError(e), 'danger');
        }
    }

    // ── Threads ──────────────────────────────────────────────

    async function loadThreads() {
        var container = $('#threads-list');
        if (!container.dataset.loaded) container.innerHTML = skeleton('rows', 4);
        try {
            var threads = await api('GET', '/api/threads');
            container.dataset.loaded = '1';
            if (threads.length === 0) {
                container.innerHTML = emptyState('threads', 'No active threads', 'Threads appear here when someone starts an AI conversation thread.');
                return;
            }

            var html = '<table class="data-table"><thead><tr>' +
                '<th scope="col">Name</th><th scope="col">Channel</th><th scope="col">Server</th><th scope="col">Messages</th><th scope="col">Model</th><th scope="col">Created</th><th scope="col"><span class="sr-only">Actions</span></th>' +
                '</tr></thead><tbody>';

            threads.forEach(function (t) {
                var created = t.created_at ? new Date(t.created_at).toLocaleDateString() : 'N/A';
                var channelDisplay = t.channel_name ? '#' + t.channel_name : (t.channel_id || 'N/A');
                html += '<tr>' +
                    '<td>' + escapeHtml(t.name || 'Unnamed') + '</td>' +
                    '<td>' + escapeHtml(channelDisplay) + '</td>' +
                    '<td>' + escapeHtml(t.guild_name || 'Unknown') + '</td>' +
                    '<td>' + escapeHtml(t.message_count || 0) + '</td>' +
                    '<td>' + (t.model ? '<span class="code">' + escapeHtml(t.model) + '</span>' : '<span class="muted">Default</span>') + '</td>' +
                    '<td>' + escapeHtml(created) + '</td>' +
                    '<td class="cell-actions"><button type="button" class="btn btn-sm btn-secondary" ' + actionAttrs('editThread', [t.thread_id]) + ' aria-label="Edit thread ' + escapeHtml(t.name || 'Unnamed') + '">Edit</button></td>' +
                    '</tr>';
            });

            html += '</tbody></table>';
            container.innerHTML = html;
        } catch (e) {
            if (e.status === 401) return;
            container.innerHTML = emptyState('alert-triangle', 'Could not load threads', describeError(e));
        }
    }

    async function editThread(threadId, opener) {
        opener = opener || document.activeElement;
        var data;
        var settings;
        try {
            var results = await Promise.all([api('GET', '/api/threads/' + encodeURIComponent(threadId)), api('GET', '/api/settings')]);
            data = results[0];
            settings = results[1];
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Could not load thread' });
            return;
        }
        var modal = $('#thread-modal');
        var form = $('#thread-edit-form');
        G.clearFieldErrors(form);
        G.setFormAlert(form, '');
        Modals.watchDirty(modal);
        $('#thread-edit-id').value = threadId;
        $('#thread-modal-name').textContent = data.name || threadId;
        $('#thread-edit-name').value = data.name || '';
        $('#thread-edit-prompt').value = data.system_prompt || '';
        var searchEl = $('#thread-model-search');
        if (searchEl) searchEl.value = '';
        modal._opener = opener;
        Modals.markClean(modal);
        Modals.open(modal, { opener: opener, focus: $('#thread-edit-name') });
        loadModelsForProvider(settings.global_provider || 'openrouter', 'thread-edit-model', data.model, 'Use channel/global default');
    }

    async function saveThread(e) {
        e.preventDefault();
        var form = e.currentTarget;
        var modal = $('#thread-modal');
        G.setFormAlert(form, '');
        var threadId = $('#thread-edit-id').value;
        var btn = e.submitter;
        setBusy(btn, true);
        try {
            await api('PUT', '/api/threads/' + encodeURIComponent(threadId), {
                name: $('#thread-edit-name').value,
                model: $('#thread-edit-model').value || null,
                system_prompt: $('#thread-edit-prompt').value || null
            });
            markOwnWrite();
            Modals.markClean(modal);
            Modals.close(modal);
            toast.success('Thread saved.');
            reloadKeepingFocus(modal._opener, loadThreads);
        } catch (err) {
            if (err.status !== 401) {
                var unmatched = err.details && !Array.isArray(err.details)
                    ? G.applyFieldErrors(form, err.details, { model: 'thread-edit-model', name: 'thread-edit-name', system_prompt: 'thread-edit-prompt' })
                    : [];
                G.setFormAlert(form, unmatched.length || !err.details ? describeError(err) : 'Fix the highlighted fields', 'danger');
            }
        }
        setBusy(btn, false);
    }

    async function deleteThread() {
        var modal = $('#thread-modal');
        var threadId = $('#thread-edit-id').value;
        var name = $('#thread-modal-name').textContent;
        var ok = await confirmDialog({
            title: 'Delete this thread?',
            message: 'Thread "' + name + '" will be deleted permanently.',
            details: ['All of its stored messages are removed', 'Its model and prompt overrides are lost', 'This cannot be undone'],
            confirmLabel: 'Delete thread'
        });
        if (!ok) return;
        try {
            await api('DELETE', '/api/threads/' + encodeURIComponent(threadId));
            markOwnWrite();
            Modals.markClean(modal);
            Modals.close(modal);
            toast.success('Thread deleted.');
            loadThreads();
        } catch (e) {
            if (e.status !== 401) G.setFormAlert($('#thread-edit-form'), describeError(e), 'danger');
        }
    }

    // ── Messages ─────────────────────────────────────────────

    async function loadMessageSources() {
        if (msgSources) return;
        try {
            msgSources = await api('GET', '/api/messages/sources');
        } catch (e) {
            if (e.status !== 401) console.error('Failed to load message sources:', e);
        }
    }

    function onSourceTypeChange() {
        var type = $('#msg-source-type').value;
        var group = $('#msg-source-select-group');
        var sel = $('#msg-source-id');

        if (type === 'all') {
            group.hidden = true;
            return;
        }
        group.hidden = false;
        sel.innerHTML = '<option value="">Loading...</option>';
        if (!msgSources) {
            loadMessageSources().then(function () { populateSourceSelect(type); });
        } else {
            populateSourceSelect(type);
        }
    }

    function populateSourceSelect(type) {
        var sel = $('#msg-source-id');
        var html = '';

        if (type === 'channel' && msgSources && msgSources.channels) {
            msgSources.channels.forEach(function (ch) {
                var label = ch.name || ch.id;
                if (ch.guild) label += ' (' + ch.guild + ')';
                label += ' - ' + ch.message_count + ' msgs';
                html += '<option value="' + escapeHtml(ch.id) + '">' + escapeHtml(label) + '</option>';
            });
        } else if (type === 'thread' && msgSources && msgSources.threads) {
            msgSources.threads.forEach(function (t) {
                var label = (t.name || t.id) + ' - ' + t.message_count + ' msgs';
                html += '<option value="' + escapeHtml(t.id) + '">' + escapeHtml(label) + '</option>';
            });
        }
        if (!html) html = '<option value="">No ' + escapeHtml(type) + 's with messages</option>';
        sel.innerHTML = html;
    }

    function renderMessage(msg) {
        var role = msg.role || 'unknown';
        var roleClass = 'msg-role-' + escapeHtml(msg.role || 'user');
        var timeStr = formatDateTime(msg.timestamp);
        var channelInfo = '';
        if (msg.channel_name) channelInfo = '#' + msg.channel_name;
        else if (msg.channel_id) channelInfo = '#' + msg.channel_id;
        else if (msg.thread_id) channelInfo = 'Thread ' + msg.thread_id;

        var content = msg.content || '';
        var isAssistant = msg.role === 'assistant';

        var imageHtml = '';
        var imageMatch = isAssistant ? content.match(/^\[image:(.*?)\]\s*/) : null;
        if (imageMatch) {
            var imageRef = sanitizeHttpUrl(imageMatch[1]);
            content = content.substring(imageMatch[0].length);
            if (imageRef) {
                imageHtml = '<div class="msg-media"><a href="' + escapeHtml(imageRef) + '" target="_blank" rel="noopener"><img src="' + escapeHtml(imageRef) + '" alt="Generated image" loading="lazy"></a></div>';
            } else {
                imageHtml = '<div class="msg-media-placeholder">Generated image (attachment - not available in dashboard)</div>';
            }
        }

        var videoHtml = '';
        var videoMatch = isAssistant ? content.match(/^\[video:(.*?)\]\s*/) : null;
        if (videoMatch) {
            var videoRef = sanitizeHttpUrl(videoMatch[1]);
            content = content.substring(videoMatch[0].length);
            if (videoRef) {
                videoHtml = '<div class="msg-media"><video class="media-video" controls preload="metadata"><source src="' + escapeHtml(videoRef) + '" type="video/mp4"></video><div><a href="' + escapeHtml(videoRef) + '" target="_blank" rel="noopener">Open video in new tab</a></div></div>';
            } else {
                videoHtml = '<div class="msg-media-placeholder">Generated video (attachment - not available in dashboard)</div>';
            }
        }

        if (content.startsWith('[image generation failed]')) {
            content = content.substring('[image generation failed] '.length);
        } else if (content.startsWith('[video generation failed]')) {
            content = content.substring('[video generation failed] '.length);
        }

        var truncated = false;
        if (content.length > 1000) {
            content = content.substring(0, 1000);
            truncated = true;
        }

        var html = '<article class="msg-bubble ' + roleClass + '"><div class="msg-header">' +
            '<span class="msg-role-badge ' + roleClass + '">' + escapeHtml(role) + '</span>';
        var who = msg.user_name || msg.user_id;
        if (who) {
            var whoTitle = msg.user_name && msg.user_id ? ' title="User ID: ' + escapeHtml(msg.user_id) + '"' : '';
            html += '<span class="msg-user"' + whoTitle + '>' + escapeHtml(who) + '</span>';
        }
        if (channelInfo) html += '<span class="msg-channel">' + escapeHtml(channelInfo) + '</span>';
        html += '<span class="msg-time">' + escapeHtml(timeStr) + '</span></div>';
        html += '<div class="msg-content">' + escapeHtml(content);
        if (truncated) html += '<span class="msg-truncated">... (truncated)</span>';
        html += '</div>' + imageHtml + videoHtml + '</article>';
        return html;
    }

    async function loadMessages(page) {
        if (!page) page = 1;
        msgCurrentPage = page;

        var container = $('#messages-container');
        container.innerHTML = skeleton('rows', 5);

        var params = 'page=' + page + '&per_page=50';
        var sourceType = $('#msg-source-type').value;
        var sourceId = $('#msg-source-id').value;
        if (sourceType === 'channel' && sourceId) params += '&channel_id=' + encodeURIComponent(sourceId);
        else if (sourceType === 'thread' && sourceId) params += '&thread_id=' + encodeURIComponent(sourceId);
        var roleFilter = $('#msg-role-filter').value;
        if (roleFilter) params += '&role=' + encodeURIComponent(roleFilter);

        try {
            var data = await api('GET', '/api/messages?' + params);
            msgTotalPages = data.total_pages;

            if (data.messages.length === 0) {
                container.innerHTML = emptyState('mail', 'No messages found', 'Try another source or role filter.');
                $('#messages-pagination').hidden = true;
                return;
            }
            container.innerHTML = data.messages.map(renderMessage).join('');
            $('#messages-pagination').hidden = false;
            $('#msg-page-info').textContent = 'Page ' + data.page + ' of ' + data.total_pages + ' (' + data.total + ' messages)';
            $('#msg-prev').disabled = data.page <= 1;
            $('#msg-next').disabled = data.page >= data.total_pages;
        } catch (e) {
            if (e.status === 401) return;
            container.innerHTML = emptyState('alert-triangle', 'Could not load messages', describeError(e));
            $('#messages-pagination').hidden = true;
        }
    }

    // ── Diagnostics ──────────────────────────────────────────

    function diagItem(label, value) {
        return '<div class="diag-item"><span class="diag-label">' + escapeHtml(label) + '</span><span class="diag-value">' + value + '</span></div>';
    }

    function statusDot(type) {
        return '<span class="status-dot is-' + type + '"></span>';
    }

    async function runDiagnostics() {
        var btn = $('#run-diagnostics-btn');
        var container = $('#diagnostics-results');
        container.innerHTML = skeleton('cards', 3);
        setBusy(btn, true);
        try {
            var data = await api('GET', '/api/diagnostics');
            var html = '';

            html += '<div class="card"><div class="card-header"><h2 class="card-title">Bot status</h2></div><div class="card-body">';
            html += diagItem('Status', statusDot(data.bot_status === 'online' ? 'ok' : 'warn') + ' ' + escapeHtml(data.bot_status));
            html += diagItem('Latency', escapeHtml(data.latency_ms !== null && data.latency_ms !== undefined ? data.latency_ms + ' ms' : 'N/A'));
            html += diagItem('Servers', escapeHtml(data.guilds));
            html += diagItem('Uptime', escapeHtml(formatUptime(data.bot_uptime_seconds != null ? data.bot_uptime_seconds : data.uptime_seconds)));
            html += '</div></div>';

            if (data.database) {
                html += '<div class="card"><div class="card-header"><h2 class="card-title">Database</h2></div><div class="card-body">';
                html += diagItem('Status', statusDot(data.database.status === 'ok' ? 'ok' : 'danger') + ' ' + escapeHtml(data.database.status));
                html += diagItem('Messages', escapeHtml(data.database.message_count));
                html += diagItem('Threads', escapeHtml(data.database.thread_count));
                html += diagItem('File size', escapeHtml(formatBytes(data.database.file_size_bytes)));
                html += '</div></div>';
            }

            if (data.providers) {
                html += '<div class="card"><div class="card-header"><h2 class="card-title">AI providers</h2></div><div class="card-body">';
                Object.keys(data.providers).forEach(function (name) {
                    var p = data.providers[name];
                    var status = p.status === 'ok' ? 'ok' : (p.status === 'configured' ? 'warn' : 'danger');
                    var detail = p.model_count ? p.model_count + ' models' : (p.error || p.status);
                    html += diagItem(name, statusDot(status) + ' ' + escapeHtml(detail));
                });
                html += '</div></div>';
            }

            if (data.settings) {
                html += '<div class="card"><div class="card-header"><h2 class="card-title">Current settings</h2></div><div class="card-body">';
                html += diagItem('Model', '<span class="mono">' + escapeHtml(data.settings.global_model) + '</span>');
                html += diagItem('Provider', escapeHtml(data.settings.global_provider));
                html += diagItem('History limit', escapeHtml(data.settings.max_channel_history));
                html += diagItem('Time window', escapeHtml(data.settings.time_window_hours + 'h'));
                html += diagItem('Prune every', escapeHtml(data.settings.prune_frequency_hours + 'h'));
                html += diagItem('Tool calling', escapeHtml(data.settings.tool_calling_enabled ? 'Enabled' : 'Disabled'));
                html += '</div></div>';
            }

            container.innerHTML = html;
        } catch (e) {
            if (e.status !== 401) container.innerHTML = emptyState('alert-triangle', 'Diagnostics failed', describeError(e));
        }
        setBusy(btn, false);
    }

    async function runPrune() {
        var ok = await confirmDialog({
            title: 'Run data pruning now?',
            message: 'Old data beyond the configured retention windows will be removed immediately.',
            details: ['Old messages and threads are deleted', 'Expired reminders and trivia sessions are deleted', 'This cannot be undone'],
            confirmLabel: 'Prune data'
        });
        if (!ok) return;
        var btn = $('#run-prune-btn');
        setBusy(btn, true);
        try {
            var data = await api('POST', '/api/diagnostics/prune');
            var s = data.stats || {};
            toast.success('Messages: ' + (s.messages_pruned || 0) + ', threads: ' + (s.threads_pruned || 0) +
                ', reminders: ' + (s.reminders_pruned || 0) + ', trivia sessions: ' + (s.trivia_sessions_pruned || 0), { title: 'Pruning complete', timeout: 9000 });
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Prune failed' });
        }
        setBusy(btn, false);
    }

    // ── Personas ─────────────────────────────────────────────

    async function loadPersonas() {
        await Promise.all([loadPersonaTemplates(), loadChannelPersonas()]);
    }

    async function loadPersonaTemplates() {
        var container = $('#persona-templates-grid');
        if (!personaTemplatesData.length) container.innerHTML = skeleton('cards', 3);
        try {
            personaTemplatesData = await api('GET', '/api/personas/templates');
            renderPersonaTemplates();
        } catch (e) {
            if (e.status !== 401) container.innerHTML = emptyState('alert-triangle', 'Could not load templates', describeError(e));
        }
    }

    function renderPersonaTemplates() {
        var container = $('#persona-templates-grid');
        if (personaTemplatesData.length === 0) {
            container.innerHTML = emptyState('users', 'No persona templates', 'Create a template to reuse an identity across channels.');
            return;
        }

        var html = '';
        personaTemplatesData.forEach(function (t) {
            var builtinBadge = t.is_builtin ? '<span class="badge badge-info">Built-in</span>' : '';
            var avatarUrl = sanitizeHttpUrl(t.avatar_url);
            var initial = String(t.display_name || t.name || '?').charAt(0).toUpperCase();
            var avatarHtml = avatarUrl
                ? '<img src="' + escapeHtml(avatarUrl) + '" class="avatar" alt="">'
                : '<div class="avatar avatar-placeholder" aria-hidden="true">' + escapeHtml(initial) + '</div>';

            html += '<div class="template-card"><div class="template-card-head">' + avatarHtml +
                '<div class="template-card-title"><strong>' + escapeHtml(t.name) + '</strong> ' + builtinBadge +
                '<div class="muted">' + escapeHtml(t.display_name) + '</div></div></div>';
            if (t.description) html += '<p class="template-desc">' + escapeHtml(t.description) + '</p>';
            html += '<div class="template-actions">' +
                '<button type="button" class="btn btn-sm btn-secondary" ' + actionAttrs('editTemplate', [t.template_id]) + ' aria-label="Edit template ' + escapeHtml(t.name) + '">Edit</button>' +
                '<button type="button" class="btn btn-sm btn-primary" ' + actionAttrs('applyTemplatePrompt', [t.template_id]) + ' aria-label="Apply template ' + escapeHtml(t.name) + ' to a channel">Apply to channel</button>' +
                '</div></div>';
        });
        container.innerHTML = html;

        var sel = $('#persona-edit-template');
        if (sel) {
            var current = sel.value;
            sel.innerHTML = '<option value="">Custom (no template)</option>' + personaTemplatesData.map(function (t) {
                return '<option value="' + escapeHtml(t.template_id) + '">' + escapeHtml(t.name + ' (' + t.display_name + ')') + '</option>';
            }).join('');
            sel.value = current;
        }
    }

    async function loadChannelPersonas() {
        var container = $('#channel-personas-list');
        if (!container.dataset.loaded) container.innerHTML = skeleton('rows', 3);
        try {
            channelPersonasData = await api('GET', '/api/personas/channels');
            container.dataset.loaded = '1';
            renderChannelPersonas();
        } catch (e) {
            if (e.status !== 401) container.innerHTML = emptyState('alert-triangle', 'Could not load personas', describeError(e));
        }
    }

    function renderChannelPersonas() {
        var container = $('#channel-personas-list');
        if (channelPersonasData.length === 0) {
            container.innerHTML = emptyState('users', 'No channel personas', 'Apply a template above or set a persona from a channel.');
            return;
        }

        var html = '<table class="data-table"><thead><tr>' +
            '<th scope="col">Channel</th><th scope="col">Persona</th><th scope="col">Template</th><th scope="col">Status</th><th scope="col"><span class="sr-only">Actions</span></th>' +
            '</tr></thead><tbody>';

        channelPersonasData.forEach(function (p) {
            var channelName = p.channel_name ? '#' + p.channel_name : 'ID:' + p.channel_id;
            html += '<tr>' +
                '<td>' + escapeHtml(channelName) + '</td>' +
                '<td><strong>' + escapeHtml(p.display_name) + '</strong></td>' +
                '<td>' + (p.template_name ? escapeHtml(p.template_name) : '<span class="muted">Custom</span>') + '</td>' +
                '<td><span class="badge ' + (p.is_active ? 'badge-accent' : 'badge-default') + '">' + (p.is_active ? 'Active' : 'Inactive') + '</span></td>' +
                '<td class="cell-actions"><div class="btn-group">' +
                '<button type="button" class="btn btn-sm btn-secondary" ' + actionAttrs('editPersona', [p.channel_id]) + ' aria-label="Edit persona in ' + escapeHtml(channelName) + '">Edit</button>' +
                '<button type="button" class="btn btn-sm ' + (p.is_active ? 'btn-secondary' : 'btn-primary') + '" ' + actionAttrs('togglePersona', [p.channel_id]) + '>' + (p.is_active ? 'Disable' : 'Enable') + '</button>' +
                '</div></td></tr>';
        });

        html += '</tbody></table>';
        container.innerHTML = html;
    }

    function updatePersonaAvatarPreview() {
        var preview = $('#persona-avatar-preview');
        var safeUrl = sanitizeHttpUrl($('#persona-edit-avatar-url').value);
        if (safeUrl) {
            preview.src = safeUrl;
            preview.hidden = false;
            preview.onerror = function () { preview.hidden = true; };
        } else {
            preview.hidden = true;
        }
    }

    async function editPersona(channelId, opener) {
        opener = opener || document.activeElement;
        var data = null;
        try {
            data = await api('GET', '/api/personas/channels/' + encodeURIComponent(channelId));
        } catch (e) {
            if (e.status !== 404) {
                if (e.status !== 401) toast.error(describeError(e), { title: 'Could not load persona' });
                return;
            }
        }
        var modal = $('#persona-modal');
        var form = $('#persona-edit-form');
        G.clearFieldErrors(form);
        G.setFormAlert(form, '');
        Modals.watchDirty(modal);
        var existing = !!data;
        data = data || {};
        $('#persona-edit-channel-id').value = channelId;
        $('#persona-modal-title').textContent = existing
            ? 'Edit persona: ' + (data.channel_name ? '#' + data.channel_name : channelId)
            : 'Set persona for channel ' + channelId;
        $('#persona-edit-display-name').value = data.display_name || '';
        $('#persona-edit-avatar-url').value = data.avatar_url || '';
        $('#persona-edit-system-prompt').value = data.system_prompt || '';
        $('#persona-edit-model').value = data.model || '';
        $('#persona-edit-provider').value = data.provider || '';
        $('#persona-edit-template').value = data.template_id || '';
        $('#persona-remove-btn').hidden = !existing;
        updatePersonaAvatarPreview();
        modal._opener = opener;
        Modals.markClean(modal);
        Modals.open(modal, { opener: opener, focus: $('#persona-edit-display-name') });
    }

    async function savePersona(e) {
        e.preventDefault();
        var form = e.currentTarget;
        var modal = $('#persona-modal');
        G.clearFieldErrors(form);
        G.setFormAlert(form, '');
        var channelId = $('#persona-edit-channel-id').value;
        var nameEl = $('#persona-edit-display-name');
        var displayName = nameEl.value.trim();
        if (!displayName) {
            G.setFieldError(nameEl, 'Display name is required');
            nameEl.focus();
            return;
        }
        var btn = e.submitter;
        setBusy(btn, true);
        try {
            await api('PUT', '/api/personas/channels/' + encodeURIComponent(channelId), {
                display_name: displayName,
                avatar_url: $('#persona-edit-avatar-url').value || null,
                system_prompt: $('#persona-edit-system-prompt').value || null,
                model: $('#persona-edit-model').value || null,
                provider: $('#persona-edit-provider').value || null,
                template_id: $('#persona-edit-template').value || null
            });
            markOwnWrite();
            Modals.markClean(modal);
            Modals.close(modal);
            toast.success('Persona saved.');
            reloadKeepingFocus(modal._opener, loadPersonas);
        } catch (err) {
            if (err.status !== 401) G.setFormAlert(form, describeError(err), 'danger');
        }
        setBusy(btn, false);
    }

    async function removePersona() {
        var modal = $('#persona-modal');
        var channelId = $('#persona-edit-channel-id').value;
        var ok = await confirmDialog({
            title: 'Remove this persona?',
            message: 'The channel goes back to Gideon\'s normal identity and prompt.',
            confirmLabel: 'Remove persona'
        });
        if (!ok) return;
        try {
            await api('DELETE', '/api/personas/channels/' + encodeURIComponent(channelId));
            markOwnWrite();
            Modals.markClean(modal);
            Modals.close(modal);
            toast.success('Persona removed.');
            loadPersonas();
        } catch (e) {
            if (e.status !== 401) G.setFormAlert($('#persona-edit-form'), describeError(e), 'danger');
        }
    }

    async function togglePersona(channelId, el) {
        setBusy(el, true);
        try {
            await api('POST', '/api/personas/channels/' + encodeURIComponent(channelId) + '/toggle');
            markOwnWrite();
            await reloadKeepingFocus(el, loadChannelPersonas);
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Could not toggle persona' });
            setBusy(el, false);
        }
    }

    function onTemplateSelect() {
        var templateId = $('#persona-edit-template').value;
        if (!templateId) return;
        var t = personaTemplatesData.find(function (x) { return x.template_id === templateId; });
        if (!t) return;
        $('#persona-edit-display-name').value = t.display_name || '';
        $('#persona-edit-avatar-url').value = t.avatar_url || '';
        $('#persona-edit-system-prompt').value = t.system_prompt || '';
        $('#persona-edit-model').value = t.model || '';
        $('#persona-edit-provider').value = t.provider || '';
        updatePersonaAvatarPreview();
    }

    // Template modal

    function openCreateTemplate(opener) {
        var modal = $('#template-modal');
        var form = $('#template-edit-form');
        G.clearFieldErrors(form);
        G.setFormAlert(form, '');
        Modals.watchDirty(modal);
        $('#template-edit-id').value = '';
        $('#template-modal-title').textContent = 'Create template';
        ['name', 'display-name', 'avatar-url', 'description', 'system-prompt', 'model', 'provider'].forEach(function (f) {
            $('#template-edit-' + f).value = '';
        });
        $('#template-delete-btn').hidden = true;
        modal._opener = opener || document.activeElement;
        Modals.markClean(modal);
        Modals.open(modal, { opener: opener || document.activeElement, focus: $('#template-edit-name') });
    }

    function editTemplate(templateId, opener) {
        var t = personaTemplatesData.find(function (x) { return x.template_id === templateId; });
        if (!t) {
            toast.error('That template no longer exists.', { title: 'Template not found' });
            return;
        }
        var modal = $('#template-modal');
        var form = $('#template-edit-form');
        G.clearFieldErrors(form);
        G.setFormAlert(form, '');
        Modals.watchDirty(modal);
        $('#template-edit-id').value = t.template_id;
        $('#template-modal-title').textContent = 'Edit template: ' + t.name;
        $('#template-edit-name').value = t.name || '';
        $('#template-edit-display-name').value = t.display_name || '';
        $('#template-edit-avatar-url').value = t.avatar_url || '';
        $('#template-edit-description').value = t.description || '';
        $('#template-edit-system-prompt').value = t.system_prompt || '';
        $('#template-edit-model').value = t.model || '';
        $('#template-edit-provider').value = t.provider || '';
        $('#template-delete-btn').hidden = !!t.is_builtin;
        modal._opener = opener || document.activeElement;
        Modals.markClean(modal);
        Modals.open(modal, { opener: opener || document.activeElement, focus: $('#template-edit-name') });
    }

    async function saveTemplate(e) {
        e.preventDefault();
        var form = e.currentTarget;
        var modal = $('#template-modal');
        G.clearFieldErrors(form);
        G.setFormAlert(form, '');
        var templateId = $('#template-edit-id').value;
        var nameEl = $('#template-edit-name');
        var displayEl = $('#template-edit-display-name');
        var name = nameEl.value.trim();
        var displayName = displayEl.value.trim();
        var missing = false;
        if (!name) { G.setFieldError(nameEl, 'Template name is required'); missing = true; }
        if (!displayName) { G.setFieldError(displayEl, 'Display name is required'); missing = true; }
        if (missing) return;

        var payload = {
            name: name,
            display_name: displayName,
            avatar_url: $('#template-edit-avatar-url').value || null,
            description: $('#template-edit-description').value || null,
            system_prompt: $('#template-edit-system-prompt').value || null,
            model: $('#template-edit-model').value || null,
            provider: $('#template-edit-provider').value || null
        };
        var btn = e.submitter;
        setBusy(btn, true);
        try {
            if (templateId) {
                await api('PUT', '/api/personas/templates/' + encodeURIComponent(templateId), payload);
            } else {
                var slug = name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
                payload.template_id = 'custom-' + (slug || Date.now().toString(36));
                await api('POST', '/api/personas/templates', payload);
            }
            markOwnWrite();
            Modals.markClean(modal);
            Modals.close(modal);
            toast.success('Template saved.');
            reloadKeepingFocus(modal._opener, loadPersonaTemplates);
        } catch (err) {
            if (err.status !== 401) G.setFormAlert(form, describeError(err), 'danger');
        }
        setBusy(btn, false);
    }

    async function deleteTemplate() {
        var modal = $('#template-modal');
        var templateId = $('#template-edit-id').value;
        if (!templateId) return;
        var ok = await confirmDialog({
            title: 'Delete this template?',
            message: 'Channels that already use it keep their copied settings, but the template itself is removed.',
            confirmLabel: 'Delete template'
        });
        if (!ok) return;
        try {
            await api('DELETE', '/api/personas/templates/' + encodeURIComponent(templateId));
            markOwnWrite();
            Modals.markClean(modal);
            Modals.close(modal);
            toast.success('Template deleted.');
            loadPersonaTemplates();
        } catch (e) {
            if (e.status !== 401) G.setFormAlert($('#template-edit-form'), describeError(e), 'danger');
        }
    }

    // Apply template to channel (picker)

    var pendingTemplateId = null;

    async function applyTemplatePrompt(templateId, opener) {
        pendingTemplateId = templateId;
        var t = personaTemplatesData.find(function (x) { return x.template_id === templateId; });
        $('#channel-picker-title').textContent = 'Apply "' + (t ? t.name : templateId) + '" to a channel';
        $('#channel-picker-search').value = '';
        Modals.open($('#channel-picker-modal'), { opener: opener || document.activeElement });
        await loadChannelPickerList();
    }

    async function loadChannelPickerList() {
        var container = $('#channel-picker-list');
        if (!allChannelsData || allChannelsData.length === 0) {
            container.innerHTML = skeleton('rows', 5);
            try {
                allChannelsData = await api('GET', '/api/channels/all');
            } catch (e) {
                if (e.status !== 401) container.innerHTML = emptyState('alert-triangle', 'Could not load channels', describeError(e));
                return;
            }
        }
        renderChannelPickerList('');
    }

    function renderChannelPickerList(filter) {
        var container = $('#channel-picker-list');
        var filterLower = (filter || '').toLowerCase();

        var filtered = allChannelsData.filter(function (ch) {
            if (!filterLower) return true;
            var searchable = (ch.channel_name || '').toLowerCase() + ' ' + (ch.guild_name || '').toLowerCase() + ' ' +
                (ch.category || '').toLowerCase() + ' ' + (ch.channel_id || '');
            return searchable.indexOf(filterLower) !== -1;
        });

        if (filtered.length === 0) {
            container.innerHTML = emptyState('search', 'No channels match your search');
            return;
        }

        var guilds = {};
        filtered.forEach(function (ch) {
            var key = ch.guild_name || 'Unknown server';
            if (!guilds[key]) guilds[key] = [];
            guilds[key].push(ch);
        });

        var html = '';
        var first = true;
        Object.keys(guilds).sort().forEach(function (guildName) {
            html += '<div class="channel-picker-group-header" role="presentation">' + escapeHtml(guildName) + '</div>';
            guilds[guildName].forEach(function (ch) {
                var name = ch.channel_name || ch.channel_id;
                html += '<div class="channel-picker-item" role="option" aria-selected="false" tabindex="' + (first ? '0' : '-1') + '" data-channel-id="' + escapeHtml(ch.channel_id) + '">' +
                    '<span class="channel-picker-name">#' + escapeHtml(name) + '</span>' +
                    (ch.category ? '<span class="channel-picker-category">' + escapeHtml(ch.category) + '</span>' : '') + '</div>';
                first = false;
            });
        });
        container.innerHTML = html;
    }

    function pickerItems() {
        return $$('#channel-picker-list .channel-picker-item');
    }

    function focusPickerItem(items, index) {
        if (!items.length) return;
        index = Math.max(0, Math.min(items.length - 1, index));
        items.forEach(function (it) { it.tabIndex = -1; });
        items[index].tabIndex = 0;
        items[index].focus();
        items[index].scrollIntoView({ block: 'nearest' });
    }

    function onPickerKeydown(e) {
        var item = e.target.closest('.channel-picker-item');
        if (!item) return;
        var items = pickerItems();
        var idx = items.indexOf(item);
        if (e.key === 'ArrowDown') { e.preventDefault(); focusPickerItem(items, idx + 1); }
        else if (e.key === 'ArrowUp') {
            e.preventDefault();
            if (idx === 0) $('#channel-picker-search').focus(); else focusPickerItem(items, idx - 1);
        }
        else if (e.key === 'Home') { e.preventDefault(); focusPickerItem(items, 0); }
        else if (e.key === 'End') { e.preventDefault(); focusPickerItem(items, items.length - 1); }
        else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); applyTemplateToChannel(item.getAttribute('data-channel-id')); }
    }

    async function applyTemplateToChannel(channelId) {
        if (!pendingTemplateId) return;
        var existing = channelPersonasData.find(function (p) { return p.channel_id === channelId; });
        if (existing) {
            var ok = await confirmDialog({
                title: 'Replace the existing persona?',
                message: 'This channel already uses the persona "' + existing.display_name + '". Applying the template replaces it.',
                confirmLabel: 'Replace persona'
            });
            if (!ok) return;
        }
        try {
            await api('POST', '/api/personas/channels/' + encodeURIComponent(channelId) + '/apply-template', { template_id: pendingTemplateId });
            markOwnWrite();
            Modals.close($('#channel-picker-modal'));
            pendingTemplateId = null;
            toast.success('Template applied to channel.');
            loadChannelPersonas();
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Could not apply template' });
        }
    }

    // ── Image generation ─────────────────────────────────────

    var IMAGE_PROVIDERS = ['ai_horde', 'cloudflare', 'openai', 'comfyui', 'openrouter'];

    async function loadImageSettings() {
        var data;
        try {
            data = await api('GET', '/api/image/settings');
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Could not load image settings' });
            return;
        }
        $('#image-active-provider').value = data.active_provider || 'ai_horde';

        var available = data.available_providers || [];
        $('#image-available-providers').innerHTML = IMAGE_PROVIDERS.map(function (p) {
            var isAvail = available.indexOf(p) !== -1;
            return '<span class="provider-badge ' + (isAvail ? 'available' : 'unavailable') + '">' + escapeHtml(p) + '</span>';
        }).join('');

        var configs = data.configs || {};
        loadProviderConfigValues(configs);

        var modalities = data.openrouter_modalities || ['image', 'text'];
        $('#mod-image').checked = modalities.indexOf('image') !== -1;
        $('#mod-text').checked = modalities.indexOf('text') !== -1;
        $('#mod-audio').checked = modalities.indexOf('audio') !== -1;
        updateModalitiesPreview();
        showProviderConfigPanel($('#image-config-provider').value);

        snapshot('imageProvider', 'modalities', 'config-ai_horde', 'config-cloudflare', 'config-openai', 'config-comfyui');
        await loadOpenRouterImageModels(configs.openrouter ? configs.openrouter.model : null);
        snapshot('config-openrouter');
    }

    function loadProviderConfigValues(configs) {
        var horde = configs.ai_horde || {};
        $('#horde-model').value = horde.model || 'stable_diffusion_xl';
        $('#horde-size').value = horde.size || '1024x1024';
        $('#horde-steps').value = horde.steps || 30;

        var cf = configs.cloudflare || {};
        $('#cf-size').value = cf.size || '768x768';
        $('#cf-steps').value = cf.steps || 25;

        var oai = configs.openai || {};
        $('#oai-model').value = oai.model || 'dall-e-3';
        $('#oai-quality').value = oai.quality || 'standard';
        $('#oai-style').value = oai.style || 'vivid';

        var comfy = configs.comfyui || {};
        $('#comfy-model').value = comfy.model || '';
        $('#comfy-size').value = comfy.size || '512x512';
        $('#comfy-steps').value = comfy.steps || 20;

        var or = configs.openrouter || {};
        $('#or-aspect').value = or.aspect_ratio || '1:1';
        $('#or-image-size').value = or.image_size || '1K';
    }

    function showProviderConfigPanel(provider) {
        IMAGE_PROVIDERS.forEach(function (p) {
            var el = document.getElementById('config-' + p);
            if (el) el.hidden = p !== provider;
        });
    }

    function populateObjectModelSelect(selectEl, models, currentValue, emptyMessage, failed) {
        var html = '';
        var inList = !!currentValue && models.some(function (m) { return (m.id || m) === currentValue; });
        if (currentValue && !inList) {
            html += '<option value="' + escapeHtml(currentValue) + '" selected>' +
                escapeHtml(currentValue + ' (current, ' + (failed ? 'model list unavailable' : 'not in live list') + ')') + '</option>';
        }
        models.forEach(function (m) {
            var id = m.id || m;
            var name = m.name || id;
            var isCurrent = id === currentValue;
            html += '<option value="' + escapeHtml(id) + '"' + (isCurrent ? ' selected' : '') + '>' +
                escapeHtml(isCurrent ? name + ' (current)' : name) + '</option>';
        });
        if (!html) html = '<option value="">' + escapeHtml(emptyMessage) + '</option>';
        selectEl.innerHTML = html;
        if (currentValue) selectEl.value = currentValue;
        revealSelected(selectEl);
        if (failed) setFieldNote(selectEl, 'Could not load the live model list. The current model is kept.', 'warning');
        else if (currentValue && !inList) setFieldNote(selectEl, 'The current model is not in the live list. It may have been retired.', 'warning');
        else setFieldNote(selectEl, '');
    }

    async function loadOpenRouterImageModels(currentModel) {
        var selectEl = $('#or-model');
        if (imageModelsCache) {
            populateObjectModelSelect(selectEl, imageModelsCache, currentModel, 'No image models available', false);
            return;
        }
        selectEl.innerHTML = '<option value="">Loading image models...</option>';
        try {
            var models = await api('GET', '/api/image/models');
            imageModelsCache = Array.isArray(models) ? models : [];
            populateObjectModelSelect(selectEl, imageModelsCache, currentModel, 'No image models available', false);
        } catch (e) {
            populateObjectModelSelect(selectEl, [], currentModel, 'Image models unavailable', true);
        }
    }

    function selectedModalities() {
        var out = [];
        if ($('#mod-image').checked) out.push('image');
        if ($('#mod-text').checked) out.push('text');
        if ($('#mod-audio').checked) out.push('audio');
        return out;
    }

    function updateModalitiesPreview() {
        var preview = $('#modalities-preview');
        if (preview) preview.textContent = JSON.stringify(selectedModalities());
    }

    async function saveImageProvider(e) {
        e.preventDefault();
        var btn = e.submitter;
        setBusy(btn, true);
        try {
            await api('PUT', '/api/image/settings', { active_provider: $('#image-active-provider').value });
            markOwnWrite();
            showSaveStatus('image-provider-save-status', 'Provider saved', false);
            snapshot('imageProvider');
        } catch (err) {
            handleFormError(e.currentTarget, 'image-provider-save-status', err, {});
        }
        setBusy(btn, false);
    }

    async function saveModalities(e) {
        e.preventDefault();
        var modalities = selectedModalities();
        if (modalities.length === 0) {
            showSaveStatus('modalities-save-status', 'Select at least one modality', true);
            return;
        }
        var btn = e.submitter;
        setBusy(btn, true);
        try {
            await api('PUT', '/api/image/settings', { openrouter_modalities: modalities });
            markOwnWrite();
            showSaveStatus('modalities-save-status', 'Modalities saved', false);
            snapshot('modalities');
        } catch (err) {
            handleFormError(e.currentTarget, 'modalities-save-status', err, {});
        }
        setBusy(btn, false);
    }

    async function saveProviderConfig(provider, form, btn) {
        G.clearFieldErrors(form);
        var config = {};
        var steps;
        switch (provider) {
            case 'ai_horde':
                steps = readInt('horde-steps');
                config = { model: $('#horde-model').value, size: $('#horde-size').value, steps: steps };
                break;
            case 'cloudflare':
                steps = readInt('cf-steps');
                config = { size: $('#cf-size').value, steps: steps };
                break;
            case 'openai':
                steps = 0;
                config = { model: $('#oai-model').value, quality: $('#oai-quality').value, style: $('#oai-style').value };
                break;
            case 'comfyui':
                steps = readInt('comfy-steps');
                config = { size: $('#comfy-size').value, steps: steps };
                if ($('#comfy-model').value) config.model = $('#comfy-model').value;
                break;
            case 'openrouter':
                steps = 0;
                config = { model: $('#or-model').value, aspect_ratio: $('#or-aspect').value, image_size: $('#or-image-size').value };
                break;
        }
        if (steps === null) {
            showSaveStatus('config-save-' + provider, 'Fix the highlighted fields', true);
            return;
        }
        setBusy(btn, true);
        try {
            await api('PUT', '/api/image/settings', { provider: provider, config: config });
            markOwnWrite();
            showSaveStatus('config-save-' + provider, 'Config saved', false);
            snapshot('config-' + provider);
        } catch (err) {
            handleFormError(form, 'config-save-' + provider, err, {});
        }
        setBusy(btn, false);
    }

    // ── Video generation ─────────────────────────────────────

    var videoActiveJob = null;
    var videoPollTimer = null;

    async function loadVideoSettings() {
        var data;
        try {
            data = await api('GET', '/api/video/settings');
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Could not load video settings' });
            return;
        }
        var cfg = data.config || {};
        $('#video-duration').value = cfg.duration != null ? String(cfg.duration) : '8';
        $('#video-aspect').value = cfg.aspect_ratio || '16:9';
        $('#video-resolution').value = cfg.resolution || '720p';
        $('#video-audio').checked = cfg.audio !== false;
        $('#video-availability').innerHTML = data.available
            ? '<span class="provider-badge available">configured</span>'
            : '<span class="provider-badge unavailable">missing</span>';
        await loadVideoModels(cfg.model);
        snapshot('video');
    }

    async function loadVideoModels(currentModel) {
        var selectEl = $('#video-model');
        if (videoModelsCache) {
            populateObjectModelSelect(selectEl, videoModelsCache, currentModel, 'No video models available', false);
            renderVideoModelInfo(currentModel);
            return;
        }
        selectEl.innerHTML = '<option value="">Loading video models...</option>';
        try {
            var resp = await api('GET', '/api/video/models');
            videoModelsCache = resp.models || [];
            populateObjectModelSelect(selectEl, videoModelsCache, currentModel, 'No video models available', false);
        } catch (e) {
            populateObjectModelSelect(selectEl, [], currentModel, 'Video models unavailable', true);
        }
        renderVideoModelInfo(currentModel);
    }

    function renderVideoModelInfo(modelId) {
        var info = $('#video-model-info');
        if (!modelId || !videoModelsCache) { info.textContent = ''; return; }
        var m = videoModelsCache.find(function (x) { return (x.id || x) === modelId; });
        if (!m) { info.textContent = ''; return; }
        var parts = [];
        if (m.supported_resolutions && m.supported_resolutions.length) parts.push('Resolutions: ' + m.supported_resolutions.join(', '));
        if (m.supported_aspect_ratios && m.supported_aspect_ratios.length) parts.push('Aspect ratios: ' + m.supported_aspect_ratios.join(', '));
        if (m.supported_durations && m.supported_durations.length) parts.push('Durations: ' + m.supported_durations.join(', ') + 's');
        if (m.supports_audio) parts.push('audio supported');
        if (m.supports_image_input) parts.push('image-to-video supported');
        info.textContent = parts.join(' · ');
    }

    async function saveVideoConfig(e) {
        e.preventDefault();
        var form = e.currentTarget;
        G.clearFieldErrors(form);
        var modelEl = $('#video-model');
        if (!modelEl.value) {
            G.setFieldError(modelEl, 'Pick a model');
            showSaveStatus('video-config-save-status', 'Please pick a model', true);
            return;
        }
        var btn = e.submitter;
        setBusy(btn, true);
        try {
            await api('PUT', '/api/video/settings', {
                active_provider: 'openrouter',
                config: {
                    model: modelEl.value,
                    duration: parseInt($('#video-duration').value, 10),
                    aspect_ratio: $('#video-aspect').value,
                    resolution: $('#video-resolution').value,
                    audio: $('#video-audio').checked
                }
            });
            markOwnWrite();
            showSaveStatus('video-config-save-status', 'Config saved', false);
            snapshot('video');
            renderVideoModelInfo(modelEl.value);
        } catch (err) {
            handleFormError(form, 'video-config-save-status', err, { model: 'video-model' });
        }
        setBusy(btn, false);
    }

    function stopVideoPolling() {
        if (videoPollTimer) {
            clearTimeout(videoPollTimer);
            videoPollTimer = null;
        }
        videoActiveJob = null;
        var cancelBtn = $('#video-test-cancel');
        if (cancelBtn) cancelBtn.hidden = true;
    }

    async function submitVideoTest(e) {
        e.preventDefault();
        var promptEl = $('#video-test-prompt');
        var prompt = (promptEl.value || '').trim();
        G.clearFieldErrors(e.currentTarget);
        if (!prompt) {
            G.setFieldError(promptEl, 'A prompt is required');
            return;
        }
        stopVideoPolling();
        $('#video-test-result').innerHTML = '';
        showSaveStatus('video-test-status', 'Submitting...', false);
        var btn = e.submitter;
        setBusy(btn, true);
        try {
            var imgEl = $('#video-test-image');
            var resp = await api('POST', '/api/video/generate', {
                prompt: prompt,
                model: $('#video-model').value || undefined,
                duration: parseInt($('#video-duration').value, 10),
                aspect_ratio: $('#video-aspect').value,
                resolution: $('#video-resolution').value,
                audio: $('#video-audio').checked,
                image_url: imgEl.value || undefined
            });
            videoActiveJob = resp.job_id;
            showSaveStatus('video-test-status', 'Job ' + resp.job_id + ' submitted (' + (resp.status || 'queued') + ')', false);
            $('#video-test-cancel').hidden = false;
            pollVideoJob(resp.job_id);
        } catch (err) {
            if (err.status !== 401) showSaveStatus('video-test-status', 'Error: ' + describeError(err), true);
        }
        setBusy(btn, false);
    }

    function pollVideoJob(jobId) {
        if (videoActiveJob !== jobId) return;
        videoPollTimer = setTimeout(async function () {
            if (videoActiveJob !== jobId) return;
            try {
                var status = await api('GET', '/api/video/jobs/' + encodeURIComponent(jobId));
                showSaveStatus('video-test-status', 'Job ' + jobId + ' - ' + (status.status || 'unknown'), false);
                var terminal = ['completed', 'failed', 'canceled', 'cancelled', 'error'];
                if (terminal.indexOf(status.status) !== -1) {
                    renderVideoTestResult(jobId, status);
                    stopVideoPolling();
                } else {
                    pollVideoJob(jobId);
                }
            } catch (err) {
                if (err.status !== 401) showSaveStatus('video-test-status', 'Poll error: ' + describeError(err), true);
                stopVideoPolling();
            }
        }, 5000);
    }

    function renderVideoTestResult(jobId, status) {
        var box = $('#video-test-result');
        if (status.status !== 'completed') {
            box.innerHTML = emptyState('x-circle', 'Job ' + jobId + ' ended', 'Status: ' + status.status);
            return;
        }
        var url = sanitizeHttpUrl((status.unsigned_urls || [])[0]);
        if (!url) {
            box.innerHTML = emptyState('info', 'Completed, but no download URL was returned');
            return;
        }
        box.innerHTML = '<video class="media-video" controls preload="metadata"><source src="' + escapeHtml(url) + '" type="video/mp4"></video>' +
            '<p><a href="' + escapeHtml(url) + '" target="_blank" rel="noopener">Open video in new tab</a></p>';
    }

    // ── WebSocket ────────────────────────────────────────────

    var WS_STATES = {
        online: { chip: 'Live', badge: 'Connected', cls: 'badge-success' },
        connecting: { chip: 'Connecting', badge: 'Connecting', cls: 'badge-warning' },
        reconnecting: { chip: 'Reconnecting', badge: 'Reconnecting', cls: 'badge-warning' },
        offline: { chip: 'Offline', badge: 'Disconnected', cls: 'badge-warning' }
    };

    function updateWsStatus(state) {
        var info = WS_STATES[state] || WS_STATES.offline;
        var chip = $('#ws-chip');
        chip.dataset.state = state;
        chip.querySelector('.status-chip-text').textContent = info.chip;
        var badge = $('#ws-status');
        badge.textContent = info.badge;
        badge.className = 'badge ' + info.cls;
    }

    function connectWs() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
        clearTimeout(wsTimer);
        wsTimer = null;

        var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        var sock = new WebSocket(proto + '//' + location.host + '/ws');
        var opened = false;
        ws = sock;
        updateWsStatus(wsAttempts ? 'reconnecting' : 'connecting');

        sock.onopen = function () {
            opened = true;
            updateWsStatus('online');
        };

        sock.onmessage = function (event) {
            var data;
            try { data = JSON.parse(event.data); } catch (e) { return; }
            if (data.type === 'connected') { wsAttempts = 0; updateWsStatus('online'); return; }
            if (data.type === 'pong' || data.type === 'error') return;
            handleWsEvent(data);
        };

        sock.onclose = function (ev) {
            if (ws !== sock) return;
            ws = null;
            if (!loggedIn) return;
            updateWsStatus('reconnecting');
            if (ev.code === 4401 || !opened) verifyAuthThenReconnect();
            else scheduleWsReconnect();
        };
    }

    async function verifyAuthThenReconnect() {
        try {
            var data = await api('GET', '/api/auth/check', undefined, { noAuthRedirect: true });
            if (!data.authenticated) {
                expiredHandled = true;
                showLogin('Your session expired. Please sign in again.');
                return;
            }
        } catch (e) { /* server unreachable: keep retrying */ }
        if (loggedIn) scheduleWsReconnect();
    }

    function scheduleWsReconnect() {
        if (wsTimer || !loggedIn) return;
        var delay = Math.min(WS_BACKOFF_MAX_MS, WS_BACKOFF_BASE_MS * Math.pow(2, wsAttempts));
        wsAttempts++;
        wsTimer = setTimeout(function () {
            wsTimer = null;
            if (loggedIn) connectWs();
        }, delay);
    }

    function reconnectNow() {
        if (!loggedIn || (ws && ws.readyState <= 1)) return;
        clearTimeout(wsTimer);
        wsTimer = null;
        wsAttempts = 0;
        connectWs();
    }

    function disconnectWs() {
        clearTimeout(wsTimer);
        wsTimer = null;
        wsAttempts = 0;
        if (ws) {
            var sock = ws;
            ws = null;
            sock.close();
        }
        updateWsStatus('offline');
    }

    function handleWsEvent(data) {
        addActivityEntry(data);
        var type = String(data.type || '');
        var tab = currentTab;
        var dirty = G.dirtyForms($('#tab-' + tab)).length > 0;
        var recentlyOwn = Date.now() - lastOwnWriteAt < 4000;

        function refresh(loader) {
            if (dirty) {
                if (!recentlyOwn) toast.info('This page was changed elsewhere. Reload the page to see the latest values; your unsaved edits were kept.');
                return;
            }
            loader();
        }

        if (type === 'config_imported' || type === 'database_restored') {
            clearCaches();
            personaTemplatesData = [];
            allChannelsData = [];
            toast.info(type === 'config_imported' ? 'Configuration was imported. Reloading this page.' : 'The database was restored. Reloading this page.');
            reloadCurrentTab();
            return;
        }
        if (type === 'settings_updated') {
            if (tab === 'settings') refresh(loadSettings);
            if (tab === 'overview') loadOverview();
        } else if (type === 'channel_updated' || type === 'channel_reset') {
            if (tab === 'channels') loadChannels();
        } else if (type === 'thread_updated' || type === 'thread_deleted') {
            if (tab === 'threads') loadThreads();
        } else if (type === 'image_settings_updated') {
            if (tab === 'image-gen') refresh(loadImageSettings);
        } else if (type === 'video_settings_updated') {
            if (tab === 'video-gen') refresh(loadVideoSettings);
        } else if (type === 'commands_synced') {
            if (tab === 'overview') showSaveStatus('sync-commands-status', 'Commands synced at ' + formatTime(new Date()), false);
        } else if (type.indexOf('api_key') === 0) {
            if (tab === 'api-keys') { loadAPIKeys(); loadAuditLog(); }
            if (tab === 'overview') loadOverview();
        } else if (type.indexOf('persona_') === 0) {
            if (tab === 'personas') loadPersonas();
        } else if (type.indexOf('memory_') === 0) {
            if (tab === 'memory') {
                loadMemoryStats();
                if (currentMemoryChannelId && (!data.channel_id || String(data.channel_id) === String(currentMemoryChannelId))) {
                    viewChannelMemories(currentMemoryChannelId, currentMemoryChannelName);
                }
            }
        }
    }

    function describeActivity(data) {
        switch (data.type) {
            case 'message_activity':
                return (data.author || 'unknown') + ' in #' + (data.channel_name || 'unknown') + ' (' + (data.guild || '') + ')';
            case 'settings_updated':
                return 'Fields: ' + (data.fields || []).join(', ');
            case 'channel_updated':
                return 'Channel ' + (data.channel_id || '') + ': ' + (data.fields || []).join(', ');
            case 'channel_reset':
                return 'Channel ' + (data.channel_id || '') + ' reset to defaults';
            case 'thread_updated':
                return 'Thread ' + (data.thread_id || '') + ': ' + (data.fields || []).join(', ');
            case 'thread_deleted':
                return 'Thread ' + (data.thread_id || '') + ' deleted';
            case 'prune_completed':
                return JSON.stringify(data.stats || {});
            default:
                var rest = {};
                Object.keys(data).forEach(function (k) { if (k !== 'type' && k !== 'timestamp') rest[k] = data[k]; });
                return truncate(JSON.stringify(rest), 240);
        }
    }

    function addActivityEntry(data) {
        var feed = $('#activity-feed');
        var placeholder = feed.querySelector('.empty-state');
        if (placeholder) placeholder.remove();

        var entry = document.createElement('div');
        entry.className = 'activity-entry';
        var time = document.createElement('span');
        time.className = 'time';
        time.textContent = formatTime(data.timestamp ? new Date(data.timestamp) : new Date());
        var kind = document.createElement('span');
        kind.className = 'event-type';
        kind.textContent = String(data.type);
        var detail = document.createElement('span');
        detail.className = 'detail';
        detail.textContent = describeActivity(data);
        entry.appendChild(time);
        entry.appendChild(kind);
        entry.appendChild(detail);
        feed.insertBefore(entry, feed.firstChild);
        while (feed.children.length > 200) feed.removeChild(feed.lastChild);
    }

    function resetActivityFeed() {
        $('#activity-feed').innerHTML = emptyState('radio', 'Waiting for events...', 'Messages, setting changes and other bot activity show up here live.');
    }

    function startPeriodicRefresh() {
        setInterval(function () {
            if (loggedIn && currentTab === 'overview' && document.visibilityState === 'visible') loadOverview();
        }, 30000);

        setInterval(function () {
            if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'ping' }));
        }, 30000);
    }

    // ── API keys ─────────────────────────────────────────────

    async function loadAPIKeys() {
        var container = $('#api-keys-list');
        if (!container.dataset.loaded) container.innerHTML = skeleton('cards', 3);
        var banner = $('#api-keys-status-banner');

        try {
            var keys = await api('GET', '/api/keys');
            container.dataset.loaded = '1';
            banner.hidden = true;
            if (keys.length === 0) {
                container.innerHTML = emptyState('key', 'No API keys stored yet', 'Add a key, or import the ones from your .env file, to get started.');
                return;
            }
            container.innerHTML = keys.map(function (key) {
                var status = key.validation_status || 'untested';
                var statusClass = status === 'valid' ? 'status-valid' : (status === 'invalid' ? 'status-invalid' : 'status-untested');
                var label = key.key_alias || key.provider;
                return '<div class="key-card' + (status === 'invalid' ? ' is-invalid' : '') + '">' +
                    '<div class="key-card-header">' +
                    '<span class="key-provider-label">' + escapeHtml(key.provider) + '</span>' +
                    '<span class="key-status ' + statusClass + '">' + escapeHtml(status) + '</span>' +
                    '</div>' +
                    '<div class="key-card-body">' +
                    '<div class="key-masked"><code>' + escapeHtml(key.masked_key) + '</code></div>' +
                    (key.key_alias ? '<div class="key-alias">' + escapeHtml(key.key_alias) + '</div>' : '') +
                    '<div class="key-meta">' +
                    '<span>Created ' + escapeHtml(formatDateTime(key.created_at)) + '</span>' +
                    (key.last_used ? '<span>Last used ' + escapeHtml(formatDateTime(key.last_used)) + '</span>' : '') +
                    '</div></div>' +
                    '<div class="key-card-actions">' +
                    '<button type="button" class="btn btn-sm btn-secondary" ' + actionAttrs('validateKey', [key.key_id]) + ' aria-label="Test key ' + escapeHtml(label) + '">Test</button>' +
                    '<button type="button" class="btn btn-sm btn-secondary" ' + actionAttrs('editKey', [key.key_id, key.provider, key.key_alias || '']) + ' aria-label="Edit key ' + escapeHtml(label) + '">Edit</button>' +
                    '<button type="button" class="btn btn-sm btn-danger-ghost" ' + actionAttrs('deleteKey', [key.key_id, key.provider]) + ' aria-label="Delete key ' + escapeHtml(label) + '">Delete</button>' +
                    '</div></div>';
            }).join('');
        } catch (e) {
            if (e.status === 401) return;
            if (e.message && e.message.indexOf('not configured') !== -1) {
                banner.hidden = false;
                $('#api-keys-status-message').textContent = 'API key management requires ENCRYPTION_MASTER_KEY to be set in your .env file.';
                container.innerHTML = '';
            } else {
                container.innerHTML = emptyState('alert-triangle', 'Could not load API keys', describeError(e));
            }
        }
    }

    async function loadAuditLog() {
        var container = $('#audit-log-container');
        if (!container.dataset.loaded) container.innerHTML = '<div class="pad">' + skeleton('rows', 3) + '</div>';
        try {
            var entries = await api('GET', '/api/keys/audit?limit=30');
            container.dataset.loaded = '1';
            if (entries.length === 0) {
                container.innerHTML = emptyState('archive', 'No audit entries yet');
                return;
            }
            var html = '<table class="data-table"><thead><tr>' +
                '<th scope="col">Time</th><th scope="col">Key ID</th><th scope="col">Action</th><th scope="col">User</th><th scope="col">Details</th>' +
                '</tr></thead><tbody>';
            entries.forEach(function (entry) {
                html += '<tr>' +
                    '<td>' + escapeHtml(formatDateTime(entry.timestamp)) + '</td>' +
                    '<td><code>' + escapeHtml((entry.key_id || '').substring(0, 8)) + '...</code></td>' +
                    '<td>' + escapeHtml(entry.action) + '</td>' +
                    '<td>' + escapeHtml(entry.user_identifier || '-') + '</td>' +
                    '<td>' + escapeHtml(entry.details || '-') + '</td>' +
                    '</tr>';
            });
            container.innerHTML = html + '</tbody></table>';
        } catch (e) {
            if (e.status !== 401) container.innerHTML = emptyState('archive', 'Audit log not available');
        }
    }

    function openKeyModal(opts) {
        var modal = $('#key-modal');
        var form = $('#key-edit-form');
        G.clearFieldErrors(form);
        G.setFormAlert(form, '');
        Modals.watchDirty(modal);
        $('#key-modal-title').textContent = opts.id ? 'Edit API key' : 'Add API key';
        $('#key-edit-id').value = opts.id || '';
        $('#key-edit-provider').value = opts.provider || '';
        $('#key-edit-provider').disabled = !!opts.id;
        $('#key-edit-value').value = '';
        $('#key-edit-value').placeholder = opts.id ? 'Leave blank to keep the current key' : 'Enter API key or URL';
        $('#key-edit-alias').value = opts.alias || '';
        var status = $('#key-validation-result');
        status.textContent = '';
        status.className = 'save-status';
        modal._opener = opts.opener || document.activeElement;
        Modals.markClean(modal);
        Modals.open(modal, { opener: opts.opener || document.activeElement, focus: opts.id ? $('#key-edit-value') : $('#key-edit-provider') });
    }

    async function deleteKey(keyId, provider, opener) {
        var ok = await confirmDialog({
            title: 'Delete this API key?',
            message: 'The stored ' + provider + ' key will be removed permanently.',
            details: ['The bot falls back to the value in .env, if there is one', 'Features that need this provider stop working without it'],
            confirmLabel: 'Delete key',
            opener: opener
        });
        if (!ok) return;
        try {
            await api('DELETE', '/api/keys/' + encodeURIComponent(keyId));
            markOwnWrite();
            toast.success('API key deleted.');
            loadAPIKeys();
            loadAuditLog();
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Could not delete key' });
        }
    }

    async function validateKey(keyId, btn) {
        setBusy(btn, true);
        try {
            var result = await api('POST', '/api/keys/' + encodeURIComponent(keyId) + '/validate');
            if (result.valid) toast.success(result.message || 'The provider accepted this key.', { title: 'Key is valid' });
            else toast.error(result.message || 'The provider rejected this key.', { title: 'Key is invalid' });
            reloadKeepingFocus(btn, loadAPIKeys);
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Validation failed' });
            setBusy(btn, false);
        }
    }

    async function saveAPIKey(e) {
        e.preventDefault();
        var form = e.currentTarget;
        var modal = $('#key-modal');
        G.clearFieldErrors(form);
        G.setFormAlert(form, '');
        var keyId = $('#key-edit-id').value;
        var providerEl = $('#key-edit-provider');
        var valueEl = $('#key-edit-value');
        var provider = providerEl.value;
        var keyValue = valueEl.value.trim();
        var alias = $('#key-edit-alias').value.trim();

        if (!provider) {
            G.setFieldError(providerEl, 'Select a provider');
            return;
        }
        if (!keyId && !keyValue) {
            G.setFieldError(valueEl, 'An API key is required');
            return;
        }
        var btn = e.submitter;
        setBusy(btn, true);
        try {
            if (keyId) {
                var updateBody = { alias: alias };
                if (keyValue) updateBody.key = keyValue;
                await api('PUT', '/api/keys/' + encodeURIComponent(keyId), updateBody);
            } else {
                await api('POST', '/api/keys', { provider: provider, key: keyValue, alias: alias || null });
            }
            markOwnWrite();
            Modals.markClean(modal);
            Modals.close(modal);
            toast.success(keyId ? 'API key updated.' : 'API key saved.');
            reloadKeepingFocus(modal._opener, loadAPIKeys);
            loadAuditLog();
        } catch (err) {
            if (err.status !== 401) G.setFormAlert(form, describeError(err), 'danger');
        }
        setBusy(btn, false);
    }

    async function testKeyFromModal() {
        var keyId = $('#key-edit-id').value;
        var provider = $('#key-edit-provider').value;
        var keyValue = $('#key-edit-value').value.trim();
        var statusEl = $('#key-validation-result');
        var btn = $('#key-test-btn');
        G.clearFieldErrors($('#key-edit-form'));

        var useStored = keyId && !keyValue;
        if (!useStored && !provider) {
            G.setFieldError($('#key-edit-provider'), 'Select a provider');
            return;
        }
        if (!useStored && !keyValue) {
            G.setFieldError($('#key-edit-value'), 'Enter a key to test');
            return;
        }
        statusEl.textContent = 'Testing...';
        statusEl.className = 'save-status';
        setBusy(btn, true);
        try {
            var result = useStored
                ? await api('POST', '/api/keys/' + encodeURIComponent(keyId) + '/validate')
                : await api('POST', '/api/keys/test', { provider: provider, key: keyValue });
            statusEl.textContent = result.valid
                ? 'Key is valid' + (result.message ? ': ' + result.message : '')
                : 'Invalid: ' + (result.message || 'the provider rejected this key');
            statusEl.className = 'save-status ' + (result.valid ? 'success' : 'error');
            if (useStored) loadAPIKeys();
        } catch (err) {
            if (err.status !== 401) {
                statusEl.textContent = 'Test failed: ' + describeError(err);
                statusEl.className = 'save-status error';
            }
        }
        setBusy(btn, false);
    }

    async function importFromEnv() {
        var ok = await confirmDialog({
            title: 'Import keys from .env?',
            message: 'Keys found in the .env file are copied into encrypted database storage.',
            details: ['Keys that already exist in the database are not overwritten'],
            confirmLabel: 'Import keys',
            danger: false
        });
        if (!ok) return;
        var btn = $('#import-env-btn');
        setBusy(btn, true);
        try {
            var result = await api('POST', '/api/keys/import-env');
            var imported = result.imported || [];
            var skipped = result.skipped || [];
            var msg = 'Imported: ' + (imported.length ? imported.join(', ') : 'none');
            if (skipped.length) msg += ' | Skipped: ' + skipped.join(', ');
            toast.success(msg, { title: 'Import finished' });
            markOwnWrite();
            loadAPIKeys();
            loadAuditLog();
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Import failed' });
        }
        setBusy(btn, false);
    }

    // ── Backup & restore ─────────────────────────────────────

    var pendingConfigFile = null;
    var pendingDbFile = null;
    var lastConfigPreview = null;

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

    async function downloadBackup(path, fallbackName, statusId, busyText, doneText, btn) {
        setBusy(btn, true);
        showSaveStatus(statusId, busyText, false);
        try {
            var res = await G.apiBlob(path);
            var filename = fallbackName;
            if (res.disposition) {
                var match = res.disposition.match(/filename="?([^";]+)"?/);
                if (match) filename = match[1];
            }
            triggerDownload(res.blob, filename);
            showSaveStatus(statusId, doneText, false);
        } catch (e) {
            if (e.status !== 401) showSaveStatus(statusId, 'Export failed: ' + describeError(e), true);
        }
        setBusy(btn, false);
    }

    function exportConfig() {
        downloadBackup('/api/backup/config', 'gideon_config.json', 'export-config-status', 'Preparing export...', 'Config exported', $('#export-config-btn'));
    }

    function exportDatabase() {
        downloadBackup('/api/backup/database', 'gideon_backup.db', 'export-db-status', 'Creating backup...', 'Database exported', $('#export-db-btn'));
    }

    function backupErrors(err) {
        var details = err.data && err.data.details;
        if (Array.isArray(details) && details.length) return details.join(', ');
        return describeError(err);
    }

    function handleConfigFileSelect(file) {
        if (!file || !/\.json$/i.test(file.name)) {
            showSaveStatus('import-config-status', 'Please select a .json file', true);
            return;
        }
        pendingConfigFile = file;
        lastConfigPreview = null;
        $('#config-file-name').textContent = file.name + ' (' + formatBytes(file.size) + ')';
        $('#validate-config-btn').disabled = false;
        $('#import-config-btn').disabled = true;
        $('#config-import-preview').hidden = true;
    }

    var CONFIG_PREVIEW_LINES = [
        ['GLOBAL_CONFIG', 'global settings'],
        ['CHANNELS', 'channels'],
        ['CHANNEL_CONFIG', 'channel configs'],
        ['PERSONA_TEMPLATES', 'persona templates'],
        ['CHANNEL_PERSONAS', 'channel personas'],
        ['USERS', 'user preferences']
    ];

    function configPreviewLines(preview) {
        return CONFIG_PREVIEW_LINES.filter(function (l) { return preview && preview[l[0]]; }).map(function (l) {
            return String(preview[l[0]]) + ' ' + l[1];
        });
    }

    async function validateConfigImport() {
        if (!pendingConfigFile) return;
        var formData = new FormData();
        formData.append('file', pendingConfigFile);
        var btn = $('#validate-config-btn');
        setBusy(btn, true);
        showSaveStatus('import-config-status', 'Validating...', false);
        try {
            var result = await api('POST', '/api/backup/config/validate', formData);
            if (result.valid) {
                lastConfigPreview = result.preview || {};
                var lines = configPreviewLines(lastConfigPreview);
                var preview = $('#config-import-preview');
                preview.innerHTML = '<h4>Import preview</h4><ul>' + lines.map(function (l) { return '<li>' + escapeHtml(l) + '</li>'; }).join('') + '</ul>';
                preview.hidden = false;
                $('#import-config-btn').disabled = false;
                showSaveStatus('import-config-status', 'Validation passed', false);
            } else {
                showSaveStatus('import-config-status', 'Validation failed: ' + (result.errors || []).join(', '), true);
                $('#import-config-btn').disabled = true;
            }
        } catch (e) {
            if (e.status !== 401) showSaveStatus('import-config-status', 'Validation error: ' + backupErrors(e), true);
        }
        setBusy(btn, false);
        if (lastConfigPreview) $('#import-config-btn').disabled = false;
    }

    async function importConfig() {
        if (!pendingConfigFile) return;
        var details = configPreviewLines(lastConfigPreview).map(function (l) { return 'Overwrites with ' + l; });
        details.push('Messages, threads and API keys are not touched');
        var ok = await confirmDialog({
            title: 'Import configuration?',
            message: 'The settings in "' + pendingConfigFile.name + '" will replace your current configuration.',
            details: details,
            confirmLabel: 'Import and overwrite'
        });
        if (!ok) return;

        var formData = new FormData();
        formData.append('file', pendingConfigFile);
        var btn = $('#import-config-btn');
        setBusy(btn, true);
        showSaveStatus('import-config-status', 'Importing...', false);
        try {
            var result = await api('POST', '/api/backup/config/import', formData);
            var applied = result.result && result.result.applied ? result.result.applied : {};
            var summary = Object.keys(applied).map(function (t) { return t + ': ' + applied[t]; }).join(', ');
            toast.success(summary || 'Configuration imported.', { title: 'Configuration imported' });
            showSaveStatus('import-config-status', 'Imported' + (summary ? ': ' + summary : ''), false);
            markOwnWrite();
            clearCaches();
            pendingConfigFile = null;
            lastConfigPreview = null;
            $('#config-file-input').value = '';
            $('#config-file-name').textContent = '';
            $('#validate-config-btn').disabled = true;
            $('#config-import-preview').hidden = true;
            setBusy(btn, false);
            btn.disabled = true;
            return;
        } catch (e) {
            if (e.status !== 401) {
                showSaveStatus('import-config-status', 'Import failed: ' + backupErrors(e), true);
                toast.error(backupErrors(e), { title: 'Import failed' });
            }
        }
        setBusy(btn, false);
    }

    function handleDbFileSelect(file) {
        if (!file || !/\.(db|sqlite|sqlite3)$/i.test(file.name)) {
            showSaveStatus('restore-db-status', 'Please select a .db, .sqlite, or .sqlite3 file', true);
            return;
        }
        pendingDbFile = file;
        $('#db-file-name').textContent = file.name + ' (' + formatBytes(file.size) + ')';
        $('#validate-db-btn').disabled = false;
        $('#restore-db-btn').disabled = true;
        $('#db-restore-preview').hidden = true;
    }

    async function validateDbRestore() {
        if (!pendingDbFile) return;
        var formData = new FormData();
        formData.append('file', pendingDbFile);
        var btn = $('#validate-db-btn');
        setBusy(btn, true);
        showSaveStatus('restore-db-status', 'Validating...', false);
        var valid = false;
        try {
            var result = await api('POST', '/api/backup/database/validate', formData);
            if (result.valid) {
                valid = true;
                var info = result.info || {};
                var html = '<h4>Backup info</h4><p>File size: ' + escapeHtml(formatBytes(info.size_bytes || 0)) + '</p>';
                if (info.table_counts) {
                    html += '<ul>' + Object.keys(info.table_counts).map(function (table) {
                        return '<li>' + escapeHtml(table) + ': ' + escapeHtml(info.table_counts[table]) + ' rows</li>';
                    }).join('') + '</ul>';
                }
                var preview = $('#db-restore-preview');
                preview.innerHTML = html;
                preview.hidden = false;
                showSaveStatus('restore-db-status', 'Validation passed', false);
            } else {
                showSaveStatus('restore-db-status', 'Validation failed: ' + (result.errors || []).join(', '), true);
            }
        } catch (e) {
            if (e.status !== 401) showSaveStatus('restore-db-status', 'Validation error: ' + backupErrors(e), true);
        }
        setBusy(btn, false);
        $('#restore-db-btn').disabled = !valid;
    }

    async function restoreDatabase() {
        if (!pendingDbFile) return;
        var ok = await confirmDialog({
            title: 'Restore the database?',
            message: 'This replaces the entire database with "' + pendingDbFile.name + '" (' + formatBytes(pendingDbFile.size) + '). It cannot be undone.',
            details: [
                'All current messages, threads, reminders, trivia stats and settings are replaced',
                'Stored API keys are replaced by the ones in the backup',
                'A copy of the current database is saved automatically before the restore'
            ],
            confirmLabel: 'Restore database',
            requireText: 'RESTORE'
        });
        if (!ok) return;

        var formData = new FormData();
        formData.append('file', pendingDbFile);
        var btn = $('#restore-db-btn');
        setBusy(btn, true);
        showSaveStatus('restore-db-status', 'Restoring database...', false);
        try {
            await api('POST', '/api/backup/database/restore', formData);
            toast.success('The database was restored from the backup.', { title: 'Database restored', timeout: 9000 });
            showSaveStatus('restore-db-status', 'Database restored successfully', false);
            clearCaches();
            pendingDbFile = null;
            $('#db-file-input').value = '';
            $('#db-file-name').textContent = '';
            $('#validate-db-btn').disabled = true;
            $('#db-restore-preview').hidden = true;
            setBusy(btn, false);
            btn.disabled = true;
            loadOverview();
            return;
        } catch (e) {
            if (e.status !== 401) {
                showSaveStatus('restore-db-status', 'Restore failed: ' + backupErrors(e), true);
                toast.error(backupErrors(e), { title: 'Restore failed' });
            }
        }
        setBusy(btn, false);
    }

    function setupDropzone(zoneId, inputId, handler) {
        var zone = document.getElementById(zoneId);
        var input = document.getElementById(inputId);
        if (!zone || !input) return;
        zone.addEventListener('click', function () { input.click(); });
        zone.addEventListener('keydown', function (e) {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
        });
        input.addEventListener('change', function () { if (input.files[0]) handler(input.files[0]); });
        zone.addEventListener('dragover', function (e) { e.preventDefault(); zone.classList.add('drag-over'); });
        zone.addEventListener('dragleave', function () { zone.classList.remove('drag-over'); });
        zone.addEventListener('drop', function (e) {
            e.preventDefault();
            zone.classList.remove('drag-over');
            if (e.dataTransfer.files.length > 0) handler(e.dataTransfer.files[0]);
        });
    }

    // ── Memory ───────────────────────────────────────────────

    async function loadMemoryStats() {
        var container = $('#memory-stats-list');
        if (!container.dataset.loaded) container.innerHTML = '<div class="pad">' + skeleton('rows', 3) + '</div>';
        try {
            var stats = await api('GET', '/api/memory');
            container.dataset.loaded = '1';
            if (!stats || stats.length === 0) {
                container.innerHTML = emptyState('database', 'No channel memories yet', 'Memories accumulate as chat sessions expire.');
                return;
            }
            var rows = stats.map(function (s) {
                var name = s.channel_name || s.channel_id;
                var latest = s.latest_summary_at ? String(s.latest_summary_at).substring(0, 16) : '-';
                return '<tr><td>' + escapeHtml(name) + '</td><td>' + escapeHtml(s.summary_count) + '</td><td>' + escapeHtml(latest) + '</td>' +
                    '<td class="cell-actions"><button type="button" class="btn btn-secondary btn-sm" ' + actionAttrs('viewChannelMemories', [s.channel_id, name]) + ' aria-label="View memories for ' + escapeHtml(name) + '">View</button></td></tr>';
            });
            container.innerHTML = '<table class="data-table"><thead><tr><th scope="col">Channel</th><th scope="col">Summaries</th><th scope="col">Latest</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>' + rows.join('') + '</tbody></table>';
        } catch (e) {
            if (e.status !== 401) container.innerHTML = emptyState('alert-triangle', 'Could not load memory stats', describeError(e));
        }
    }

    async function viewChannelMemories(channelId, channelName) {
        currentMemoryChannelId = channelId;
        currentMemoryChannelName = channelName || currentMemoryChannelName || channelId;
        var card = $('#memory-channel-card');
        var list = $('#memory-entries-list');
        $('#memory-channel-title').textContent = 'Memories: #' + currentMemoryChannelName;
        card.hidden = false;
        list.innerHTML = skeleton('rows', 3);
        try {
            var data = await api('GET', '/api/memory/' + encodeURIComponent(channelId));
            var memories = data.memories || [];
            if (memories.length === 0) {
                list.innerHTML = emptyState('database', 'No memories stored for this channel');
                return;
            }
            list.innerHTML = memories.map(function (m) {
                var ts = String(m.conversation_start || m.created_at || '').substring(0, 16);
                var label = m.conversation_start ? 'Conversation on' : 'Archived';
                return '<div class="memory-entry"><div class="memory-meta">' + label + ' ' + escapeHtml(ts) + ' &bull; ' + escapeHtml(m.message_count) + ' messages</div>' +
                    '<div class="memory-summary">' + escapeHtml(m.summary) + '</div></div>';
            }).join('');
        } catch (e) {
            if (e.status !== 401) list.innerHTML = emptyState('alert-triangle', 'Could not load memories', describeError(e));
        }
    }

    async function clearChannelMemories() {
        if (!currentMemoryChannelId) return;
        var name = currentMemoryChannelName || currentMemoryChannelId;
        var ok = await confirmDialog({
            title: 'Clear all memories?',
            message: 'Every stored memory summary for #' + name + ' will be deleted.',
            details: ['Gideon forgets the long-term context of this channel', 'This cannot be undone'],
            confirmLabel: 'Clear memories',
            requireText: 'DELETE'
        });
        if (!ok) return;
        try {
            await api('DELETE', '/api/memory/' + encodeURIComponent(currentMemoryChannelId));
            markOwnWrite();
            toast.success('Memories cleared for #' + name + '.');
            currentMemoryChannelId = null;
            $('#memory-channel-card').hidden = true;
            $('#memory-entries-list').innerHTML = '';
            loadMemoryStats();
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Could not clear memories' });
        }
    }

    async function triggerSummarize() {
        if (!currentMemoryChannelId) return;
        var ok = await confirmDialog({
            title: 'Summarize and rotate now?',
            message: 'The current conversation history for this channel is summarized and a fresh session starts.',
            confirmLabel: 'Summarize now',
            danger: false
        });
        if (!ok) return;
        var btn = $('#memory-summarize-btn');
        setBusy(btn, true);
        try {
            var result = await api('POST', '/api/memory/' + encodeURIComponent(currentMemoryChannelId) + '/summarize');
            markOwnWrite();
            toast.success(result.messages_archived + ' messages archived.', { title: 'Summary stored' });
            await loadMemoryStats();
            await viewChannelMemories(currentMemoryChannelId, null);
        } catch (e) {
            if (e.status !== 401) toast.error(describeError(e), { title: 'Summarize failed' });
        }
        setBusy(btn, false);
    }

    // ── Actions & init ───────────────────────────────────────

    var ACTIONS = {
        editChannel: function (a, el) { editChannel(a[0], el); },
        editThread: function (a, el) { editThread(a[0], el); },
        editTemplate: function (a, el) { editTemplate(a[0], el); },
        applyTemplatePrompt: function (a, el) { applyTemplatePrompt(a[0], el); },
        editPersona: function (a, el) { editPersona(a[0], el); },
        togglePersona: function (a, el) { togglePersona(a[0], el); },
        validateKey: function (a, el) { validateKey(a[0], el); },
        editKey: function (a, el) { openKeyModal({ id: a[0], provider: a[1], alias: a[2], opener: el }); },
        deleteKey: function (a, el) { deleteKey(a[0], a[1], el); },
        viewChannelMemories: function (a) { viewChannelMemories(a[0], a[1]); },
        loadMemoryStats: function () { loadMemoryStats(); }
    };

    function handleActionClick(e) {
        var el = e.target.closest('[data-action]');
        if (!el || !ACTIONS.hasOwnProperty(el.dataset.action)) return;
        var args = [];
        for (var i = 0; ('arg' + i) in el.dataset; i++) args.push(el.dataset['arg' + i]);
        ACTIONS[el.dataset.action](args, el);
    }

    function debounce(fn, ms) {
        var t = null;
        return function () {
            var args = arguments;
            clearTimeout(t);
            t = setTimeout(function () { fn.apply(null, args); }, ms);
        };
    }

    function initEmptyStates() {
        $('#messages-container').innerHTML = emptyState('mail', 'Pick a source', 'Choose a source and role, then press Load messages.');
        resetActivityFeed();
        $$('.stat-value').forEach(function (el) { el.classList.add('skeleton-text'); });
        $('#diagnostics-results').innerHTML = emptyState('activity', 'No diagnostics yet', 'Press "Run diagnostics" to check bot, database and provider health.');
        $('#api-keys-list').innerHTML = skeleton('cards', 3);
        $('#audit-log-container').innerHTML = '<div class="pad">' + skeleton('rows', 3) + '</div>';
        $('#channels-list').innerHTML = skeleton('rows', 5);
        $('#threads-list').innerHTML = skeleton('rows', 4);
        $('#persona-templates-grid').innerHTML = skeleton('cards', 3);
        $('#channel-personas-list').innerHTML = skeleton('rows', 3);
        $('#memory-stats-list').innerHTML = '<div class="pad">' + skeleton('rows', 3) + '</div>';
    }

    function init() {
        G.onUnauthorized(handleSessionExpired);
        applyTheme(currentTheme());
        initEmptyStates();
        initNavGroups();
        setSidebarCollapsed(store.get('gideon-sidebar') === 'collapsed');
        trackSettingsForms();

        document.addEventListener('click', handleActionClick);

        bindEvent('#login-form', 'submit', onLoginSubmit);
        bindEvent('#logout-btn', 'click', handleLogout);
        bindEvent('#theme-toggle', 'click', toggleTheme);
        bindEvent('#sidebar-collapse', 'click', function () {
            setSidebarCollapsed(!$('#dashboard').classList.contains('sidebar-collapsed'));
        });
        bindEvent('#sidebar-open', 'click', openDrawer);
        bindEvent('#sidebar-close', 'click', function () { closeDrawer(true); });
        bindEvent('#sidebar-scrim', 'click', function () { closeDrawer(true); });

        $$('.nav-item').forEach(function (item) {
            item.addEventListener('click', function (e) {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return;
                e.preventDefault();
                navigate(item.dataset.tab);
            });
        });
        window.addEventListener('hashchange', onHashChange);
        window.addEventListener('resize', debounce(function () {
            if (window.innerWidth > 900) closeDrawer(false);
        }, 100));
        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && document.body.classList.contains('drawer-open') && !Modals.hasOpen()) closeDrawer(true);
        });
        window.addEventListener('beforeunload', function (e) {
            if (loggedIn && G.dirtyForms().length) {
                e.preventDefault();
                e.returnValue = '';
            }
        });
        document.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'visible') reconnectNow();
        });
        window.addEventListener('online', reconnectNow);

        // Overview
        bindEvent('#overview-refresh', 'click', loadOverview);
        bindEvent('#action-sync-commands', 'click', syncCommands);

        // Settings
        bindEvent('#settings-form', 'submit', saveSettings);
        bindEvent('#memory-settings-form', 'submit', saveMemorySettings);
        bindEvent('#tool-calling-form', 'submit', saveToolCallingSettings);
        bindEvent('#tool-calling-enabled', 'change', updateToolCallingLabel);
        bindEvent('#setting-provider', 'change', onProviderChange);
        setupModelSearch('setting-model-search', 'setting-model');
        setupModelSearch('channel-model-search', 'channel-edit-model');
        setupModelSearch('thread-model-search', 'thread-edit-model');
        setupModelSearch('or-model-search', 'or-model');
        setupModelSearch('video-model-search', 'video-model');

        // Channels
        bindEvent('#channel-filter-input', 'input', debounce(renderChannels, 120));
        bindEvent('#channel-filter-override', 'change', renderChannels);
        bindEvent('#channel-edit-form', 'submit', saveChannel);
        bindEvent('#channel-reset-btn', 'click', resetChannel);
        bindEvent('#channel-edit-provider', 'change', onChannelProviderChange);
        ['#channel-edit-model', '#channel-edit-prompt', '#channel-edit-timeout', '#channel-edit-summary', '#channel-edit-max-summaries'].forEach(function (sel) {
            bindEvent(sel, 'input', updateSourceChips);
            bindEvent(sel, 'change', updateSourceChips);
        });

        // Threads
        bindEvent('#thread-edit-form', 'submit', saveThread);
        bindEvent('#thread-delete-btn', 'click', deleteThread);

        // Personas
        bindEvent('#persona-edit-form', 'submit', savePersona);
        bindEvent('#persona-remove-btn', 'click', removePersona);
        bindEvent('#persona-edit-avatar-url', 'input', updatePersonaAvatarPreview);
        bindEvent('#persona-edit-template', 'change', onTemplateSelect);
        bindEvent('#template-edit-form', 'submit', saveTemplate);
        bindEvent('#template-delete-btn', 'click', deleteTemplate);
        bindEvent('#create-template-btn', 'click', function (e) { openCreateTemplate(e.currentTarget); });
        bindEvent('#channel-picker-search', 'input', function () { renderChannelPickerList(this.value); });
        bindEvent('#channel-picker-search', 'keydown', function (e) {
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                focusPickerItem(pickerItems(), 0);
            }
        });
        bindEvent('#channel-picker-list', 'keydown', onPickerKeydown);
        bindEvent('#channel-picker-list', 'click', function (e) {
            var item = e.target.closest('.channel-picker-item');
            if (item) applyTemplateToChannel(item.getAttribute('data-channel-id'));
        });
        $('#channel-picker-modal').addEventListener('modal:closed', function () { pendingTemplateId = null; });

        // Messages
        bindEvent('#msg-source-type', 'change', onSourceTypeChange);
        bindEvent('#msg-filter-btn', 'click', function () { loadMessages(1); });
        bindEvent('#msg-prev', 'click', function () { if (msgCurrentPage > 1) loadMessages(msgCurrentPage - 1); });
        bindEvent('#msg-next', 'click', function () { if (msgCurrentPage < msgTotalPages) loadMessages(msgCurrentPage + 1); });

        // Image & video
        bindEvent('#image-provider-form', 'submit', saveImageProvider);
        bindEvent('#modalities-form', 'submit', saveModalities);
        bindEvent('#image-config-provider', 'change', function () { showProviderConfigPanel(this.value); });
        ['mod-image', 'mod-text', 'mod-audio'].forEach(function (id) {
            document.getElementById(id).addEventListener('change', updateModalitiesPreview);
        });
        IMAGE_PROVIDERS.forEach(function (p) {
            var form = document.getElementById('config-form-' + p);
            form.addEventListener('submit', function (e) {
                e.preventDefault();
                saveProviderConfig(p, form, e.submitter);
            });
        });
        bindEvent('#video-config-form', 'submit', saveVideoConfig);
        bindEvent('#video-test-form', 'submit', submitVideoTest);
        bindEvent('#video-test-cancel', 'click', function () {
            stopVideoPolling();
            showSaveStatus('video-test-status', 'Polling stopped', false);
        });
        bindEvent('#video-model', 'change', function () { renderVideoModelInfo(this.value); });

        // Diagnostics
        bindEvent('#run-diagnostics-btn', 'click', runDiagnostics);
        bindEvent('#run-prune-btn', 'click', runPrune);

        // Backup
        bindEvent('#export-config-btn', 'click', exportConfig);
        bindEvent('#export-db-btn', 'click', exportDatabase);
        bindEvent('#validate-config-btn', 'click', validateConfigImport);
        bindEvent('#import-config-btn', 'click', importConfig);
        bindEvent('#validate-db-btn', 'click', validateDbRestore);
        bindEvent('#restore-db-btn', 'click', restoreDatabase);
        setupDropzone('config-upload-zone', 'config-file-input', handleConfigFileSelect);
        setupDropzone('db-upload-zone', 'db-file-input', handleDbFileSelect);

        // Memory & activity
        bindEvent('#memory-clear-btn', 'click', clearChannelMemories);
        bindEvent('#memory-summarize-btn', 'click', triggerSummarize);
        bindEvent('#clear-activity-btn', 'click', resetActivityFeed);

        // API keys
        bindEvent('#add-key-btn', 'click', function (e) { openKeyModal({ opener: e.currentTarget }); });
        bindEvent('#key-edit-form', 'submit', saveAPIKey);
        bindEvent('#key-test-btn', 'click', testKeyFromModal);
        bindEvent('#import-env-btn', 'click', importFromEnv);

        G.palette.init({
            tabs: TABS,
            tabById: tabById,
            navigate: navigate,
            focusField: focusField,
            openChannel: openChannelFromPalette,
            openThread: openThreadFromPalette,
            openTemplate: openTemplateFromPalette,
            openPersona: openPersonaFromPalette
        });

        startPeriodicRefresh();
        checkAuth();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
