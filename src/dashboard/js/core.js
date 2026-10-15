/**
 * Gideon Admin Dashboard - shared UI kit (escaping, API client, toasts, modals, form helpers)
 */
(function () {
    'use strict';

    var G = window.Gideon = window.Gideon || {};

    function $(selector, root) { return (root || document).querySelector(selector); }
    function $$(selector, root) { return Array.prototype.slice.call((root || document).querySelectorAll(selector)); }

    // ── Escaping & URLs ──────────────────────────────────────

    var HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

    function escapeHtml(str) {
        if (str === null || str === undefined) return '';
        return String(str).replace(/[&<>"']/g, function (c) { return HTML_ESCAPES[c]; });
    }

    function actionAttrs(action, args) {
        var attrs = ' data-action="' + escapeHtml(action) + '"';
        (args || []).forEach(function (arg, i) {
            attrs += ' data-arg' + i + '="' + escapeHtml(arg) + '"';
        });
        return attrs;
    }

    function sanitizeHttpUrl(url) {
        if (!url || typeof url !== 'string') return null;
        try {
            var parsed = new URL(url);
            if (parsed.protocol === 'http:' || parsed.protocol === 'https:') return parsed.href;
            return null;
        } catch (e) {
            return null;
        }
    }

    function icon(name, cls) {
        return '<svg class="icon' + (cls ? ' ' + cls : '') + '" aria-hidden="true" focusable="false"><use href="#i-' + name + '"/></svg>';
    }

    // ── API client ───────────────────────────────────────────

    function ApiError(message, status, data, retryAfter) {
        this.name = 'ApiError';
        this.message = message;
        this.status = status;
        this.data = data || {};
        this.details = (data && data.details && typeof data.details === 'object') ? data.details : null;
        this.retryAfter = retryAfter || null;
    }
    ApiError.prototype = Object.create(Error.prototype);
    ApiError.prototype.constructor = ApiError;

    var unauthorizedHandler = null;

    function onUnauthorized(fn) { unauthorizedHandler = fn; }

    async function request(method, path, body, opts) {
        opts = opts || {};
        var init = { method: method, credentials: 'same-origin', headers: {}, signal: opts.signal };
        if (typeof FormData !== 'undefined' && body instanceof FormData) {
            init.body = body;
        } else if (body !== undefined) {
            init.headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(body);
        }
        var res;
        try {
            res = await fetch(path, init);
        } catch (e) {
            if (e && e.name === 'AbortError') throw e;
            throw new ApiError('Could not reach the server', 0, {});
        }
        if (res.status === 401 && !opts.noAuthRedirect) {
            if (unauthorizedHandler) unauthorizedHandler();
            throw new ApiError('Session expired', 401, {});
        }
        return res;
    }

    async function api(method, path, body, opts) {
        var res = await request(method, path, body, opts);
        var data = {};
        try { data = await res.json(); } catch (e) { data = {}; }
        if (!res.ok) {
            var retry = data && data.retry_after != null ? Number(data.retry_after) : parseInt(res.headers.get('Retry-After'), 10);
            throw new ApiError((data && data.error) || ('Request failed (' + res.status + ')'), res.status, data,
                isFinite(retry) ? retry : null);
        }
        return data;
    }

    async function apiBlob(path) {
        var res = await request('GET', path);
        if (!res.ok) {
            var data = {};
            try { data = await res.json(); } catch (e) { data = {}; }
            throw new ApiError((data && data.error) || ('Request failed (' + res.status + ')'), res.status, data);
        }
        return { blob: await res.blob(), disposition: res.headers.get('Content-Disposition') };
    }

    // ── Toasts ───────────────────────────────────────────────

    var TOAST_ICONS = { success: 'check-circle', error: 'x-circle', info: 'info', warning: 'alert-triangle' };
    var MAX_TOASTS = 4;

    function toast(message, opts) {
        opts = opts || {};
        var type = opts.type || 'info';
        var region = document.getElementById('toast-region');
        if (!region) return null;

        var el = document.createElement('div');
        el.className = 'toast toast-' + type;
        el.setAttribute('role', type === 'error' ? 'alert' : 'status');
        el.innerHTML = icon(TOAST_ICONS[type] || 'info', 'toast-icon');

        var body = document.createElement('div');
        body.className = 'toast-body';
        if (opts.title) {
            var title = document.createElement('strong');
            title.textContent = opts.title;
            body.appendChild(title);
        }
        var text = document.createElement('span');
        text.textContent = message;
        body.appendChild(text);
        el.appendChild(body);

        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'icon-btn toast-close';
        close.setAttribute('aria-label', 'Dismiss notification');
        close.innerHTML = icon('x');
        el.appendChild(close);

        var timer = null;
        var remaining = opts.timeout != null ? opts.timeout : (type === 'error' ? 9000 : 5000);
        var startedAt = 0;

        function dismiss() {
            if (el.classList.contains('leaving')) return;
            clearTimeout(timer);
            el.classList.add('leaving');
            setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 180);
        }
        function arm() {
            if (!remaining) return;
            startedAt = Date.now();
            timer = setTimeout(dismiss, remaining);
        }
        function pause() {
            if (!timer) return;
            clearTimeout(timer);
            timer = null;
            remaining = Math.max(1500, remaining - (Date.now() - startedAt));
        }
        close.addEventListener('click', dismiss);
        el.addEventListener('mouseenter', pause);
        el.addEventListener('focusin', pause);
        el.addEventListener('mouseleave', function () { if (!timer) arm(); });
        el.addEventListener('focusout', function () { if (!timer) arm(); });

        region.appendChild(el);
        while (region.children.length > MAX_TOASTS) region.removeChild(region.firstChild);
        arm();
        return { dismiss: dismiss };
    }
    ['success', 'error', 'info', 'warning'].forEach(function (type) {
        toast[type] = function (message, opts) {
            return toast(message, Object.assign({ type: type }, opts || {}));
        };
    });

    // ── Modals ───────────────────────────────────────────────

    var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    var stack = [];

    function focusableIn(root) {
        return $$(FOCUSABLE, root).filter(function (el) {
            return !el.closest('[hidden]') && el.offsetParent !== null;
        });
    }

    function syncStack() {
        stack.forEach(function (entry, i) {
            entry.el.inert = i !== stack.length - 1;
        });
        ['dashboard', 'login-screen'].forEach(function (id) {
            var node = document.getElementById(id);
            if (node) node.inert = stack.length > 0;
        });
        document.body.classList.toggle('modal-open', stack.length > 0);
    }

    function dialogOf(modal) { return $('.modal-dialog', modal) || modal; }

    function openModal(modal, opts) {
        opts = opts || {};
        if (!modal) return;
        if (stack.some(function (s) { return s.el === modal; })) return;
        var opener = opts.opener || document.activeElement;
        modal.hidden = false;
        stack.push({ el: modal, opener: opener });
        syncStack();
        var target = opts.focus || $('[data-autofocus]', modal) || focusableIn(dialogOf(modal))[0] || dialogOf(modal);
        setTimeout(function () { if (target && target.focus) target.focus(); }, 0);
    }

    function closeModal(modal) {
        var idx = -1;
        stack.forEach(function (s, i) { if (s.el === modal) idx = i; });
        if (idx === -1) { modal.hidden = true; return; }
        var entry = stack.splice(idx, 1)[0];
        modal.hidden = true;
        modal.inert = false;
        syncStack();
        modal.dispatchEvent(new CustomEvent('modal:closed'));
        var opener = entry.opener;
        if (opener && opener.focus && document.contains(opener) && !opener.closest('[hidden]')) {
            opener.focus();
        } else if (!stack.length) {
            var main = document.getElementById('main');
            if (main) main.focus();
        }
    }

    async function requestClose(modal) {
        if (modal._beforeClose) {
            var proceed = await modal._beforeClose();
            if (proceed === false) return false;
        }
        closeModal(modal);
        return true;
    }

    function watchDirty(modal) {
        if (modal._dirtyWatched) return;
        modal._dirtyWatched = true;
        modal._dirty = false;
        function mark(e) {
            if (e.target.hasAttribute && e.target.hasAttribute('data-ignore-dirty')) return;
            modal._dirty = true;
        }
        modal.addEventListener('input', mark);
        modal.addEventListener('change', mark);
        modal._beforeClose = async function () {
            if (!modal._dirty) return true;
            return confirmDialog({
                title: 'Discard unsaved changes?',
                message: 'You have edits in this dialog that have not been saved.',
                confirmLabel: 'Discard changes',
                cancelLabel: 'Keep editing'
            });
        };
    }

    function markClean(modal) { modal._dirty = false; }

    document.addEventListener('keydown', function (e) {
        var entry = stack.length ? stack[stack.length - 1] : null;
        if (!entry) return;
        if (e.key === 'Escape') {
            e.preventDefault();
            e.stopPropagation();
            requestClose(entry.el);
            return;
        }
        if (e.key === 'Tab') {
            var dialog = dialogOf(entry.el);
            var items = focusableIn(dialog);
            if (!items.length) { e.preventDefault(); return; }
            var first = items[0];
            var last = items[items.length - 1];
            var active = document.activeElement;
            if (!dialog.contains(active)) {
                e.preventDefault();
                first.focus();
            } else if (e.shiftKey && (active === first || active === dialog)) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && active === last) {
                e.preventDefault();
                first.focus();
            }
        }
    }, true);

    var mouseDownTarget = null;
    document.addEventListener('mousedown', function (e) { mouseDownTarget = e.target; });
    document.addEventListener('click', function (e) {
        var closer = e.target.closest('[data-modal-close]');
        if (closer) {
            var modal = closer.closest('.modal');
            if (modal) requestClose(modal);
            return;
        }
        if (e.target.classList && e.target.classList.contains('modal') && mouseDownTarget === e.target) {
            requestClose(e.target);
        }
    });

    var Modals = {
        open: openModal,
        close: closeModal,
        requestClose: requestClose,
        watchDirty: watchDirty,
        markClean: markClean,
        isOpen: function (modal) { return stack.some(function (s) { return s.el === modal; }); },
        hasOpen: function () { return stack.length > 0; }
    };

    // ── Confirm dialog ───────────────────────────────────────

    function confirmDialog(o) {
        o = o || {};
        return new Promise(function (resolve) {
            var modal = document.getElementById('confirm-modal');
            var title = $('#confirm-title');
            var message = $('#confirm-message');
            var list = $('#confirm-details');
            var typed = $('#confirm-typed');
            var input = $('#confirm-input');
            var ok = $('#confirm-ok');
            var cancel = $('#confirm-cancel');
            var iconBox = $('#confirm-icon');
            var danger = o.danger !== false;

            title.textContent = o.title || 'Are you sure?';
            message.textContent = o.message || '';
            message.hidden = !o.message;
            list.innerHTML = '';
            (o.details || []).forEach(function (d) {
                var li = document.createElement('li');
                li.textContent = d;
                list.appendChild(li);
            });
            list.hidden = !(o.details && o.details.length);
            iconBox.className = 'confirm-icon ' + (danger ? 'is-danger' : 'is-info');
            iconBox.innerHTML = icon(danger ? 'alert-triangle' : 'info');
            ok.textContent = o.confirmLabel || 'Confirm';
            ok.className = 'btn ' + (danger ? 'btn-danger' : 'btn-primary');
            cancel.textContent = o.cancelLabel || 'Cancel';

            var word = o.requireText || '';
            typed.hidden = !word;
            input.value = '';
            if (word) $('#confirm-word').textContent = word;
            ok.disabled = !!word;

            var finished = false;
            function finish(value) {
                if (finished) return;
                finished = true;
                ok.onclick = cancel.onclick = input.oninput = input.onkeydown = null;
                modal._beforeClose = null;
                closeModal(modal);
                resolve(value);
            }
            ok.onclick = function () { finish(true); };
            cancel.onclick = function () { finish(false); };
            modal._beforeClose = function () { finish(false); return false; };
            input.oninput = function () { ok.disabled = input.value.trim() !== word; };
            input.onkeydown = function (e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    if (!ok.disabled) finish(true);
                }
            };

            openModal(modal, {
                opener: o.opener || document.activeElement,
                focus: word ? input : (danger ? cancel : ok)
            });
        });
    }

    // ── Loading & empty states ───────────────────────────────

    function skeleton(kind, count) {
        var n = count || 3;
        var html = '';
        if (kind === 'cards') {
            for (var c = 0; c < n; c++) {
                html += '<div class="skeleton-card"><span class="skeleton sk-title"></span><span class="skeleton sk-line"></span><span class="skeleton sk-line short"></span></div>';
            }
            return '<div class="skeleton-grid" role="status" aria-label="Loading">' + html + '</div>';
        }
        for (var i = 0; i < n; i++) html += '<span class="skeleton sk-row"></span>';
        return '<div class="skeleton-stack" role="status" aria-label="Loading">' + html + '</div>';
    }

    function emptyState(iconName, title, text) {
        return '<div class="empty-state">' + icon(iconName || 'info', 'empty-icon') +
            '<p class="empty-title">' + escapeHtml(title) + '</p>' +
            (text ? '<p class="empty-text">' + escapeHtml(text) + '</p>' : '') + '</div>';
    }

    function setBusy(btn, busy) {
        if (!btn) return;
        btn.disabled = !!busy;
        btn.classList.toggle('is-busy', !!busy);
        if (busy) btn.setAttribute('aria-busy', 'true'); else btn.removeAttribute('aria-busy');
    }

    // ── Field errors & form alerts ───────────────────────────

    function clearFieldError(el) {
        el.classList.remove('invalid');
        el.removeAttribute('aria-invalid');
        var errId = 'err-' + el.id;
        var err = document.getElementById(errId);
        if (err) err.remove();
        var described = (el.getAttribute('aria-describedby') || '').split(' ').filter(function (x) { return x && x !== errId; });
        if (described.length) el.setAttribute('aria-describedby', described.join(' '));
        else el.removeAttribute('aria-describedby');
    }

    function clearFieldErrors(root) {
        $$('[aria-invalid="true"]', root).forEach(clearFieldError);
        $$('.field-error', root).forEach(function (n) { n.remove(); });
    }

    function setFieldError(el, message) {
        clearFieldError(el);
        var err = document.createElement('div');
        err.className = 'field-error';
        err.id = 'err-' + el.id;
        err.textContent = message;
        var field = el.closest('.field') || el.parentNode;
        field.appendChild(err);
        el.classList.add('invalid');
        el.setAttribute('aria-invalid', 'true');
        var described = (el.getAttribute('aria-describedby') || '').split(' ').filter(Boolean);
        described.push(err.id);
        el.setAttribute('aria-describedby', described.join(' '));
        el.addEventListener('input', function () { clearFieldError(el); }, { once: true });
        el.addEventListener('change', function () { clearFieldError(el); }, { once: true });
    }

    function applyFieldErrors(root, details, fieldMap) {
        var unmatched = [];
        var firstEl = null;
        Object.keys(details || {}).forEach(function (key) {
            var el = fieldMap && fieldMap[key] ? document.getElementById(fieldMap[key]) : null;
            if (el && root.contains(el)) {
                setFieldError(el, String(details[key]));
                if (!firstEl) firstEl = el;
            } else {
                unmatched.push(key + ': ' + details[key]);
            }
        });
        if (firstEl) firstEl.focus();
        return unmatched;
    }

    function setFormAlert(root, message, type) {
        var box = $('[data-form-alert]', root);
        if (!box) return;
        box.textContent = message || '';
        box.hidden = !message;
        box.className = 'form-alert' + (type ? ' is-' + type : '');
    }

    // ── Dirty form tracking ──────────────────────────────────

    var tracked = [];

    function serializeForm(form) {
        var out = {};
        $$('input, select, textarea', form).forEach(function (el) {
            if (!el.id || el.type === 'file' || el.hasAttribute('data-ignore-dirty')) return;
            out[el.id] = el.type === 'checkbox' ? el.checked : el.value;
        });
        return JSON.stringify(out);
    }

    function trackForm(form, label) {
        if (!form) return null;
        var entry = { form: form, label: label || form.id, base: serializeForm(form), dirty: false, pill: null };
        var actions = $('.form-actions', form);
        if (actions) {
            var pill = document.createElement('span');
            pill.className = 'dirty-pill';
            pill.hidden = true;
            pill.innerHTML = '<span class="dirty-dot"></span>Unsaved changes';
            actions.appendChild(pill);
            entry.pill = pill;
        }
        function update() {
            entry.dirty = serializeForm(form) !== entry.base;
            if (entry.pill) entry.pill.hidden = !entry.dirty;
            form.classList.toggle('is-dirty', entry.dirty);
        }
        entry.snapshot = function () { entry.base = serializeForm(form); update(); };
        form.addEventListener('input', update);
        form.addEventListener('change', update);
        tracked.push(entry);
        return entry;
    }

    function captureFocus(el) {
        var selector = null;
        if (el && el.dataset && el.dataset.action) {
            selector = '[data-action="' + el.dataset.action + '"]';
            if (el.dataset.arg0 !== undefined) selector += '[data-arg0="' + el.dataset.arg0.replace(/(["\\])/g, '\\$1') + '"]';
        }
        return function () {
            var active = document.activeElement;
            if (active && active !== document.body && active.id !== 'main') return;
            var node = selector ? document.querySelector(selector) : (el && document.contains(el) ? el : null);
            if (node) node.focus();
        };
    }

    function dirtyForms(scope) {
        return tracked.filter(function (t) {
            return t.dirty && (!scope || scope.contains(t.form));
        });
    }

    G.$ = $;
    G.$$ = $$;
    G.escapeHtml = escapeHtml;
    G.actionAttrs = actionAttrs;
    G.sanitizeHttpUrl = sanitizeHttpUrl;
    G.icon = icon;
    G.ApiError = ApiError;
    G.api = api;
    G.apiBlob = apiBlob;
    G.onUnauthorized = onUnauthorized;
    G.toast = toast;
    G.Modals = Modals;
    G.confirmDialog = confirmDialog;
    G.skeleton = skeleton;
    G.emptyState = emptyState;
    G.setBusy = setBusy;
    G.clearFieldErrors = clearFieldErrors;
    G.setFieldError = setFieldError;
    G.applyFieldErrors = applyFieldErrors;
    G.setFormAlert = setFormAlert;
    G.trackForm = trackForm;
    G.dirtyForms = dirtyForms;
    G.captureFocus = captureFocus;
})();
