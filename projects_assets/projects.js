/* ═══════════════════════════════════════════════════════════════════════════
   Projects module — core front end (window.PM).
   Served ONLY by the gated route /projects/assets/projects.js (never static/).
   Runs on /projects (the list) and /projects/<id> (one project).

   THE CONTRACT — projects_gantt.js (Timeline) and projects_docs.js (Budget,
   Files, Notes) are built against exactly this; keep every member stable.

   State
     PM.pid                     the project id (int)
     PM.state                   the bundle from GET /api/projects/<pid>:
                                {project, me, members, tasks, shows, contacts,
                                 venues, discussion_count, counts, activity, today}
     PM.me                      PM.state.me
     PM.can                     {edit, manage, comment, budget, createShow}
                                — recomputed on every reload. Read-only
                                accounts, view-only members and archived
                                projects have edit = false: show no write
                                control and make handlers no-ops.
   Network
     PM.api(method, path, body?) -> Promise<json>
                                path is relative to /api/projects/<pid>
                                ('' = the project itself). Never toasts.
                                Rejects with Error(message) carrying .status
                                (HTTP code) and .data (the JSON). A non-JSON
                                reply rejects with 'Your session ended —
                                reload the page.' and .status 0 (.http then
                                holds the real HTTP code, e.g. 404 for a
                                route that is not deployed).
     PM.upload(path, FormData, onProgress?) -> Promise<json>   (same errors;
                                onProgress(fraction 0..1))
     PM.reload() -> Promise     refetch the bundle, emit 'state'
     PM.applyWrite(resp)        after YOUR OWN successful write: merges
                                resp.tasks (by id), resp.project, resp.members
                                and resp.contact into PM.state, adopts
                                resp.rev when it is the known rev + 1 (else
                                PM.reload()), emits 'state'.
   Events
     PM.on(event, fn) / PM.emit(event, payload)
                                'state' (PM.state changed — see the rule
                                below), 'tab' (name), 'task' (id — a task
                                dialog was opened)
   Tabs
     PM.registerTab(name, {mount(el), show(params), hide()})
                                mount(el) runs once, lazily, with the pane
                                (#pm-pane-<name>); show(params) every time the
                                tab is entered (params = deep-link ids, e.g.
                                {line: 7}); hide() when it is left.
                                Registering the already-active tab mounts and
                                shows it at once.
     PM.setTab(name, params?)   also rewrites ?tab=… (history.replaceState)
   Shared actions
     PM.openTask(id, {comment?})    the task dialog
     PM.newTask(defaults?)          e.g. {start_date, due_date, group_name, status}
     PM.newShow(defaults?)          creates a REAL show + links it
     PM.linkShow()                  search + link an existing show
     PM.newContact(defaults?)       e.g. {name, email}
     PM.saveTask(id, changes) -> Promise<resp>
                                partial PUT → applyWrite. On failure it
                                toasts, emits 'state' (so a dragged bar snaps
                                back) and rejects. When the save leaves tasks
                                starting too early it shows a non-modal
                                notice with "Review shift…".
     PM.reviewShift(taskId)     preview dialog (old → new dates) → Apply.
                                Nothing ever moves without it.
   UI helpers
     PM.dialog({title, wide?, drawer?, noDirtyGuard?, onClose})
                                -> {el, body, footer, close()}
                                the house .modal-overlay: role=dialog,
                                aria-modal, focus moves in, Tab is trapped,
                                Esc closes, focus returns. Fill body/footer
                                right after the call.
     PM.confirm(message, okLabel?) -> Promise<bool>
     PM.toast(msg, isError?)
     PM.el(tag, cls?, text?)    createElement + className + textContent
     PM.clear(node)
     PM.richText(str) -> DocumentFragment
                                project text for display: escaped, newlines
                                kept, @Name highlighted for current members,
                                NOTHING linked. Never use Tags.richHtml on
                                project text (its #tags link to public pages).
     PM.fmtDate(iso), PM.money(v), PM.userName(id), PM.contactName(id),
     PM.taskById(id)
     PM.addDays(iso, n), PM.diffDays(a, b)     local-date math, DST-safe
                                (diffDays = b − a in whole days)
     PM.taskGroups() -> [{name, tasks}]        ungrouped ('') first, then
                                groups by first appearance in sort order;
                                tasks by sort_order then id
     PM.STATUS   {todo, doing, blocked, done}  each {label, cls}
     PM.PRIORITY {0, 1, 2, 3}                  each {label, cls}
                                (cls → the .pm-st-* / .pm-pr-* rules and the
                                 --pm-st-* tokens in projects.css)
   Extras (stable, small)
     PM.activeTab               the current tab name
     PM.dialogOpen()            true while any PM.dialog is open
     PM.ready                   Promise resolved after the first state load

   THE RULE FOR 'state' — a tab must NOT rebuild
     (a) any input / textarea / contenteditable that has focus or an unsaved
         value (park the server value, apply it on blur);
     (b) an open dialog or drawer;
     (c) anything while a pointer drag is active.
   Tabs that fetch their own data refetch on 'state' only while visible and
   PM.dialogOpen() is false.

   Lifecycle: starts on DOMContentLoaded. ONE poll — GET …/rev every 10 s
   while the page is visible (at once on becoming visible, stopped on
   pagehide, sequence-guarded) → PM.reload() when the rev moved. A 404 means
   the project is gone or you were removed: the page is replaced by "This
   project is no longer available." (or the session-ended note).

   House rules kept throughout: DOM is built with createElement/textContent
   (no innerHTML with data); nothing of a project is logged to the console;
   search words travel in POST bodies, ids in URLs.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const root = document.getElementById('pm-root');
  if (!root || window.PM) return;
  const PAGE = root.dataset.page;
  const PM = window.PM = {};

  /* ── Small helpers ─────────────────────────────────────────────────────── */

  // h('div', {class, text, onclick, 'aria-label', …}, ...children) — the one
  // element builder. Strings become TEXT nodes; there is no HTML path.
  function h(tag, props) {
    const n = document.createElement(tag);
    if (props) {
      for (const k of Object.keys(props)) {
        const v = props[k];
        if (v == null || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k === 'text') n.textContent = v;
        else if (k === 'data') Object.keys(v).forEach(d => { n.dataset[d] = String(v[d]); });
        else if (k.slice(0, 2) === 'on') n.addEventListener(k.slice(2), v);
        else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'hidden' || k === 'readOnly' ||
                 k === 'type' || k === 'href' || k === 'id' || k === 'title' || k === 'tabIndex' ||
                 k === 'placeholder' || k === 'htmlFor' || k === 'selected' || k === 'rows' || k === 'maxLength' ||
                 k === 'name' || k === 'target' || k === 'rel' || k === 'required') n[k] = v;
        else n.setAttribute(k, v === true ? '' : String(v));
      }
    }
    for (let i = 2; i < arguments.length; i++) add(n, arguments[i]);
    return n;
  }
  function add(n, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach(x => add(n, x)); return; }
    n.appendChild(typeof c === 'object' ? c : document.createTextNode(String(c)));
  }
  PM.el = function (tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  PM.clear = function (node) { if (node) node.textContent = ''; return node; };
  const clear = PM.clear;
  const $ = id => document.getElementById(id);

  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function parseIso(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
    return m ? new Date(+m[1], +m[2] - 1, +m[3]) : null;
  }
  function todayIso() {
    return (PM.state && PM.state.today) || window.localTodayIso();
  }
  // Local-date math only: `+ n * 86400000` drifts across DST and
  // toISOString() is the UTC day.
  PM.addDays = function (iso, n) {
    const d = parseIso(iso);
    if (!d) return null;
    d.setDate(d.getDate() + (n | 0));
    return window.localIsoDate(d);
  };
  PM.diffDays = function (a, b) {
    const x = parseIso(a), y = parseIso(b);
    if (!x || !y) return null;
    return Math.round((Date.UTC(y.getFullYear(), y.getMonth(), y.getDate()) -
                       Date.UTC(x.getFullYear(), x.getMonth(), x.getDate())) / 86400000);
  };
  PM.fmtDate = function (iso) {
    const d = parseIso(iso);
    if (!d) return '';
    const thisYear = (parseIso(todayIso()) || new Date()).getFullYear();
    return MONTHS[d.getMonth()] + ' ' + d.getDate() + (d.getFullYear() !== thisYear ? ', ' + d.getFullYear() : '');
  };
  // "Nov 22 – Nov 29", "Jan 14 – Jan 17, 2027" (the year once, and only
  // when it is not this year), or the one date there is.
  function fmtRange(a, b) {
    const x = parseIso(a), y = parseIso(b);
    if (x && y && a !== b) {
      if (x.getFullYear() !== y.getFullYear()) return PM.fmtDate(a) + ' – ' + PM.fmtDate(b);
      const thisYear = (parseIso(todayIso()) || new Date()).getFullYear();
      return MONTHS[x.getMonth()] + ' ' + x.getDate() + ' – ' + MONTHS[y.getMonth()] + ' ' + y.getDate() +
             (y.getFullYear() !== thisYear ? ', ' + y.getFullYear() : '');
    }
    return PM.fmtDate(a || b);
  }
  // A server timestamp → "Nov 14, 3:05 PM" in the browser's zone.
  function fmtWhen(ts) {
    const d = typeof parseServerTime === 'function' ? parseServerTime(ts) : null;
    if (!d) return '';
    const day = PM.fmtDate(window.localIsoDate(d));
    let hr = d.getHours();
    const ap = hr >= 12 ? 'PM' : 'AM';
    hr = hr % 12 || 12;
    return day + ', ' + hr + ':' + String(d.getMinutes()).padStart(2, '0') + ' ' + ap;
  }
  PM.money = function (v) {
    if (v == null || v === '' || !isFinite(+v)) return '—';
    const n = +v;
    return (n < 0 ? '−$' : '$') + Math.abs(n).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  };
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));
  const isHex = v => typeof v === 'string' && /^#[0-9a-fA-F]{6}$/.test(v);
  function initials(name) {
    const parts = String(name || '?').trim().split(/\s+/).slice(0, 2);
    return parts.map(w => w.charAt(0)).join('').toUpperCase() || '?';
  }
  function avatar(name, small, off) {
    const a = h('span', {class: 'pm-avatar' + (small ? ' pm-avatar-sm' : '') + (off ? ' is-off' : ''),
                         title: name, 'aria-hidden': 'true', text: initials(name)});
    a.style.background = typeof _userColor === 'function' ? _userColor(String(name || '?')) : '#64748b';
    return a;
  }

  PM.STATUS = {
    todo:    {label: 'To do',       cls: 'pm-st-todo'},
    doing:   {label: 'In progress', cls: 'pm-st-doing'},
    blocked: {label: 'Blocked',     cls: 'pm-st-blocked'},
    done:    {label: 'Done',        cls: 'pm-st-done'},
  };
  PM.PRIORITY = {
    0: {label: 'Low',    cls: 'pm-pr-0'},
    1: {label: 'Normal', cls: 'pm-pr-1'},
    2: {label: 'High',   cls: 'pm-pr-2'},
    3: {label: 'Urgent', cls: 'pm-pr-3'},
  };
  const STATUS_KEYS = ['todo', 'doing', 'blocked', 'done'];
  const PROJECT_STATUS = {planning: 'Planning', active: 'Active', on_hold: 'On hold', done: 'Done', archived: 'Archived'};
  const ROLE_LABEL = {owner: 'Owner', editor: 'Editor', viewer: 'Viewer'};
  function statusPill(key) {
    const s = PM.STATUS[key] || PM.STATUS.todo;
    return h('span', {class: 'pm-pill ' + s.cls, text: s.label});
  }
  function projectPill(key) {
    return h('span', {class: 'pm-pill pm-ps-' + (PROJECT_STATUS[key] ? key : 'planning'), text: PROJECT_STATUS[key] || key});
  }
  // A glyph-only indicator always says what it is (title + aria-label).
  function glyph(ch, label, cls) {
    return h('span', {class: 'pm-glyph' + (cls ? ' ' + cls : ''), title: label, 'aria-label': label, role: 'img', text: ch});
  }

  PM.toast = function (msg, isError) {
    if (typeof showSaveToast === 'function') showSaveToast(msg, isError ? 'error' : undefined);
  };

  /* ── Network ───────────────────────────────────────────────────────────── */

  // One fetch → JSON. Errors carry .status (HTTP code; 0 = the reply was not
  // JSON, i.e. a login page or the plain 404 page — .http keeps the real
  // code for the gone-check) and .data.
  async function req(method, url, body, signal) {
    const opt = {method, credentials: 'same-origin', cache: 'no-store', signal,
                 headers: {'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}};
    if (body !== undefined) {
      opt.headers['Content-Type'] = 'application/json';
      opt.body = JSON.stringify(body);
    }
    let r;
    try {
      r = await fetch(url, opt);
    } catch (e) {
      if (e && e.name === 'AbortError') throw e;
      const err = new Error("Can't reach the server — check your connection.");
      err.status = -1;
      throw err;
    }
    if ((r.headers.get('Content-Type') || '').indexOf('application/json') < 0) {
      const err = new Error('Your session ended — reload the page.');
      err.status = 0;
      err.http = r.status;
      throw err;
    }
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) {
      const err = new Error((data && data.error) || 'Something went wrong. Please try again.');
      err.status = r.status;
      err.data = data;
      throw err;
    }
    return data;
  }
  const base = () => '/api/projects/' + PM.pid;
  PM.api = function (method, path, body) { return req(method, base() + (path || ''), body); };
  PM.upload = function (path, formData, onProgress) {
    // XMLHttpRequest for upload progress; it does not pass through app.js's
    // fetch wrapper, so the CSRF header is set here.
    return new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open('POST', base() + (path || ''));
      x.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
      x.setRequestHeader('Accept', 'application/json');
      if (onProgress && x.upload) {
        x.upload.addEventListener('progress', e => { if (e.lengthComputable) onProgress(e.loaded / e.total); });
      }
      const fail = (msg, status, data, http) => {
        const err = new Error(msg);
        err.status = status; err.data = data; err.http = http;
        reject(err);
      };
      x.onerror = () => fail("Can't reach the server — check your connection.", -1);
      x.onload = () => {
        if ((x.getResponseHeader('Content-Type') || '').indexOf('application/json') < 0) {
          return fail('Your session ended — reload the page.', 0, null, x.status);
        }
        let data = null;
        try { data = JSON.parse(x.responseText); } catch (e) { data = null; }
        if (x.status < 200 || x.status >= 300) {
          return fail((data && data.error) || 'Something went wrong. Please try again.', x.status, data);
        }
        resolve(data);
      };
      x.send(formData);
    });
  };

  /* ── Events ────────────────────────────────────────────────────────────── */
  const listeners = {};
  PM.on = function (ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); };
  PM.emit = function (ev, payload) {
    (listeners[ev] || []).slice().forEach(fn => {
      // One listener failing (another file's tab) must not stop the rest.
      try { fn(payload); } catch (e) { console.error('PM listener failed:', ev, e && e.name); }
    });
  };

  /* ── Dialogs ───────────────────────────────────────────────────────────── */
  const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
                    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  const dialogs = [];
  let dlgSeq = 0;
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const focusables = card => Array.from(card.querySelectorAll(FOCUSABLE)).filter(visible);
  PM.dialogOpen = () => dialogs.length > 0;

  PM.dialog = function (opts) {
    opts = opts || {};
    const back = document.activeElement;
    const titleId = 'pm-dlg-' + (++dlgSeq);
    const titleEl = h('span', {id: titleId, text: opts.title || ''});
    const x = h('button', {type: 'button', class: 'modal-close', 'aria-label': 'Close', text: '×'});
    const body = h('div', {class: 'modal-body'});
    const footer = h('div', {class: 'modal-footer'});
    const card = h('div', {class: 'modal-card' + (opts.wide ? ' modal-card-wide' : '') + (opts.xl ? ' modal-card-wide pm-card-xl' : ''),
                           role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId, tabIndex: -1},
                   h('div', {class: 'modal-header'}, titleEl, x), body, footer);
    const overlay = h('div', {class: 'modal-overlay pm-dialog' + (opts.drawer ? ' pm-drawer' : '')}, card);
    // The house convention: an overlay is open while style.display is 'flex'
    // (app.js's unsaved-changes guard watches exactly that).
    overlay.style.display = 'flex';
    if (opts.noDirtyGuard) overlay.dataset.noDirtyGuard = '1';
    let closed = false;
    const d = {
      el: card, overlay, body, footer, titleEl,
      setTitle(t) { titleEl.textContent = t; },
      dirty() { return !opts.noDirtyGuard && overlay.dataset.dirty === '1'; },
      clean() { overlay.dataset.dirty = ''; },
      // close(): no questions asked (after a save). ask(): the × / Esc path.
      close() {
        if (closed) return;
        closed = true;
        overlay.style.display = 'none';
        overlay.remove();
        const i = dialogs.indexOf(d);
        if (i >= 0) dialogs.splice(i, 1);
        if (typeof opts.onClose === 'function') { try { opts.onClose(); } catch (e) { /* closing must finish */ } }
        if (back && back.isConnected && typeof back.focus === 'function') back.focus({preventScroll: true});
      },
      ask() {
        if (d.dirty() && !window.confirm('You have unsaved changes in this dialog. Discard them?')) return;
        d.close();
      },
      focusFirst() {
        const first = body.querySelector('input:not([type="hidden"]):not([disabled]):not([readonly]), ' +
                                         'select:not([disabled]), textarea:not([disabled]):not([readonly])');
        const f = (first && visible(first)) ? first
          : (footer.querySelector('.btn-primary:not([disabled])') || focusables(card)[0] || card);
        f.focus({preventScroll: true});
      },
    };
    x.addEventListener('click', () => d.ask());
    // A backdrop press: app.js's capture-phase guard has already asked
    // "Discard them?" when the dialog is dirty (and swallowed the click on No).
    overlay.addEventListener('click', e => { if (e.target === overlay) d.close(); });
    document.body.appendChild(overlay);
    dialogs.push(d);
    // The caller fills body/footer right after this returns: focus next frame.
    requestAnimationFrame(() => { if (!closed && !card.contains(document.activeElement)) d.focusFirst(); });
    return d;
  };
  document.addEventListener('keydown', e => {
    const d = dialogs[dialogs.length - 1];
    if (!d) return;
    if (e.key === 'Escape') {
      // A popover or menu inside the dialog took the key first.
      if (e.defaultPrevented || (window.ToolsMenu && window.ToolsMenu.isOpen())) return;
      e.preventDefault();
      d.ask();
    } else if (e.key === 'Tab') {
      const f = focusables(d.el);
      if (!f.length) { e.preventDefault(); d.el.focus(); return; }
      const i = f.indexOf(document.activeElement);
      if (e.shiftKey && i <= 0) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && (i < 0 || i === f.length - 1)) { e.preventDefault(); f[0].focus(); }
    }
  });
  function closeAllDialogs() { dialogs.slice().reverse().forEach(d => d.close()); }

  PM.confirm = function (message, okLabel) {
    return new Promise(resolve => {
      let answer = false;
      const d = PM.dialog({title: 'Please confirm', noDirtyGuard: true, onClose: () => resolve(answer)});
      d.body.appendChild(h('p', {class: 'pm-desc', text: message}));
      const danger = /^(delete|remove|leave|unlink|archive|discard)/i.test(okLabel || '');
      const cancel = h('button', {type: 'button', class: 'btn btn-ghost', text: 'Cancel', onclick: () => d.close()});
      const ok = h('button', {type: 'button', class: 'btn ' + (danger ? 'btn-danger' : 'btn-primary'), text: okLabel || 'OK',
                              onclick: () => { answer = true; d.close(); }});
      d.footer.append(cancel, ok);
      // A destructive question starts on Cancel (an accidental Enter is safe).
      requestAnimationFrame(() => (danger ? cancel : ok).focus({preventScroll: true}));
    });
  };
  // "Type DELETE" — for the two things that cannot be undone.
  function confirmTyped(title, message, okLabel) {
    return new Promise(resolve => {
      let answer = false;
      const d = PM.dialog({title, noDirtyGuard: true, onClose: () => resolve(answer)});
      const input = h('input', {type: 'text', class: 'field-input', id: 'pm-typed', autocomplete: 'off',
                                autocapitalize: 'characters', spellcheck: 'false', placeholder: 'DELETE'});
      const ok = h('button', {type: 'button', class: 'btn btn-danger', text: okLabel, disabled: true,
                              onclick: () => { answer = true; d.close(); }});
      input.addEventListener('input', () => { ok.disabled = input.value.trim() !== 'DELETE'; });
      input.addEventListener('keydown', e => { if (e.key === 'Enter' && !ok.disabled) ok.click(); });
      d.body.append(h('p', {class: 'pm-desc', text: message}),
                    h('div', {class: 'field-group'}, h('label', {class: 'field-label', htmlFor: 'pm-typed', text: 'Type DELETE to confirm'}), input));
      d.footer.append(h('button', {type: 'button', class: 'btn btn-ghost', text: 'Cancel', onclick: () => d.close()}), ok);
    });
  }

  // field('Label', control) → the house .field-group with a real <label for>.
  let fieldSeq = 0;
  function field(label, control, opts) {
    opts = opts || {};
    if (!control.id) control.id = 'pm-f-' + (++fieldSeq);
    return h('div', {class: 'field-group' + (opts.required ? ' required' : '')},
             h('label', {class: 'field-label', htmlFor: control.id, text: label}), control,
             opts.hint ? h('p', {class: 'pm-note', text: opts.hint}) : null);
  }
  function select(options, value, props) {
    const s = h('select', Object.assign({class: 'field-input'}, props || {}));
    options.forEach(o => {
      if (o.group) {
        const g = h('optgroup', {label: o.group});
        o.items.forEach(i => g.appendChild(h('option', {value: String(i.value), text: i.label})));
        s.appendChild(g);
      } else s.appendChild(h('option', {value: String(o.value), text: o.label}));
    });
    s.value = value == null ? '' : String(value);
    return s;
  }
  // Disable a button while its request runs (no double submit); returns the
  // promise so callers can chain.
  function busy(btn, promise) {
    if (btn) btn.disabled = true;
    const done = () => { if (btn && btn.isConnected) btn.disabled = false; };
    promise.then(done, done);
    return promise;
  }
  const say = e => PM.toast((e && e.message) || 'Something went wrong.', true);

  // The same markup ToolsMenu.html() makes, built as DOM. Items come from the
  // provider registered under `provider` when the menu OPENS (never stale).
  function menu(provider, opts) {
    opts = opts || {};
    const title = opts.title || 'Tools';
    const btn = h('button', {type: 'button', class: 'btn btn-sm btn-ghost tools-menu-btn', title, 'aria-label': title,
                             'aria-haspopup': 'menu', 'aria-expanded': 'false'},
                  h('span', {class: 'tools-menu-label', text: opts.label || 'Tools'}),
                  h('span', {class: 'tools-menu-busy', hidden: true}),
                  opts.caret === false ? null : h('span', {class: 'tools-menu-caret', 'aria-hidden': 'true', text: '▾'}));
    const wrap = h('div', {class: 'tools-menu', data: Object.assign({tools: provider}, opts.data || {})}, btn,
                   h('div', {class: 'tools-menu-panel', role: 'menu', 'aria-label': title, hidden: true}));
    return wrap;
  }
  const regMenu = (name, fn) => { if (window.ToolsMenu) window.ToolsMenu.register(name, fn); };

  // Re-render `box` with `fn` but keep keyboard focus on the "same" control
  // (matched by data-fk) — a poll must not throw a keyboard user to <body>.
  function keepFocus(box, fn) {
    const a = document.activeElement;
    const key = (a && box.contains(a) && a.dataset) ? a.dataset.fk : null;
    fn();
    if (key) {
      const again = Array.from(box.querySelectorAll('[data-fk]')).find(n => n.dataset.fk === key);
      if (again) again.focus({preventScroll: true});
    }
  }
  // True while the user is in a form control inside `box` (rule (a)).
  function editingIn(box) {
    const a = document.activeElement;
    return !!(a && box.contains(a) && /^(SELECT|INPUT|TEXTAREA)$/.test(a.tagName) && a.type !== 'checkbox');
  }

  /* ── Colour swatches (new project, Settings) ───────────────────────────── */
  const COLORS = [['', 'No colour'], ['#F57F20', 'Orange'], ['#4A9EFF', 'Blue'], ['#14B8A6', 'Teal'], ['#22C55E', 'Green'],
                  ['#EAB308', 'Yellow'], ['#EF4444', 'Red'], ['#8B5CF6', 'Purple'], ['#EC4899', 'Pink']];
  function swatches(value, onChange) {
    let cur = isHex(value) ? value : '';
    const box = h('div', {class: 'pm-swatches', role: 'radiogroup', 'aria-label': 'Colour'});
    const list = COLORS.slice();
    if (cur && !list.some(c => c[0].toLowerCase() === cur.toLowerCase())) list.push([cur, 'Current colour']);
    const paint = () => Array.from(box.children).forEach(b => {
      const on = b.dataset.c.toLowerCase() === cur.toLowerCase();
      b.setAttribute('aria-checked', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
    });
    list.forEach(c => {
      const b = h('button', {type: 'button', class: 'pm-swatch' + (c[0] ? '' : ' is-none'), role: 'radio',
                             title: c[1], 'aria-label': c[1], data: {c: c[0]}});
      if (c[0]) b.style.setProperty('--sw', c[0]);
      b.addEventListener('click', () => { cur = c[0]; paint(); if (onChange) onChange(cur); });
      box.appendChild(b);
    });
    // Arrow keys move the choice, like a native radio group.
    box.addEventListener('keydown', e => {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      e.preventDefault();
      const all = Array.from(box.children);
      const i = Math.max(0, all.findIndex(b => b.getAttribute('aria-checked') === 'true'));
      const n = all[(i + (e.key === 'ArrowRight' ? 1 : -1) + all.length) % all.length];
      n.click(); n.focus();
    });
    paint();
    return {el: box, get: () => cur, set(v) { cur = isHex(v) ? v : ''; paint(); }};
  }

  /* ═══ The list page (/projects) ════════════════════════════════════════ */
  function initIndex() {
    const box = $('pm-index-body');
    const canCreate = root.dataset.canCreate === '1';
    let projects = [];
    let showArchived = false;

    function card(p) {
      const a = h('a', {class: 'pm-card' + (p.status === 'archived' ? ' is-archived' : ''), href: '/projects/' + p.id});
      if (isHex(p.color)) a.style.setProperty('--pm-edge', p.color);
      a.appendChild(h('div', {class: 'pm-card-top'}, h('span', {class: 'pm-card-name', text: p.name}), projectPill(p.status)));
      const meta = h('div', {class: 'pm-card-meta'});
      meta.appendChild(h('span', null, h('strong', {class: 'mono', text: String(p.open || 0)}), ' open'));
      if (p.overdue) meta.appendChild(h('span', {class: 'is-over'}, h('strong', {class: 'mono', text: String(p.overdue)}), ' overdue'));
      meta.appendChild(h('span', null, h('span', {class: 'mono', text: (p.done || 0) + '/' + (p.total || 0)}), ' done'));
      a.appendChild(meta);
      const bar = h('div', {class: 'pm-bar', role: 'img', 'aria-label': (p.done || 0) + ' of ' + (p.total || 0) + ' tasks done'});
      const fill = h('span', {class: 'pm-st-done'});
      fill.style.width = (p.total ? Math.round(100 * p.done / p.total) : 0) + '%';
      bar.appendChild(fill);
      a.appendChild(bar);
      if (p.next_milestone) {
        a.appendChild(h('div', {class: 'pm-card-meta'},
          h('span', null, glyph('◆', 'Next milestone', 'pm-glyph-ms'), ' ', p.next_milestone.title, ' · ',
            h('span', {class: 'mono', text: PM.fmtDate(p.next_milestone.date)}))));
      }
      const foot = h('div', {class: 'pm-card-foot'}, h('span', {class: 'pm-badge', text: ROLE_LABEL[p.role] || p.role}));
      const dates = fmtRange(p.start_date, p.end_date);
      if (dates) foot.appendChild(h('span', {class: 'mono', text: dates}));
      foot.appendChild(h('span', {text: plural(p.members || 0, 'person', 'people') +
                                        (p.role !== 'owner' && p.owner_name ? ' · owner ' + p.owner_name : '')}));
      a.appendChild(foot);
      return a;
    }

    function render() {
      const live = projects.filter(p => p.status !== 'archived');
      const arch = projects.filter(p => p.status === 'archived');
      $('pm-index-count').textContent = String(live.length);
      clear(box);
      if (!live.length) {
        const e = h('div', {class: 'pm-empty'}, arch.length ? 'No active projects.' : 'No projects yet.',
                    h('span', {class: 'pm-empty-sub', text: 'A project is private to the people its owner adds to it.'}));
        if (canCreate) e.appendChild(h('button', {type: 'button', class: 'btn btn-primary', text: 'New project', onclick: newProject}));
        box.appendChild(e);
      } else {
        box.appendChild(h('div', {class: 'pm-grid'}, live.map(card)));
      }
      if (arch.length) {
        box.appendChild(h('div', {class: 'pm-archived-toggle'},
          h('button', {type: 'button', class: 'pm-toggle', 'aria-pressed': showArchived ? 'true' : 'false',
                       'aria-controls': 'pm-archived', data: {fk: 'arch'},
                       text: (showArchived ? 'Hide archived' : 'Show archived') + ' (' + arch.length + ')',
                       onclick: () => { showArchived = !showArchived; keepFocus(box, render); }})));
        if (showArchived) box.appendChild(h('div', {class: 'pm-grid', id: 'pm-archived'}, arch.map(card)));
      }
    }

    async function load() {
      try {
        const j = await req('GET', '/api/projects');
        projects = j.projects || [];
        render();
      } catch (e) {
        clear(box).appendChild(h('div', {class: 'pm-empty'}, e.message,
          h('div', null, h('button', {type: 'button', class: 'btn btn-ghost', text: 'Retry', onclick: load}))));
      }
    }

    function newProject() {
      if (!canCreate) return;
      const d = PM.dialog({title: 'New project'});
      const name = h('input', {type: 'text', class: 'field-input', maxLength: 200, autocomplete: 'off'});
      const desc = h('textarea', {class: 'field-input field-textarea', rows: 3, maxLength: 10000});
      const start = h('input', {type: 'date', class: 'field-input'});
      const end = h('input', {type: 'date', class: 'field-input'});
      const col = swatches('');
      d.body.append(
        field('Name', name, {required: true}),
        field('Description', desc),
        h('div', {class: 'form-row cols-2'}, field('Starts', start), field('Ends', end)),
        h('div', {class: 'field-group'}, h('span', {class: 'field-label', text: 'Colour'}), col.el),
        h('p', {class: 'pm-note', text: 'Only you can see a new project until you add people to it in its Settings.'}));
      const save = h('button', {type: 'button', class: 'btn btn-primary', text: 'Create project'});
      const go = () => {
        if (!name.value.trim()) { PM.toast('Give the project a name.', true); name.focus(); return; }
        busy(save, req('POST', '/api/projects', {
          name: name.value, description: desc.value, start_date: start.value || null,
          end_date: end.value || null, color: col.get()})).then(j => {
            d.clean();
            window.location.href = '/projects/' + j.id;
          }, say);
      };
      save.addEventListener('click', go);
      name.addEventListener('keydown', e => { if (e.key === 'Enter') go(); });
      d.footer.append(h('button', {type: 'button', class: 'btn btn-ghost', text: 'Cancel', onclick: () => d.ask()}), save);
    }

    const btn = $('pm-new-project');
    if (btn) btn.addEventListener('click', newProject);
    load();
  }

  /* ═══ One project (/projects/<id>) ═════════════════════════════════════ */
  function initProject() {
    PM.pid = parseInt(root.dataset.pid, 10);
    PM.state = null; PM.me = null; PM.activeTab = null;
    PM.can = {edit: false, manage: false, comment: false, budget: false, createShow: false};

    const TABS = ['overview', 'tasks', 'timeline', 'budget', 'files', 'notes', 'contacts', 'discussion', 'settings'];
    const LINK_PARAMS = ['task', 'comment', 'contact', 'line', 'file', 'note'];
    const tabs = {};                 // name → {impl, mounted}
    const taskMap = new Map();
    let knownRev = null;             // the project rev PM.state reflects
    let stateSeq = 0, pollSeq = 0, pollTimer = null;
    let started = false, gone = false, lastParams = {};
    let readyResolve;
    PM.ready = new Promise(r => { readyResolve = r; });

    const bySort = (a, b) => (a.sort_order - b.sort_order) || (a.id - b.id);
    function reindex() {
      const s = PM.state;
      s.tasks.sort(bySort);
      taskMap.clear();
      s.tasks.forEach(t => taskMap.set(t.id, t));
    }
    function setState(s) {
      PM.state = s;
      PM.me = s.me;
      PM.can = {edit: !!s.me.can_edit, manage: !!s.me.can_manage, comment: !!s.me.can_comment,
                budget: !!s.me.can_budget, createShow: !!s.me.can_create_show};
      knownRev = s.project.rev;
      reindex();
    }

    PM.taskById = id => taskMap.get(+id) || null;
    PM.userName = function (id) {
      if (id == null) return '';
      const m = PM.state.members.find(x => x.id === +id);
      return m ? m.name : '(not on project)';     // an assignee who left keeps the task
    };
    PM.contactName = function (id) {
      if (id == null) return '';
      const c = PM.state.contacts.find(x => x.id === +id);
      return c ? c.name : '';
    };
    PM.taskGroups = function () {
      const out = [{name: '', tasks: []}], at = {'': 0};
      PM.state.tasks.forEach(t => {            // already in sort_order, id order
        const g = t.group_name || '';
        if (!(g in at)) { at[g] = out.length; out.push({name: g, tasks: []}); }
        out[at[g]].tasks.push(t);
      });
      return out;
    };
    const isOverdue = t => !!(t.due_date && t.status !== 'done' && t.due_date < todayIso());
    const canSetStatus = t => PM.can.edit ||
      (t.assignee_id === PM.me.id && !PM.me.readonly && !PM.me.archived);   // the assignee may tick their own task off

    /* ── State: reload, own writes, the poll ─────────────────────────────── */
    const isGone = e => !!e && (e.status === 404 || (e.status === 0 && (e.http === 404 || e.http === 200)));

    PM.reload = async function (attempt) {
      const my = ++stateSeq;
      let s;
      try {
        s = await req('GET', base());
      } catch (e) {
        if (isGone(e)) await handleGone();
        throw e;
      }
      if (my !== stateSeq || gone) return;                  // a newer reload answered / is in flight
      if (knownRev != null && s.project.rev < knownRev && (attempt || 0) < 2) {
        return PM.reload((attempt || 0) + 1);               // this read raced one of our own writes
      }
      setState(s);
      afterState();
      PM.emit('state');
    };

    PM.applyWrite = function (resp) {
      const s = PM.state;
      if (!resp || !s) return;
      const rev = typeof resp.rev === 'number' ? resp.rev : null;
      if (rev != null && rev < knownRev) {                  // an answer older than what we hold: don't merge it
        PM.reload().catch(() => {});
        return;
      }
      if (Array.isArray(resp.tasks)) {
        resp.tasks.forEach(t => {
          if (!t || typeof t.id !== 'number') return;
          const i = s.tasks.findIndex(x => x.id === t.id);
          if (i >= 0) s.tasks[i] = t; else s.tasks.push(t);
        });
      }
      if (resp.project && typeof resp.project === 'object') s.project = resp.project;
      if (Array.isArray(resp.members)) s.members = resp.members;
      if (resp.contact && typeof resp.contact.id === 'number') {
        const i = s.contacts.findIndex(x => x.id === resp.contact.id);
        if (i >= 0) s.contacts[i] = resp.contact; else s.contacts.push(resp.contact);
        s.contacts.sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()) || a.id - b.id);
      }
      reindex();
      if (rev != null) {
        if (rev === knownRev || rev === knownRev + 1) { knownRev = rev; s.project.rev = rev; }
        else PM.reload().catch(() => {});                   // someone else wrote in between: take it all
      }
      afterState();
      PM.emit('state');
    };
    // Internal: a task we just deleted leaves the state before applyWrite.
    function dropTask(id) {
      const s = PM.state;
      s.tasks = s.tasks.filter(t => t.id !== id);
      s.tasks.forEach(t => {
        t.depends_on = (t.depends_on || []).filter(x => x !== id);
      });
    }

    async function pollOnce() {
      if (gone || document.hidden || !started) return;
      const my = ++pollSeq;
      try {
        const j = await req('GET', base() + '/rev');
        if (my !== pollSeq || gone) return;
        // `>`: a poll that raced our own write answers an older rev.
        if (typeof j.rev === 'number' && knownRev != null && j.rev > knownRev) await PM.reload();
      } catch (e) {
        if (isGone(e)) handleGone();
        // anything else (offline, a 503): keep the page; the next tick retries
      }
    }
    function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }
    function startPoll() {
      stopPoll();
      if (gone || document.hidden) return;
      pollTimer = setInterval(pollOnce, 10000);
    }
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stopPoll();
      else { pollOnce(); startPoll(); }
    });
    window.addEventListener('pagehide', stopPoll);
    window.addEventListener('pageshow', e => { if (e.persisted) { pollOnce(); startPoll(); } });

    // The project is gone, I was removed from it, my access was taken away —
    // or my session simply ended. Ask the app's own (side-effect-free)
    // session endpoint which, so the person is told the truth.
    async function handleGone() {
      if (gone) return;
      gone = true;
      stopPoll();
      closeAllDialogs();
      if (window.ToolsMenu) window.ToolsMenu.close(false);
      closePop();
      let signedIn = true;
      try {
        const r = await fetch('/api/session/status', {credentials: 'same-origin', cache: 'no-store',
                                                      headers: {'Accept': 'application/json'}});
        signedIn = !!(await r.json()).authenticated;
      } catch (e) { signedIn = true; }
      const head = $('pm-head');
      if (head) head.remove();
      document.title = 'Project — 3·2·1→Theater';
      clear(root).appendChild(h('div', {class: 'pm-empty', role: 'alert'},
        signedIn ? 'This project is no longer available.' : 'Your session ended — sign in again.',
        h('div', null, signedIn ? h('a', {class: 'btn btn-ghost', href: '/', text: 'Back to the timeline'})
                                : h('a', {class: 'btn btn-primary', href: '/login', text: 'Sign in'}))));
    }

    /* ── Tabs ────────────────────────────────────────────────────────────── */
    function tabAllowed(name) {
      if (TABS.indexOf(name) < 0) return false;
      if (name === 'budget') return PM.can.budget;
      if (name === 'settings') return PM.can.manage;
      return true;
    }
    function enterTab(name, params) {
      const pane = $('pm-pane-' + name);
      const t = tabs[name];
      if (!t) {
        // Its file is not deployed (or failed to load): say so quietly.
        clear(pane).appendChild(h('p', {class: 'pm-empty', text: 'Not available yet.'}));
        return;
      }
      try {
        if (!t.mounted) {
          clear(pane);
          t.mounted = true;
          if (t.impl.mount) t.impl.mount(pane);
        }
        if (t.impl.show) t.impl.show(params || {});
      } catch (e) {
        console.error('PM tab failed:', name, e && e.name);
        if (!pane.firstChild) pane.appendChild(h('p', {class: 'pm-empty', text: 'This tab could not be shown.'}));
      }
    }
    PM.registerTab = function (name, impl) {
      if (TABS.indexOf(name) < 0 || !impl) return;
      tabs[name] = {impl, mounted: false};
      if (started && PM.activeTab === name) enterTab(name, lastParams);
    };
    PM.setTab = function (name, params) {
      if (!started || gone) return;
      if (!tabAllowed(name)) name = 'overview';
      const prev = PM.activeTab;
      if (prev && prev !== name && tabs[prev] && tabs[prev].mounted && tabs[prev].impl.hide) {
        try { tabs[prev].impl.hide(); } catch (e) { /* the next tab still opens */ }
      }
      PM.activeTab = name;
      lastParams = params || {};
      TABS.forEach(n => {
        const on = n === name, b = $('pm-tab-' + n);
        $('pm-pane-' + n).hidden = !on;
        b.setAttribute('aria-selected', on ? 'true' : 'false');
        b.tabIndex = on ? 0 : -1;
      });
      // ?tab=… only: ids from a deep link are used once, not kept in the bar.
      try {
        const u = new URL(window.location.href);
        u.search = name === 'overview' ? '' : '?tab=' + name;
        window.history.replaceState(null, '', u.pathname + u.search + u.hash);
      } catch (e) { /* history unavailable: the tab still switches */ }
      enterTab(name, lastParams);
      if (prev !== name) PM.emit('tab', name);
    };
    const tabBar = $('pm-tabs');
    tabBar.addEventListener('click', e => {
      const b = e.target.closest('.pm-tab');
      if (b) PM.setTab(b.dataset.tab);
    });
    // The tablist keyboard pattern: arrows / Home / End move AND select.
    tabBar.addEventListener('keydown', e => {
      if (['ArrowRight', 'ArrowLeft', 'Home', 'End'].indexOf(e.key) < 0) return;
      const live = TABS.filter(n => !$('pm-tab-' + n).hidden);
      let i = live.indexOf(PM.activeTab);
      if (e.key === 'ArrowRight') i = (i + 1) % live.length;
      else if (e.key === 'ArrowLeft') i = (i - 1 + live.length) % live.length;
      else if (e.key === 'Home') i = 0;
      else i = live.length - 1;
      e.preventDefault();
      PM.setTab(live[i]);
      const b = $('pm-tab-' + live[i]);
      b.focus();
      b.scrollIntoView({block: 'nearest', inline: 'nearest'});
    });
    function tabCount(name, n) {
      const b = $('pm-tab-' + name);
      let c = b.querySelector('.pm-count');
      if (!n) { if (c) c.remove(); return; }
      if (!c) { c = h('span', {class: 'pm-count'}); b.appendChild(c); }
      c.textContent = String(n);
    }

    /* ── Header ──────────────────────────────────────────────────────────── */
    // Redrawn on every state change — keeping keyboard focus where it was.
    function renderHeader() { keepFocus($('pm-head'), drawHeader); }
    function drawHeader() {
      const s = PM.state, p = s.project, me = s.me;
      document.title = p.name + ' — 3·2·1→Theater';
      $('pm-name').textContent = p.name;
      const tile = $('pm-tile');
      if (isHex(p.color)) tile.style.setProperty('--pm-tile', p.color); else tile.style.removeProperty('--pm-tile');
      const tags = clear($('pm-head-tags'));
      tags.appendChild(projectPill(p.status));
      if (me.archived) {
        tags.appendChild(h('span', {class: 'pm-badge pm-badge-muted', text: 'Archived — read only'}));
        if (PM.can.manage) {
          tags.appendChild(h('button', {type: 'button', class: 'btn btn-xs btn-ghost', text: 'Reopen', data: {fk: 'reopen'},
                                        onclick: e => reopen(e.currentTarget)}));
        }
      } else if (!PM.can.edit) {
        tags.appendChild(h('span', {class: 'rv-badge', text: 'VIEW ONLY',
          title: me.readonly ? 'Your account is read-only: you can see this project but not change it'
                             : 'You are a viewer on this project' + (PM.can.comment ? ': you can comment, and set the status of tasks assigned to you' : '')}));
      }
      const meta = clear($('pm-head-meta'));
      const dates = fmtRange(p.start_date, p.end_date);
      meta.appendChild(h('span', {class: 'mono', text: dates || 'No dates set'}));
      const av = h('button', {type: 'button', class: 'pm-link-btn pm-avatars', title: 'People on this project',
                              'aria-label': plural(s.members.length, 'person', 'people') + ' on this project', data: {fk: 'people'},
                              onclick: peopleDialog});
      s.members.slice(0, 6).forEach(m => av.appendChild(avatar(m.name, false, !m.active)));
      if (s.members.length > 6) av.appendChild(h('span', {class: 'pm-avatars-more', text: '+' + (s.members.length - 6)}));
      meta.appendChild(av);
      meta.appendChild(h('span', {text: 'You: ' + (ROLE_LABEL[me.role] || me.role)}));

      const ban = clear($('pm-head-banner'));
      if (p.ownerless) {
        const b = h('div', {class: 'pm-banner pm-banner-warn pm-head-banner', role: 'status'},
                    h('span', null, h('strong', {text: 'This project has no owner.'}),
                      me.can_claim ? ' You have been on it longest — you can take it over.'
                                   : ' Its owner’s account was removed; the member first in line can take it over.'));
        if (me.can_claim) {
          b.appendChild(h('button', {type: 'button', class: 'btn btn-sm btn-primary', text: 'Become the owner', data: {fk: 'claim'},
            onclick: e => busy(e.currentTarget, PM.api('POST', '/claim-owner')).then(() => {
              PM.toast('✓ You are now the owner');
              return PM.reload();
            }).catch(say)}));
        }
        ban.appendChild(b);
      }
      $('pm-head-actions').hidden = false;
      $('pm-tab-budget').hidden = !PM.can.budget;
      $('pm-tab-settings').hidden = !PM.can.manage;
      tabCount('tasks', s.tasks.filter(t => t.status !== 'done').length);
      tabCount('discussion', s.discussion_count);
      tabCount('files', s.counts && s.counts.files);
      tabCount('notes', s.counts && s.counts.notes);
      tabCount('contacts', s.contacts.length);
      pinHeader();
    }
    // Phones: let the title block scroll away and keep only the tab strip
    // pinned (projects.css reads --pm-pin).
    function pinHeader() {
      const head = $('pm-head');
      if (!head) return;
      if (!document.documentElement.classList.contains('mobile-view')) { head.style.removeProperty('--pm-pin'); return; }
      head.style.setProperty('--pm-pin', Math.max(0, head.offsetHeight - tabBar.offsetHeight - 6) + 'px');
    }
    window.addEventListener('resize', pinHeader);

    function reopen(btn) {
      if (!PM.can.manage) return;
      busy(btn, PM.api('PUT', '', {status: 'active'})).then(() => {
        PM.toast('✓ Project reopened');
        return PM.reload();          // every permission changes with it
      }).catch(say);
    }

    function peopleDialog() {
      const d = PM.dialog({title: 'People on this project', noDirtyGuard: true});
      const list = h('div', {class: 'pm-list'});
      PM.state.members.forEach(m => {
        list.appendChild(h('div', {class: 'pm-row'}, avatar(m.name, false, !m.active),
          h('span', {class: 'pm-row-main'}, h('span', {class: 'pm-row-title', text: m.name + (m.id === PM.me.id ? ' (you)' : '')}),
            m.active ? null : h('span', {class: 'pm-row-sub', text: 'no longer has access'})),
          h('span', {class: 'pm-badge', text: ROLE_LABEL[m.role] || m.role})));
      });
      d.body.style.padding = '8px 6px';
      d.body.appendChild(list);
      if (PM.can.manage && !PM.me.archived) {
        d.footer.appendChild(h('button', {type: 'button', class: 'btn btn-ghost', text: 'Manage people…',
                                          onclick: () => { d.close(); PM.setTab('settings'); }}));
      }
      d.footer.appendChild(h('button', {type: 'button', class: 'btn btn-primary', text: 'Close', onclick: () => d.close()}));
    }
    function leaveProject() {
      if (PM.me.role === 'owner' || PM.me.readonly) return;
      PM.confirm('Leave this project? You will no longer see it. Tasks assigned to you stay assigned until someone changes them.',
                 'Leave project').then(ok => {
        if (!ok) return;
        PM.api('DELETE', '/members/' + PM.me.id).then(() => {
          gone = true;                 // no poll, no "no longer available" flash on the way out
          stopPoll();
          window.location.href = '/projects';
        }, say);
      });
    }
    // The header's ⋯ menu. NOT a ToolsMenu: that panel is position:fixed
    // inside its wrapper, and the frosted header (backdrop-filter) is a
    // containing block for fixed descendants — the panel would land off
    // screen. This one is a popover on <body> (see below), keyboard-driven
    // from the button: ↓ / ↑ move, Enter picks, Esc closes.
    (function initHeadMenu() {
      const btn = h('button', {type: 'button', class: 'btn btn-sm btn-ghost', title: 'More', 'aria-label': 'More',
                               'aria-haspopup': 'menu', 'aria-expanded': 'false', 'aria-controls': 'pm-head-menu-list', text: '⋯'});
      const isOpen = () => !!(pop && pop.anchor === btn);
      function open(viaKeyboard) {
        if (!PM.state) return;
        const rows = [{label: 'People…', run: peopleDialog}];
        if (PM.can.manage) rows.push({label: 'Project settings', run: () => PM.setTab('settings')});
        if (PM.me.role !== 'owner' && !PM.me.readonly) rows.push({label: 'Leave project…', cls: 'is-danger', run: leaveProject});
        rows.forEach(r => { r.kids = [h('span', {text: r.label})]; });
        openPop(btn, 'pm-head-menu-list', rows, null, row => { closePop(); btn.focus(); row.run(); }, 'menu');
        if (viaKeyboard) popMove(1);
      }
      btn.addEventListener('click', e => { if (isOpen()) closePop(); else open(e.detail === 0); });
      btn.addEventListener('keydown', e => {
        if (popKey(e, btn, false)) return;
        if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !isOpen()) { e.preventDefault(); open(false); popMove(e.key === 'ArrowDown' ? 1 : -1); }
      });
      btn.addEventListener('blur', () => setTimeout(() => { if (isOpen()) closePop(); }, 120));
      $('pm-head-menu').appendChild(btn);
    })();

    /* ── Popovers on <body> (search results, @mentions) ──────────────────── */
    // One at a time. Lives on <body> so no card / header can clip it, and
    // above the dialog layer because the mention list opens inside dialogs.
    let pop = null;            // {el, anchor, items: [button], active, onPick}
    function closePop() {
      if (!pop) return;
      const a = pop.anchor;
      pop.el.remove();
      pop = null;
      if (a) { a.setAttribute('aria-expanded', 'false'); a.removeAttribute('aria-activedescendant'); }
    }
    function placePop() {
      if (!pop) return;
      const r = pop.anchor.getBoundingClientRect();
      if (!r.width && !r.height) { closePop(); return; }
      const el = pop.el, vw = document.documentElement.clientWidth, vh = window.innerHeight, m = 8;
      el.style.minWidth = Math.min(Math.max(pop.role === 'menu' ? 190 : 240, r.width), vw - 2 * m) + 'px';
      const hgt = el.offsetHeight, w = el.offsetWidth;
      const below = vh - r.bottom - m, above = r.top - m;
      const top = (hgt <= below || below >= above) ? r.bottom + 4 : Math.max(m, r.top - 4 - hgt);
      el.style.top = Math.round(top) + 'px';
      // A menu hangs from its button's right edge; a list starts at its field's left.
      el.style.left = Math.round(Math.max(m, Math.min(pop.role === 'menu' ? r.right - w : r.left, vw - w - m))) + 'px';
    }
    function openPop(anchor, id, rows, note, onPick, role) {
      closePop();
      const el = h('div', {class: 'pm-pop', id, role: role || 'listbox'});
      const items = [];
      rows.forEach((row, i) => {
        const b = h('div', {class: 'pm-pop-item' + (row.cls ? ' ' + row.cls : ''), role: role === 'menu' ? 'menuitem' : 'option',
                            id: id + '-' + i, 'aria-selected': role === 'menu' ? null : 'false'},
                    row.kids);
        // mousedown, not click: the anchor must not lose focus first.
        b.addEventListener('mousedown', e => { e.preventDefault(); onPick(row, i); });
        el.appendChild(b);
        items.push(b);
      });
      if (note) el.appendChild(h('div', {class: 'pm-pop-note', text: note}));
      document.body.appendChild(el);
      pop = {el, anchor, items, active: -1, rows, onPick, role: role || 'listbox'};
      anchor.setAttribute('aria-expanded', 'true');
      placePop();
    }
    function popMove(step) {
      if (!pop || !pop.items.length) return;
      const n = pop.items.length;
      pop.active = pop.active < 0 ? (step > 0 ? 0 : n - 1) : (pop.active + step + n) % n;
      pop.items.forEach((b, i) => {
        b.classList.toggle('is-active', i === pop.active);
        if (b.hasAttribute('aria-selected')) b.setAttribute('aria-selected', i === pop.active ? 'true' : 'false');
      });
      pop.anchor.setAttribute('aria-activedescendant', pop.items[pop.active].id);
      pop.items[pop.active].scrollIntoView({block: 'nearest'});
    }
    // ↑ ↓ Enter Tab Esc for whichever input owns the open popover. Returns
    // true when it used the key.
    function popKey(e, anchor, tabPicks) {
      if (!pop || pop.anchor !== anchor) return false;
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); popMove(e.key === 'ArrowDown' ? 1 : -1); return true; }
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closePop(); return true; }
      if ((e.key === 'Enter' || (tabPicks && e.key === 'Tab')) && pop.items.length) {
        const i = pop.active < 0 ? (tabPicks ? 0 : -1) : pop.active;
        if (i < 0) return false;
        e.preventDefault();
        pop.onPick(pop.rows[i], i);
        return true;
      }
      return false;
    }
    document.addEventListener('pointerdown', e => {
      if (pop && !pop.el.contains(e.target) && e.target !== pop.anchor) closePop();
    }, true);
    window.addEventListener('resize', placePop);
    window.addEventListener('scroll', e => { if (pop && !(e.target instanceof Node && pop.el.contains(e.target))) placePop(); }, true);

    /* ── Search inside the project ───────────────────────────────────────── */
    const TYPE_LABEL = {task: 'Task', comment: 'Comment', contact: 'Contact', note: 'Note', file: 'File',
                        budget_line: 'Budget', budget_source: 'Budget source'};
    (function initSearch() {
      const input = $('pm-search-q');
      let seq = 0, timer = null;
      function pick(row) {
        const r = row.res;
        closePop();
        input.value = '';
        input.blur();
        const params = {};
        Object.keys(r.params || {}).forEach(k => { if (LINK_PARAMS.indexOf(k) >= 0 && Number.isInteger(r.params[k])) params[k] = r.params[k]; });
        PM.setTab(TABS.indexOf(r.tab) >= 0 ? r.tab : 'overview', params);
      }
      async function run() {
        const q = input.value.trim();
        const my = ++seq;
        if (q.length < 2) { closePop(); return; }
        let j;
        try {
          // POST: the words never sit in a URL.
          j = await PM.api('POST', '/search', {q});
        } catch (e) {
          if (my === seq) openPop(input, 'pm-search-results', [], e.message, pick);
          return;
        }
        if (my !== seq || document.activeElement !== input) return;
        const rows = (j.results || []).map(r => ({res: r, kids: [
          h('span', {class: 'k', text: TYPE_LABEL[r.type] || 'Result'}),
          h('span', {class: 't', text: r.title || '(untitled)'}),
          r.snippet ? h('span', {class: 's', text: r.snippet}) : null]}));
        openPop(input, 'pm-search-results', rows, rows.length ? null : 'Nothing in this project matches.', pick);
      }
      input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 250); });
      input.addEventListener('keydown', e => {
        if (popKey(e, input, false)) return;
        if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); run(); }
        else if (e.key === 'Escape' && input.value) { input.value = ''; seq++; }
      });
      input.addEventListener('blur', () => setTimeout(() => { if (pop && pop.anchor === input) closePop(); }, 120));
    })();

    /* ── Project text → DOM ──────────────────────────────────────────────── */
    // Escaped (text nodes only), newlines kept, "@Name" highlighted when
    // Name is a current member — matched longest name first with a
    // non-word character on both sides, as the server's mention rule does.
    // Nothing is ever linked. Scans by hand: no RegExp is built from names.
    const isWord = ch => !!ch && /[A-Za-z0-9_]/.test(ch);
    PM.richText = function (str) {
      const frag = document.createDocumentFragment();
      const text = String(str == null ? '' : str);
      const names = ((PM.state && PM.state.members) || []).map(m => m.name).filter(Boolean)
        .sort((a, b) => b.length - a.length);
      text.split('\n').forEach((line, li) => {
        if (li) frag.appendChild(document.createElement('br'));
        const lower = line.toLowerCase();
        let pos = 0, at = names.length ? line.indexOf('@') : -1;
        while (at >= 0) {
          let hit = null;
          if (!isWord(line.charAt(at - 1))) {
            hit = names.find(n => lower.startsWith(n.toLowerCase(), at + 1) && !isWord(line.charAt(at + 1 + n.length))) || null;
          }
          if (hit) {
            if (at > pos) frag.appendChild(document.createTextNode(line.slice(pos, at)));
            frag.appendChild(h('span', {class: 'pm-mention', text: line.slice(at, at + 1 + hit.length)}));
            pos = at + 1 + hit.length;
          }
          at = line.indexOf('@', hit ? pos : at + 1);
        }
        if (pos < line.length) frag.appendChild(document.createTextNode(line.slice(pos)));
      });
      return frag;
    };

    /* ── @mention autocomplete (members of THIS project only) ────────────── */
    function attachMentions(ta) {
      function find() {
        const before = ta.value.slice(0, ta.selectionStart);
        const m = /(^|[^A-Za-z0-9_])@([A-Za-z0-9_]*)$/.exec(before);
        return m ? {q: m[2].toLowerCase(), start: before.length - m[2].length - 1} : null;
      }
      function update() {
        const f = find();
        if (!f) { if (pop && pop.anchor === ta) closePop(); return; }
        const rows = PM.state.members.filter(m => m.active && m.id !== PM.me.id && m.name.toLowerCase().indexOf(f.q) >= 0)
          .slice(0, 6).map(m => ({name: m.name, cls: 'pm-pop-row', kids: [avatar(m.name, true), h('span', {text: m.name})]}));
        if (!rows.length) { if (pop && pop.anchor === ta) closePop(); return; }
        openPop(ta, 'pm-mention-list', rows, null, row => {
          const g = find();
          if (g) {
            const end = ta.selectionStart;
            ta.value = ta.value.slice(0, g.start) + '@' + row.name + ' ' + ta.value.slice(end);
            const caret = g.start + row.name.length + 2;
            ta.setSelectionRange(caret, caret);
          }
          closePop();
          ta.focus();
        });
      }
      ta.addEventListener('input', update);
      ta.addEventListener('click', update);
      ta.addEventListener('blur', () => setTimeout(() => { if (pop && pop.anchor === ta) closePop(); }, 120));
      // Returns true when the key belonged to the open list.
      return e => popKey(e, ta, true);
    }

    /* ── A comment thread (task dialog + Discussion tab) ─────────────────── */
    // opts: {taskId | null, focusComment?}. Returns {el, composer, load, sync}.
    // The composer is a SEPARATE node that is never rebuilt, so a refetch
    // can't touch what is being typed; while a comment is being edited
    // inline the list is not redrawn either (rule (a)).
    function makeThread(opts) {
      const taskId = opts.taskId || null;
      const list = h('div', {class: 'pm-thread-list', role: 'list', 'aria-label': 'Comments'});
      const note = h('p', {class: 'pm-note', role: 'status'});
      const more = h('button', {type: 'button', class: 'btn btn-xs btn-ghost', text: 'Show earlier comments', hidden: true});
      const el = h('div', {class: 'pm-thread'}, more, note, list);
      let comments = [], seq = 0, loadedRev = null, editing = false, stale = false, mine = false;
      let target = opts.focusComment || null;

      // My own write: merge it, and remember this thread is already current.
      function own(resp) {
        mine = true;
        try { PM.applyWrite(resp); } finally { mine = false; }
        loadedRev = knownRev;
      }
      function bump(n) {
        if (taskId) {
          const t = PM.taskById(taskId);
          if (t) t.comment_count = Math.max(0, (t.comment_count || 0) + n);
        } else {
          PM.state.discussion_count = Math.max(0, (PM.state.discussion_count || 0) + n);
        }
      }

      function row(c) {
        const body = h('div', {class: 'pm-cmt-body'}, PM.richText(c.body));
        const acts = h('span', {class: 'pm-cmt-acts'});
        const item = h('div', {class: 'pm-cmt', role: 'listitem', data: {cid: c.id}}, avatar(c.author),
          h('div', {class: 'pm-cmt-bubble'},
            h('div', {class: 'pm-cmt-head'}, h('strong', {text: c.author}), h('time', {text: fmtWhen(c.at)}),
              c.edited_at ? h('span', {class: 'ed', text: '(edited)'}) : null, acts), body));
        if (c.can_edit && PM.can.comment) {
          acts.appendChild(h('button', {type: 'button', text: 'Edit', data: {fk: 'ce' + c.id}, onclick: () => edit(c, body, acts)}));
        }
        if (c.can_delete) {
          acts.appendChild(h('button', {type: 'button', class: 'del', text: 'Delete', data: {fk: 'cd' + c.id}, onclick: () => del(c)}));
        }
        return item;
      }
      function draw() {
        if (editing) { stale = true; return; }
        stale = false;
        keepFocus(list, () => {
          clear(list);
          comments.forEach(c => list.appendChild(row(c)));
        });
        note.textContent = comments.length ? '' : (PM.can.comment ? 'No comments yet — start the conversation.' : 'No comments yet.');
        note.hidden = !!comments.length;
        if (target) {
          const hit = Array.from(list.children).find(n => n.dataset.cid === String(target));
          if (hit) {
            hit.classList.add('is-target');
            hit.scrollIntoView({block: 'center'});
          } else {
            PM.toast('That item no longer exists.', true);
          }
          target = null;
        }
      }
      function edit(c, body, acts) {
        if (editing || !PM.can.comment) return;
        editing = true;
        acts.hidden = true;
        const ta = h('textarea', {class: 'field-input field-textarea', rows: 3, maxLength: 4000, 'aria-label': 'Edit comment'});
        ta.value = c.body;
        const key = attachMentions(ta);
        const done = () => { editing = false; draw(); };
        const save = h('button', {type: 'button', class: 'btn btn-xs btn-primary', text: 'Save'});
        save.addEventListener('click', () => {
          if (!ta.value.trim()) { PM.toast('Write a comment first.', true); return; }
          busy(save, PM.api('PUT', '/comments/' + c.id, {body: ta.value})).then(resp => {
            const i = comments.findIndex(x => x.id === c.id);
            if (i >= 0 && resp.comment) comments[i] = resp.comment;
            own(resp);
            done();
          }, say);
        });
        ta.addEventListener('keydown', e => {
          if (key(e)) return;
          if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(); }
        });
        clear(body).append(ta, h('div', {class: 'pm-field-row', style: 'margin-top:6px'}, save,
          h('button', {type: 'button', class: 'btn btn-xs btn-ghost', text: 'Cancel', onclick: done})));
        ta.focus();
      }
      function del(c) {
        PM.confirm('Delete this comment?', 'Delete').then(ok => {
          if (!ok) return;
          PM.api('DELETE', '/comments/' + c.id).then(resp => {
            comments = comments.filter(x => x.id !== c.id);
            bump(-1);
            own(resp);
            draw();
          }, say);
        });
      }

      async function load(older) {
        const my = ++seq;
        const rev = knownRev;
        let path = '/comments?' + (taskId ? 'task_id=' + taskId : '');
        if (older && comments.length) path += '&before=' + comments[0].id;
        try {
          const j = await PM.api('GET', path);
          if (my !== seq) return;
          comments = older ? (j.comments || []).concat(comments) : (j.comments || []);
          more.hidden = !j.has_more;
          if (!older) loadedRev = rev;
          draw();
        } catch (e) {
          if (my !== seq) return;
          if (e.status === 404 && taskId) { note.hidden = false; note.textContent = 'This task was deleted.'; return; }
          note.hidden = false;
          note.textContent = e.message;
        }
      }
      more.addEventListener('click', () => load(true));

      // Composer
      let composer = null;
      if (PM.can.comment) {
        const ta = h('textarea', {class: 'field-input field-textarea', rows: 2, maxLength: 4000,
                                  placeholder: 'Write a comment… type @ to mention someone on the project',
                                  'aria-label': 'Write a comment', 'aria-autocomplete': 'list', 'aria-expanded': 'false'});
        const post = h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: 'Post'});
        const key = attachMentions(ta);
        const send = () => {
          if (!PM.can.comment) return;
          const text = ta.value.trim();
          if (!text) { ta.focus(); return; }
          busy(post, PM.api('POST', '/comments', taskId ? {task_id: taskId, body: text} : {body: text})).then(resp => {
            ta.value = '';
            if (resp.comment) comments.push(resp.comment);
            bump(1);
            own(resp);
            if (opts.onPosted) opts.onPosted();
            draw();
            const last = list.lastElementChild;
            if (last) last.scrollIntoView({block: 'nearest'});
          }, say);
        };
        post.addEventListener('click', send);
        const phone = document.documentElement.classList.contains('mobile-view');
        ta.addEventListener('keydown', e => {
          if (key(e)) return;
          // Desktop: Enter posts, Shift+Enter is a new line (the Comments tab's
          // rule). Phones: Enter is a new line, the button posts.
          if (e.key === 'Enter' && !e.shiftKey && !phone && !e.isComposing) { e.preventDefault(); send(); }
        });
        composer = h('div', null, h('div', {class: 'pm-composer'}, ta, post),
                     h('p', {class: 'pm-composer-hint', text: phone ? 'Type @ to mention someone.' : 'Enter to post · Shift+Enter for a new line'}));
        composer.textarea = ta;
      } else if (PM.me.archived || PM.me.readonly) {
        composer = null;
      } else {
        composer = h('p', {class: 'pm-note', text: 'Viewers can’t comment on this project.'});
      }
      return {
        el, composer, load,
        hasDraft: () => !!(composer && composer.textarea && composer.textarea.value.trim()),
        // On 'state': refetch when the project moved since this thread loaded.
        sync() { if (!mine && loadedRev != null && knownRev !== loadedRev) load(); },
      };
    }

    /* ── Saving tasks ────────────────────────────────────────────────────── */
    let noticeEl = null, noticeTimer = null;
    function closeNotice() {
      clearTimeout(noticeTimer);
      if (noticeEl) { noticeEl.remove(); noticeEl = null; }
    }
    // NON-modal: the save is done; shifting is offered, never assumed.
    function conflictNotice(taskId, n) {
      closeNotice();
      noticeEl = h('div', {class: 'pm-notice', role: 'status'},
        h('span', {text: 'Saved — ' + plural(n, 'task') + ' now ' + (n === 1 ? 'starts' : 'start') + ' too early'}),
        PM.can.edit ? h('button', {type: 'button', class: 'btn btn-sm btn-accent', text: 'Review shift…',
                                   onclick: () => { closeNotice(); PM.reviewShift(taskId); }}) : null,
        h('button', {type: 'button', class: 'x', 'aria-label': 'Dismiss', text: '×', onclick: closeNotice}));
      document.body.appendChild(noticeEl);
      noticeTimer = setTimeout(closeNotice, 15000);
    }
    PM.saveTask = function (id, changes) {
      const t = PM.taskById(id);
      const keys = Object.keys(changes || {});
      const statusOnly = keys.length === 1 && keys[0] === 'status';
      if (!t || !(PM.can.edit || (statusOnly && canSetStatus(t)))) {
        // View-only: the handler is a no-op (the server would refuse anyway).
        PM.emit('state');
        return Promise.reject(new Error(t ? 'You have view-only access to this project.' : 'That item no longer exists.'));
      }
      return PM.api('PUT', '/tasks/' + id, changes).then(resp => {
        PM.applyWrite(resp);
        if (resp.new_conflicts && resp.new_conflicts.length) conflictNotice(id, resp.new_conflicts.length);
        return resp;
      }, e => {
        PM.toast(e.message, true);
        if (e.status === 404 || e.status === 403) PM.reload().catch(() => {});
        else PM.emit('state');           // whatever was dragged snaps back
        throw e;
      });
    };

    PM.reviewShift = async function (taskId) {
      if (!PM.can.edit) return;
      let plan;
      try {
        plan = await PM.api('POST', '/tasks/' + taskId + '/shift-preview');
      } catch (e) { say(e); return; }
      const moves = plan.moves || [];
      const d = PM.dialog({title: 'Review the shift', wide: true, noDirtyGuard: true});
      if (!moves.length) {
        d.body.appendChild(h('p', {class: 'pm-desc', text: 'Nothing needs to move — every task here already starts after the tasks it depends on.'}));
        d.footer.appendChild(h('button', {type: 'button', class: 'btn btn-primary', text: 'Close', onclick: () => d.close()}));
        return;
      }
      d.body.appendChild(h('p', {class: 'pm-desc',
        text: 'Each task below moves later by the fewest days that lets it start after what it depends on, keeping its length. Nothing else changes, and nothing moves until you apply.'}));
      const tb = h('tbody');
      let withShow = 0;
      moves.forEach(m => {
        const days = PM.diffDays(m.old_start || m.old_due, m.new_start || m.new_due);
        if (m.show_id) withShow++;
        tb.appendChild(h('tr', null,
          h('td', null, m.is_milestone ? [glyph('◆', 'Milestone', 'pm-glyph-ms'), ' '] : null, m.title,
            m.show_id ? [' ', h('span', {class: 'pm-badge pm-badge-accent', title: 'This task has a linked show. The show itself is not moved.', text: 'Linked show'})] : null),
          h('td', {class: 'd'}, h('span', {class: 'old', text: fmtRange(m.old_start, m.old_due)})),
          h('td', {class: 'd'}, h('span', {class: 'new', text: fmtRange(m.new_start, m.new_due)})),
          h('td', {class: 'd', text: '+' + plural(days || 0, 'day')})));
      });
      d.body.appendChild(h('div', {class: 'pm-scroll-x'}, h('table', {class: 'pm-shift-table'},
        h('thead', null, h('tr', null, h('th', {text: 'Task'}), h('th', {text: 'Was'}), h('th', {text: 'Becomes'}), h('th', {text: 'Moves'}))), tb)));
      if (withShow) {
        d.body.appendChild(h('div', {class: 'pm-banner'}, h('span', {
          text: 'Linked shows are never moved: only the task dates change. Check the ' + (withShow === 1 ? 'show' : 'shows') + ' yourself.'})));
      }
      const apply = h('button', {type: 'button', class: 'btn btn-primary', text: 'Move ' + plural(moves.length, 'task')});
      apply.addEventListener('click', () => {
        busy(apply, PM.api('POST', '/tasks/' + taskId + '/shift', {token: plan.token})).then(resp => {
          d.close();
          closeNotice();
          PM.applyWrite(resp);
          PM.toast('✓ Moved ' + plural(resp.moved || moves.length, 'task'));
        }, e => {
          say(e);
          if (e.status === 409) { d.close(); PM.reload().catch(() => {}).then(() => PM.reviewShift(taskId)); }
        });
      });
      d.footer.append(h('button', {type: 'button', class: 'btn btn-ghost', text: 'Cancel', onclick: () => d.close()}), apply);
      requestAnimationFrame(() => apply.focus({preventScroll: true}));
    };

    /* ── The task dialog ─────────────────────────────────────────────────── */
    let taskDlg = null;        // the open one: {id, d, sync}
    PM.on('state', () => { if (taskDlg) taskDlg.sync(); });

    PM.openTask = function (id, opts) {
      const t = PM.taskById(id);
      if (!t) { PM.toast('That item no longer exists.', true); return; }
      if (taskDlg) taskDlg.d.close();
      taskDialog(t, opts || {});
      PM.emit('task', t.id);
    };
    PM.newTask = function (defaults) {
      if (!PM.can.edit) return;
      if (taskDlg) taskDlg.d.close();
      taskDialog(null, {defaults: defaults || {}});
    };

    function taskDialog(task, opts) {
      const isNew = !task;
      const id = isNew ? null : task.id;
      const editable = PM.can.edit;
      const ownStatus = !isNew && !editable && canSetStatus(task);
      const snap = t => ({
        title: t.title || '', description: t.description || '', status: t.status || 'todo',
        priority: t.priority == null ? 1 : +t.priority, assignee_id: t.assignee_id == null ? null : +t.assignee_id,
        group_name: t.group_name || '', start_date: t.start_date || null, due_date: t.due_date || null,
        is_milestone: !!t.is_milestone, contact_id: t.contact_id == null ? null : +t.contact_id,
        depends_on: (t.depends_on || []).slice().sort((a, b) => a - b)});
      let B = snap(task || opts.defaults || {});       // what the SERVER holds (the baseline)
      let baseStamp = isNew ? null : task.updated_at;
      let saving = false, deleted = false;
      const d = PM.dialog({title: isNew ? 'New task' : 'Task', xl: true,
                           noDirtyGuard: !editable && !ownStatus && !PM.can.comment,
                           onClose: () => { if (taskDlg && taskDlg.d === d) taskDlg = null; }});
      const ro = !editable;

      // Controls
      const title = h('input', {type: 'text', class: 'field-input', maxLength: 200, readOnly: ro, autocomplete: 'off'});
      const desc = h('textarea', {class: 'field-input field-textarea', rows: 4, maxLength: 10000, readOnly: ro});
      const status = select(STATUS_KEYS.map(k => ({value: k, label: PM.STATUS[k].label})), B.status,
                            {disabled: !(editable || ownStatus)});
      const priority = select([3, 2, 1, 0].map(k => ({value: k, label: PM.PRIORITY[k].label})), B.priority, {disabled: ro});
      const who = [{value: '', label: 'Unassigned'}].concat(PM.state.members.map(m =>
        ({value: m.id, label: m.name + (m.active ? '' : ' (no access)')})));
      if (B.assignee_id != null && !PM.state.members.some(m => m.id === B.assignee_id)) {
        who.push({value: B.assignee_id, label: '(not on project)'});
      }
      const assignee = select(who, B.assignee_id, {disabled: ro});
      const groupList = h('datalist', {id: 'pm-groups-' + (++fieldSeq)},
        PM.taskGroups().filter(g => g.name).map(g => h('option', {value: g.name})));
      const group = h('input', {type: 'text', class: 'field-input', maxLength: 120, readOnly: ro, list: groupList.id, autocomplete: 'off'});
      const start = h('input', {type: 'date', class: 'field-input', readOnly: ro});
      const due = h('input', {type: 'date', class: 'field-input', readOnly: ro});
      const milestone = h('input', {type: 'checkbox', disabled: ro});
      const contact = select([{value: '', label: 'None'}].concat(PM.state.contacts.map(c =>
        ({value: c.id, label: c.name + (c.company ? ' — ' + c.company : '')}))), B.contact_id, {disabled: ro});
      let deps = B.depends_on.slice();
      const depBox = h('div', {class: 'pm-chips'});
      const intOrNull = v => (v === '' || v == null) ? null : +v;

      const F = {
        title:        {get: () => title.value.trim(),        set: v => { title.value = v; }, el: title},
        description:  {get: () => desc.value.trim(),         set: v => { desc.value = v; }, el: desc},
        status:       {get: () => status.value,              set: v => { status.value = v; }, el: status},
        priority:     {get: () => +priority.value,           set: v => { priority.value = String(v); }, el: priority},
        assignee_id:  {get: () => intOrNull(assignee.value), set: v => { assignee.value = v == null ? '' : String(v); }, el: assignee},
        group_name:   {get: () => group.value.trim(),        set: v => { group.value = v; }, el: group},
        start_date:   {get: () => start.value || null,       set: v => { start.value = v || ''; }, el: start},
        due_date:     {get: () => due.value || null,         set: v => { due.value = v || ''; }, el: due},
        is_milestone: {get: () => milestone.checked,         set: v => { milestone.checked = !!v; }, el: milestone},
        contact_id:   {get: () => intOrNull(contact.value),  set: v => { contact.value = v == null ? '' : String(v); }, el: contact},
        depends_on:   {get: () => deps.slice().sort((a, b) => a - b), set: v => { deps = v.slice(); drawDeps(); }, el: depBox},
      };
      const same = (a, b) => Array.isArray(a) ? a.join(',') === (b || []).join(',') : a === b;
      const changed = k => !same(F[k].get(), B[k]);
      function changes() {
        const out = {};
        Object.keys(F).forEach(k => { if (changed(k)) out[k] = F[k].get(); });
        return out;
      }
      // Take the server's values. keepMine: a field I changed keeps my edit
      // (it then simply differs from the new baseline and is saved as mine).
      function fill(v, keepMine) {
        const mine = keepMine ? Object.keys(F).filter(changed) : [];
        Object.keys(F).forEach(k => { if (mine.indexOf(k) < 0) F[k].set(v[k]); });
        B = v;
      }

      function drawDeps() {
        clear(depBox);
        deps.forEach(did => {
          const t = PM.taskById(did);
          const chip = h('span', {class: 'pm-chip'}, h('span', {text: t ? t.title : '(deleted task)'}));
          if (editable) {
            chip.appendChild(h('button', {type: 'button', class: 'pm-chip-x', 'aria-label': 'Remove dependency on ' + (t ? t.title : 'deleted task'),
              text: '×', onclick: () => { deps = deps.filter(x => x !== did); d.overlay.dataset.dirty = '1'; drawDeps(); }}));
          }
          depBox.appendChild(chip);
        });
        if (!deps.length && !editable) depBox.appendChild(h('span', {class: 'pm-note', text: 'Nothing'}));
        if (!editable) return;
        const groups = PM.taskGroups().map(g => ({group: g.name || 'No group',
          items: g.tasks.filter(t => t.id !== id && deps.indexOf(t.id) < 0).map(t => ({value: t.id, label: t.title}))}))
          .filter(g => g.items.length);
        if (!groups.length) return;
        const pick = select([{value: '', label: deps.length ? 'Add another…' : 'Choose a task…'}].concat(groups), '',
                            {'aria-label': 'Add a task this one depends on'});
        pick.style.width = 'auto';
        pick.style.maxWidth = '100%';
        pick.addEventListener('change', () => {
          const v = +pick.value;
          if (v && deps.indexOf(v) < 0) { deps.push(v); drawDeps(); const again = depBox.querySelector('select'); if (again) again.focus(); }
        });
        depBox.appendChild(pick);
      }
      fill(B, false);

      // Banners (re-drawn on every 'state'; they are not inputs)
      const banners = h('div', {style: 'display:flex;flex-direction:column;gap:8px'});
      let updatedBy = null;
      function drawBanners() {
        clear(banners);
        const t = isNew ? null : PM.taskById(id);
        if (deleted) {
          banners.appendChild(h('div', {class: 'pm-banner pm-banner-warn', role: 'status'},
            h('span', {text: 'This task was deleted by someone else. Your text is still here to copy; it can’t be saved.'})));
          return;
        }
        if (updatedBy != null) {
          banners.appendChild(h('div', {class: 'pm-banner', role: 'status', id: 'pm-task-updated'},
            h('span', null, h('strong', {text: 'Updated by ' + (updatedBy || 'someone') + ' — reload fields'}),
              h('span', {class: 'pm-note', style: 'display:block', text: 'Fields you have changed keep your edit; the rest take the saved values.'})),
            h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Reload fields', onclick: () => {
              const cur = PM.taskById(id);
              if (cur) { fill(snap(cur), true); baseStamp = cur.updated_at; }
              updatedBy = null;
              drawBanners();
            }})));
        }
        if (!t) return;
        if (t.conflict) {
          const names = (t.conflict_with || []).map(x => { const o = PM.taskById(x); return o ? '“' + o.title + '”' : null; }).filter(Boolean);
          banners.appendChild(h('div', {class: 'pm-banner pm-banner-warn', id: 'pm-task-conflict'},
            h('span', null, glyph('⚠', 'Schedule conflict', 'pm-glyph-warn'), ' Starts before ' + (names.join(', ') || 'a task it depends on') +
              (names.length > 1 ? ' finish.' : ' finishes.')),
            editable ? h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Move this task and the ones after it…',
                                    onclick: () => PM.reviewShift(id)}) : null));
        }
        const late = PM.state.tasks.filter(o => (o.conflict_with || []).indexOf(id) >= 0);
        if (late.length) {
          banners.appendChild(h('div', {class: 'pm-banner pm-banner-warn', id: 'pm-task-dependents'},
            h('span', null, glyph('⚠', 'Schedule conflict', 'pm-glyph-warn'), ' ' + plural(late.length, 'task') + ' that ' +
              (late.length === 1 ? 'depends' : 'depend') + ' on this now ' + (late.length === 1 ? 'starts' : 'start') + ' too early.'),
            editable ? h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Shift dependent tasks…',
                                    onclick: () => PM.reviewShift(id)}) : null));
        }
      }

      // The linked show / "Make a show from this"
      const showRow = h('div', {class: 'pm-field-row'});
      function drawShow() {
        clear(showRow);
        const t = isNew ? null : PM.taskById(id);
        if (!t) return;
        if (t.show_id) {
          const s = PM.state.shows.find(x => x.id === t.show_id);
          showRow.appendChild(h('span', {class: 'pm-section-label', text: 'Show'}));
          showRow.appendChild(s
            ? h('a', {class: 'pm-chip', href: '/shows/' + s.id, target: '_blank', rel: 'noopener', title: 'Open show (new tab)'},
                h('span', {text: 'Open show: ' + s.name}), h('span', {'aria-hidden': 'true', text: '↗'}))
            : h('span', {class: 'pm-chip'}, h('span', {text: 'Linked to a show you can’t open'})));
        } else if (editable && PM.can.createShow) {
          showRow.appendChild(h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Make a show from this…', onclick: () => {
            const cur = PM.taskById(id) || t;
            // A milestone IS the day; otherwise the show starts when the work does.
            PM.newShow({task_id: id, name: cur.title,
                        show_date: cur.is_milestone ? (cur.due_date || cur.start_date) : (cur.start_date || cur.due_date),
                        onDone: resp => { const mine = (resp.tasks || []).find(x => x.id === id); if (mine) baseStamp = mine.updated_at; }});
          }}));
        }
      }

      d.body.append(banners,
        field('Title', title, {required: editable}),
        field('Description', desc),
        h('div', {class: 'form-row cols-3'}, field('Status', status), field('Priority', priority), field('Assigned to', assignee)),
        h('div', {class: 'form-row cols-3'}, field('Start', start), field('Due', due), h('div', {class: 'field-group'},
          h('span', {class: 'field-label', text: 'Milestone'}),
          h('label', {class: 'pm-check', style: 'min-height:38px'}, milestone, 'A single key date (◆)'))),
        h('div', {class: 'form-row cols-2'}, h('div', null, field('Group', group), groupList), field('Contact', contact)),
        h('div', {class: 'field-group'}, h('span', {class: 'field-label', text: 'Depends on (must finish first)'}), depBox),
        showRow);
      if (ownStatus) {
        d.body.insertBefore(h('p', {class: 'pm-note', text: 'This task is assigned to you: you can set its status. Everything else is view-only.'}), d.body.children[1]);
      }

      // Comments + History (existing tasks)
      let thread = null;
      if (!isNew) {
        const tabC = h('button', {type: 'button', class: 'pm-seg-btn', 'aria-pressed': 'true', text: 'Comments'});
        const tabH = h('button', {type: 'button', class: 'pm-seg-btn', 'aria-pressed': 'false', text: 'History'});
        const paneC = h('div'), paneH = h('div', {hidden: true});
        thread = makeThread({taskId: id, focusComment: opts.comment || null,
                             // posting a comment is not an unsaved field edit
                             onPosted: () => { if (!Object.keys(changes()).length) d.clean(); }});
        paneC.append(thread.el, thread.composer || '');
        let histLoaded = false;
        async function loadHistory() {
          histLoaded = true;
          clear(paneH).appendChild(h('p', {class: 'pm-note', text: 'Loading…'}));
          try {
            const j = await PM.api('GET', '/activity?entity=task&entity_id=' + id);
            clear(paneH);
            if (!(j.items || []).length) { paneH.appendChild(h('p', {class: 'pm-note', text: 'No history yet.'})); return; }
            paneH.appendChild(activityList(j.items));
          } catch (e) {
            histLoaded = false;
            clear(paneH).appendChild(h('p', {class: 'pm-note', text: e.message}));
          }
        }
        const pickTab = hist => {
          tabC.setAttribute('aria-pressed', hist ? 'false' : 'true');
          tabH.setAttribute('aria-pressed', hist ? 'true' : 'false');
          paneC.hidden = hist; paneH.hidden = !hist;
          if (hist && !histLoaded) loadHistory();
        };
        tabC.addEventListener('click', () => pickTab(false));
        tabH.addEventListener('click', () => pickTab(true));
        d.body.append(h('hr', {class: 'pm-hr'}), h('div', {class: 'pm-seg', style: 'align-self:flex-start', role: 'group', 'aria-label': 'Comments or history'}, tabC, tabH), paneC, paneH);
        thread.load();
      }

      // Footer
      const saveBtn = h('button', {type: 'button', class: 'btn btn-primary', text: isNew ? 'Add task' : 'Save'});
      async function save() {
        if (deleted || saving || !(editable || ownStatus)) return;
        const v = {};
        Object.keys(F).forEach(k => { v[k] = F[k].get(); });
        if (editable && !v.title) { PM.toast('Give the task a title.', true); title.focus(); return; }
        if (editable && v.start_date && v.due_date && v.due_date < v.start_date) {
          PM.toast('The due date is before the start date.', true); due.focus(); return;
        }
        saving = true;
        try {
          if (isNew) {
            const resp = await busy(saveBtn, PM.api('POST', '/tasks', v));
            d.close();
            PM.applyWrite(resp);
            if (resp.new_conflicts && resp.new_conflicts.length) conflictNotice(resp.id, resp.new_conflicts.length);
            else PM.toast('✓ Task added');
          } else {
            // PARTIAL: only what I changed — someone else's edit to another
            // field of this task is never written back over.
            const ch = changes();
            if (!editable) Object.keys(ch).forEach(k => { if (k !== 'status') delete ch[k]; });
            if (!Object.keys(ch).length) { d.close(); return; }
            const resp = await busy(saveBtn, PM.saveTask(id, ch));
            d.close();
            if (!(resp.new_conflicts && resp.new_conflicts.length)) PM.toast('✓ Saved');
          }
        } catch (e) {
          if (isNew) say(e);              // PM.saveTask already toasted
        } finally {
          saving = false;
        }
      }
      saveBtn.addEventListener('click', save);
      [title, group, start, due].forEach(n => n.addEventListener('keydown', e => {
        if (e.key === 'Enter' && (editable || ownStatus)) { e.preventDefault(); save(); }
      }));
      if (!isNew && editable) {
        d.footer.appendChild(h('div', {class: 'pm-foot-left'}, h('button', {type: 'button', class: 'btn btn-danger-ghost', text: 'Delete', onclick: e => {
          const btn = e.currentTarget;
          PM.confirm('Delete this task? It can be restored from Tasks → Tools → Deleted tasks.', 'Delete').then(ok => {
            if (!ok) return;
            saving = true;
            busy(btn, PM.api('DELETE', '/tasks/' + id)).then(resp => {
              d.close();
              dropTask(id);
              PM.applyWrite(resp);
              PM.toast('✓ Task deleted');
            }, e2 => { saving = false; say(e2); });
          });
        }})));
      }
      d.footer.appendChild(h('button', {type: 'button', class: 'btn btn-ghost', text: (editable || ownStatus) ? 'Cancel' : 'Close', onclick: () => d.ask()}));
      if (editable || ownStatus) d.footer.appendChild(saveBtn);

      drawBanners();
      drawShow();
      if (opts.comment) requestAnimationFrame(() => { /* focus stays on the list for a deep link */ d.el.focus({preventScroll: true}); });

      // What a poll (or my own other write) changes while this dialog is
      // open. Never rebuild the fields; never overwrite silently.
      function sync() {
        if (isNew || saving) return;
        const t = PM.taskById(id);
        if (!t) { deleted = true; saveBtn.disabled = true; drawBanners(); return; }
        if (t.updated_at !== baseStamp) {
          const typing = document.activeElement && d.body.contains(document.activeElement) &&
                         Object.keys(F).some(k => F[k].el === document.activeElement || F[k].el.contains(document.activeElement));
          if (Object.keys(changes()).length || typing) {
            updatedBy = t.updated_name || '';
          } else {
            fill(snap(t), false);
            baseStamp = t.updated_at;
          }
        }
        drawBanners();
        drawShow();
        if (thread) thread.sync();
      }
      taskDlg = {id, d, sync};
    }

    // [{actor, detail, at}] → rows "Dan Lupo created task “…” · when"
    function activityList(items) {
      const list = h('div', {class: 'pm-list pm-activity'});
      items.forEach(a => {
        list.appendChild(h('div', {class: 'pm-row'}, h('span', {class: 'pm-row-main'},
          h('span', {class: 'who', text: a.actor}), ' ', a.detail), h('time', {text: fmtWhen(a.at)})));
      });
      return list;
    }

    /* ── Shows: create a real one, or link an existing one ───────────────── */
    PM.newShow = function (defaults) {
      defaults = defaults || {};
      if (!PM.can.edit) return;
      if (!PM.can.createShow) { PM.toast('Your account can’t create shows.', true); return; }
      const d = PM.dialog({title: defaults.task_id ? 'Make a show from this task' : 'New show'});
      const name = h('input', {type: 'text', class: 'field-input', maxLength: 200, autocomplete: 'off'});
      name.value = defaults.name || '';
      const date = h('input', {type: 'date', class: 'field-input'});
      date.value = defaults.show_date || '';
      const vl = h('datalist', {id: 'pm-venues-' + (++fieldSeq)}, (PM.state.venues || []).map(v => h('option', {value: String(v)})));
      const venue = h('input', {type: 'text', class: 'field-input', maxLength: 200, list: vl.id, autocomplete: 'off'});
      venue.value = defaults.venue || '';
      const mode = select([{value: 'show', label: 'Show'}, {value: 'event', label: 'Event'}], defaults.show_mode || 'show');
      d.body.append(
        h('div', {class: 'pm-banner pm-banner-warn', role: 'note'},
          h('span', null, h('strong', {text: 'This creates a real show that everyone in 3·2·1→Theater can see.'}),
            ' It appears on the Shows board like any other; the project itself stays private.')),
        field('Show name', name, {required: true}),
        h('div', {class: 'form-row cols-2'}, field('Date', date), field('Type', mode)),
        h('div', null, field('Venue', venue), vl));
      const go = h('button', {type: 'button', class: 'btn btn-primary', text: 'Create show'});
      go.addEventListener('click', () => {
        if (!name.value.trim()) { PM.toast('Give the show a name.', true); name.focus(); return; }
        const body = {name: name.value, show_date: date.value || null, venue: venue.value, show_mode: mode.value};
        const call = defaults.task_id ? PM.api('POST', '/tasks/' + defaults.task_id + '/to-show', body)
                                      : PM.api('POST', '/shows', {new: body});
        busy(go, call).then(resp => {
          d.close();
          if (typeof defaults.onDone === 'function') defaults.onDone(resp);
          PM.applyWrite(resp);
          PM.toast('✓ Show created');
          return PM.reload();          // the linked-shows list comes with the bundle
        }).catch(say);
      });
      d.footer.append(h('button', {type: 'button', class: 'btn btn-ghost', text: 'Cancel', onclick: () => d.ask()}), go);
    };

    // A search-and-pick dialog (shows, the app's contacts): POSTs {q}, lists
    // the results as buttons. Typing in it is not "unsaved changes".
    function pickDialog(title, placeholder, path, rowOf, onPick, emptyText) {
      const d = PM.dialog({title, noDirtyGuard: true});
      const q = h('input', {type: 'search', class: 'field-input', placeholder, 'aria-label': placeholder, maxLength: 200, autocomplete: 'off'});
      const out = h('div', {class: 'pm-list', role: 'list', style: 'margin:0 -22px -22px'});
      let seq = 0, timer = null;
      async function run() {
        const my = ++seq;
        try {
          const j = await PM.api('POST', path, {q: q.value.trim()});
          if (my !== seq) return;
          clear(out);
          const res = j.results || [];
          if (!res.length) { out.appendChild(h('p', {class: 'pm-empty', text: emptyText})); return; }
          res.forEach(r => {
            const b = h('button', {type: 'button', class: 'pm-row', role: 'listitem'}, rowOf(r));
            b.addEventListener('click', () => onPick(r, d, b));
            out.appendChild(b);
          });
        } catch (e) {
          if (my === seq) clear(out).appendChild(h('p', {class: 'pm-empty', text: e.message}));
        }
      }
      q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(run, 250); });
      d.body.append(q, out);
      d.footer.appendChild(h('button', {type: 'button', class: 'btn btn-ghost', text: 'Close', onclick: () => d.close()}));
      run();
      return d;
    }
    PM.linkShow = function () {
      if (!PM.can.edit) return;
      pickDialog('Link a show', 'Search shows by name or venue…', '/show-search',
        r => [h('span', {class: 'pm-row-main'}, h('span', {class: 'pm-row-title', text: r.name}),
                h('span', {class: 'pm-row-sub', style: 'display:block', text: [r.venue, r.date ? PM.fmtDate(r.date) : 'No date'].filter(Boolean).join(' · ')})),
              r.show_mode === 'event' ? h('span', {class: 'pm-badge pm-badge-event', text: 'Event'}) : null,
              r.status !== 'active' ? h('span', {class: 'pm-badge pm-badge-muted', text: 'Archived'}) : null,
              r.is_test ? h('span', {class: 'pm-badge pm-badge-muted', text: 'Test'}) : null],
        (r, d, b) => busy(b, PM.api('POST', '/shows', {show_id: r.id})).then(() => {
          d.close();
          PM.toast('✓ Show linked');
          return PM.reload();
        }).catch(say),
        'No shows match (shows already linked are not listed).');
    };

    /* ── Contacts ────────────────────────────────────────────────────────── */
    PM.newContact = function (defaults) {
      if (!PM.can.edit) return;
      contactDialog(null, defaults || {});
    };
    function contactDialog(contact, defaults) {
      const isNew = !contact;
      const editable = PM.can.edit;
      const v0 = contact || defaults || {};
      const d = PM.dialog({title: isNew ? 'New contact' : 'Contact', wide: !isNew, noDirtyGuard: !editable});
      const mk = (k, max, type) => {
        const i = h('input', {type: type || 'text', class: 'field-input', maxLength: max, readOnly: !editable, autocomplete: 'off'});
        i.value = v0[k] || '';
        return i;
      };
      const f = {name: mk('name', 200), company: mk('company', 120), role: mk('role', 120),
                 email: mk('email', 254, 'email'), phone: mk('phone', 120, 'tel'),
                 notes: h('textarea', {class: 'field-input field-textarea', rows: 3, maxLength: 10000, readOnly: !editable})};
      f.notes.value = v0.notes || '';
      d.body.append(field('Name', f.name, {required: true}),
        h('div', {class: 'form-row cols-2'}, field('Company', f.company), field('Role', f.role)),
        h('div', {class: 'form-row cols-2'}, field('Email', f.email), field('Phone', f.phone)),
        field('Notes', f.notes));
      if (!isNew) {
        const mine = PM.state.tasks.filter(t => t.contact_id === contact.id);
        d.body.appendChild(h('hr', {class: 'pm-hr'}));
        d.body.appendChild(h('span', {class: 'pm-section-label', text: 'Tasks for this contact'}));
        if (mine.length) {
          const list = h('div', {class: 'pm-list', style: 'margin:0 -22px'});
          mine.forEach(t => list.appendChild(h('button', {type: 'button', class: 'pm-row', onclick: () => { d.ask(); if (!dialogs.includes(d)) PM.openTask(t.id); }},
            statusPill(t.status), h('span', {class: 'pm-row-main pm-row-title', text: t.title}),
            t.due_date ? h('span', {class: 'pm-row-date' + (isOverdue(t) ? ' is-over' : ''), text: PM.fmtDate(t.due_date)}) : null)));
          d.body.appendChild(list);
        } else {
          d.body.appendChild(h('p', {class: 'pm-note', text: 'No tasks point at this contact yet — pick it in a task’s Contact field.'}));
        }
        if (PM.can.budget) {
          // Budget lines live with the Budget tab's API; shown when it answers.
          const slot = h('div');
          d.body.appendChild(slot);
          PM.api('GET', '/budget').then(j => {
            const lines = (j.lines || []).filter(l => l.contact_id === contact.id);
            if (!lines.length || !d.el.isConnected) return;
            const list = h('div', {class: 'pm-list', style: 'margin:0 -22px'});
            lines.forEach(l => list.appendChild(h('button', {type: 'button', class: 'pm-row', onclick: () => { d.ask(); if (!dialogs.includes(d)) PM.setTab('budget', {line: l.id}); }},
              h('span', {class: 'pm-row-main'}, h('span', {class: 'pm-row-title', text: l.description || l.category || 'Budget line'}),
                l.category ? h('span', {class: 'pm-row-sub', style: 'display:block', text: l.category}) : null),
              h('span', {class: 'pm-row-date', text: PM.money(l.actual != null ? l.actual : (l.committed != null ? l.committed : l.estimate))}))));
            slot.append(h('span', {class: 'pm-section-label', text: 'Budget lines for this contact'}), list);
          }, () => { /* no budget API yet, or not allowed: just no list */ });
        }
      }
      const vals = () => { const o = {}; Object.keys(f).forEach(k => { o[k] = f[k].value.trim(); }); return o; };
      const save = h('button', {type: 'button', class: 'btn btn-primary', text: isNew ? 'Add contact' : 'Save'});
      const go = () => {
        if (!editable) return;
        const v = vals();
        if (!v.name) { PM.toast('Give the contact a name.', true); f.name.focus(); return; }
        let call;
        if (isNew) {
          if (defaults && Number.isInteger(defaults.source_contact_id)) v.source_contact_id = defaults.source_contact_id;
          call = PM.api('POST', '/contacts', v);
        } else {
          const ch = {};
          Object.keys(v).forEach(k => { if (v[k] !== (contact[k] || '')) ch[k] = v[k]; });
          if (!Object.keys(ch).length) { d.close(); return; }
          call = PM.api('PUT', '/contacts/' + contact.id, ch);
        }
        busy(save, call).then(resp => { d.close(); PM.applyWrite(resp); PM.toast(isNew ? '✓ Contact added' : '✓ Saved'); }, say);
      };
      save.addEventListener('click', go);
      Object.keys(f).forEach(k => { if (k !== 'notes') f[k].addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); go(); } }); });
      if (!isNew && editable) {
        d.footer.appendChild(h('div', {class: 'pm-foot-left'}, h('button', {type: 'button', class: 'btn btn-danger-ghost', text: 'Delete', onclick: e => {
          const btn = e.currentTarget;
          PM.confirm('Delete this contact? Tasks and budget lines that point at it are kept, without the contact.', 'Delete').then(ok => {
            if (!ok) return;
            busy(btn, PM.api('DELETE', '/contacts/' + contact.id)).then(resp => {
              d.close();
              PM.state.contacts = PM.state.contacts.filter(c => c.id !== contact.id);
              PM.applyWrite(resp);
              PM.toast('✓ Contact deleted');
            }, say);
          });
        }})));
      }
      d.footer.appendChild(h('button', {type: 'button', class: 'btn btn-ghost', text: editable ? 'Cancel' : 'Close', onclick: () => d.ask()}));
      if (editable) d.footer.appendChild(save);
    }
    function pickAppContact() {
      if (!PM.can.edit) return;
      pickDialog('Add from the app’s contacts', 'Search the contact list…', '/contact-search',
        r => [h('span', {class: 'pm-row-main'}, h('span', {class: 'pm-row-title', text: r.name}),
                h('span', {class: 'pm-row-sub', style: 'display:block', text: [r.title, r.department, r.email, r.phone].filter(Boolean).join(' · ')}))],
        (r, d) => {
          d.close();
          // A COPY: later edits here never touch the app's contact list.
          contactDialog(null, {name: r.name, role: r.title, company: r.department, email: r.email, phone: r.phone,
                               source_contact_id: r.id});
        },
        'No contacts match.');
    }

    /* ═══ Tab: Overview ════════════════════════════════════════════════════ */
    let activityStale = false, activityTimer = null;
    PM.registerTab('overview', (function () {
      let pane = null, c = null;       // c = the card bodies that get redrawn
      const notes = {};                // summary_text / risks_text → {ta, status, dirty, timer, sent}

      function card(titleText, bodyEl, actions) {
        return h('div', {class: 'form-section'},
          h('div', {class: 'form-section-header'}, h('span', {text: titleText}), actions || null), bodyEl);
      }
      function taskRows(list, max, emptyText) {
        const box = h('div', {class: 'pm-list'});
        if (!list.length) { box.appendChild(h('p', {class: 'pm-empty', text: emptyText})); return box; }
        list.slice(0, max).forEach(t => {
          const date = t.due_date || t.start_date;
          box.appendChild(h('button', {type: 'button', class: 'pm-row', data: {fk: 'ov' + t.id}, onclick: () => PM.openTask(t.id)},
            t.is_milestone ? glyph('◆', 'Milestone', 'pm-glyph-ms') : null,
            h('span', {class: 'pm-row-main'}, h('span', {class: 'pm-row-title', text: t.title}),
              t.assignee_id != null ? h('span', {class: 'pm-row-sub', style: 'display:block', text: PM.userName(t.assignee_id)}) : null),
            t.conflict ? glyph('⚠', 'Starts before a task it depends on finishes', 'pm-glyph-warn') : null,
            date ? h('span', {class: 'pm-row-date' + (isOverdue(t) ? ' is-over' : ''), text: PM.fmtDate(date)}) : null));
        });
        if (list.length > max) {
          box.appendChild(h('button', {type: 'button', class: 'pm-row', data: {fk: 'ovmore'}, onclick: () => PM.setTab('tasks')},
            h('span', {class: 'pm-row-main pm-note', text: 'and ' + (list.length - max) + ' more — open Tasks'})));
        }
        return box;
      }

      // Summary / Risks: autosaved text. The <textarea> is created once and
      // NEVER rebuilt; a poll only writes into it while it is neither
      // focused nor holding unsaved text (rule (a)).
      function noteBox(key, label, hint) {
        const n = notes[key] = {dirty: false, timer: null, ta: null, view: null,
                                status: h('span', {class: 'pm-autosave', role: 'status'})};
        n.ta = h('textarea', {class: 'field-input field-textarea', rows: 5, maxLength: 10000, placeholder: hint, 'aria-label': label});
        n.view = h('p', {class: 'pm-desc'});
        const save = () => {
          clearTimeout(n.timer);
          if (!n.dirty || !PM.can.edit) return;
          const sent = n.ta.value;
          n.status.textContent = 'Saving…';
          const body = {};
          body[key] = sent;
          PM.api('PUT', '', body).then(resp => {
            n.dirty = n.ta.value !== sent;          // typed more while it was saving
            n.status.textContent = n.dirty ? 'Unsaved changes' : 'Saved';
            PM.applyWrite(resp);
            if (n.dirty) n.timer = setTimeout(save, 1500);
          }, e => { n.status.textContent = 'Not saved — ' + e.message; });
        };
        n.ta.addEventListener('input', () => {
          n.dirty = true;
          n.status.textContent = 'Unsaved changes';
          clearTimeout(n.timer);
          n.timer = setTimeout(save, 1500);
        });
        n.ta.addEventListener('blur', () => { if (n.dirty) save(); else sync(); });
        n.save = save;
        return card(label, h('div', {class: 'form-section-body'}, n.ta, n.view), n.status);
      }
      function syncNotes() {
        Object.keys(notes).forEach(key => {
          const n = notes[key], val = PM.state.project[key] || '';
          n.ta.hidden = !PM.can.edit;
          n.view.hidden = PM.can.edit;
          if (PM.can.edit) {
            if (!n.dirty && document.activeElement !== n.ta && n.ta.value !== val) n.ta.value = val;
          } else {
            clear(n.view);
            if (val) n.view.appendChild(PM.richText(val));
            else n.view.appendChild(h('span', {class: 'pm-note', text: 'Nothing written yet.'}));
          }
        });
      }

      function mount(el) {
        pane = el;
        c = {desc: h('div', {class: 'form-section-body'}), progress: h('div', {class: 'form-section-body'}),
             overdue: h('div'), week: h('div'), miles: h('div'), shows: h('div'), showActs: h('span', {class: 'pm-h-actions'}),
             activity: h('div'), overdueN: h('span'), descCard: null};
        c.descCard = card('About', c.desc);
        pane.append(c.descCard, h('div', {class: 'pm-cols'},
          h('div', null,
            card('Progress', c.progress),
            card('Overdue', c.overdue),
            card('Due in the next 7 days', c.week),
            card('Upcoming milestones', c.miles)),
          h('div', null,
            card('Linked shows', c.shows, c.showActs),
            noteBox('summary_text', 'Summary', 'Where the project stands — this text opens the Summary PDF.'),
            noteBox('risks_text', 'Risks / issues', 'What could go wrong, what is blocked, what needs a decision.'),
            card('Recent activity', c.activity))));
      }

      function drawActivity() {
        const items = PM.state.activity || [];
        clear(c.activity).appendChild(items.length ? activityList(items.slice(0, 12))
          : h('p', {class: 'pm-empty', text: 'Nothing has happened yet.'}));
      }
      function refreshActivity() {
        activityStale = false;
        PM.api('GET', '/activity').then(j => {
          PM.state.activity = (j.items || []).slice(0, 30);
          if (PM.activeTab === 'overview') drawActivity();
        }, () => { activityStale = true; });
      }

      function sync() {
        if (!pane || PM.activeTab !== 'overview') return;
        const s = PM.state, tasks = s.tasks, today = todayIso();
        c.descCard.hidden = !s.project.description;
        clear(c.desc).appendChild(h('p', {class: 'pm-desc'}, PM.richText(s.project.description)));

        // Progress: numbers AND words (never colour alone)
        const n = {todo: 0, doing: 0, blocked: 0, done: 0};
        tasks.forEach(t => { n[t.status] = (n[t.status] || 0) + 1; });
        const total = tasks.length;
        const over = tasks.filter(isOverdue), conf = tasks.filter(t => t.conflict);
        clear(c.progress);
        if (!total) {
          c.progress.appendChild(h('p', {class: 'pm-empty', style: 'padding:8px 0'}, 'No tasks yet.',
            PM.can.edit ? h('div', null, h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: 'Add the first task', data: {fk: 'first'},
                                                       onclick: () => PM.newTask()})) : null));
        } else {
          const bar = h('div', {class: 'pm-bar', role: 'img',
                                'aria-label': n.done + ' of ' + total + ' tasks done'});
          ['done', 'doing', 'blocked', 'todo'].forEach(k => {
            if (!n[k]) return;
            const seg = h('span', {class: PM.STATUS[k].cls, title: PM.STATUS[k].label + ': ' + n[k]});
            seg.style.width = (100 * n[k] / total) + '%';
            bar.appendChild(seg);
          });
          c.progress.append(
            h('div', {class: 'pm-progress-top'}, h('span', {class: 'big', text: n.done + '/' + total}),
              h('span', {class: 'of', text: 'tasks done · ' + Math.round(100 * n.done / total) + '%'})),
            bar,
            h('div', {class: 'pm-stats'}, STATUS_KEYS.map(k => h('span', null, h('strong', {text: String(n[k])}), PM.STATUS[k].label)),
              over.length ? h('span', {class: 'pm-glyph-warn'}, h('strong', {text: String(over.length)}), 'overdue') : null,
              conf.length ? h('span', {class: 'pm-glyph-warn'}, h('strong', {text: String(conf.length)}), conf.length === 1 ? 'schedule conflict' : 'schedule conflicts') : null));
        }
        const byDue = (a, b) => (a.due_date || a.start_date || '').localeCompare(b.due_date || b.start_date || '') || bySort(a, b);
        keepFocus(pane, () => {
          clear(c.overdue).appendChild(taskRows(over.slice().sort(byDue), 8, 'Nothing is overdue.'));
          const end = PM.addDays(today, 7);
          clear(c.week).appendChild(taskRows(tasks.filter(t => t.status !== 'done' && t.due_date && t.due_date >= today && t.due_date <= end)
            .sort(byDue), 8, 'Nothing is due in the next 7 days.'));
          clear(c.miles).appendChild(taskRows(tasks.filter(t => t.is_milestone && t.status !== 'done' && (t.due_date || t.start_date || '') >= today)
            .sort(byDue), 6, 'No upcoming milestones — tick “Milestone” on a task to mark a key date.'));

          // Linked shows
          clear(c.showActs);
          if (PM.can.edit) {
            c.showActs.appendChild(h('button', {type: 'button', class: 'btn btn-xs btn-ghost', text: 'Link a show', data: {fk: 'link'}, onclick: () => PM.linkShow()}));
            if (PM.can.createShow) c.showActs.appendChild(h('button', {type: 'button', class: 'btn btn-xs btn-ghost', text: 'New show', data: {fk: 'newshow'}, onclick: () => PM.newShow()}));
          }
          const sl = h('div', {class: 'pm-list'});
          (s.shows || []).forEach(sh => {
            sl.appendChild(h('div', {class: 'pm-row'},
              h('span', {class: 'pm-row-main'},
                h('a', {class: 'pm-row-title', href: '/shows/' + sh.id, target: '_blank', rel: 'noopener', text: sh.name, data: {fk: 'sh' + sh.id}}),
                h('span', {class: 'pm-row-sub', style: 'display:block',
                           text: [sh.venue, fmtRange(sh.start, sh.end) || 'No date yet'].filter(Boolean).join(' · ')})),
              sh.show_mode === 'event' ? h('span', {class: 'pm-badge pm-badge-event', text: 'Event'}) : null,
              sh.is_past ? h('span', {class: 'pm-badge pm-badge-muted', text: 'Past'}) : null,
              sh.status !== 'active' ? h('span', {class: 'pm-badge pm-badge-muted', text: 'Archived'}) : null,
              sh.is_test ? h('span', {class: 'pm-badge pm-badge-muted', text: 'Test'}) : null,
              PM.can.edit ? h('button', {type: 'button', class: 'pm-chip-x', title: 'Unlink this show', 'aria-label': 'Unlink ' + sh.name,
                                         text: '×', data: {fk: 'un' + sh.id}, onclick: () => unlink(sh)}) : null));
          });
          if (!(s.shows || []).length) {
            sl.appendChild(h('p', {class: 'pm-empty', text: 'No shows linked.' + (PM.can.edit ? ' Link one that exists, or create a new one.' : '')}));
          }
          clear(c.shows).appendChild(sl);
        });
        syncNotes();
        drawActivity();
        if (activityStale) refreshActivity();
      }
      function unlink(sh) {
        if (!PM.can.edit) return;
        PM.confirm('Unlink “' + sh.name + '” from this project? The show itself is not changed.', 'Unlink').then(ok => {
          if (!ok) return;
          PM.api('DELETE', '/shows/' + sh.id).then(resp => {
            PM.state.shows = PM.state.shows.filter(x => x.id !== sh.id);
            PM.applyWrite(resp);
            PM.toast('✓ Show unlinked');
          }, say);
        });
      }
      PM.on('state', sync);
      return {mount, show: sync, hide() { Object.keys(notes).forEach(k => { if (notes[k].dirty) notes[k].save(); }); }};
    })());

    /* ═══ Tab: Tasks (list + board) ════════════════════════════════════════ */
    PM.registerTab('tasks', (function () {
      let pane = null, body = null, quick = null, bar = null;
      let view = 'list', sort = 'manual', pending = false, drag = null, noClickUntil = 0;
      const f = {mine: false, overdue: false, conflicts: false, status: '', assignee: ''};
      try { if (window.localStorage.getItem('pm.tasks.view') === 'board') view = 'board'; } catch (e) { /* private mode */ }

      const filtered = () => !!(f.mine || f.overdue || f.conflicts || f.status || f.assignee);
      function matches(t) {
        if (f.mine && t.assignee_id !== PM.me.id) return false;
        if (f.overdue && !isOverdue(t)) return false;
        if (f.conflicts && !t.conflict) return false;
        if (f.status && t.status !== f.status) return false;
        if (f.assignee === 'none' ? t.assignee_id != null : (f.assignee && String(t.assignee_id) !== f.assignee)) return false;
        return true;
      }
      const SORTS = {
        manual: null,
        due: (a, b) => (a.due_date || '9999').localeCompare(b.due_date || '9999') || bySort(a, b),
        start: (a, b) => (a.start_date || a.due_date || '9999').localeCompare(b.start_date || b.due_date || '9999') || bySort(a, b),
        priority: (a, b) => (b.priority - a.priority) || bySort(a, b),
        title: (a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()) || bySort(a, b),
        status: (a, b) => (STATUS_KEYS.indexOf(a.status) - STATUS_KEYS.indexOf(b.status)) || bySort(a, b),
        assignee: (a, b) => PM.userName(a.assignee_id).toLowerCase().localeCompare(PM.userName(b.assignee_id).toLowerCase()) || bySort(a, b),
      };

      /* toolbar (built once; only its counts and option lists are refreshed) */
      let tg = {}, selStatus, selWho, selSort, segList, segBoard, newBtn, toolsWrap;
      function toggle(key, label) {
        const b = h('button', {type: 'button', class: 'pm-toggle', 'aria-pressed': 'false'}, label, ' ', h('span', {class: 'pm-count'}));
        b.addEventListener('click', () => { f[key] = !f[key]; b.setAttribute('aria-pressed', f[key] ? 'true' : 'false'); draw(); });
        tg[key] = b;
        return b;
      }
      function setView(v) {
        view = v;
        try { window.localStorage.setItem('pm.tasks.view', v); } catch (e) { /* private mode */ }
        draw();
      }
      function mount(el) {
        pane = el;
        segList = h('button', {type: 'button', class: 'pm-seg-btn', text: 'List', onclick: () => setView('list')});
        segBoard = h('button', {type: 'button', class: 'pm-seg-btn', text: 'Board', onclick: () => setView('board')});
        selStatus = select([{value: '', label: 'Any status'}].concat(STATUS_KEYS.map(k => ({value: k, label: PM.STATUS[k].label}))), '',
                           {'aria-label': 'Filter by status'});
        selStatus.addEventListener('change', () => { f.status = selStatus.value; draw(); });
        selWho = select([], '', {'aria-label': 'Filter by assignee'});
        selWho.addEventListener('change', () => { f.assignee = selWho.value; draw(); });
        selSort = select([{value: 'manual', label: 'Sort: my order'}, {value: 'due', label: 'Sort: due date'},
                          {value: 'start', label: 'Sort: start date'}, {value: 'priority', label: 'Sort: priority'},
                          {value: 'status', label: 'Sort: status'}, {value: 'assignee', label: 'Sort: assignee'},
                          {value: 'title', label: 'Sort: title'}], 'manual', {'aria-label': 'Sort the list'});
        selSort.addEventListener('change', () => { sort = selSort.value; draw(); });
        newBtn = h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: '+ New task', onclick: () => PM.newTask()});
        toolsWrap = menu('pm-tasks', {label: 'Tools', title: 'Task tools'});
        bar = h('div', {class: 'pm-toolbar'},
          h('div', {class: 'pm-seg', role: 'group', 'aria-label': 'View'}, segList, segBoard),
          toggle('mine', 'Mine'), toggle('overdue', 'Overdue'), toggle('conflicts', 'Conflicts'),
          selStatus, selWho, selSort,
          h('div', {class: 'pm-toolbar-right'}, toolsWrap, newBtn));
        body = h('div');
        // Quick add: its own node, never rebuilt, so it keeps focus and text
        // through every redraw.
        const qi = h('input', {type: 'text', class: 'field-input', maxLength: 200, placeholder: 'Add a task and press Enter…',
                               'aria-label': 'Add a task', autocomplete: 'off', enterkeyhint: 'done'});
        const qb = h('button', {type: 'button', class: 'btn btn-ghost btn-sm', text: 'Add'});
        const addQuick = () => {
          const t = qi.value.trim();
          if (!t || !PM.can.edit || qb.disabled) return;
          const bodyJson = {title: t};
          if (f.status) bodyJson.status = f.status;
          busy(qb, PM.api('POST', '/tasks', bodyJson)).then(resp => { qi.value = ''; PM.applyWrite(resp); qi.focus(); }, say);
        };
        qi.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addQuick(); } });
        qb.addEventListener('click', addQuick);
        quick = h('div', {class: 'form-section'}, h('div', {class: 'pm-quick', style: 'border-top:0'}, qi, qb));
        pane.append(bar, body, quick);
        // A redraw that waited for the status menu to close (rule (a)).
        body.addEventListener('focusout', () => { if (pending) setTimeout(() => { if (pending) draw(); }, 0); });
        body.addEventListener('click', onClick);
        body.addEventListener('change', onChange);
        body.addEventListener('pointerdown', onDown);
      }

      function onClick(e) {
        if (performance.now() < noClickUntil) { e.preventDefault(); return; }   // the click that ends a drag
        if (e.target.closest('.tools-menu, a, select')) return;
        const warn = e.target.closest('[data-shift]');
        if (warn) { PM.reviewShift(+warn.dataset.shift); return; }
        const row = e.target.closest('[data-open]');
        if (row) PM.openTask(+row.dataset.open);
      }
      function onChange(e) {
        const s = e.target.closest('select.pm-status');
        if (!s) return;
        const id = +s.dataset.id, t = PM.taskById(id);
        s.blur();                       // let the redraw that follows go ahead
        if (!t || !canSetStatus(t)) { draw(); return; }
        PM.saveTask(id, {status: s.value}).catch(() => {});
      }

      function statusSelect(t) {
        const s = h('select', {class: 'pm-status ' + (PM.STATUS[t.status] || PM.STATUS.todo).cls, 'aria-label': 'Status of ' + t.title,
                               disabled: !canSetStatus(t), data: {id: t.id, fk: 'st' + t.id}},
                    STATUS_KEYS.map(k => h('option', {value: k, text: PM.STATUS[k].label})));
        s.value = t.status;
        return s;
      }
      function marks(t) {
        const out = [];
        if (t.is_milestone) out.push(glyph('◆', 'Milestone', 'pm-glyph-ms'));
        if (isOverdue(t)) out.push(h('span', {class: 'pm-badge pm-badge-late', text: 'Overdue'}));
        if (t.conflict) {
          const label = 'Schedule conflict: starts before a task it depends on finishes';
          out.push(PM.can.edit
            ? h('button', {type: 'button', class: 'pm-link-btn pm-glyph pm-glyph-warn', title: label + ' — review the shift', 'aria-label': label + ' — review the shift',
                           data: {shift: t.id, fk: 'w' + t.id}, text: '⚠ conflict'})
            : h('span', {class: 'pm-glyph pm-glyph-warn', title: label, text: '⚠ conflict'}));
        }
        if (t.comment_count) out.push(glyph('💬 ' + t.comment_count, plural(t.comment_count, 'comment')));
        if (t.show_id) {
          const s = PM.state.shows.find(x => x.id === t.show_id);
          out.push(s ? h('a', {class: 'pm-chip', href: '/shows/' + s.id, target: '_blank', rel: 'noopener', title: 'Open show (new tab)', data: {fk: 'sc' + t.id}},
                        h('span', {text: s.name}), h('span', {'aria-hidden': 'true', text: '↗'}))
                     : h('span', {class: 'pm-chip'}, h('span', {text: 'Show'})));
        }
        return out;
      }
      const datesOf = t => (t.start_date || t.due_date) ? fmtRange(t.start_date, t.due_date) : '';

      function drawList() {
        const all = PM.state.tasks;
        clear(body);
        if (!all.length) {
          body.appendChild(h('div', {class: 'form-section'}, h('p', {class: 'pm-empty'}, 'No tasks yet.',
            h('span', {class: 'pm-empty-sub', text: PM.can.edit ? 'Type a title below and press Enter, or use New task for dates and people.'
                                                                 : 'Tasks added by the project’s editors appear here.'}))));
          return;
        }
        const cmp = SORTS[sort];
        const groups = PM.taskGroups().map(g => ({name: g.name, tasks: g.tasks.filter(matches)})).filter(g => g.tasks.length);
        if (!groups.length) {
          body.appendChild(h('div', {class: 'form-section'}, h('p', {class: 'pm-empty'}, 'No tasks match these filters.',
            h('div', null, h('button', {type: 'button', class: 'btn btn-ghost btn-sm', text: 'Clear filters', onclick: clearFilters, data: {fk: 'clear'}})))));
          return;
        }
        const cardEl = h('div', {class: 'form-section'});
        const multi = groups.length > 1 || groups[0].name;
        groups.forEach(g => {
          const sec = h('div', {class: 'pm-tgroup', role: 'group', 'aria-label': g.name || 'No group'});
          if (multi) sec.appendChild(h('div', {class: 'pm-tgroup-head'}, h('span', {text: g.name || 'No group'}), h('span', {class: 'pm-count', text: String(g.tasks.length)})));
          (cmp ? g.tasks.slice().sort(cmp) : g.tasks).forEach(t => {
            const who = t.assignee_id != null ? PM.userName(t.assignee_id) : '';
            const pr = PM.PRIORITY[t.priority] || PM.PRIORITY[1];
            sec.appendChild(h('div', {class: 'pm-trow' + (t.status === 'done' ? ' is-done' : ''), data: {id: t.id}},
              h('div', {class: 'pm-t-status'}, statusSelect(t)),
              h('div', {class: 'pm-t-main'}, h('button', {type: 'button', class: 'pm-t-title', data: {open: t.id, fk: 'tt' + t.id}, text: t.title}), marks(t)),
              h('div', {class: 'pm-t-who', title: who, text: who}),
              h('div', {class: 'pm-t-dates' + (isOverdue(t) ? ' is-over' : ''), title: datesOf(t), text: datesOf(t)}),
              h('div', {class: 'pm-pri ' + pr.cls, text: t.priority === 1 ? '' : pr.label}),
              h('div', {class: 'pm-t-menu'}, PM.can.edit ? menu('pm-task-row', {label: '⋯', title: 'Task actions', caret: false, data: {id: t.id, group: g.name}}) : null)));
          });
          cardEl.appendChild(sec);
        });
        body.appendChild(cardEl);
      }

      function drawBoard() {
        clear(body);
        const board = h('div', {class: 'pm-board'});
        const shown = PM.state.tasks.filter(matches);
        STATUS_KEYS.forEach(k => {
          const mine = shown.filter(t => t.status === k);
          const colBody = h('div', {class: 'pm-col-body'});
          mine.forEach(t => {
            const pr = PM.PRIORITY[t.priority] || PM.PRIORITY[1];
            const who = t.assignee_id != null ? PM.userName(t.assignee_id) : '';
            const movable = canSetStatus(t);
            colBody.appendChild(h('div', {class: 'pm-tcard', data: {id: t.id, open: t.id}},
              h('div', {class: 'pm-tcard-top'},
                movable ? h('span', {class: 'pm-grip', title: 'Drag to move (or use the ⋯ menu)', 'aria-hidden': 'true', text: '⠿'}) : null,
                h('button', {type: 'button', class: 'pm-t-title pm-tcard-title', data: {open: t.id, fk: 'bt' + t.id}, text: t.title}),
                movable ? menu('pm-card', {label: '⋯', title: 'Move or open this task', caret: false, data: {id: t.id}}) : null),
              h('div', {class: 'pm-tcard-foot'},
                t.priority !== 1 ? h('span', {class: 'pm-pri ' + pr.cls, text: pr.label}) : null,
                datesOf(t) ? h('span', {class: 'mono', title: datesOf(t), text: PM.fmtDate(t.due_date || t.start_date)}) : null,
                marks(t), who ? avatar(who, true) : null)));
          });
          if (!mine.length) colBody.appendChild(h('p', {class: 'pm-col-empty', text: filtered() ? 'Nothing matches' : 'Nothing here'}));
          const col = h('section', {class: 'pm-col ' + PM.STATUS[k].cls, 'aria-label': PM.STATUS[k].label, data: {status: k}},
            h('div', {class: 'pm-col-head'}, h('span', {class: 'pm-pill ' + PM.STATUS[k].cls, text: PM.STATUS[k].label}),
              h('span', {class: 'pm-count', text: String(mine.length)})), colBody);
          board.appendChild(col);
        });
        body.appendChild(board);
      }

      function clearFilters() {
        f.mine = f.overdue = f.conflicts = false; f.status = f.assignee = '';
        selStatus.value = ''; selWho.value = '';
        Object.keys(tg).forEach(k => tg[k].setAttribute('aria-pressed', 'false'));
        draw();
      }
      function draw() {
        if (!pane || PM.activeTab !== 'tasks') return;
        // Rules (a) and (c): not under an open status menu, not mid-drag.
        if ((drag && drag.on) || (editingIn(body) && document.activeElement.tagName === 'SELECT')) { pending = true; return; }
        pending = false;
        const all = PM.state.tasks;
        tg.mine.lastChild.textContent = String(all.filter(t => t.assignee_id === PM.me.id && t.status !== 'done').length);
        tg.overdue.lastChild.textContent = String(all.filter(isOverdue).length);
        tg.conflicts.lastChild.textContent = String(all.filter(t => t.conflict).length);
        // The assignee filter lists current members (+ "nobody"); keep the choice.
        if (document.activeElement !== selWho) {
          const keep = f.assignee;
          clear(selWho);
          [{value: '', label: 'Anyone'}, {value: 'none', label: 'Unassigned'}].concat(PM.state.members.map(m => ({value: m.id, label: m.name})))
            .forEach(o => selWho.appendChild(h('option', {value: String(o.value), text: o.label})));
          selWho.value = keep;
          if (selWho.value !== keep) { f.assignee = ''; selWho.value = ''; }
        }
        segList.setAttribute('aria-pressed', view === 'list' ? 'true' : 'false');
        segBoard.setAttribute('aria-pressed', view === 'board' ? 'true' : 'false');
        selSort.hidden = view !== 'list';
        newBtn.hidden = toolsWrap.hidden = quick.hidden = !PM.can.edit;
        keepFocus(body, view === 'list' ? drawList : drawBoard);
      }

      /* reorder helpers (POST …/tasks/reorder takes ONE group / column: a
         stale tab can then never undo someone's order elsewhere) */
      function reorder(ids) {
        return PM.api('POST', '/tasks/reorder', {ids}).then(resp => PM.applyWrite(resp), e => { say(e); PM.emit('state'); });
      }
      function nudge(ids, id, step) {
        const i = ids.indexOf(id), j = i + step;
        if (i < 0 || j < 0 || j >= ids.length) return;
        ids.splice(i, 1);
        ids.splice(j, 0, id);
        reorder(ids);
      }
      function deleteTask(id) {
        const t = PM.taskById(id);
        if (!t || !PM.can.edit) return;
        PM.confirm('Delete “' + t.title + '”? It can be restored from Tools → Deleted tasks.', 'Delete').then(ok => {
          if (!ok) return;
          PM.api('DELETE', '/tasks/' + id).then(resp => { dropTask(id); PM.applyWrite(resp); PM.toast('✓ Task deleted'); }, say);
        });
      }
      regMenu('pm-task-row', wrap => {
        const id = +wrap.dataset.id, t = PM.taskById(id);
        if (!t || !PM.can.edit) return [];
        const g = PM.taskGroups().find(x => x.name === (t.group_name || ''));
        const ids = g ? g.tasks.filter(matches).map(x => x.id) : [];
        const i = ids.indexOf(id);
        const canOrder = sort === 'manual';
        return [
          {label: 'Open', onSelect: () => PM.openTask(id)},
          {separator: true},
          {label: 'Move up', disabled: !canOrder || i <= 0, title: canOrder ? '' : 'Switch the sort to “my order” first', onSelect: () => nudge(ids, id, -1)},
          {label: 'Move down', disabled: !canOrder || i < 0 || i >= ids.length - 1, title: canOrder ? '' : 'Switch the sort to “my order” first', onSelect: () => nudge(ids, id, 1)},
          {separator: true},
          {label: 'Delete…', danger: true, onSelect: () => deleteTask(id)},
        ];
      });
      regMenu('pm-card', wrap => {
        const id = +wrap.dataset.id, t = PM.taskById(id);
        if (!t || !canSetStatus(t)) return [];
        const ids = PM.state.tasks.filter(x => matches(x) && x.status === t.status).map(x => x.id);
        const i = ids.indexOf(id);
        const items = [
          {label: 'Open', onSelect: () => PM.openTask(id)},
          {label: 'Move to', submenu: STATUS_KEYS.filter(k => k !== t.status).map(k =>
            ({label: PM.STATUS[k].label, onSelect: () => { PM.saveTask(id, {status: k}).catch(() => {}); }}))},
        ];
        if (PM.can.edit) {
          items.push({label: 'Move up', disabled: i <= 0, onSelect: () => nudge(ids, id, -1)},
                     {label: 'Move down', disabled: i < 0 || i >= ids.length - 1, onSelect: () => nudge(ids, id, 1)});
        }
        return items;
      });
      regMenu('pm-tasks', () => PM.can.edit ? [{label: 'Deleted tasks…', onSelect: deletedDialog}] : []);

      function deletedDialog() {
        const d = PM.dialog({title: 'Deleted tasks', wide: true, noDirtyGuard: true});
        const box = h('div', {class: 'pm-list', style: 'margin:-22px'});
        d.body.appendChild(box);
        d.footer.appendChild(h('button', {type: 'button', class: 'btn btn-primary', text: 'Close', onclick: () => d.close()}));
        const load = () => PM.api('GET', '/tasks/deleted').then(j => {
          clear(box);
          if (!(j.tasks || []).length) { box.appendChild(h('p', {class: 'pm-empty', text: 'No deleted tasks.'})); return; }
          j.tasks.forEach(t => {
            box.appendChild(h('div', {class: 'pm-row'},
              h('span', {class: 'pm-row-main'}, h('span', {class: 'pm-row-title', text: t.title}),
                h('span', {class: 'pm-row-sub', style: 'display:block', text: 'Deleted ' + fmtWhen(t.deleted_at) + (t.deleted_by ? ' by ' + t.deleted_by : '')})),
              h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Restore', onclick: e => {
                busy(e.currentTarget, PM.api('POST', '/tasks/' + t.id + '/restore')).then(resp => {
                  PM.applyWrite(resp);
                  if (resp.new_conflicts && resp.new_conflicts.length) conflictNotice(t.id, resp.new_conflicts.length);
                  else PM.toast('✓ Task restored');
                  load();
                }, say);
              }})));
          });
        }, e => { clear(box).appendChild(h('p', {class: 'pm-empty', text: e.message})); });
        box.appendChild(h('p', {class: 'pm-empty', text: 'Loading…'}));
        load();
      }

      /* ── Board drag (Pointer Events — HTML5 drag never fires on iPhones).
         Mouse: drag the card. Touch / pen: drag its ⠿ grip only (the only
         element with touch-action:none, so the page still scrolls). The ⋯
         menu's "Move to ▸ / Move up / Move down" is the non-drag path. ── */
      function onDown(e) {
        if (drag || view !== 'board' || (e.pointerType === 'mouse' && e.button !== 0)) return;
        const cardEl = e.target.closest('.pm-tcard');
        if (!cardEl) return;
        const t = PM.taskById(cardEl.dataset.id);
        if (!t || !canSetStatus(t)) return;
        const grip = e.target.closest('.pm-grip');
        if (e.target.closest('.tools-menu, a, select')) return;
        if (!grip && e.pointerType !== 'mouse') return;
        drag = {id: t.id, card: cardEl, x0: e.clientX, y0: e.clientY, pointer: e.pointerId, on: false,
                ghost: null, line: h('div', {class: 'pm-dropline'}), col: null, before: null, dx: 0, dy: 0};
        document.addEventListener('pointermove', onMove, {passive: false});
        document.addEventListener('pointerup', onUp);
        document.addEventListener('pointercancel', onCancel);
        document.addEventListener('keydown', onKey, true);
        if (grip) e.preventDefault();
      }
      function onMove(e) {
        if (!drag || e.pointerId !== drag.pointer) return;
        if (!drag.on) {
          if (Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < 6) return;
          const r = drag.card.getBoundingClientRect();
          drag.dx = drag.x0 - r.left; drag.dy = drag.y0 - r.top;
          const g = drag.card.cloneNode(true);
          g.classList.add('pm-ghost');
          g.removeAttribute('data-open');
          g.querySelectorAll('[data-fk]').forEach(n => n.removeAttribute('data-fk'));
          g.style.width = r.width + 'px';
          document.body.appendChild(g);
          drag.ghost = g;
          drag.card.classList.add('is-dragging');
          drag.on = true;
          document.documentElement.classList.add('pm-drag-on');
          if (window.ToolsMenu) window.ToolsMenu.close(false);
          if (window.getSelection) { try { window.getSelection().removeAllRanges(); } catch (e2) { /* nothing selected */ } }
        }
        e.preventDefault();
        drag.ghost.style.left = (e.clientX - drag.dx) + 'px';
        drag.ghost.style.top = (e.clientY - drag.dy) + 'px';
        const board = body.querySelector('.pm-board');
        if (!board) return;
        const br = board.getBoundingClientRect();
        if (e.clientX > br.right - 36) board.scrollLeft += 16;          // swipe the columns along on a phone
        else if (e.clientX < br.left + 36) board.scrollLeft -= 16;
        // …and the page, so a card can travel up or down a long column.
        if (e.clientY > window.innerHeight - 90) window.scrollBy(0, 16);
        else if (e.clientY < 130) window.scrollBy(0, -16);
        const under = document.elementFromPoint(e.clientX, e.clientY);
        const col = (under && under.closest) ? under.closest('.pm-col') : null;
        body.querySelectorAll('.pm-col').forEach(cn => cn.classList.toggle('is-over', cn === col));
        if (!col || !body.contains(col)) { drag.col = null; drag.line.remove(); return; }
        const colBody = col.querySelector('.pm-col-body');
        let before = null;
        Array.from(colBody.querySelectorAll('.pm-tcard')).some(cn => {
          if (cn === drag.card) return false;
          const r = cn.getBoundingClientRect();
          if (e.clientY < r.top + r.height / 2) { before = cn; return true; }
          return false;
        });
        drag.col = col.dataset.status;
        drag.before = before ? +before.dataset.id : null;
        if (before) colBody.insertBefore(drag.line, before); else colBody.appendChild(drag.line);
      }
      function endDrag(commit) {
        const dg = drag;
        drag = null;
        document.removeEventListener('pointermove', onMove);
        document.removeEventListener('pointerup', onUp);
        document.removeEventListener('pointercancel', onCancel);
        document.removeEventListener('keydown', onKey, true);
        if (!dg || !dg.on) return;                 // it was a plain click
        noClickUntil = performance.now() + 350;
        if (dg.ghost) dg.ghost.remove();
        dg.line.remove();
        dg.card.classList.remove('is-dragging');
        document.documentElement.classList.remove('pm-drag-on');
        body.querySelectorAll('.pm-col.is-over').forEach(cn => cn.classList.remove('is-over'));
        if (commit && dg.col) moveCard(dg.id, dg.col, dg.before);
        else if (pending) draw();
      }
      function onUp(e) { if (drag && e.pointerId === drag.pointer) endDrag(true); }
      function onCancel(e) { if (drag && e.pointerId === drag.pointer) endDrag(false); }
      function onKey(e) { if (e.key === 'Escape' && drag && drag.on) { e.preventDefault(); e.stopPropagation(); endDrag(false); } }

      async function moveCard(id, status, beforeId) {
        const t = PM.taskById(id);
        if (!t || !canSetStatus(t) || STATUS_KEYS.indexOf(status) < 0) { draw(); return; }
        const ids = PM.state.tasks.filter(x => matches(x) && x.status === status && x.id !== id).map(x => x.id);
        const i = beforeId == null ? -1 : ids.indexOf(beforeId);
        ids.splice(i < 0 ? ids.length : i, 0, id);
        try {
          if (t.status !== status) await PM.saveTask(id, {status});       // toasts + snaps back on failure
        } catch (e) { return; }
        // Order is an editor's to set; an assignee who only moved their own
        // card across keeps the order it lands in.
        if (PM.can.edit && ids.length > 1) {
          const now = ids.slice().sort((a, b) => bySort(PM.taskById(a), PM.taskById(b)));
          if (now.join() !== ids.join()) { await reorder(ids); return; }
        }
        draw();
      }

      PM.on('state', draw);
      return {
        mount,
        show(params) {
          draw();
          if (params && params.task) PM.openTask(params.task, {comment: params.comment});
        },
        hide() { if (drag) endDrag(false); },
      };
    })());

    /* ═══ Tab: Contacts ════════════════════════════════════════════════════ */
    PM.registerTab('contacts', (function () {
      let pane = null, grid = null, q = null, acts = null, target = null;
      function mount(el) {
        pane = el;
        q = h('input', {type: 'search', class: 'field-input', placeholder: 'Filter contacts…', 'aria-label': 'Filter contacts', autocomplete: 'off'});
        q.style.width = '240px';
        q.addEventListener('input', draw);
        acts = h('div', {class: 'pm-toolbar-right'},
          h('button', {type: 'button', class: 'btn btn-ghost btn-sm', text: 'From the app’s contacts…', onclick: pickAppContact}),
          h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: '+ New contact', onclick: () => PM.newContact()}));
        grid = h('div');
        pane.append(h('div', {class: 'pm-toolbar'}, q, acts), grid);
      }
      function draw() {
        if (!pane || PM.activeTab !== 'contacts') return;
        acts.hidden = !PM.can.edit;
        const all = PM.state.contacts, needle = q.value.trim().toLowerCase();
        const list = needle ? all.filter(c => [c.name, c.company, c.role, c.email, c.phone, c.notes].join(' ').toLowerCase().indexOf(needle) >= 0) : all;
        keepFocus(grid, () => {
          clear(grid);
          if (!all.length) {
            grid.appendChild(h('div', {class: 'form-section'}, h('p', {class: 'pm-empty'}, 'No contacts yet.',
              PM.can.edit ? h('div', null, h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: 'Add a contact', data: {fk: 'add'}, onclick: () => PM.newContact()})) : null)));
            return;
          }
          if (!list.length) { grid.appendChild(h('p', {class: 'pm-empty', text: 'No contacts match that filter.'})); return; }
          const g = h('div', {class: 'pm-contact-grid'});
          list.forEach(c => {
            const n = PM.state.tasks.filter(t => t.contact_id === c.id).length;
            g.appendChild(h('button', {type: 'button', class: 'pm-contact' + (target === c.id ? ' is-target' : ''), data: {fk: 'c' + c.id},
                                       onclick: () => contactDialog(c)},
              h('span', {class: 'n', text: c.name}),
              (c.company || c.role) ? h('span', {class: 's', text: [c.role, c.company].filter(Boolean).join(' · ')}) : null,
              c.email ? h('span', {class: 'l', text: c.email}) : null,
              c.phone ? h('span', {class: 'l mono', text: c.phone}) : null,
              n ? h('span', {class: 's', text: plural(n, 'task')}) : null));
          });
          grid.appendChild(g);
        });
      }
      PM.on('state', draw);
      return {mount, show(params) {
        target = (params && params.contact) || null;
        draw();
        if (target) {
          const c = PM.state.contacts.find(x => x.id === target);
          if (c) contactDialog(c); else PM.toast('That item no longer exists.', true);
        }
      }};
    })());

    /* ═══ Tab: Discussion ══════════════════════════════════════════════════ */
    PM.registerTab('discussion', (function () {
      let pane = null, thread = null, couldComment = null;
      function build(focusComment) {
        thread = makeThread({taskId: null, focusComment});
        couldComment = PM.can.comment;
        clear(pane).appendChild(h('div', {class: 'form-section pm-discussion'},
          h('div', {class: 'form-section-body'}, thread.el),
          thread.composer ? h('div', {class: 'pm-composer-wrap'}, thread.composer) : null));
        thread.load();
      }
      PM.on('state', () => {
        if (!pane || PM.activeTab !== 'discussion' || !thread) return;
        // My rights changed (role, archive): rebuild for the composer —
        // unless that would throw away a comment being written.
        if (couldComment !== PM.can.comment && !thread.hasDraft()) build(null);
        else thread.sync();
      });
      return {mount(el) { pane = el; }, show(params) {
        const cid = (params && params.comment) || null;
        if (!thread || cid || (couldComment !== PM.can.comment && !thread.hasDraft())) build(cid);
        else thread.sync();
      }};
    })());

    /* ═══ Tab: Settings (the owner) ════════════════════════════════════════ */
    PM.registerTab('settings', (function () {
      let pane = null, built = false, people = [], peopleSeq = 0;
      let f = null, base0 = null, membersBox = null, addBox = null, lockNote = null, saveBtn = null, vis = null, vcc = null, col = null;

      const snap = p => ({name: p.name, description: p.description || '', status: p.status, color: p.color || '',
                          start_date: p.start_date || '', end_date: p.end_date || ''});
      const get = () => ({name: f.name.value.trim(), description: f.description.value.trim(), status: f.status.value,
                          color: col.get(), start_date: f.start_date.value, end_date: f.end_date.value});
      function build() {
        built = true;
        const p = PM.state.project;
        f = {name: h('input', {type: 'text', class: 'field-input', maxLength: 200, autocomplete: 'off'}),
             description: h('textarea', {class: 'field-input field-textarea', rows: 3, maxLength: 10000}),
             status: select(Object.keys(PROJECT_STATUS).map(k => ({value: k, label: PROJECT_STATUS[k] + (k === 'archived' ? ' (read only for everyone)' : '')})), p.status),
             start_date: h('input', {type: 'date', class: 'field-input'}), end_date: h('input', {type: 'date', class: 'field-input'})};
        col = swatches(p.color);
        saveBtn = h('button', {type: 'button', class: 'btn btn-primary', text: 'Save details', onclick: saveDetails});
        lockNote = h('div', {class: 'pm-banner pm-banner-warn', hidden: true},
          h('span', {text: 'This project is archived: it is read only for everyone. Reopen it to change anything.'}),
          h('button', {type: 'button', class: 'btn btn-sm btn-primary', text: 'Reopen', onclick: e => reopen(e.currentTarget)}));
        vis = select([{value: 'members', label: 'Everyone on the project'}, {value: 'editors', label: 'Owner and editors'},
                      {value: 'owner', label: 'Only the owner'}], p.budget_visibility);
        vis.addEventListener('change', () => saveSharing({budget_visibility: vis.value}));
        vcc = h('input', {type: 'checkbox'});
        vcc.addEventListener('change', () => saveSharing({viewers_can_comment: vcc.checked}));
        membersBox = h('div');
        addBox = h('div', {class: 'pm-add-member'});
        clear(pane).append(lockNote,
          h('div', {class: 'form-section'}, h('div', {class: 'form-section-header'}, h('span', {text: 'Details'})),
            h('div', {class: 'form-section-body'},
              field('Name', f.name, {required: true}), field('Description', f.description),
              h('div', {class: 'form-row cols-3'}, field('Status', f.status), field('Starts', f.start_date), field('Ends', f.end_date)),
              h('div', {class: 'field-group'}, h('span', {class: 'field-label', text: 'Colour'}), col.el),
              h('div', {class: 'form-actions', style: 'justify-content:flex-start;margin-top:0'}, saveBtn))),
          h('div', {class: 'form-section'}, h('div', {class: 'form-section-header'}, h('span', {text: 'People'})),
            membersBox, addBox),
          h('div', {class: 'form-section'}, h('div', {class: 'form-section-header'}, h('span', {text: 'Sharing'})),
            h('div', {class: 'form-section-body'},
              field('Who can see the budget', vis, {hint: 'People who can’t see it are not shown the Budget tab, its files or its history at all.'}),
              h('label', {class: 'pm-check'}, vcc, 'Viewers can comment'))),
          h('div', {class: 'form-section pm-danger'}, h('div', {class: 'form-section-header'}, h('span', {text: 'Delete project'})),
            h('div', {class: 'form-section-body'},
              h('p', {class: 'pm-note', text: 'Deletes the project with its tasks, budget, files, notes, contacts and discussion — for everyone, for good. Linked shows are not touched.'}),
              h('div', null, h('button', {type: 'button', class: 'btn btn-danger', text: 'Delete this project…', onclick: deleteProject})))));
        base0 = snap(p);
        fill(base0, false);
      }
      // Take server values into fields I have not changed (and am not in).
      function fill(v, keepMine) {
        const cur = keepMine ? get() : null;
        Object.keys(v).forEach(k => {
          if (keepMine && cur[k] !== base0[k]) return;
          if (k === 'color') col.set(v[k]);
          else if (document.activeElement !== f[k] || !keepMine) f[k].value = v[k];
        });
        base0 = v;
      }
      function saveDetails() {
        if (!PM.can.manage || PM.me.archived) return;
        const v = get(), ch = {};
        Object.keys(v).forEach(k => { if (v[k] !== base0[k]) ch[k] = (k === 'start_date' || k === 'end_date') ? (v[k] || null) : v[k]; });
        if (!v.name) { PM.toast('Give the project a name.', true); f.name.focus(); return; }
        if (!Object.keys(ch).length) { PM.toast('Nothing to save.'); return; }
        const go = () => busy(saveBtn, PM.api('PUT', '', ch)).then(resp => {
          base0 = snap(resp.project);
          PM.toast('✓ Saved');
          if ('status' in ch) return PM.reload();      // archiving changes every permission
          PM.applyWrite(resp);
        }).catch(say);
        if (ch.status === 'archived') {
          PM.confirm('Archive this project? It becomes read only for everyone until you reopen it.', 'Archive').then(ok => { if (ok) go(); });
        } else go();
      }
      function saveSharing(body) {
        if (!PM.can.manage || PM.me.archived) { sync(); return; }
        PM.api('PUT', '', body).then(resp => { PM.toast('✓ Saved'); return PM.reload(); }).catch(e => { say(e); sync(); });
      }
      function loadPeople() {
        const my = ++peopleSeq;
        PM.api('GET', '/people').then(j => { if (my === peopleSeq) { people = j.people || []; drawAdd(); } },
                                      () => { if (my === peopleSeq) { people = []; drawAdd(); } });
      }
      function memberWrite(promise, okMsg) {
        return promise.then(resp => { PM.applyWrite(resp); PM.toast(okMsg); loadPeople(); }, e => { say(e); sync(); });
      }
      function drawMembers() {
        if (editingIn(membersBox)) return;            // a role menu is open
        const locked = PM.me.archived;
        keepFocus(membersBox, () => {
          clear(membersBox);
          PM.state.members.forEach(m => {
            const row = h('div', {class: 'pm-member'}, avatar(m.name, false, !m.active),
              h('div', {class: 'who'}, h('div', {class: 'n', text: m.name + (m.id === PM.me.id ? ' (you)' : '')}),
                m.active ? null : h('div', {class: 's', text: 'no longer has access'})));
            if (m.role === 'owner') {
              row.appendChild(h('span', {class: 'pm-badge pm-badge-accent', text: 'Owner'}));
            } else {
              const sel = select([{value: 'editor', label: 'Editor — can change things'}, {value: 'viewer', label: 'Viewer — can look'}], m.role,
                                 {'aria-label': 'Role of ' + m.name, disabled: locked, data: {fk: 'role' + m.id}});
              sel.addEventListener('change', () => { sel.blur(); memberWrite(PM.api('PUT', '/members/' + m.id, {role: sel.value}), '✓ Role changed'); });
              row.appendChild(sel);
              row.appendChild(h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Make owner', disabled: locked || !m.active, data: {fk: 'own' + m.id},
                title: m.active ? 'Hand the project over to ' + m.name : 'They no longer have access',
                onclick: () => PM.confirm('Make ' + m.name + ' the owner? You become an editor and can no longer manage people or delete the project.', 'Make owner')
                  .then(ok => { if (ok) PM.api('POST', '/members/' + m.id + '/owner').then(() => { PM.toast('✓ Ownership transferred'); return PM.reload(); }).catch(say); })}));
              row.appendChild(h('button', {type: 'button', class: 'btn btn-sm btn-danger-ghost', text: 'Remove', disabled: locked, data: {fk: 'rm' + m.id},
                onclick: () => PM.confirm('Remove ' + m.name + ' from this project? Tasks assigned to them stay assigned.', 'Remove')
                  .then(ok => { if (ok) memberWrite(PM.api('DELETE', '/members/' + m.id), '✓ Removed'); })}));
            }
            membersBox.appendChild(row);
          });
        });
      }
      function drawAdd() {
        if (editingIn(addBox)) return;
        clear(addBox);
        if (PM.me.archived) { addBox.hidden = true; return; }
        addBox.hidden = false;
        if (!people.length) {
          addBox.appendChild(h('p', {class: 'pm-note', text: 'Only people an administrator has given access to Projects can be added.' +
            (PM.state.members.length > 1 ? ' Everyone who has it is already here.' : '')}));
          return;
        }
        const who = select([{value: '', label: 'Add someone…'}].concat(people.map(p => ({value: p.id, label: p.name}))), '', {'aria-label': 'Person to add'});
        const role = select([{value: 'editor', label: 'as Editor'}, {value: 'viewer', label: 'as Viewer'}], 'editor', {class: 'field-input pm-role-sel', 'aria-label': 'Their role'});
        const btn = h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: 'Add'});
        btn.addEventListener('click', () => {
          if (!who.value) { who.focus(); return; }
          who.blur(); role.blur();
          busy(btn, memberWrite(PM.api('POST', '/members', {user_id: +who.value, role: role.value}), '✓ Added — they have been notified'));
        });
        addBox.append(who, role, btn);
      }
      function deleteProject() {
        if (!PM.can.manage) return;
        confirmTyped('Delete this project', 'This deletes “' + PM.state.project.name + '” and everything in it for everyone. It cannot be undone.', 'Delete project')
          .then(ok => {
            if (!ok) return;
            PM.api('DELETE', '', {confirm: 'DELETE'}).then(() => {
              gone = true;
              stopPoll();
              window.location.href = '/projects';
            }, say);
          });
      }
      function sync() {
        if (!pane || PM.activeTab !== 'settings' || !PM.can.manage) return;
        if (!built) build();
        const p = PM.state.project, locked = PM.me.archived;
        fill(snap(p), true);
        lockNote.hidden = !locked;
        Object.keys(f).forEach(k => { f[k].disabled = locked; });
        Array.from(col.el.children).forEach(b => { b.disabled = locked; });
        saveBtn.disabled = locked;
        vis.disabled = vcc.disabled = locked;
        if (document.activeElement !== vis) vis.value = p.budget_visibility;
        vcc.checked = !!p.viewers_can_comment;
        drawMembers();
        drawAdd();
      }
      PM.on('state', sync);
      return {mount(el) { pane = el; }, show() { sync(); loadPeople(); }};
    })());

    /* ── After every state change ────────────────────────────────────────── */
    function afterState() {
      renderHeader();
      // The tab I am on may have just disappeared (Budget hidden from me, no
      // longer the owner): go somewhere that exists.
      if (started && PM.activeTab && !tabAllowed(PM.activeTab)) PM.setTab('overview');
    }
    // My own write moved the history; refresh it when Overview next shows it.
    const applyWrite0 = PM.applyWrite;
    PM.applyWrite = function (resp) {
      applyWrite0(resp);
      activityStale = true;
      if (PM.activeTab === 'overview') {
        clearTimeout(activityTimer);
        activityTimer = setTimeout(() => PM.emit('state'), 500);
      }
    };

    /* ── Start ───────────────────────────────────────────────────────────── */
    async function start() {
      const boot = $('pm-boot');
      let s;
      try {
        s = await req('GET', base());
      } catch (e) {
        if (isGone(e)) { handleGone(); return; }
        clear(boot).appendChild(h('div', {class: 'pm-empty', role: 'alert'}, e.message,
          h('div', null, h('button', {type: 'button', class: 'btn btn-primary', text: 'Retry', onclick: () => {
            clear(boot).appendChild(h('p', {class: 'pm-loading', role: 'status', text: 'Loading…'}));
            start();
          }}))));
        return;
      }
      setState(s);
      started = true;
      boot.remove();
      afterState();
      const q = new URLSearchParams(window.location.search), params = {};
      LINK_PARAMS.forEach(k => { const v = q.get(k); if (v && /^\d{1,9}$/.test(v)) params[k] = +v; });
      PM.setTab(q.get('tab') || 'overview', params);
      PM.emit('state');
      readyResolve();
      startPoll();
    }
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
    else start();
  }

  if (PAGE === 'index') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initIndex);
    else initIndex();
  } else if (PAGE === 'project') {
    initProject();
  }
})();
