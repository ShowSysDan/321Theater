/* ═══════════════════════════════════════════════════════════════════════════
   Projects module — the Timeline tab (a date-scale Gantt).
   Served ONLY by the gated route /projects/assets/projects_gantt.js.
   Built against window.PM exactly as documented at the top of projects.js;
   it registers ONE tab: PM.registerTab('timeline', …).

   What is drawn
     ONE scroll box (.pm-g-scroll): a sticky date axis on top, a sticky name
     column on the left. Rows: PM.taskGroups() (dated tasks, collapsible
     groups), then "Shows" (one bar per linked show), then "No dates".
     Dependency arrows live in one SVG overlay (finish → start; red while the
     dependent starts too early).

   Rules kept here
   - DOM is built with createElement / textContent only (SVG through
     createElementNS). No project text ever goes into a URL or the console,
     and none is kept in localStorage: collapsed groups are remembered as a
     HASH of the group name.
   - Dates are ISO strings; all date math is PM.addDays / PM.diffDays or
     calendar steps on a local noon Date (never ms arithmetic — DST safe).
   - On PM 'state' the chart re-renders keeping scroll position, collapsed
     groups and keyboard focus — but NEVER while a pointer drag or a pending
     keyboard move is in progress (it is applied right after).
   - Nothing is written without PM.can.edit: view-only members, read-only
     accounts and archived projects get no handles and no Add menu, and the
     handlers are no-ops. Phones (html.mobile-view) and coarse pointers never
     drag: a tap opens the task, and the chart scrolls with one finger
     (touch-action: none is on the resize handles only — projects_gantt.css).
   - Dates only ever change through PM.saveTask (which owns the conflict
     notice and "Review shift…"); a failed save re-emits 'state' and the bar
     snaps back.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const PM = window.PM;
  if (!PM || typeof PM.registerTab !== 'function' || !document.getElementById('pm-pane-timeline')) return;

  const el = PM.el;
  const SVGNS = 'http://www.w3.org/2000/svg';
  const root = document.documentElement;
  const isPhone = () => root.classList.contains('mobile-view');
  const finePointer = window.matchMedia ? window.matchMedia('(pointer: fine)') : {matches: true};
  const reducedMotion = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : {matches: false};
  // Drag = an editor, a mouse / pen, the desktop presentation.
  const canDrag = () => !!PM.can.edit && finePointer.matches && !isPhone();

  /* ── Constants ─────────────────────────────────────────────────────────── */
  // dw = pixels per day; pad = days of air around the items; cap = the most
  // days one chart draws at that zoom (a longer plan is windowed, and says so).
  const ZOOMS = {
    day:   {label: 'Day',   dw: 30, pad: 7,  cap: 1100, capText: '3 years'},
    week:  {label: 'Week',  dw: 12, pad: 14, cap: 2200, capText: '6 years'},
    month: {label: 'Month', dw: 4,  pad: 31, cap: 7400, capText: '20 years'},
  };
  const ZOOM_KEYS = ['day', 'week', 'month'];
  const NEW_TASK_LEN = {day: 2, week: 4, month: 13};       // + days → a bar you can grab at that zoom
  const AXIS_H = 48;
  const MS = 16;                 // milestone diamond box
  const TODAY_NEAR = 120;        // today joins the range when it is this close to the items
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const MONTHS_LONG = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
                       'October', 'November', 'December'];
  const DOW = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];
  const EMPTY_TEXT = 'Nothing has dates yet — set a start or due date on a task, or link a show.';

  // Row metrics: phones get taller rows (touch targets). CSS reads the same
  // numbers through custom properties set on the scroll box.
  function metrics() {
    return isPhone() ? {row: 42, grp: 38, bar: 28, name: 136}
                     : {row: 34, grp: 30, bar: 22, name: 280};
  }

  /* ── Small helpers ─────────────────────────────────────────────────────── */
  const todayIso = () => (PM.state && PM.state.today) || window.localTodayIso();
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));
  const isHex = v => typeof v === 'string' && /^#[0-9a-fA-F]{6}$/.test(v);
  function noonDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || '');
    return m ? new Date(+m[1], +m[2] - 1, +m[3], 12) : null;      // noon: a calendar day whatever DST does
  }
  // Not the text itself: a group name is project content and must not sit in
  // the browser's storage.
  function hash(s) {
    let x = 5381;
    for (let i = 0; i < s.length; i++) x = ((x << 5) + x + s.charCodeAt(i)) | 0;
    return (x >>> 0).toString(36);
  }
  function fmtSpan(a, b) {
    return (!b || a === b) ? PM.fmtDate(a) : PM.fmtDate(a) + ' – ' + PM.fmtDate(b);
  }
  function monthYear(iso) {
    const d = noonDate(iso);
    return d ? MONTHS[d.getMonth()] + ' ' + d.getFullYear() : '';
  }
  function svg(tag, attrs) {
    const n = document.createElementNS(SVGNS, tag);
    if (attrs) Object.keys(attrs).forEach(k => n.setAttribute(k, attrs[k]));
    return n;
  }
  function linkIcon() {
    const s = svg('svg', {viewBox: '0 0 16 16', width: '12', height: '12', fill: 'none', stroke: 'currentColor',
                          'stroke-width': '1.7', 'stroke-linecap': 'round', 'aria-hidden': 'true', class: 'pm-g-linkico'});
    s.appendChild(svg('path', {d: 'M6.7 9.3l2.6-2.6M7.2 4.6l1-1a2.6 2.6 0 0 1 3.7 3.7l-1 1M8.8 11.4l-1 1a2.6 2.6 0 0 1-3.7-3.7l1-1'}));
    return s;
  }
  function attrs(node, map) {
    Object.keys(map).forEach(k => {
      if (map[k] == null || map[k] === false) node.removeAttribute(k);
      else node.setAttribute(k, map[k] === true ? '' : String(map[k]));
    });
    return node;
  }
  const cssEsc = s => (window.CSS && window.CSS.escape) ? window.CSS.escape(s) : String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&');

  /* ── Preferences (zoom + collapsed groups, per project) ────────────────── */
  const LS_KEY = 'pm.gantt.v1.' + PM.pid;
  let zoom = 'week';
  const collapsed = new Set();
  let legendOn = !isPhone();
  (function loadPrefs() {
    try {
      const j = JSON.parse(window.localStorage.getItem(LS_KEY) || 'null');
      if (j && ZOOMS[j.zoom]) zoom = j.zoom;
      if (j && Array.isArray(j.collapsed)) {
        j.collapsed.slice(0, 300).forEach(k => { if (typeof k === 'string' && k.length < 24) collapsed.add(k); });
      }
      const lg = window.localStorage.getItem('pm.gantt.legend');
      if (lg === '1' || lg === '0') legendOn = lg === '1';
    } catch (e) { /* private mode / blocked storage: defaults */ }
  })();
  function savePrefs() {
    try {
      window.localStorage.setItem(LS_KEY, JSON.stringify({zoom, collapsed: Array.from(collapsed).slice(0, 300)}));
      window.localStorage.setItem('pm.gantt.legend', legendOn ? '1' : '0');
    } catch (e) { /* the chart still works */ }
  }

  /* ── Tab state ─────────────────────────────────────────────────────────── */
  let pane = null, mounted = false;
  let toolbar = null, segBtns = {}, todayBtn = null, confBtn = null, legendBtn = null, legendEl = null, addWrap = null;
  let capNote = null, scroller = null, inner = null, emptyBox = null, tip = null;
  let M = metrics();
  let R = null;                 // the drawn range: {start, end, days, dw, zoom, capped}
  let rowsByTask = new Map();   // task id → {y, h, hidden: groupRow|null}
  let geomByTask = new Map();   // task id → {x1, x2} (lane px; where arrows attach)
  let arrowsByTask = new Map(); // task id → [path d] (its own arrows, for the hover highlight)
  let conflictIds = [];         // conflicted task ids in row order
  let confCursor = -1;
  const barEls = new Map();     // task id → the bar element (re-used across renders: focus + capture survive)
  const optimistic = new Map(); // task id → {start_date, due_date} shown while a save / keyboard move is pending
  let lastSig = '', stale = true, pending = false, firstDraw = true;
  let drag = null, kbd = null, eatClickUntil = 0;
  let hlPath = null;
  let parked = null;            // the scroll position while another tab is shown

  const busyNow = () => !!drag || !!kbd;

  // What the chart shows for a task right now (an optimistic value wins).
  function eff(t) {
    const o = optimistic.get(t.id);
    const start = o ? o.start_date : t.start_date, due = o ? o.due_date : t.due_date;
    return {start: start || null, due: due || null, begin: start || due || null, finish: due || start || null};
  }
  // New begin/finish → the task's own two columns. A single-date task stays
  // single-date while begin == finish; stretching it fills the other date.
  function toFields(t, nb, nf) {
    if (nb === nf) {
      if (t.start_date && t.due_date) return {start_date: nb, due_date: nf};
      return t.start_date ? {start_date: nb, due_date: null} : {start_date: null, due_date: nf};
    }
    return {start_date: nb, due_date: nf};
  }
  function changedFields(t, f) {
    const ch = {};
    if ((t.start_date || null) !== (f.start_date || null)) ch.start_date = f.start_date || null;
    if ((t.due_date || null) !== (f.due_date || null)) ch.due_date = f.due_date || null;
    return ch;
  }

  /* ═══ Mount: the toolbar and the (empty) scroll box ════════════════════ */
  function mount(p) {
    pane = p;
    mounted = true;
    pane.classList.add('pm-g');

    const seg = el('div', 'pm-seg');
    attrs(seg, {role: 'group', 'aria-label': 'Zoom'});
    ZOOM_KEYS.forEach(k => {
      const b = el('button', 'pm-seg-btn', ZOOMS[k].label);
      b.type = 'button';
      b.dataset.zoom = k;
      b.addEventListener('click', () => setZoom(k));
      segBtns[k] = b;
      seg.appendChild(b);
    });

    todayBtn = el('button', 'btn btn-ghost btn-sm', 'Today');
    todayBtn.type = 'button';
    todayBtn.title = 'Scroll to today';
    todayBtn.addEventListener('click', () => goToday(true));

    confBtn = el('button', 'pm-toggle pm-g-conf');
    confBtn.type = 'button';
    confBtn.hidden = true;
    confBtn.addEventListener('click', nextConflict);

    legendBtn = el('button', 'pm-toggle', 'Legend');
    legendBtn.type = 'button';
    legendBtn.addEventListener('click', () => { legendOn = !legendOn; savePrefs(); paintLegend(); fitHeight(); });

    // The Add ▾ menu: the app's ToolsMenu markup (same as ToolsMenu.html()),
    // built as DOM. Its items are produced when it OPENS.
    const addBtn = el('button', 'btn btn-sm btn-primary tools-menu-btn');
    attrs(addBtn, {type: 'button', title: 'Add to this project', 'aria-haspopup': 'menu', 'aria-expanded': 'false'});
    const busySpan = el('span', 'tools-menu-busy');
    busySpan.hidden = true;
    const caret = el('span', 'tools-menu-caret', '▾');
    caret.setAttribute('aria-hidden', 'true');
    addBtn.append(el('span', 'tools-menu-label', 'Add'), busySpan, caret);
    const panel = el('div', 'tools-menu-panel');
    attrs(panel, {role: 'menu', 'aria-label': 'Add to this project'});
    panel.hidden = true;
    addWrap = el('div', 'tools-menu pm-g-add');
    addWrap.dataset.tools = 'pm-g-add';
    addWrap.append(addBtn, panel);
    if (window.ToolsMenu) {
      window.ToolsMenu.register('pm-g-add', () => {
        if (!PM.can.edit) return [];
        const items = [{label: 'Task', onSelect: () => PM.newTask(defaultDates())}];
        if (PM.can.createShow) items.push({label: 'New show…', onSelect: () => PM.newShow()});
        items.push({label: 'Link a show…', onSelect: () => PM.linkShow()});
        return items;
      });
    }

    const right = el('div', 'pm-toolbar-right');
    right.append(legendBtn, addWrap);
    toolbar = el('div', 'pm-toolbar pm-g-toolbar');
    toolbar.append(seg, todayBtn, confBtn, right);

    legendEl = buildLegend();
    capNote = el('p', 'pm-note pm-g-capnote');
    capNote.hidden = true;

    scroller = el('div', 'pm-g-scroll');
    attrs(scroller, {role: 'group', 'aria-label': 'Project timeline'});
    inner = el('div', 'pm-g-inner');
    scroller.appendChild(inner);
    emptyBox = el('div', 'pm-g-emptybox');
    emptyBox.hidden = true;

    tip = el('div', 'pm-g-tip');
    tip.hidden = true;
    tip.setAttribute('aria-hidden', 'true');

    pane.append(toolbar, legendEl, capNote, scroller, emptyBox, tip);

    scroller.addEventListener('click', onClick);
    scroller.addEventListener('keydown', onKeyDown);
    scroller.addEventListener('pointerdown', onPointerDown);
    scroller.addEventListener('pointerover', onHover);
    scroller.addEventListener('pointerout', onHoverOut);
    scroller.addEventListener('focusin', onHover);
    scroller.addEventListener('focusout', onHoverOut);
    emptyBox.addEventListener('click', onClick);

    let raf = 0, lastW = 0;
    window.addEventListener('resize', () => {
      if (raf || PM.activeTab !== 'timeline') return;
      raf = requestAnimationFrame(() => {
        raf = 0;
        fitHeight();
        const w = scroller.clientWidth;
        if (w !== lastW) { lastW = w; draw(true); }
      });
    });
    // A mouse plugged into a tablet (or unplugged): handles appear / go.
    if (finePointer.addEventListener) finePointer.addEventListener('change', () => { if (PM.activeTab === 'timeline') draw(true); });
  }

  function buildLegend() {
    const box = el('div', 'pm-g-legend');
    box.setAttribute('aria-label', 'Legend');
    const item = (sw, text) => {
      const i = el('span', 'pm-g-lg');
      i.append(sw, el('span', null, text));
      box.appendChild(i);
    };
    const sw = (cls, text) => { const s = el('span', 'pm-g-sw ' + cls, text || ''); s.setAttribute('aria-hidden', 'true'); return s; };
    item(sw('pm-g-bar pm-st-todo'), 'To do');
    item(sw('pm-g-bar pm-st-doing'), 'In progress');
    item(sw('pm-g-bar pm-st-blocked', '⊘'), 'Blocked');
    item(sw('pm-g-bar pm-st-done is-done', '✓'), 'Done');
    item(sw('pm-g-sw-ms'), 'Milestone');
    item(sw('pm-g-bar pm-st-todo is-late'), 'Overdue');
    item(sw('pm-g-bar pm-st-todo is-conflict', '⚠'), 'Starts too early');
    item(sw('pm-g-sw-dep'), 'Depends on');
    item(sw('pm-g-sw-dep is-conflict'), 'Dependency in conflict');
    item(sw('pm-g-showbar'), 'Show');
    item(sw('pm-g-showbar is-event'), 'Event');
    item(sw('pm-g-sw-band'), 'Project dates');
    item(sw('pm-g-sw-today'), 'Today');
    return box;
  }
  function paintLegend() {
    legendBtn.setAttribute('aria-pressed', legendOn ? 'true' : 'false');
    legendEl.hidden = !legendOn || !emptyBox.hidden;
  }

  /* ── Sizing: the box fills the window below the toolbar ─────────────── */
  function fitHeight() {
    if (!mounted || scroller.hidden || PM.activeTab !== 'timeline') return;
    const main = document.querySelector('.main-content');
    const pb = main ? parseFloat(getComputedStyle(main).paddingBottom) || 0 : 0;
    let avail;
    if (isPhone()) {
      // Between the pinned tab strip and the bottom tab bar (the page
      // scrolls the title block and the toolbar away).
      const layout = document.querySelector('.app-layout');
      const pt = layout ? parseFloat(getComputedStyle(layout).paddingTop) || 0 : 0;
      const tabs = document.getElementById('pm-tabs');
      avail = window.innerHeight - pt - (tabs ? tabs.offsetHeight + 6 : 50) - pb - 8;
    } else {
      const top = scroller.getBoundingClientRect().top + window.scrollY;
      avail = window.innerHeight - top - pb;
    }
    scroller.style.maxHeight = Math.max(300, Math.floor(avail)) + 'px';
  }

  /* ═══ The model: rows + range ══════════════════════════════════════════ */
  function buildModel() {
    const s = PM.state;
    const groups = [];
    const undated = [];
    const taskGroups = PM.taskGroups();
    const named = taskGroups.some(g => g.name && g.tasks.length);
    taskGroups.forEach(g => {
      const dated = [];
      g.tasks.forEach(t => { (eff(t).begin ? dated : undated).push(t); });
      if (!dated.length) return;
      // A project with no groups at all gets no "No group" heading.
      groups.push({key: named ? 'g' + hash(g.name) : null, name: g.name || 'No group', kind: 'tasks', items: dated});
    });
    const shows = (s.shows || []).slice();
    if (shows.length) groups.push({key: '~shows', name: 'Shows', kind: 'shows', items: shows});
    if (undated.length) groups.push({key: '~nodates', name: 'No dates', kind: 'undated', items: undated});
    return {groups, undated, shows};
  }

  function computeRange(model, viewW) {
    const s = PM.state, z = ZOOMS[zoom], today = todayIso();
    let lo = null, hi = null, n = 0;
    const take = (a, b) => {
      if (!a) return;
      n++;
      if (!lo || a < lo) lo = a;
      if (!hi || (b || a) > hi) hi = b || a;
    };
    model.groups.forEach(g => {
      if (g.kind === 'tasks') g.items.forEach(t => { const d = eff(t); take(d.begin, d.finish); });
      else if (g.kind === 'shows') g.items.forEach(x => take(x.start, x.end));
    });
    if (!n) return null;
    const p = s.project;
    if (p.start_date) take(p.start_date, p.end_date || p.start_date);
    if (p.end_date) take(p.end_date, p.end_date);
    if (PM.diffDays(today, lo) <= TODAY_NEAR && PM.diffDays(hi, today) <= TODAY_NEAR) take(today, today);

    let start = PM.addDays(lo, -z.pad), end = PM.addDays(hi, z.pad);
    // Whole weeks (Monday first) — whole months at the month zoom.
    if (zoom === 'month') {
      const a = noonDate(start), b = noonDate(end);
      start = window.localIsoDate(new Date(a.getFullYear(), a.getMonth(), 1, 12));
      end = window.localIsoDate(new Date(b.getFullYear(), b.getMonth() + 1, 0, 12));
    } else {
      start = PM.addDays(start, -((noonDate(start).getDay() + 6) % 7));
      end = PM.addDays(end, (7 - noonDate(end).getDay()) % 7);
    }
    let days = PM.diffDays(start, end) + 1;
    let capped = null;
    if (days > z.cap) {
      // Too long to draw at this zoom: a window that holds today when it can.
      const fullStart = start, fullEnd = end;
      let ws = fullStart;
      if (today > fullStart && today < fullEnd) ws = PM.addDays(today, -Math.floor(z.cap / 4));
      if (ws < fullStart) ws = fullStart;
      if (PM.diffDays(ws, fullEnd) + 1 < z.cap) ws = PM.addDays(fullEnd, -(z.cap - 1));
      if (zoom !== 'month') ws = PM.addDays(ws, -((noonDate(ws).getDay() + 6) % 7));
      start = ws;
      end = PM.addDays(ws, z.cap - 1);
      days = z.cap;
      capped = {fullStart, fullEnd, outside: 0};
    }
    // Never narrower than the box.
    const need = Math.ceil(Math.max(0, viewW) / z.dw);
    if (!capped && days < need) { end = PM.addDays(end, need - days); days = need; }
    return {start, end, days, dw: z.dw, zoom, capped};
  }

  // Lane geometry for begin..finish. Clamped to the drawn range (a capped
  // chart): off = wholly outside ('l' | 'r'), clipL/clipR = cut at an edge.
  function geom(nb, nf, ms) {
    let a = PM.diffDays(R.start, nb), b = PM.diffDays(R.start, nf);
    const g = {off: null, clipL: false, clipR: false};
    if (b < 0) g.off = 'l';
    else if (a > R.days - 1) g.off = 'r';
    if (a < 0) { g.clipL = true; a = 0; }
    if (b > R.days - 1) { g.clipR = true; b = R.days - 1; }
    if (b < a) b = a;
    if (ms) {
      const c = (b + 0.5) * R.dw;
      g.left = c - MS / 2; g.width = MS; g.x1 = c - MS / 2 - 3; g.x2 = c + MS / 2 + 3;
    } else {
      g.left = a * R.dw + 1;
      g.width = Math.max(R.dw <= 4 ? 5 : 8, (b - a + 1) * R.dw - 2);
      g.x1 = g.left; g.x2 = g.left + g.width;
    }
    return g;
  }

  /* ═══ Render ═══════════════════════════════════════════════════════════ */
  function signature() {
    const s = PM.state, p = s.project;
    return JSON.stringify([
      zoom, Array.from(collapsed).sort(), !!PM.can.edit, canDrag(), isPhone(), todayIso(),
      p.start_date, p.end_date, p.color,
      s.tasks.map(t => { const d = eff(t); return [t.id, t.title, t.status, d.start, d.due, t.is_milestone ? 1 : 0, t.group_name,
                                                    t.sort_order, t.show_id, t.assignee_id, t.depends_on, t.conflict_with]; }),
      (s.shows || []).map(x => [x.id, x.name, x.start, x.end, x.perf_dates, x.show_mode, x.is_past ? 1 : 0, x.is_test ? 1 : 0]),
      s.members.map(m => [m.id, m.name]),
    ]);
  }

  // onState → draw(): re-render unless nothing the chart shows has changed.
  function draw(force) {
    if (!mounted || !PM.state) return;
    if (PM.activeTab !== 'timeline') { stale = true; return; }
    if (busyNow()) { pending = true; return; }
    pending = false;
    stale = false;
    M = metrics();
    const sig = signature();
    paintToolbar();
    if (!force && sig === lastSig) return;
    lastSig = sig;
    // Measure the box even when the empty state had it hidden.
    const wasHidden = scroller.hidden;
    scroller.hidden = false;
    render(Math.max(0, scroller.clientWidth - M.name), wasHidden);
  }

  function paintToolbar() {
    ZOOM_KEYS.forEach(k => segBtns[k].setAttribute('aria-pressed', k === zoom ? 'true' : 'false'));
    addWrap.hidden = !PM.can.edit;
    const n = PM.state.tasks.filter(t => t.conflict).length;
    confBtn.hidden = n === 0;
    if (n) {
      confBtn.textContent = '';
      const g = el('span', null, '⚠');
      g.setAttribute('aria-hidden', 'true');
      confBtn.append(g, ' ' + plural(n, 'conflict'));
      confBtn.title = 'Jump to ' + (n === 1 ? 'the task that starts' : 'the next task that starts') + ' before what it depends on finishes';
    }
    paintLegend();
  }

  function render(viewW, wasHidden) {
    const model = buildModel();
    const prev = R;
    const range = computeRange(model, viewW);

    // Remember where the user is looking (as a date) and what has focus.
    const keep = (prev && !wasHidden) ? {
      zoomChanged: prev.zoom !== zoom,
      day: (scroller.scrollLeft + (prev.zoom !== zoom ? viewW / 2 : 0)) / prev.dw,
      start: prev.start, top: scroller.scrollTop,
    } : null;
    const act = document.activeElement;
    const focusKey = (act && pane.contains(act) && act.dataset && act.dataset.fk) ? act.dataset.fk : null;

    if (!range) { renderEmpty(model); R = null; refocus(focusKey); return; }
    R = range;
    emptyBox.hidden = true;
    emptyBox.textContent = '';
    scroller.hidden = false;
    todayBtn.hidden = false;
    segBtns.day.parentNode.hidden = false;
    legendBtn.hidden = false;
    paintLegend();

    const W = R.days * R.dw;
    scroller.dataset.zoom = zoom;
    scroller.style.setProperty('--pm-g-namew', M.name + 'px');
    scroller.style.setProperty('--pm-g-row', M.row + 'px');
    scroller.style.setProperty('--pm-g-grp', M.grp + 'px');
    scroller.style.setProperty('--pm-g-barh', M.bar + 'px');
    const pc = PM.state.project.color;
    if (isHex(pc)) scroller.style.setProperty('--pm-g-proj', pc); else scroller.style.removeProperty('--pm-g-proj');

    const frag = document.createDocumentFragment();
    frag.appendChild(buildAxis(W));

    const body = el('div', 'pm-g-body');
    const bg = buildBackground(W);
    const rows = document.createDocumentFragment();

    rowsByTask = new Map();
    geomByTask = new Map();
    conflictIds = [];
    const live = new Set();
    let y = 0, outside = 0;
    const today = todayIso();
    const drag0 = canDrag();

    model.groups.forEach(g => {
      const isCollapsed = !!g.key && collapsed.has(g.key);
      let groupRow = null;
      if (g.key) {
        groupRow = {y, h: M.grp};
        rows.appendChild(buildGroupRow(g, isCollapsed, W));
        y += M.grp;
      }
      g.items.forEach(it => {
        if (g.kind === 'tasks') {
          const d = eff(it);
          const gm = geom(d.begin, d.finish, it.is_milestone);
          if (gm.off) outside++;
          geomByTask.set(it.id, gm);
          if (it.conflict) conflictIds.push(it.id);
          if (isCollapsed) { rowsByTask.set(it.id, {y: groupRow.y, h: groupRow.h, hidden: true, group: g.key}); return; }
          rowsByTask.set(it.id, {y, h: M.row, hidden: false, group: g.key});
          live.add(it.id);
          rows.appendChild(buildTaskRow(it, d, gm, W, today, drag0));
          y += M.row;
        } else if (!isCollapsed) {
          if (g.kind === 'shows') {
            if (it.start && geom(it.start, it.end || it.start, false).off) outside++;
            rows.appendChild(buildShowRow(it, W));
          } else rows.appendChild(buildUndatedRow(it, W));
          y += M.row;
        }
      });
    });
    // Bars of tasks that are gone (deleted, collapsed away, lost their dates).
    barEls.forEach((b, id) => { if (!live.has(id)) barEls.delete(id); });

    const H = y;
    bg.style.height = H + 'px';
    // Order matters: every row is its own stacking context (content-visibility
    // in the CSS), so the arrows go BEFORE the rows to be drawn under the bars.
    body.append(bg, buildArrows(W, H), rows);
    body.style.height = H + 'px';
    frag.appendChild(body);

    inner.style.width = (M.name + W) + 'px';
    inner.replaceChildren(frag);

    if (R.capped) {
      capNote.hidden = false;
      capNote.textContent = 'This plan is longer than the ' + ZOOMS[zoom].capText + ' the ' + ZOOMS[zoom].label +
        ' view draws: showing ' + monthYear(R.start) + ' – ' + monthYear(R.end) +
        (outside ? ' — ' + plural(outside, 'item') + ' outside it ' + (outside === 1 ? 'is' : 'are') + ' marked at the edge' : '') +
        (zoom !== 'month' ? '. Zoom out to see more.' : '.');
    } else capNote.hidden = true;

    fitHeight();
    // Put the same date back under the same spot.
    if (keep) {
      const day = keep.day + PM.diffDays(R.start, keep.start);
      scroller.scrollLeft = Math.max(0, Math.round(day * R.dw - (keep.zoomChanged ? viewW / 2 : 0)));
      scroller.scrollTop = keep.top;
    } else if (firstDraw || !prev) {
      goToday(false);
    }
    firstDraw = false;
    refocus(focusKey);
  }

  function refocus(key) {
    if (!key || PM.dialogOpen()) return;
    const n = pane.querySelector('[data-fk="' + cssEsc(key) + '"]');
    if (n && n !== document.activeElement) n.focus({preventScroll: true});
  }

  /* ── Empty: nothing has a date ───────────────────────────────────────── */
  function renderEmpty(model) {
    scroller.hidden = true;
    inner.textContent = '';
    barEls.clear();
    rowsByTask = new Map(); geomByTask = new Map(); arrowsByTask = new Map(); conflictIds = [];
    capNote.hidden = true;
    todayBtn.hidden = true;
    segBtns.day.parentNode.hidden = true;
    legendBtn.hidden = true;
    emptyBox.hidden = false;
    paintLegend();
    emptyBox.textContent = '';
    emptyBox.appendChild(el('p', 'pm-empty', EMPTY_TEXT));
    const list = model.undated, shows = model.shows;
    if (!list.length && !shows.length) return;
    const card = el('div', 'form-section');
    const head = el('div', 'form-section-header');
    head.append(el('span', null, 'No dates'), el('span', 'pm-count', String(list.length + shows.length)));
    const rows = el('div', 'pm-list');
    list.forEach(t => {
      const r = el('div', 'pm-row');
      const main = el('span', 'pm-row-main');
      const title = el('button', 'pm-link-btn pm-row-title', t.title);
      attrs(title, {type: 'button', 'data-open': t.id, 'data-fk': 'n' + t.id});
      main.appendChild(title);
      r.appendChild(main);
      if (PM.can.edit) {
        const b = el('button', 'btn btn-ghost btn-sm', 'Set dates');
        attrs(b, {type: 'button', 'data-open': t.id, 'data-fk': 'd' + t.id, 'aria-label': 'Set dates for ' + t.title});
        r.appendChild(b);
      }
      rows.appendChild(r);
    });
    shows.forEach(x => {
      const r = el('div', 'pm-row');
      const main = el('span', 'pm-row-main');
      const a = el('a', 'pm-row-title no-prefetch', x.name);
      attrs(a, {href: '/shows/' + (+x.id), target: '_blank', rel: 'noopener'});
      main.appendChild(a);
      r.append(main, el('span', 'pm-row-date', 'no date yet'));
      rows.appendChild(r);
    });
    card.append(head, rows);
    emptyBox.appendChild(card);
  }

  /* ── The date axis ───────────────────────────────────────────────────── */
  function buildAxis(W) {
    const head = el('div', 'pm-g-head');
    const corner = el('div', 'pm-g-corner', 'Task');
    const axis = el('div', 'pm-g-axis');
    axis.style.width = W + 'px';
    const today = todayIso();
    const dw = R.dw;
    const top = (x, w, text, long) => {
      const c = el('div', 'pm-g-ax-top');
      c.style.left = x + 'px'; c.style.width = w + 'px';
      const lab = el('span', null, w >= 120 && long ? long : text);
      c.appendChild(lab);
      if (w < 70) lab.hidden = true;                        // a sliver of a month: no room for its name
      axis.appendChild(c);
    };
    const cell = (x, w, text, cls, sub) => {
      const c = el('div', 'pm-g-ax-cell' + (cls ? ' ' + cls : ''));
      c.style.left = x + 'px'; c.style.width = w + 'px';
      if (sub != null) c.appendChild(el('i', null, sub));
      c.appendChild(el('b', null, text));
      axis.appendChild(c);
    };
    const d = noonDate(R.start);
    // Walk the days once; cut the top row at month (or year) changes and
    // the bottom row at day / week / month changes.
    let topStart = 0, topKey = null, topText = '', topLong = '';
    let botStart = 0, botKey = null, botText = '';
    for (let i = 0; i <= R.days; i++) {
      const y = d.getFullYear(), m = d.getMonth(), dom = d.getDate(), wd = d.getDay();
      const last = i === R.days;
      const tKey = zoom === 'month' ? y : y * 12 + m;
      if (last || tKey !== topKey) {
        if (topKey !== null) top(topStart * dw, (i - topStart) * dw, topText, topLong);
        topKey = tKey; topStart = i;
        topText = zoom === 'month' ? String(y) : MONTHS[m] + ' ' + y;
        topLong = zoom === 'month' ? String(y) : MONTHS_LONG[m] + ' ' + y;
      }
      if (zoom === 'day') {
        if (!last) {
          const iso = window.localIsoDate(d);
          cell(i * dw, dw, String(dom), (wd === 0 || wd === 6 ? 'is-we' : '') + (iso === today ? ' is-today' : ''), DOW[wd]);
        }
      } else {
        const bKey = zoom === 'week' ? ((wd === 1 || botKey === null) ? i : botKey) : y * 12 + m;
        if (last || bKey !== botKey) {
          if (botKey !== null) cell(botStart * dw, (i - botStart) * dw, botText);
          botKey = bKey; botStart = i;
          botText = zoom === 'week' ? MONTHS[m] + ' ' + dom : MONTHS[m];
        }
      }
      d.setDate(dom + 1);
    }
    const ti = PM.diffDays(R.start, today);
    if (ti >= 0 && ti < R.days && zoom !== 'day') {
      const mark = el('div', 'pm-g-ax-now');
      mark.style.left = ((ti + 0.5) * dw) + 'px';
      attrs(mark, {title: 'Today, ' + PM.fmtDate(today), role: 'img', 'aria-label': 'Today, ' + PM.fmtDate(today)});
      axis.appendChild(mark);
    }
    head.append(corner, axis);
    return head;
  }

  /* ── Weekend shading, month lines, the project band, the today line ── */
  function buildBackground(W) {
    const bg = el('div', 'pm-g-bg');
    bg.style.width = W + 'px';
    bg.setAttribute('aria-hidden', 'true');
    const dw = R.dw;
    const band = (cls, x, w) => {
      const b = el('div', cls);
      b.style.left = x + 'px'; b.style.width = w + 'px';
      bg.appendChild(b);
      return b;
    };
    const p = PM.state.project;
    if (p.start_date || p.end_date) {
      const a = p.start_date ? Math.max(0, PM.diffDays(R.start, p.start_date)) : 0;
      const b = p.end_date ? Math.min(R.days - 1, PM.diffDays(R.start, p.end_date)) : R.days - 1;
      if (b >= a) {
        const x = band('pm-g-band', a * dw, (b - a + 1) * dw);
        if (p.start_date && PM.diffDays(R.start, p.start_date) >= 0) x.classList.add('has-l');
        if (p.end_date && PM.diffDays(R.start, p.end_date) <= R.days - 1) x.classList.add('has-r');
      }
    }
    const d = noonDate(R.start);
    for (let i = 0; i < R.days; i++) {
      const wd = d.getDay(), dom = d.getDate();
      if (zoom !== 'month' && wd === 6) band('pm-g-we', i * dw, Math.min(2, R.days - i) * dw);
      else if (zoom !== 'month' && wd === 0 && i === 0) band('pm-g-we', 0, dw);
      // Hairlines: every Monday, a stronger one where a month begins (the
      // month zoom draws months only, stronger at a new year).
      if (i > 0) {
        if (zoom === 'month') { if (dom === 1) band('pm-g-vl' + (d.getMonth() === 0 ? ' is-month' : ''), i * dw, 0); }
        else if (dom === 1) band('pm-g-vl is-month', i * dw, 0);
        else if (wd === 1) band('pm-g-vl', i * dw, 0);
      }
      d.setDate(dom + 1);
    }
    const ti = PM.diffDays(R.start, todayIso());
    if (ti >= 0 && ti < R.days) band('pm-g-now', (ti + 0.5) * dw, 0);
    return bg;
  }

  /* ── Rows ────────────────────────────────────────────────────────────── */
  function lane(W) {
    const l = el('div', 'pm-g-lane');
    l.style.width = W + 'px';
    return l;
  }

  function buildGroupRow(g, isCollapsed, W) {
    const row = el('div', 'pm-g-row pm-g-grow');
    const name = el('div', 'pm-g-name');
    const btn = el('button', 'pm-g-gtoggle');
    attrs(btn, {type: 'button', 'aria-expanded': isCollapsed ? 'false' : 'true', 'data-group': g.key, 'data-fk': 'g' + g.key,
                title: (isCollapsed ? 'Show ' : 'Hide ') + g.name});
    const caret = el('span', 'pm-g-caret', isCollapsed ? '▸' : '▾');
    caret.setAttribute('aria-hidden', 'true');
    btn.append(caret, el('span', 'pm-g-gname', g.name), el('span', 'pm-count', String(g.items.length)));
    name.appendChild(btn);
    const l = lane(W);
    if (g.kind === 'tasks') {
      const conf = g.items.filter(t => t.conflict).length;
      if (conf) {
        const w = el('span', 'pm-glyph pm-glyph-warn', '⚠ ' + conf);
        const label = plural(conf, 'task') + ' in this group ' + (conf === 1 ? 'starts' : 'start') + ' too early';
        attrs(w, {title: label, 'aria-label': label, role: 'img'});
        name.appendChild(w);
      }
      // The group's own span: a thin bracket from its first start to its last finish.
      let lo = null, hi = null;
      g.items.forEach(t => { const d = eff(t); if (!lo || d.begin < lo) lo = d.begin; if (!hi || d.finish > hi) hi = d.finish; });
      const gm = geom(lo, hi, false);
      if (!gm.off) {
        const sum = el('div', 'pm-g-sum');
        sum.style.left = gm.left + 'px'; sum.style.width = gm.width + 'px';
        sum.title = g.name + ' — ' + fmtSpan(lo, hi);
        l.appendChild(sum);
      }
    }
    row.append(name, l);
    return row;
  }

  function conflictText(t) {
    const names = (t.conflict_with || []).map(id => { const x = PM.taskById(id); return x ? '“' + x.title + '”' : null; }).filter(Boolean);
    return 'Starts before ' + (names.length ? names.join(', ') : 'a task it depends on') + ' ' + (names.length > 1 ? 'finish' : 'finishes');
  }
  function barLabels(t, d, today) {
    const st = PM.STATUS[t.status] || PM.STATUS.todo;
    const span = fmtSpan(d.begin, d.finish);
    const late = !!(d.finish && t.status !== 'done' && d.finish < today);
    const who = t.assignee_id != null ? PM.userName(t.assignee_id) : '';
    return {
      // The contract's wording: "<title>, <status>, <start>–<due>[, schedule conflict]"
      aria: t.title + ', ' + st.label + ', ' + (d.begin === d.finish ? PM.fmtDate(d.begin) : PM.fmtDate(d.begin) + '–' + PM.fmtDate(d.finish)) +
            (t.conflict ? ', schedule conflict' : ''),
      title: t.title + ' — ' + (t.is_milestone ? 'Milestone · ' : '') + st.label + ' · ' + span + (who ? ' · ' + who : '') +
             (late ? ' · overdue' : '') + (t.conflict ? ' · ' + conflictText(t) : ''),
      late,
    };
  }

  // (Re)build one task's bar. The element is kept across renders so focus
  // returns to it after a dialog and a captured pointer keeps its target.
  function paintBar(t, d, gm, today, drag0) {
    let bar = barEls.get(t.id);
    if (!bar) { bar = document.createElement('div'); barEls.set(t.id, bar); }
    const st = PM.STATUS[t.status] || PM.STATUS.todo;
    const lab = barLabels(t, d, today);
    const done = t.status === 'done';
    const clipped = gm.clipL || gm.clipR;
    bar.className = 'pm-g-bar ' + st.cls + (t.is_milestone ? ' is-ms' : '') + (done ? ' is-done' : '') +
      (t.conflict ? ' is-conflict' : '') + (lab.late ? ' is-late' : '') + (clipped ? ' is-clip' : '') +
      (gm.clipL ? ' is-clip-l' : '') + (gm.clipR ? ' is-clip-r' : '') + (drag0 && !clipped ? ' can-drag' : '');
    attrs(bar, {role: 'button', tabindex: '0', 'data-id': t.id, 'data-fk': 'b' + t.id, 'aria-label': lab.aria, title: lab.title});
    bar.style.left = gm.left + 'px';
    bar.style.width = gm.width + 'px';
    bar.textContent = '';
    if (t.is_milestone) {
      const dia = el('span', 'pm-g-dia');
      dia.setAttribute('aria-hidden', 'true');
      bar.appendChild(dia);
      if (t.conflict) bar.appendChild(warnGlyph('pm-g-warn pm-g-warn-ms'));
      return bar;
    }
    const room = gm.width;
    const inn = el('span', 'pm-g-bar-in');
    inn.setAttribute('aria-hidden', 'true');
    if (t.conflict && room >= 18) inn.appendChild(warnGlyph('pm-g-warn'));
    if (done && room >= 18) inn.appendChild(el('span', 'pm-g-tick', '✓'));
    else if (t.status === 'blocked' && room >= 18) inn.appendChild(el('span', 'pm-g-tick', '⊘'));
    if (t.show_id && room >= 34) inn.appendChild(linkIcon());
    if (room >= 56) inn.appendChild(el('span', 'pm-g-bar-t', t.title));
    bar.appendChild(inn);
    if (drag0 && !clipped && room >= 18) {
      const hl = el('span', 'pm-g-h pm-g-h-l'), hr = el('span', 'pm-g-h pm-g-h-r');
      hl.setAttribute('aria-hidden', 'true'); hr.setAttribute('aria-hidden', 'true');
      bar.append(hl, hr);
    }
    return bar;
  }
  function warnGlyph(cls) {
    const w = el('span', cls, '⚠');
    if (PM.can.edit) w.dataset.review = '1';
    return w;
  }

  function buildTaskRow(t, d, gm, W, today, drag0) {
    const row = el('div', 'pm-g-row');
    row.dataset.id = t.id;
    const st = PM.STATUS[t.status] || PM.STATUS.todo;
    const name = el('div', 'pm-g-name');
    const dot = el('span', 'pm-g-dot ' + st.cls);
    attrs(dot, {title: st.label, 'aria-label': st.label, role: 'img'});
    // The bar is the keyboard stop for a dated task; the name is a pointer /
    // touch target for the same thing (tabindex -1: no double tab stop).
    const title = el('button', 'pm-g-title' + (t.status === 'done' ? ' is-done' : ''), t.title);
    attrs(title, {type: 'button', tabindex: '-1', title: t.title, 'data-open': t.id});
    name.append(dot, title);
    if (t.is_milestone) {
      const m = el('span', 'pm-glyph pm-glyph-ms', '◆');
      attrs(m, {title: 'Milestone', 'aria-label': 'Milestone', role: 'img'});
      name.appendChild(m);
    }
    if (t.show_id) {
      const a = el('a', 'pm-g-showlink no-prefetch');
      attrs(a, {href: '/shows/' + (+t.show_id), target: '_blank', rel: 'noopener', title: 'Open the linked show (new tab)',
                'aria-label': 'Open the show linked to ' + t.title + ' in a new tab', 'data-fk': 'l' + t.id});
      a.appendChild(linkIcon());
      name.appendChild(a);
    }
    if (t.conflict) {
      const text = conflictText(t);
      if (PM.can.edit) {
        const b = el('button', 'pm-g-warnbtn', '⚠');
        attrs(b, {type: 'button', title: text + ' — review a shift', 'aria-label': 'Schedule conflict: ' + text + '. Review a shift',
                  'data-review': t.id, 'data-fk': 'w' + t.id});
        name.appendChild(b);
      } else {
        const g = el('span', 'pm-glyph pm-glyph-warn', '⚠');
        attrs(g, {title: text, 'aria-label': 'Schedule conflict: ' + text, role: 'img'});
        name.appendChild(g);
      }
    }
    const l = lane(W);
    if (gm.off) {
      // A capped chart: this task lies wholly outside the drawn dates.
      const b = el('button', 'pm-g-off is-' + gm.off, gm.off === 'l' ? '◂ ' + fmtSpan(d.begin, d.finish) : fmtSpan(d.begin, d.finish) + ' ▸');
      attrs(b, {type: 'button', 'data-open': t.id, 'data-fk': 'o' + t.id, title: t.title + ' — outside the dates drawn at this zoom'});
      l.appendChild(b);
      barEls.delete(t.id);
    } else {
      l.appendChild(paintBar(t, d, gm, today, drag0));
    }
    row.append(name, l);
    return row;
  }

  function buildShowRow(x, W) {
    const row = el('div', 'pm-g-row pm-g-srow');
    const name = el('div', 'pm-g-name');
    const kind = x.show_mode === 'event' ? 'Event' : 'Show';
    const a = el('a', 'pm-g-title no-prefetch', x.name);
    attrs(a, {href: '/shows/' + (+x.id), target: '_blank', rel: 'noopener', tabindex: x.start ? '-1' : null,
              title: x.name + ' — opens the ' + kind.toLowerCase() + ' in a new tab', 'data-fk': x.start ? null : 's' + x.id});
    name.appendChild(a);
    name.appendChild(el('span', 'pm-badge ' + (x.show_mode === 'event' ? 'pm-badge-event' : 'pm-badge-accent'), kind));
    if (x.is_past) name.appendChild(el('span', 'pm-badge pm-badge-muted', 'Past'));
    if (x.is_test) name.appendChild(el('span', 'pm-badge pm-badge-muted', 'Test'));
    const l = lane(W);
    if (!x.start) {
      l.appendChild(el('span', 'pm-g-nodate', 'no date yet'));
    } else {
      const end = x.end || x.start;
      const gm = geom(x.start, end, false);
      if (gm.off) {
        l.appendChild(el('span', 'pm-g-off is-' + gm.off, gm.off === 'l' ? '◂ ' + fmtSpan(x.start, end) : fmtSpan(x.start, end) + ' ▸'));
        a.removeAttribute('tabindex');
      } else {
        const perfs = (x.perf_dates || []).filter(p => p >= x.start && p <= end);
        const label = x.name + ', ' + kind.toLowerCase() + ', ' + fmtSpan(x.start, end).replace(' – ', '–') +
          (perfs.length ? ', ' + plural(perfs.length, 'performance') : '') + ', opens in a new tab';
        const bar = el('a', 'pm-g-showbar no-prefetch' + (x.show_mode === 'event' ? ' is-event' : '') + (x.is_past ? ' is-past' : '') +
                             (gm.clipL ? ' is-clip-l' : '') + (gm.clipR ? ' is-clip-r' : ''));
        attrs(bar, {href: '/shows/' + (+x.id), target: '_blank', rel: 'noopener', draggable: 'false', 'data-fk': 's' + x.id, 'aria-label': label,
                    title: x.name + ' — ' + kind + ' · ' + fmtSpan(x.start, end) + (x.venue ? ' · ' + x.venue : '') +
                           (perfs.length ? ' · ' + plural(perfs.length, 'performance') : '') + (x.is_past ? ' · past' : '')});
        bar.style.left = gm.left + 'px';
        bar.style.width = gm.width + 'px';
        perfs.forEach(p => {
          const i = PM.diffDays(R.start, p);
          if (i < 0 || i > R.days - 1) return;
          const tick = el('span', 'pm-g-perf');
          tick.style.left = ((i + 0.5) * R.dw - gm.left) + 'px';
          tick.setAttribute('aria-hidden', 'true');
          bar.appendChild(tick);
        });
        if (gm.width >= 70) {
          const tt = el('span', 'pm-g-bar-t', x.name);
          tt.setAttribute('aria-hidden', 'true');
          bar.appendChild(tt);
        }
        l.appendChild(bar);
      }
    }
    row.append(name, l);
    return row;
  }

  function buildUndatedRow(t, W) {
    const row = el('div', 'pm-g-row pm-g-urow');
    row.dataset.id = t.id;
    const st = PM.STATUS[t.status] || PM.STATUS.todo;
    const name = el('div', 'pm-g-name');
    const dot = el('span', 'pm-g-dot ' + st.cls);
    attrs(dot, {title: st.label, 'aria-label': st.label, role: 'img'});
    const title = el('button', 'pm-g-title' + (t.status === 'done' ? ' is-done' : ''), t.title);
    attrs(title, {type: 'button', title: t.title, 'data-open': t.id, 'data-fk': 'n' + t.id});
    name.append(dot, title);
    if (t.is_milestone) {
      const m = el('span', 'pm-glyph pm-glyph-ms', '◆');
      attrs(m, {title: 'Milestone', 'aria-label': 'Milestone', role: 'img'});
      name.appendChild(m);
    }
    const l = lane(W);
    if (PM.can.edit) {
      const b = el('button', 'pm-g-setdates', 'set dates');
      attrs(b, {type: 'button', 'data-open': t.id, 'data-fk': 'd' + t.id, 'aria-label': 'Set dates for ' + t.title});
      l.appendChild(b);
    } else {
      l.appendChild(el('span', 'pm-g-nodate', 'no dates yet'));
    }
    row.append(name, l);
    return row;
  }

  /* ── Dependency arrows (one SVG over the lanes) ──────────────────────── */
  // An orthogonal polyline with small rounded corners.
  function rounded(pts) {
    let d = 'M' + pts[0][0] + ' ' + pts[0][1];
    for (let i = 1; i < pts.length - 1; i++) {
      const p = pts[i - 1], c = pts[i], n = pts[i + 1];
      const r = Math.min(4, Math.abs((c[0] - p[0]) + (c[1] - p[1])) / 2, Math.abs((n[0] - c[0]) + (n[1] - c[1])) / 2);
      const ax = c[0] - Math.sign(c[0] - p[0]) * r, ay = c[1] - Math.sign(c[1] - p[1]) * r;
      const bx = c[0] + Math.sign(n[0] - c[0]) * r, by = c[1] + Math.sign(n[1] - c[1]) * r;
      d += 'L' + ax + ' ' + ay + 'Q' + c[0] + ' ' + c[1] + ' ' + bx + ' ' + by;
    }
    const z = pts[pts.length - 1];
    return d + 'L' + z[0] + ' ' + z[1];
  }
  // finish of the dependency (x1, y1) → start of the dependent (x2, y2).
  // Room between them: out, down, in. No room (a conflict, or back-to-back):
  // out, down to the gap between rows beside the dependent, back, and in —
  // so the line never runs through a bar.
  function route(x1, y1, x2, y2, half2) {
    const S = 9, tip2 = x2 - 5;
    if (x2 >= x1 + S + 7) return rounded([[x1, y1], [x1 + S, y1], [x1 + S, y2], [tip2, y2]]);
    const ym = y2 + (y2 > y1 ? -half2 : half2);
    return rounded([[x1, y1], [x1 + S, y1], [x1 + S, ym], [x2 - S - 4, ym], [x2 - S - 4, y2], [tip2, y2]]);
  }
  const headPath = (x, y) => 'M' + x + ' ' + y + 'l-6 -3.5v7z';

  function buildArrows(W, H) {
    const s = svg('svg', {class: 'pm-g-arrows', width: W, height: H, viewBox: '0 0 ' + W + ' ' + H, 'aria-hidden': 'true', focusable: 'false'});
    arrowsByTask = new Map();
    let lines = '', heads = '';
    const red = [];
    const note = (id, d) => { const a = arrowsByTask.get(id); if (a) a.push(d); else arrowsByTask.set(id, [d]); };
    PM.state.tasks.forEach(t => {
      const to = rowsByTask.get(t.id), tg = geomByTask.get(t.id);
      if (!to || !tg || tg.off || !(t.depends_on || []).length) return;
      t.depends_on.forEach(did => {
        const from = rowsByTask.get(did), fg = geomByTask.get(did);
        if (!from || !fg || fg.off) return;
        // Both tucked into the same collapsed group: nothing to show.
        if (from.hidden && to.hidden && from.group === to.group) return;
        const y1 = from.y + from.h / 2, y2 = to.y + to.h / 2;
        if (y1 === y2) return;
        const d = route(Math.round(fg.x2), y1, Math.round(tg.x1), y2, to.h / 2);
        const hd = headPath(Math.round(tg.x1), y2);
        const bad = (t.conflict_with || []).indexOf(did) >= 0;
        if (bad) red.push({id: t.id, d, hd, dep: did});
        else { lines += d; heads += hd; }
        note(t.id, d + hd); note(did, d + hd);
      });
    });
    if (lines) {
      s.appendChild(svg('path', {class: 'pm-g-dep-line', d: lines}));
      s.appendChild(svg('path', {class: 'pm-g-dep-head', d: heads}));
    }
    hlPath = svg('path', {class: 'pm-g-dep-hl', d: ''});
    s.appendChild(hlPath);
    red.forEach(a => {
      const g = svg('g', {class: 'pm-g-dep is-conflict' + (PM.can.edit ? ' can-review' : ''), 'data-review': a.id});
      const t = PM.taskById(a.id), dep = PM.taskById(a.dep);
      const title = svg('title');
      title.textContent = (t ? '“' + t.title + '”' : 'This task') + ' starts before ' + (dep ? '“' + dep.title + '”' : 'what it depends on') +
        ' finishes' + (PM.can.edit ? ' — click to review a shift' : '');
      g.appendChild(title);
      if (PM.can.edit) g.appendChild(svg('path', {class: 'pm-g-dep-hit', d: a.d}));
      g.appendChild(svg('path', {class: 'pm-g-dep-line', d: a.d}));
      g.appendChild(svg('path', {class: 'pm-g-dep-head', d: a.hd}));
      s.appendChild(g);
    });
    return s;
  }
  function onHover(e) {
    if (drag || !hlPath) return;
    const row = e.target.closest && e.target.closest('.pm-g-row[data-id]');
    const list = row ? arrowsByTask.get(+row.dataset.id) : null;
    hlPath.setAttribute('d', list ? list.join('') : '');
  }
  function onHoverOut(e) {
    if (!hlPath) return;
    const to = e.relatedTarget;
    if (to && to.closest && to.closest('.pm-g-row[data-id]')) return;     // onHover repaints
    hlPath.setAttribute('d', '');
  }

  /* ═══ Scrolling: today, conflicts ══════════════════════════════════════ */
  const lanesViewW = () => Math.max(0, scroller.clientWidth - M.name);
  function scrollToX(x, smooth) {
    const left = Math.max(0, Math.round(x));
    if (smooth && !reducedMotion.matches && scroller.scrollTo) scroller.scrollTo({left, behavior: 'smooth'});
    else scroller.scrollLeft = left;
  }
  function goToday(byUser) {
    if (!R) return;
    const i = PM.diffDays(R.start, todayIso());
    if (i < 0 || i > R.days - 1) {
      if (byUser) { PM.toast('Today is outside this timeline’s dates.'); scrollToX(i < 0 ? 0 : R.days * R.dw, true); }
      else scroller.scrollLeft = 0;
      return;
    }
    scrollToX((i + 0.5) * R.dw - lanesViewW() / (isPhone() ? 4 : 3), byUser);
  }

  function reveal(id) {
    const info = rowsByTask.get(id);
    if (!info) return false;
    if (info.hidden && info.group) {
      collapsed.delete(info.group);
      savePrefs();
      draw(true);
    }
    const row = rowsByTask.get(id), gm = geomByTask.get(id);
    if (!row || !gm) return false;
    const left = Math.max(0, Math.round(gm.x1 - lanesViewW() / 3));
    const top = Math.max(0, Math.round(row.y - (scroller.clientHeight - AXIS_H) / 2 + row.h / 2));
    const smooth = !reducedMotion.matches && !!scroller.scrollTo;
    // The box can hang below the window (a phone before the page is
    // scrolled): bring all of it up first, or the row lands out of sight.
    const main = document.querySelector('.main-content');
    const floor = window.innerHeight - (main ? parseFloat(getComputedStyle(main).paddingBottom) || 0 : 0);
    const over = scroller.getBoundingClientRect().bottom - floor;
    if (over > 2) window.scrollBy({top: Math.ceil(over), behavior: smooth ? 'smooth' : 'auto'});
    if (smooth) scroller.scrollTo({left, top, behavior: 'smooth'});
    else { scroller.scrollLeft = left; scroller.scrollTop = top; }
    const bar = barEls.get(id) || scroller.querySelector('[data-fk="o' + id + '"]');
    if (bar) {
      bar.focus({preventScroll: true});
      bar.classList.remove('is-flash');
      void bar.offsetWidth;                      // restart the animation
      bar.classList.add('is-flash');
      setTimeout(() => bar.classList.remove('is-flash'), 1800);
    }
    return true;
  }
  // The pill: the first conflicted bar, then the next one on each press.
  function nextConflict() {
    if (!conflictIds.length) return;
    flushKbd();
    confCursor = (confCursor + 1) % conflictIds.length;
    reveal(conflictIds[confCursor]);
  }

  function setZoom(k) {
    if (!ZOOMS[k] || k === zoom) return;
    flushKbd();
    zoom = k;
    savePrefs();
    draw(true);
  }

  // Where a new task lands: on today when today is on screen, else a
  // quarter of the way into what is on screen.
  function defaultDates() {
    const today = todayIso();
    let start = today;
    if (R && !scroller.hidden) {
      const viewDays = Math.max(1, Math.floor(lanesViewW() / R.dw));
      const first = PM.addDays(R.start, Math.floor(scroller.scrollLeft / R.dw));
      const last = PM.addDays(first, viewDays - 1);
      if (today < first || today > last) start = PM.addDays(first, Math.floor(viewDays / 4));
    }
    return {start_date: start, due_date: PM.addDays(start, NEW_TASK_LEN[zoom] || 2)};
  }

  /* ═══ Clicks ═══════════════════════════════════════════════════════════ */
  function onClick(e) {
    if (performance.now() < eatClickUntil) { eatClickUntil = 0; e.preventDefault(); return; }
    const t = e.target;
    if (!t.closest) return;
    const g = t.closest('.pm-g-gtoggle');
    if (g) {
      const key = g.dataset.group;
      if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key);
      savePrefs();
      flushKbd();
      draw(true);
      return;
    }
    if (t.closest('a')) return;                              // a show: the browser opens it
    const rev = t.closest('[data-review]');
    if (rev) {
      const bar = rev.closest('.pm-g-bar');
      const id = +(rev.dataset.review !== '1' ? rev.dataset.review : (bar && bar.dataset.id));
      if (id && PM.can.edit) { flushKbd().then(() => PM.reviewShift(id)); return; }
    }
    const open = t.closest('[data-open]');
    if (open) { openTask(+open.dataset.open); return; }
    const bar = t.closest('.pm-g-bar[data-id]');
    if (bar) openTask(+bar.dataset.id);
  }
  function openTask(id) {
    flushKbd().then(() => PM.openTask(id));
  }

  /* ═══ Keyboard ═════════════════════════════════════════════════════════ */
  function onKeyDown(e) {
    const bar = e.target;
    if (!bar.classList || e.altKey || e.ctrlKey || e.metaKey) return;
    const isBar = bar.classList.contains('pm-g-bar'), isShow = bar.classList.contains('pm-g-showbar');
    if (!isBar && !isShow) return;
    if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
      const all = Array.from(scroller.querySelectorAll('.pm-g-bar, .pm-g-showbar'));
      const next = all[all.indexOf(bar) + (e.key === 'ArrowDown' ? 1 : -1)];
      if (next) { e.preventDefault(); next.focus(); }
      return;
    }
    if (!isBar) return;
    const id = +bar.dataset.id;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      if (!e.repeat) openTask(id);
      return;
    }
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    if (!PM.can.edit || drag) return;                        // view-only: a no-op
    e.preventDefault();
    nudge(id, e.key === 'ArrowRight' ? 1 : -1, e.shiftKey);
  }

  // ←/→ one day; Shift+←/→ the due date. Shown at once, saved 600 ms after
  // the last key.
  function nudge(id, step, resize) {
    const t = PM.taskById(id);
    if (!t) return;
    if (kbd && kbd.id !== id) flushKbd();
    const d = eff(t);
    if (!d.begin) return;
    let nb = d.begin, nf = d.finish;
    if (resize && !(t.is_milestone && nb === nf)) {
      nf = PM.addDays(nf, step);
      if (nf < nb) return;                                   // the due date stops at the start
    } else {
      nb = PM.addDays(nb, step);
      nf = PM.addDays(nf, step);
    }
    optimistic.set(id, toFields(t, nb, nf));
    const bar = barEls.get(id);
    if (bar && R) {
      const now = eff(t);
      const gm = geom(now.begin, now.finish, t.is_milestone);
      bar.style.left = gm.left + 'px';
      bar.style.width = gm.width + 'px';
      const lab = barLabels(t, now, todayIso());
      bar.setAttribute('aria-label', lab.aria);
      bar.title = lab.title;
      bar.classList.add('is-pending');
      // Keep the moving bar on screen.
      const vl = scroller.scrollLeft, vr = vl + lanesViewW();
      if (gm.left < vl + 8) scroller.scrollLeft = Math.max(0, gm.left - 40);
      else if (gm.left + gm.width > vr - 8) scroller.scrollLeft = gm.left + gm.width - lanesViewW() + 40;
    }
    if (kbd) clearTimeout(kbd.timer);
    kbd = {id, timer: setTimeout(flushKbd, 600)};
  }
  function flushKbd() {
    if (!kbd) return Promise.resolve();
    clearTimeout(kbd.timer);
    const id = kbd.id;
    kbd = null;
    return commit(id);
  }

  // Save what `optimistic` holds for this task. Always ends with a render
  // (the saved state, or — after a refused save — the bar back where it was).
  function commit(id) {
    const t = PM.taskById(id), f = optimistic.get(id);
    const ch = (t && f) ? changedFields(t, f) : {};
    if (!Object.keys(ch).length || !PM.can.edit) {
      optimistic.delete(id);
      draw(true);
      return Promise.resolve();
    }
    // Saved: PM.saveTask has already emitted 'state' (drawn with the new
    // dates). Refused: force a redraw so the bar goes back.
    return PM.saveTask(id, ch).then(() => true, () => false).then(ok => {
      optimistic.delete(id);
      draw(!ok);
    });
  }

  /* ═══ Pointer drag: move / resize ══════════════════════════════════════ */
  function onPointerDown(e) {
    if (drag || e.button !== 0 || e.pointerType === 'touch' || !canDrag() || !R) return;
    const t0 = e.target;
    if (!t0.closest) return;
    const bar = t0.closest('.pm-g-bar.can-drag');
    if (!bar || t0.closest('[data-review]')) return;
    const id = +bar.dataset.id, t = PM.taskById(id);
    if (!t) return;
    if (kbd && kbd.id !== id) flushKbd();
    else if (kbd) { clearTimeout(kbd.timer); kbd = null; }     // same task: the drag continues from where the keys left it
    const d = eff(t);
    if (!d.begin) return;
    const mode = t0.closest('.pm-g-h-l') ? 'l' : t0.closest('.pm-g-h-r') ? 'r' : 'move';
    drag = {id, bar, mode, pointer: e.pointerId, x0: e.clientX, sl0: scroller.scrollLeft, cx: e.clientX,
            begin: d.begin, finish: d.finish, nb: d.begin, nf: d.finish, ms: !!t.is_milestone, moved: false,
            left0: bar.style.left, width0: bar.style.width, hadOpt: optimistic.has(id), raf: 0};
    try { bar.setPointerCapture(e.pointerId); } catch (err) { /* the drag still works while the pointer is over the box */ }
    bar.addEventListener('pointermove', onPointerMove);
    bar.addEventListener('pointerup', onPointerUp);
    bar.addEventListener('pointercancel', onPointerCancel);
    bar.addEventListener('lostpointercapture', onPointerCancel);
    document.addEventListener('keydown', onDragKey, true);
  }
  function onPointerMove(e) {
    if (!drag || e.pointerId !== drag.pointer) return;
    drag.cx = e.clientX;
    drag.cy = e.clientY;
    dragUpdate();
  }
  function dragUpdate() {
    const d = drag;
    if (!d) return;
    const dx = d.cx - d.x0 + (scroller.scrollLeft - d.sl0);
    if (!d.moved) {
      if (Math.abs(dx) < 4) return;
      d.moved = true;
      d.bar.classList.add('is-dragging');
      root.classList.add(d.mode === 'move' ? 'pm-g-moving' : 'pm-g-resizing');
      tip.hidden = false;
    }
    let n = Math.round(dx / R.dw);
    // Whole days, inside the drawn dates (the range grows again after the save).
    if (d.mode === 'move') {
      n = Math.max(-PM.diffDays(R.start, d.begin), Math.min(PM.diffDays(d.finish, R.end), n));
      d.nb = PM.addDays(d.begin, n);
      d.nf = PM.addDays(d.finish, n);
    } else if (d.mode === 'l') {
      n = Math.max(-PM.diffDays(R.start, d.begin), Math.min(PM.diffDays(d.begin, d.finish), n));    // never past the due date
      d.nb = PM.addDays(d.begin, n);
    } else {
      n = Math.max(-PM.diffDays(d.begin, d.finish), Math.min(PM.diffDays(d.finish, R.end), n));    // never before the start
      d.nf = PM.addDays(d.finish, n);
    }
    const gm = geom(d.nb, d.nf, d.ms);
    d.bar.style.left = gm.left + 'px';
    d.bar.style.width = gm.width + 'px';
    const len = PM.diffDays(d.nb, d.nf) + 1;
    tip.textContent = fmtSpan(d.nb, d.nf) + (d.ms || len === 1 ? '' : ' · ' + plural(len, 'day')) +
      (d.mode === 'move' && n ? ' (' + (n > 0 ? '+' : '−') + plural(Math.abs(n), 'day') + ')' : '');
    const br = d.bar.getBoundingClientRect();
    const tw = tip.offsetWidth || 160;
    tip.style.left = Math.max(8, Math.min(window.innerWidth - tw - 8, d.cx - tw / 2)) + 'px';
    tip.style.top = Math.max(8, br.top - 34) + 'px';
    autoScroll();
  }
  // Dragging against an edge of the box scrolls it.
  function autoScroll() {
    const d = drag;
    if (!d || d.raf || !d.moved) return;
    const r = scroller.getBoundingClientRect();
    const leftEdge = r.left + M.name, zone = 36;
    let v = 0;
    if (d.cx < leftEdge + zone) v = -Math.ceil((leftEdge + zone - d.cx) / 3);
    else if (d.cx > r.right - zone) v = Math.ceil((d.cx - (r.right - zone)) / 3);
    if (!v) return;
    v = Math.max(-24, Math.min(24, v));
    d.raf = requestAnimationFrame(() => {
      if (drag !== d) return;
      d.raf = 0;
      const before = scroller.scrollLeft;
      scroller.scrollLeft = before + v;
      if (scroller.scrollLeft !== before) dragUpdate();
    });
  }
  function onDragKey(e) {
    if (!drag || e.key !== 'Escape') return;
    e.preventDefault();
    e.stopPropagation();
    // The button is still down: swallow the click its release will send.
    const eat = () => { eatClickUntil = performance.now() + 350; window.removeEventListener('pointerup', eat, true); };
    window.addEventListener('pointerup', eat, true);
    dragEnd(false);
  }
  function onPointerUp(e) {
    if (!drag || e.pointerId !== drag.pointer) return;
    dragEnd(true);
  }
  function onPointerCancel(e) {
    if (!drag || e.pointerId !== drag.pointer) return;
    dragEnd(false);                                           // the browser took the gesture (a pen pan), or capture was lost
  }
  function dragEnd(apply) {
    const d = drag;
    if (!d) return;
    drag = null;
    if (d.raf) cancelAnimationFrame(d.raf);
    d.bar.removeEventListener('pointermove', onPointerMove);
    d.bar.removeEventListener('pointerup', onPointerUp);
    d.bar.removeEventListener('pointercancel', onPointerCancel);
    d.bar.removeEventListener('lostpointercapture', onPointerCancel);
    document.removeEventListener('keydown', onDragKey, true);
    try { d.bar.releasePointerCapture(d.pointer); } catch (err) { /* already released */ }
    d.bar.classList.remove('is-dragging');
    root.classList.remove('pm-g-moving', 'pm-g-resizing');
    tip.hidden = true;
    const t = PM.taskById(d.id);
    if (apply && !d.moved) {
      // A click without movement: open the task (and swallow the click event).
      eatClickUntil = performance.now() + 350;
      if (pending) draw(true);
      openTask(d.id);
      return;
    }
    if (apply) eatClickUntil = performance.now() + 350;
    const same = d.nb === d.begin && d.nf === d.finish;
    if (!apply || same || !t || !PM.can.edit) {
      // Cancelled, or dropped where it started: back to where it was.
      d.bar.style.left = d.left0;
      d.bar.style.width = d.width0;
      if (d.hadOpt) { commit(d.id); return; }                 // keyboard moves made just before the drag still save
      draw(pending);
      return;
    }
    optimistic.set(d.id, toFields(t, d.nb, d.nf));
    commit(d.id);
  }

  /* ═══ Tab wiring ═══════════════════════════════════════════════════════ */
  PM.on('state', () => {
    confCursor = -1;
    draw(false);
  });

  PM.registerTab('timeline', {
    mount,
    show(params) {
      // Some browsers forget a hidden box's scroll position: put it back.
      if (parked && !scroller.hidden) { scroller.scrollLeft = parked.left; scroller.scrollTop = parked.top; }
      parked = null;
      fitHeight();
      draw(stale || firstDraw);
      fitHeight();
      if (params && params.task && R) reveal(+params.task);
    },
    hide() {
      if (drag) dragEnd(false);
      flushKbd();
      parked = scroller.hidden ? null : {left: scroller.scrollLeft, top: scroller.scrollTop};
      if (window.ToolsMenu && addWrap && addWrap.querySelector('[aria-expanded="true"]')) window.ToolsMenu.close(false);
    },
  });
})();
