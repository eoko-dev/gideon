/**
 * Gideon Admin Dashboard - command palette (Ctrl/Cmd+K)
 */
(function () {
    'use strict';

    var G = window.Gideon;
    var $ = G.$;
    var SEARCH_DEBOUNCE_MS = 200;
    var MAX_LOCAL_RESULTS = 8;

    var app = null;
    var modal = null;
    var input = null;
    var list = null;
    var statusEl = null;
    var searchingEl = null;
    var trigger = null;

    var fieldIndex = null;
    var remote = [];
    var entries = [];
    var activeIndex = -1;
    var debounceTimer = null;
    var searchSeq = 0;
    var abortCtl = null;

    var GROUP_ICONS = {
        'Pages': 'corner',
        'Settings & fields': 'settings',
        'Channels': 'hash',
        'Threads': 'threads',
        'Persona templates': 'users',
        'Channel personas': 'users'
    };

    function buildFieldIndex() {
        fieldIndex = [];
        G.$$('.tab-content').forEach(function (section) {
            var tabId = section.id.replace(/^tab-/, '');
            G.$$('label', section).forEach(function (label) {
                var target = label.htmlFor ? document.getElementById(label.htmlFor) : label.querySelector('input, select, textarea');
                if (!target || target.type === 'hidden') return;
                var clone = label.cloneNode(true);
                G.$$('input, select, textarea, .chip, .dirty-pill', clone).forEach(function (n) { n.remove(); });
                var text = clone.textContent.replace(/\s+/g, ' ').trim();
                if (!text) return;
                var card = label.closest('.card');
                var titleEl = card ? card.querySelector('.card-title') : null;
                fieldIndex.push({
                    label: text.replace(/\s*\*$/, ''),
                    tab: tabId,
                    target: target.id,
                    section: titleEl ? titleEl.textContent.trim() : ''
                });
            });
        });
    }

    function tokenize(q) {
        return q.toLowerCase().split(/\s+/).filter(Boolean);
    }

    function matchScore(text, tokens, extra) {
        var hay = (text + ' ' + (extra || '')).toLowerCase();
        for (var i = 0; i < tokens.length; i++) {
            if (hay.indexOf(tokens[i]) === -1) return -1;
        }
        var lower = text.toLowerCase();
        var joined = tokens.join(' ');
        if (lower === joined) return 0;
        if (lower.indexOf(joined) === 0) return 1;
        if (lower.indexOf(joined) !== -1) return 2;
        return 3;
    }

    function localResults(q) {
        var tokens = tokenize(q);
        var pages = [];
        app.tabs.forEach(function (tab, order) {
            var score = tokens.length ? matchScore(tab.title, tokens, tab.group + ' ' + (tab.keywords || '')) : 0;
            if (score >= 0) pages.push({ score: score, order: order, tab: tab });
        });
        pages.sort(function (a, b) { return a.score - b.score || a.order - b.order; });

        var out = pages.slice(0, tokens.length ? MAX_LOCAL_RESULTS : pages.length).map(function (p) {
            return {
                group: 'Pages',
                label: p.tab.title,
                meta: p.tab.group,
                run: function () { app.navigate(p.tab.id); }
            };
        });

        if (tokens.length) {
            if (!fieldIndex) buildFieldIndex();
            var fields = [];
            fieldIndex.forEach(function (f, order) {
                var tab = app.tabById(f.tab);
                var score = matchScore(f.label, tokens, f.section + ' ' + (tab ? tab.title : ''));
                if (score >= 0) fields.push({ score: score, order: order, f: f });
            });
            fields.sort(function (a, b) { return a.score - b.score || a.order - b.order; });
            fields.slice(0, MAX_LOCAL_RESULTS).forEach(function (r) {
                var tab = app.tabById(r.f.tab);
                out.push({
                    group: 'Settings & fields',
                    label: r.f.label,
                    meta: (tab ? tab.title : r.f.tab) + (r.f.section ? ' / ' + r.f.section : ''),
                    run: function () { app.focusField(r.f.tab, r.f.target); }
                });
            });
        }
        return out;
    }

    function setRemote(data) {
        var out = [];
        (data.channels || []).forEach(function (c) {
            out.push({
                group: 'Channels',
                label: '#' + (c.channel_name || c.channel_id),
                meta: c.guild_name || '',
                run: function () { app.openChannel(c.channel_id); }
            });
        });
        (data.threads || []).forEach(function (t) {
            out.push({
                group: 'Threads',
                label: t.name || t.thread_id,
                meta: t.channel_name ? '#' + t.channel_name : '',
                run: function () { app.openThread(t.thread_id); }
            });
        });
        (data.persona_templates || []).forEach(function (t) {
            out.push({
                group: 'Persona templates',
                label: t.name || t.template_id,
                meta: t.display_name || '',
                run: function () { app.openTemplate(t.template_id); }
            });
        });
        (data.channel_personas || []).forEach(function (p) {
            out.push({
                group: 'Channel personas',
                label: p.display_name || p.channel_id,
                meta: p.channel_name ? '#' + p.channel_name : '',
                run: function () { app.openPersona(p.channel_id); }
            });
        });
        remote = out;
    }

    function render() {
        var q = input.value.trim();
        var items = localResults(q).concat(remote);
        entries = items;
        list.innerHTML = '';

        if (!items.length) {
            var empty = document.createElement('li');
            empty.className = 'palette-empty';
            empty.setAttribute('role', 'presentation');
            empty.textContent = q ? 'No results for "' + q + '"' : 'Nothing to show';
            list.appendChild(empty);
            activeIndex = -1;
            input.removeAttribute('aria-activedescendant');
            statusEl.textContent = q ? 'No results' : '';
            return;
        }

        var lastGroup = null;
        items.forEach(function (item, i) {
            if (item.group !== lastGroup) {
                var head = document.createElement('li');
                head.className = 'palette-group';
                head.setAttribute('role', 'presentation');
                head.textContent = item.group;
                list.appendChild(head);
                lastGroup = item.group;
            }
            var li = document.createElement('li');
            li.className = 'palette-item';
            li.id = 'palette-opt-' + i;
            li.setAttribute('role', 'option');
            li.setAttribute('aria-selected', 'false');
            li.dataset.index = String(i);
            li.innerHTML = G.icon(GROUP_ICONS[item.group] || 'corner');
            var label = document.createElement('span');
            label.className = 'palette-label';
            label.textContent = item.label;
            li.appendChild(label);
            if (item.meta) {
                var meta = document.createElement('span');
                meta.className = 'palette-meta';
                meta.textContent = item.meta;
                li.appendChild(meta);
            }
            list.appendChild(li);
        });
        setActive(0);
        statusEl.textContent = items.length + (items.length === 1 ? ' result' : ' results');
    }

    function setActive(i) {
        if (!entries.length) return;
        if (i < 0) i = entries.length - 1;
        if (i >= entries.length) i = 0;
        var prev = list.querySelector('[aria-selected="true"]');
        if (prev) prev.setAttribute('aria-selected', 'false');
        activeIndex = i;
        var el = document.getElementById('palette-opt-' + i);
        if (el) {
            el.setAttribute('aria-selected', 'true');
            input.setAttribute('aria-activedescendant', el.id);
            el.scrollIntoView({ block: 'nearest' });
        }
    }

    function choose(i) {
        var item = entries[i];
        if (!item) return;
        G.Modals.close(modal);
        item.run();
    }

    function scheduleRemote() {
        clearTimeout(debounceTimer);
        var q = input.value.trim();
        if (abortCtl) abortCtl.abort();
        if (q.length < 2) {
            remote = [];
            searchingEl.hidden = true;
            return;
        }
        searchingEl.hidden = false;
        var seq = ++searchSeq;
        debounceTimer = setTimeout(async function () {
            abortCtl = new AbortController();
            try {
                var data = await G.api('GET', '/api/search?q=' + encodeURIComponent(q), undefined, { signal: abortCtl.signal });
                if (seq !== searchSeq) return;
                setRemote(data || {});
                searchingEl.hidden = true;
                render();
            } catch (e) {
                if (e && e.name === 'AbortError') return;
                if (seq !== searchSeq) return;
                searchingEl.hidden = true;
            }
        }, SEARCH_DEBOUNCE_MS);
    }

    function open() {
        if (G.Modals.isOpen(modal)) return;
        if (G.Modals.hasOpen()) return;
        input.value = '';
        remote = [];
        searchSeq++;
        searchingEl.hidden = true;
        render();
        G.Modals.open(modal, { opener: document.activeElement, focus: input });
    }

    function close() {
        clearTimeout(debounceTimer);
        if (abortCtl) abortCtl.abort();
        G.Modals.close(modal);
    }

    function init(appApi) {
        app = appApi;
        modal = $('#palette-modal');
        input = $('#palette-input');
        list = $('#palette-list');
        statusEl = $('#palette-status');
        searchingEl = $('#palette-searching');
        trigger = $('#global-search');

        var isMac = /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent || '');
        var hint = $('#palette-hint');
        if (hint) hint.textContent = isMac ? '⌘K' : 'Ctrl K';

        if (trigger) trigger.addEventListener('click', open);

        document.addEventListener('keydown', function (e) {
            if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'k') {
                if (document.getElementById('dashboard').hidden) return;
                e.preventDefault();
                if (G.Modals.isOpen(modal)) close(); else open();
            }
        });

        input.addEventListener('input', function () {
            render();
            scheduleRemote();
        });

        input.addEventListener('keydown', function (e) {
            if (e.isComposing) return;
            if (e.key === 'ArrowDown') { e.preventDefault(); setActive(activeIndex + 1); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); setActive(activeIndex - 1); }
            else if (e.key === 'Enter') { e.preventDefault(); choose(activeIndex); }
        });

        list.addEventListener('mousemove', function (e) {
            var li = e.target.closest('.palette-item');
            if (li && Number(li.dataset.index) !== activeIndex) setActive(Number(li.dataset.index));
        });
        list.addEventListener('click', function (e) {
            var li = e.target.closest('.palette-item');
            if (li) choose(Number(li.dataset.index));
        });

        modal.addEventListener('modal:closed', function () {
            clearTimeout(debounceTimer);
            if (abortCtl) abortCtl.abort();
        });
    }

    G.palette = { init: init, open: open, close: close };
})();
