/*
  3·2·1→Theater — Home day timeline (3.35.0).
  © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
  MIT Licensed — see LICENSE for details.

  Draws GET /api/timeline into #tl-scroll: a time axis on the LEFT (sticky
  while scrolling sideways), one lane per show/event side by side (lane
  headers sticky at the top), each with a Schedule and a Crew sub-column,
  plus an Overhead & Project Crew lane. Every time from the server is
  minutes from the viewed day's midnight (next-day items run past 1440).

  Rules (see CLAUDE.md → "Home day timeline"):
  - ONE interval (30 s): moves the NOW line and refetches once a minute while
    the tab is visible; stopped while hidden and on pagehide.
  - Fetches are sequence-guarded (and the previous one aborted), so a slow
    response can never paint over a newer day. render() / updateNow() draw
    only when `data` is the picked day (dayShown()), so a zoom / venue chip /
    resize while a new day loads can't relabel the old one.
  - All user text goes in with textContent / attributes — never innerHTML.
  - Listeners are delegated on #tl-scroll (added once), so re-rendering never
    stacks handlers.
  - "Today" / the NOW line come from the BROWSER clock (localTodayIso()).
*/
(function () {
  'use strict';
  const root = document.getElementById('tl-root');
  const scroller = document.getElementById('tl-scroll');
  if (!root || !scroller) return;

  const $ = (id) => document.getElementById(id);
  const ctxEl = $('tl-context'), statusEl = $('tl-status'), emptyEl = $('tl-empty');
  const emptyText = $('tl-empty-text'), venuesEl = $('tl-venues'), tip = $('tl-tip');
  const dateInput = $('tl-date'), testCb = $('tl-include-test'), testCount = $('tl-test-count');
  const prevBtn = $('tl-prev'), nextBtn = $('tl-next'), todayBtn = $('tl-today');
  const zoomOutBtn = $('tl-zoom-out'), zoomInBtn = $('tl-zoom-in');

  const IS_MOBILE = document.documentElement.classList.contains('mobile-view');
  const ZOOMS = [36, 48, 60, 72, 96, 128];             // px per hour
  const AXIS_W = IS_MOBILE ? 46 : 60;
  // Sub-column widths: a base, then grown to fit side-by-side (overlapping) items.
  const W = IS_MOBILE
    ? { sched: 96, schedCol: 84, schedEmpty: 40, crew: 58, crewCol: 32, crewEmpty: 30, pad: 4, max: 420 }
    : { sched: 190, schedCol: 128, schedEmpty: 70, crew: 112, crewCol: 40, crewEmpty: 44, pad: 6, max: 1100 };
  const RANGE_START = 6 * 60, RANGE_END = 24 * 60, RANGE_CAP = 30 * 60;
  const MIN_ITEM_PX = 18;
  const TICK_MS = 30000, REFRESH_MS = 60000;
  const LS_ZOOM = 'home_tl_zoom', LS_VENUES = 'home_tl_venues', LS_TEST = 'home_tl_test';
  const ISO_RE = /^\d{4}-\d{2}-\d{2}$/;

  // ── small helpers ─────────────────────────────────────────────────────
  function lsGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (_) { /* private mode */ } }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null && text !== '') e.textContent = text;
    return e;
  }
  function isoAdd(iso, n) {
    const p = iso.split('-').map(Number);
    return localIsoDate(new Date(p[0], p[1] - 1, p[2] + n));
  }
  function nowMinutes() { const d = new Date(); return d.getHours() * 60 + d.getMinutes(); }
  function hhmm(m) {
    const r = ((Math.round(m) % 1440) + 1440) % 1440;
    return String(Math.floor(r / 60)).padStart(2, '0') + ':' + String(r % 60).padStart(2, '0');
  }
  function venueKey(s) { return s.venue || 'Unassigned'; }
  // PM contact links use the ONE shared rule (app.js LaborDays.telHref /
  // mailtoHref = the Jinja tel_href / mailto_href filters), so a number with
  // an extension dials the same here as on the show page. '' = no link.
  function telHref(p) { return window.LaborDays && LaborDays.telHref ? LaborDays.telHref(p) : ''; }
  function mailHref(e) { return window.LaborDays && LaborDays.mailtoHref ? LaborDays.mailtoHref(e) : ''; }

  // ── state ─────────────────────────────────────────────────────────────
  const initial = root.dataset.initialDate || '';
  let curDate = ISO_RE.test(initial) ? initial : localTodayIso();
  // Follow the calendar: a page opened without ?date= moves on to the new
  // day by itself (a wall display left on Home overnight), until the user
  // picks another day.
  let followToday = !ISO_RE.test(initial) || initial === localTodayIso();
  let includeTest = lsGet(LS_TEST) === '1';
  let zoomIdx = parseInt(lsGet(LS_ZOOM), 10);
  if (!(zoomIdx >= 0 && zoomIdx < ZOOMS.length)) zoomIdx = IS_MOBILE ? 2 : 3;
  const selVenues = new Set();
  try {
    const v = JSON.parse(lsGet(LS_VENUES) || '[]');
    if (Array.isArray(v)) v.slice(0, 50).forEach(x => { if (typeof x === 'string' && x) selVenues.add(x.slice(0, 200)); });
  } catch (_) { /* ignore a bad value */ }

  let data = null, dataSig = '';
  let fetchSeq = 0, ctrl = null, lastFetch = 0, timer = null;
  let pendingScroll = 'auto';       // 'auto' (now / first item) | null (keep)
  let geom = null;                  // {start, end, ppm} of the current render
  let nowEl = null, headEl = null;
  const itemInfo = new Map();       // data-k → tooltip details (rebuilt per render)

  // ── data ──────────────────────────────────────────────────────────────
  function setStatus(msg, isError) {
    statusEl.textContent = msg || '';
    statusEl.classList.toggle('is-error', !!isError);
  }

  function load(opts) {
    opts = opts || {};
    const seq = ++fetchSeq;
    if (ctrl) { try { ctrl.abort(); } catch (_) { /* already done */ } }
    ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null;
    if (!opts.silent) setStatus('Loading…');
    const url = '/api/timeline?date=' + encodeURIComponent(curDate) + '&include_test=' + (includeTest ? '1' : '0');
    fetch(url, {
      credentials: 'same-origin', cache: 'no-store', signal: ctrl ? ctrl.signal : undefined,
      headers: { 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
    }).then(r => {
      const ct = r.headers.get('Content-Type') || '';
      if (ct.indexOf('application/json') < 0) {
        throw new Error(r.ok ? 'Your session may have ended — reload the page to sign in again.'
                             : 'Could not load the day (HTTP ' + r.status + ').');
      }
      return r.json().then(j => ({ ok: r.ok, j: j }));
    }).then(res => {
      if (seq !== fetchSeq) return;
      lastFetch = Date.now();
      if (!res.ok || !res.j || !res.j.success) throw new Error((res.j && res.j.error) || 'Could not load the day.');
      setStatus('');
      const sig = JSON.stringify(res.j);
      if (sig === dataSig && !opts.force) { updateNow(); return; }
      dataSig = sig;
      data = res.j;
      render();
    }).catch(err => {
      if (seq !== fetchSeq || (err && err.name === 'AbortError')) return;
      lastFetch = Date.now();
      setStatus((err && err.message) || 'Could not load the day.', true);
      // A failed day change must not leave the previous day's grid under the
      // new date (a failed refresh of the SAME day keeps the last good view).
      if (!dayShown()) {
        data = null;
        hideTip();
        testCount.textContent = '';   // nor its '(N hidden)' count or venue chips
        renderVenueChips();
        showEmpty('This day could not be loaded. It will try again in a minute.');
        renderContext();
      }
    });
  }

  // The grid on screen belongs to `data`. While another day loads (go() has
  // moved curDate), nothing may redraw it: a zoom / venue chip / resize would
  // paint the old day under the new date and use up the new day's auto-scroll.
  function dayShown() { return !!data && data.date === curDate; }

  function go(iso, opts) {
    if (!ISO_RE.test(iso)) return;
    curDate = iso;
    followToday = (opts && opts.follow) || iso === localTodayIso();
    pendingScroll = 'auto';
    dataSig = '';
    syncControls();
    try {
      const u = new URL(location.href);
      if (iso === localTodayIso()) u.searchParams.delete('date'); else u.searchParams.set('date', iso);
      history.replaceState(history.state, '', u.pathname + u.search + u.hash);
    } catch (_) { /* old browser — the URL just won't follow */ }
    load();
  }

  function syncControls() {
    if (document.activeElement !== dateInput) dateInput.value = curDate;
    todayBtn.disabled = curDate === localTodayIso();
    testCb.checked = includeTest;
    zoomOutBtn.disabled = zoomIdx <= 0;
    zoomInBtn.disabled = zoomIdx >= ZOOMS.length - 1;
  }

  // ── layout ────────────────────────────────────────────────────────────
  // Pack overlapping items into side-by-side columns (calendar style): each
  // item gets _col / _ncol; returns the most columns any overlap needed.
  function pack(items) {
    const sorted = items.slice().sort((a, b) => a._vs - b._vs || b._ve - a._ve);
    let cluster = [], colsEnd = [], clusterEnd = -Infinity, most = 0;
    const flush = () => {
      cluster.forEach(it => { it._ncol = colsEnd.length; });
      most = Math.max(most, colsEnd.length);
      cluster = []; colsEnd = []; clusterEnd = -Infinity;
    };
    sorted.forEach(it => {
      if (cluster.length && it._vs >= clusterEnd) flush();
      let c = colsEnd.findIndex(e => e <= it._vs);
      if (c < 0) { c = colsEnd.length; colsEnd.push(it._ve); } else colsEnd[c] = it._ve;
      it._col = c;
      cluster.push(it);
      clusterEnd = Math.max(clusterEnd, it._ve);
    });
    if (cluster.length) flush();
    return most;
  }

  // The axis stops at RANGE_CAP (06:00 next day), so an item starting at or
  // after it ('1+07:00', '2+00:30') has nowhere to go on this day — it would
  // be drawn below the grid. Left off the grid and listed in the lane header
  // instead (laterList): the next day's view carries only the day before's
  // rows, so a '2+06:00'+ row would otherwise show on no day at all. (Crew
  // bars can't get here: the server drops in-times past midnight.)
  function onAxis(list) { return (list || []).filter(i => i.start < RANGE_CAP); }
  function offAxis(list) { return (list || []).filter(i => i.start >= RANGE_CAP); }

  function visibleLanes() {
    const shows = (data && data.shows) || [];
    const lanes = shows.filter(s => selVenues.size === 0 || selVenues.has(venueKey(s)))
      .map(s => ({ kind: 'show', show: s, sched: onAxis(s.schedule), crew: onAxis(s.crew) }));
    lanes.sort((a, b) => {
      const sa = a.show, sb = b.show;
      if (sa.is_test !== sb.is_test) return sa.is_test ? 1 : -1;
      const va = venueKey(sa).toLowerCase(), vb = venueKey(sb).toLowerCase();
      if (va !== vb) {
        if (!sa.venue) return 1;
        if (!sb.venue) return -1;
        return va < vb ? -1 : 1;
      }
      const fa = firstStart(a), fb = firstStart(b);
      return fa - fb || sa.name.localeCompare(sb.name);
    });
    if (data && data.overhead && selVenues.size === 0) {
      lanes.push({ kind: 'oh', oh: data.overhead, sched: [], crew: onAxis(data.overhead.crew) });
    }
    return lanes;
  }
  function firstStart(lane) {
    let m = Infinity;
    lane.sched.forEach(i => { m = Math.min(m, i.start); });
    lane.crew.forEach(i => { m = Math.min(m, i.start); });
    return m;
  }

  function computeRange(lanes) {
    let lo = RANGE_START, hi = RANGE_END;
    lanes.forEach(l => {
      l.sched.concat(l.crew).forEach(i => { lo = Math.min(lo, i.start); hi = Math.max(hi, i.end); });
      if (l.kind === 'show') (l.show.performances || []).forEach(p => {
        if (p.minutes != null) { lo = Math.min(lo, p.minutes); hi = Math.max(hi, p.minutes + 30); }
      });
    });
    lo = Math.max(0, Math.floor(lo / 60) * 60);
    hi = Math.min(RANGE_CAP, Math.ceil(hi / 60) * 60);
    if (hi <= lo) hi = Math.min(RANGE_CAP, lo + 60);
    return { start: lo, end: hi };
  }

  // ── render ────────────────────────────────────────────────────────────
  // 'Today · Sunday, November 1 · 3 happening'. The relative prefix follows
  // the browser clock, so tick() refreshes it too (midnight passes between
  // data changes). With no data (a failed load) it names the picked day.
  function dayLabel(iso) {
    const p = iso.split('-').map(Number);
    try { return new Date(p[0], p[1] - 1, p[2]).toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' }); }
    catch (_) { return iso; }
  }
  function renderContext() {
    const today = localTodayIso();
    const rel = curDate === today ? 'Today · '
      : curDate === isoAdd(today, -1) ? 'Yesterday · '
      : curDate === isoAdd(today, 1) ? 'Tomorrow · ' : '';
    let t = null;
    if (dayShown()) {
      const n = (data.shows || []).length;
      t = rel + data.label + ' · ' + (n ? n + ' happening' : 'nothing scheduled');
    } else if (!data) {
      t = rel + dayLabel(curDate);
    }
    // aria-live region: write only a real change, or every tick re-announces it.
    if (t != null && ctxEl.textContent !== t) ctxEl.textContent = t;
  }

  function showEmpty(msg) {
    scroller.hidden = true;
    emptyEl.hidden = false;
    emptyText.textContent = msg;
    geom = null; nowEl = null;
    itemInfo.clear();
    setHead(null);
  }

  function render() {
    if (!dayShown()) { syncControls(); return; }
    hideTip();
    syncControls();
    renderVenueChips();
    renderContext();
    const n = (data.shows || []).length;
    testCount.textContent = (!includeTest && data.test_hidden) ? ' (' + data.test_hidden + ' hidden)' : '';

    const lanes = visibleLanes();
    if (!lanes.length) {
      let msg = 'Nothing on the schedule for ' + data.label + '.';
      if (n && selVenues.size) msg = 'Nothing at the selected venue' + (selVenues.size === 1 ? '' : 's') + ' on ' + data.label + '.';
      if (data.test_hidden && !includeTest) msg += ' (' + data.test_hidden + ' test show' + (data.test_hidden === 1 ? '' : 's') + ' hidden.)';
      showEmpty(msg);
      return;
    }
    emptyEl.hidden = true;
    const wasHidden = scroller.hidden;
    scroller.hidden = false;

    // Keep the view steady across a refresh: remember scroll + focus.
    const prevGeom = geom;
    const centerMin = prevGeom && !wasHidden
      ? prevGeom.start + (scroller.scrollTop + scroller.clientHeight / 2) / prevGeom.ppm : null;
    const ae = document.activeElement;
    const focusKey = ae && scroller.contains(ae) && ae.dataset ? ae.dataset.k : null;
    const scrollLeft = scroller.scrollLeft;

    const range = computeRange(lanes);
    const ppm = ZOOMS[zoomIdx] / 60;
    geom = { start: range.start, end: range.end, ppm: ppm };
    const bodyH = Math.round((range.end - range.start) * ppm);
    const minMin = MIN_ITEM_PX / ppm;

    // Visual extents + packing → natural lane widths.
    lanes.forEach(l => {
      [l.sched, l.crew].forEach(list => list.forEach(it => {
        it._vs = Math.max(it.start, range.start);
        it._ve = Math.max(Math.min(it.end, range.end), it._vs + minMin);
      }));
      const sc = pack(l.sched), cc = pack(l.crew);
      l.sw = l.kind === 'oh' ? 0 : (l.sched.length ? Math.max(W.sched, sc * W.schedCol) : W.schedEmpty);
      l.cw = l.crew.length ? Math.max(W.crew, cc * W.crewCol) : W.crewEmpty;
      if (l.kind === 'oh') l.cw = Math.max(l.cw, W.sched);
      l.w = l.sw + l.cw + W.pad * (l.sw ? 3 : 2);
    });
    // Few lanes: widen them to fill the box (capped), keeping the split.
    const avail = scroller.clientWidth - AXIS_W - 2;
    const total = lanes.reduce((a, l) => a + l.w, 0);
    if (avail > total) {
      const extra = (avail - total) / lanes.length;
      lanes.forEach(l => {
        const grow = Math.max(0, Math.min(extra, W.max - l.w));
        if (grow <= 0) return;
        const inner = l.sw + l.cw;
        l.sw += l.sw ? grow * l.sw / inner : 0;
        l.cw += l.sw ? grow * l.cw / inner : grow;
        l.w += grow;
      });
    }
    lanes.forEach(l => { l.sw = Math.floor(l.sw); l.cw = Math.floor(l.cw); l.w = l.sw + l.cw + W.pad * (l.sw ? 3 : 2); });

    itemInfo.clear();
    const inner = el('div', 'tl-inner');
    inner.style.width = (AXIS_W + lanes.reduce((a, l) => a + l.w, 0)) + 'px';
    inner.style.setProperty('--tl-axis-w', AXIS_W + 'px');

    // Header row: corner + one header per lane (sticky top).
    const head = el('div', 'tl-headrow');
    const corner = el('div', 'tl-corner');
    corner.style.width = AXIS_W + 'px';
    head.appendChild(corner);
    lanes.forEach(l => head.appendChild(laneHead(l)));

    // Body: axis + gridlines + lanes + NOW line.
    const body = el('div', 'tl-bodyrow');
    body.style.height = bodyH + 'px';
    const axis = el('div', 'tl-axis');
    axis.style.width = AXIS_W + 'px';
    axis.style.height = bodyH + 'px';
    for (let m = range.start; m <= range.end; m += 30) {
      const y = Math.round((m - range.start) * ppm);
      const line = el('div', 'tl-hline' + (m % 60 ? ' tl-half' : '') + (m === 1440 ? ' tl-midnight' : ''));
      line.style.top = y + 'px';
      body.appendChild(line);
      if (m % 60 === 0 && m < range.end) {
        const lab = el('div', 'tl-hour' + (m >= 1440 ? ' tl-hour-next' : ''), hhmm(m));
        lab.style.top = y + 'px';
        if (m === 1440) {
          lab.classList.add('tl-hour-midnight');
          lab.title = 'Midnight — ' + isoAdd(curDate, 1);
        }
        axis.appendChild(lab);
      }
    }
    body.appendChild(axis);
    lanes.forEach((l, idx) => body.appendChild(laneBody(l, idx, ppm, range)));
    nowEl = el('div', 'tl-now');
    nowEl.appendChild(el('span', 'tl-now-label'));
    nowEl.hidden = true;
    body.appendChild(nowEl);

    inner.appendChild(head);
    inner.appendChild(body);
    scroller.textContent = '';
    scroller.appendChild(inner);
    setHead(head);
    syncHeadHeight();
    fitHeight();
    updateNow();

    // Scroll: first paint of a day → now (today) or the first item; a refresh
    // or zoom keeps the same time in the middle of the box.
    if (pendingScroll === 'auto') {
      const nm = nowOnView();
      let target;
      if (nm != null && nm >= range.start && nm <= range.end) {
        target = (nm - range.start) * ppm - scroller.clientHeight / 3;
      } else {
        const f = Math.min.apply(null, lanes.map(firstStart));
        target = isFinite(f) ? (Math.max(f, range.start) - range.start) * ppm - 24 : 0;
      }
      scroller.scrollTop = Math.max(0, target);
      scroller.scrollLeft = 0;
      pendingScroll = null;
    } else {
      if (centerMin != null) scroller.scrollTop = Math.max(0, (centerMin - range.start) * ppm - scroller.clientHeight / 2);
      scroller.scrollLeft = scrollLeft;
    }
    if (focusKey) {
      const f = scroller.querySelector('[data-k="' + CSS.escape(focusKey) + '"]');
      if (f) { try { f.focus({ preventScroll: true }); } catch (_) { f.focus(); } }
    }
  }

  function badge(cls, text, title) {
    const b = el('span', cls, text);
    if (title) b.title = title;
    return b;
  }

  function laneHead(l) {
    const h = el('div', 'tl-lanehead');
    h.style.width = l.w + 'px';
    if (l.kind === 'oh') {
      h.classList.add('tl-lane-oh');
      h.dataset.href = '/overhead-crew';
      const a = el('a', 'tl-lh-name', 'Overhead & Project Crew');
      a.href = '/overhead-crew';
      h.appendChild(a);
      const pj = el('div', 'tl-lh-projects');
      (l.oh.projects || []).slice(0, 12).forEach(p => {
        const c = el('span', 'tl-proj', p.name);
        if (p.color) c.style.setProperty('--tl-proj', p.color);
        pj.appendChild(c);
      });
      h.appendChild(pj);
      untimedList(h, [], l.oh.crew_untimed || [], null);
      h.appendChild(colLabels(l));
      return h;
    }
    const s = l.show;
    laneVariant(h, s);
    h.dataset.href = '/shows/' + s.id;
    const top = el('div', 'tl-lh-top');
    const a = el('a', 'tl-lh-name', s.name);
    a.href = '/shows/' + s.id + '?tab=advance';
    top.appendChild(a);
    h.appendChild(top);

    const badges = el('div', 'tl-lh-badges');
    if (s.day_num && s.day_total > 1) badges.appendChild(badge('tl-badge tl-badge-day', 'DAY ' + s.day_num + '/' + s.day_total,
      'Day ' + s.day_num + ' of ' + s.day_total + ' of this show\u2019s schedule'));
    if (s.show_mode === 'event') badges.appendChild(badge('event-badge', 'EVENT', 'Event mode'));
    if (s.is_test) badges.appendChild(badge('test-badge', 'TEST', 'Test / demo — excluded from reports'));
    if (s.prism_status) badges.appendChild(badge('prism-tag prism-tag-' + String(s.prism_status).toLowerCase().replace(/[^a-z]/g, ''),
      String(s.prism_status).toUpperCase(), 'Prism event status'));
    if (s.dark) badges.appendChild(badge('tl-badge tl-badge-dark', 'DARK', 'Marked dark on the Schedule tab — left off the schedule PDF'));
    if (s.status === 'archived') badges.appendChild(badge('past-badge', 'ARCHIVED', 'This show is archived'));
    if (badges.childNodes.length) h.appendChild(badges);

    h.appendChild(el('div', 'tl-lh-venue', s.venue || 'No venue'));
    const pm = el('div', 'tl-lh-pm');
    if (s.pm) {
      pm.appendChild(el('span', 'tl-lh-label', 'PM'));
      const nm = el('span', 'tl-lh-pmname', s.pm.name);
      nm.title = [s.pm.name, s.pm.title, s.pm.phone, s.pm.email].filter(Boolean).join(' · ');
      pm.appendChild(nm);
      if (s.pm.is_cover) pm.appendChild(badge('tl-cover', 'cover', 'Covering PM for this day'));
      const tel = telHref(s.pm.phone), mail = mailHref(s.pm.email);
      if (tel) pm.appendChild(contactLink(tel, 'Call ' + s.pm.name + ' (' + s.pm.phone + ')', 'phone'));
      if (mail) pm.appendChild(contactLink(mail, 'Email ' + s.pm.name + ' (' + s.pm.email + ')', 'mail'));
    } else {
      pm.appendChild(el('span', 'tl-lh-label', 'PM'));
      pm.appendChild(el('em', 'tl-lh-none', 'none set'));
    }
    h.appendChild(pm);
    const perfs = (s.performances || []).filter(p => p.label);
    if (perfs.length) {
      const pf = el('div', 'tl-lh-perf');
      pf.appendChild(el('span', 'tl-lh-label', s.show_mode === 'event' ? 'Event' : 'Perf'));
      pf.appendChild(el('span', 'mono', perfs.map(p => p.label).join(', ')));
      h.appendChild(pf);
    }
    untimedList(h, s.schedule_untimed || [], s.crew_untimed || [], s);
    laterList(h, offAxis(s.schedule), s);
    h.appendChild(colLabels(l));
    return h;
  }

  function laneVariant(node, s) {
    if (s.show_mode === 'event') node.classList.add('tl-lane-event');
    if (s.is_test) node.classList.add('tl-lane-test');
    if (s.dark) node.classList.add('tl-lane-dark');
  }

  const ICONS = {
    phone: 'M22 16.9v3a2 2 0 01-2.2 2 19.8 19.8 0 01-8.6-3.1 19.5 19.5 0 01-6-6A19.8 19.8 0 012.1 4.2 2 2 0 014.1 2h3a2 2 0 012 1.7c.1.9.4 1.8.7 2.7a2 2 0 01-.5 2.1L8 9.8a16 16 0 006 6l1.3-1.3a2 2 0 012.1-.4c.9.3 1.8.6 2.7.7a2 2 0 011.7 2z',
    mail: 'M4 4h16a2 2 0 012 2v12a2 2 0 01-2 2H4a2 2 0 01-2-2V6a2 2 0 012-2zM22 6l-10 7L2 6',
  };
  function contactLink(href, label, icon) {
    const a = el('a', 'tl-contact');
    a.href = href;
    a.title = label;
    a.setAttribute('aria-label', label);
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', ICONS[icon]);
    svg.appendChild(path);
    a.appendChild(svg);
    return a;
  }

  function untimedList(h, sched, crew, s) {
    if (!sched.length && !crew.length) return;
    const d = el('details', 'tl-untimed');
    const n = sched.length + crew.length;
    d.appendChild(el('summary', null, n + ' without a time'));
    const ul = el('ul');
    sched.forEach(r => {
      const li = el('li');
      const a = el('a', null, [r.start_text, r.desc || r.notes].filter(Boolean).join(' · ') || '(blank)');
      a.href = '/shows/' + s.id + '?tab=schedule&focus=sched:' + r.id;
      li.appendChild(a);
      ul.appendChild(li);
    });
    crew.forEach(r => {
      const li = el('li');
      const who = (r.position && r.position !== '—' ? r.position + ' · ' : '') + r.tech;
      const a = el('a', null, 'Crew: ' + who + (r.in_text ? ' (' + r.in_text + ')' : ''));
      a.href = s ? '/shows/' + s.id + '?tab=staffing' : '/overhead-crew';
      li.appendChild(a);
      ul.appendChild(li);
    });
    d.appendChild(ul);
    h.appendChild(d);
  }

  // Schedule rows past the end of the axis (see onAxis) — same look as the
  // 'without a time' list, each linking to its row on the Schedule tab.
  function laterList(h, rows, s) {
    if (!rows.length) return;
    const d = el('details', 'tl-untimed');
    d.appendChild(el('summary', null, rows.length + ' after ' + hhmm(RANGE_CAP) + ' next day'));
    const ul = el('ul');
    rows.forEach(r => {
      const li = el('li');
      const a = el('a', null, [r.start_label, r.desc || r.notes].filter(Boolean).join(' · ') || '(blank)');
      a.href = '/shows/' + s.id + '?tab=schedule&focus=sched:' + r.id;
      li.appendChild(a);
      ul.appendChild(li);
    });
    d.appendChild(ul);
    h.appendChild(d);
  }

  function colLabels(l) {
    const row = el('div', 'tl-lh-cols');
    if (l.sw) {
      const a = el('span', null, 'Schedule');
      a.style.width = l.sw + 'px';
      row.appendChild(a);
    }
    const b = el('span', null, 'Crew');
    b.style.width = l.cw + 'px';
    row.appendChild(b);
    return row;
  }

  function laneBody(l, idx, ppm, range) {
    const lane = el('div', 'tl-lane');
    lane.style.width = l.w + 'px';
    if (l.kind === 'oh') lane.classList.add('tl-lane-oh');
    else laneVariant(lane, l.show);
    if (l.sw) {
      const col = el('div', 'tl-col tl-col-sched');
      col.style.width = l.sw + 'px';
      l.sched.forEach(it => col.appendChild(schedItem(l.show, it, ppm, range, l.sw)));
      lane.appendChild(col);
    }
    const ccol = el('div', 'tl-col tl-col-crew');
    ccol.style.width = l.cw + 'px';
    l.crew.forEach(it => ccol.appendChild(crewItem(l, it, ppm, range, l.cw)));
    if (!l.crew.length) ccol.appendChild(el('span', 'tl-col-none', 'no crew'));
    lane.appendChild(ccol);
    if (l.kind === 'show') {
      (l.show.performances || []).forEach(p => {
        if (p.minutes == null || p.minutes < range.start || p.minutes > range.end) return;
        const m = el('div', 'tl-perf');
        m.style.top = Math.round((p.minutes - range.start) * ppm) + 'px';
        m.title = (l.show.show_mode === 'event' ? 'Event start ' : 'Performance ') + p.label;
        lane.appendChild(m);
      });
    }
    return lane;
  }

  function place(node, it, ppm, range, colW, vertAt) {
    const top = Math.round((it._vs - range.start) * ppm);
    const h = Math.max(MIN_ITEM_PX, Math.round((it._ve - it._vs) * ppm) - 1);
    node.style.top = top + 'px';
    node.style.height = h + 'px';
    const n = it._ncol || 1;
    node.style.left = 'calc(' + (100 * it._col / n) + '% + 1px)';
    node.style.width = 'calc(' + (100 / n) + '% - 2px)';
    node.dataset.end = String(it.t_end);
    if (h < 34) node.classList.add('tl-short');
    // Narrow + tall: the label runs down the bar (vertical text).
    else if (colW / n < vertAt && h >= 72) node.classList.add('tl-vert');
    if (it.t_start < it.start || it.start < range.start || it.end > range.end) node.classList.add('tl-clipped');
  }

  // The label rides in one wrapper that sticks under the lane headers, so a
  // long block stays labelled while it's scrolled past.
  function label(parts) {
    const w = el('span', 'tl-item-label');
    parts.forEach(p => { if (p[1]) w.appendChild(el('span', p[0], p[1])); });
    return w;
  }

  function schedItem(s, it, ppm, range, colW) {
    const a = el('a', 'tl-item tl-sched no-prefetch');
    const k = 's' + s.id + ':' + it.id + (it.from_prev ? 'p' : '');
    a.dataset.k = k;
    a.href = '/shows/' + s.id + '?tab=schedule&focus=sched:' + it.id;
    if (it.point) a.classList.add('tl-point');
    if (it.from_prev) a.classList.add('tl-prev');
    if (it.piano) a.classList.add('tl-piano');
    const times = it.point ? it.start_label : it.start_label + '–' + it.end_label;
    a.appendChild(label([['tl-item-time', times], ['tl-item-desc', it.desc || it.notes || '(no description)']]));
    place(a, it, ppm, range, colW, 64);
    a.setAttribute('aria-label', s.name + ': ' + times + ' ' + (it.desc || ''));
    itemInfo.set(k, { kind: 'sched', show: s, it: it });
    return a;
  }

  function crewItem(l, it, ppm, range, colW) {
    const a = el('a', 'tl-item tl-crew no-prefetch');
    const oh = l.kind === 'oh';
    const k = (oh ? 'o' : 'c' + l.show.id) + ':' + it.id + (it.from_prev ? 'p' : '');
    a.dataset.k = k;
    a.href = oh ? '/overhead-crew' : '/shows/' + l.show.id + '?tab=staffing';
    if (it.training) a.classList.add('tl-training');
    if (it.tech_state === 'unfilled') a.classList.add('tl-unfilled');
    else if (it.tech_state === 'requested' || !it.scheduled) a.classList.add('tl-requested');
    if (it.from_prev) a.classList.add('tl-prev');
    if (it.open_end) a.classList.add('tl-open');
    if (oh && it.color && /^#[0-9a-fA-F]{3,8}$/.test(it.color)) a.style.setProperty('--tl-accent', it.color);
    place(a, it, ppm, range, colW, 96);
    // Lunches: hatched spans placed against the bar's DRAWN extent.
    const vs = it._vs, span = Math.max(1, it._ve - it._vs);
    (it.breaks || []).forEach(b => {
      const bs = Math.max(b.start, vs), be = Math.min(b.end, it._ve);
      if (be <= bs) return;
      const br = el('span', 'tl-break');
      br.style.top = (100 * (bs - vs) / span) + '%';
      br.style.height = (100 * (be - bs) / span) + '%';
      a.appendChild(br);
    });
    const times = it.start_label + (it.open_end ? ' →' : '–' + it.end_label);
    a.appendChild(label([['tl-item-time', times],
      ['tl-item-desc', oh ? it.project + ' · ' + it.position : it.position], ['tl-item-sub', it.tech]]));
    a.setAttribute('aria-label', (oh ? it.project : l.show.name) + ': ' + it.position + ', ' + it.tech + ', ' + times);
    itemInfo.set(k, { kind: oh ? 'oh' : 'crew', show: oh ? null : l.show, it: it });
    return a;
  }

  // Labels stick just under the lane headers; their height changes when a
  // "without a time" list is opened.
  function setHead(h) {          // the observed header row (old one released)
    if (headRO && headEl) headRO.unobserve(headEl);
    headEl = h;
    if (headRO && h) headRO.observe(h);
  }
  function syncHeadHeight() {
    if (headEl && headEl.parentNode) headEl.parentNode.style.setProperty('--tl-head-h', headEl.offsetHeight + 'px');
  }

  // ── NOW line ──────────────────────────────────────────────────────────
  // Minutes on the viewed day's axis for the browser's "now": today → now,
  // the day before today → now + 24 h (its after-midnight hours), else none.
  function nowOnView() {
    const today = localTodayIso();
    if (curDate === today) return nowMinutes();
    if (isoAdd(curDate, 1) === today) return nowMinutes() + 1440;
    return null;
  }

  function updateNow() {
    if (!geom || !nowEl || !dayShown()) return;   // the grid is still the previous day
    const today = localTodayIso();
    const nm = nowOnView();
    const shown = nm != null && nm >= geom.start && nm <= geom.end;
    nowEl.hidden = !shown;
    if (shown) {
      nowEl.style.top = Math.round((nm - geom.start) * geom.ppm) + 'px';
      nowEl.firstChild.textContent = 'NOW ' + hhmm(nm);
    }
    // Finished items fade (slate) — everything on a past day, nothing on a future one.
    const cutoff = nm != null ? nm : (curDate < today ? Infinity : -Infinity);
    scroller.querySelectorAll('.tl-item').forEach(n => {
      n.classList.toggle('tl-past', Number(n.dataset.end) <= cutoff);
    });
  }

  // ── venue chips ───────────────────────────────────────────────────────
  function renderVenueChips() {
    const counts = new Map();
    ((data && data.shows) || []).forEach(s => counts.set(venueKey(s), (counts.get(venueKey(s)) || 0) + 1));
    selVenues.forEach(v => { if (!counts.has(v)) counts.set(v, 0); });
    const names = Array.from(counts.keys()).sort((a, b) =>
      (a === 'Unassigned') - (b === 'Unassigned') || a.toLowerCase().localeCompare(b.toLowerCase()));
    venuesEl.textContent = '';
    if (names.length < 2 && !selVenues.size) { venuesEl.hidden = true; return; }
    venuesEl.hidden = false;
    const all = el('button', 'venue-pill venue-pill-all' + (selVenues.size ? '' : ' active'), 'All venues');
    all.type = 'button';
    all.dataset.venue = '';
    all.setAttribute('aria-pressed', selVenues.size ? 'false' : 'true');
    venuesEl.appendChild(all);
    names.forEach(v => {
      const on = selVenues.has(v);
      const b = el('button', 'venue-pill' + (on ? ' active' : ''), v + ' ' );
      b.type = 'button';
      b.dataset.venue = v;
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      b.title = on ? 'Showing ' + v + ' — click to drop it' : 'Show only ' + v + ' (click more venues to add them)';
      b.appendChild(el('span', 'tl-pill-count', String(counts.get(v))));
      venuesEl.appendChild(b);
    });
  }

  venuesEl.addEventListener('click', e => {
    const b = e.target.closest('.venue-pill');
    if (!b) return;
    const v = b.dataset.venue || '';
    if (!v) selVenues.clear();
    else if (selVenues.has(v)) selVenues.delete(v);
    else selVenues.add(v);
    lsSet(LS_VENUES, JSON.stringify(Array.from(selVenues)));
    // Mid day-change (or after a failed one) only the chips change; the next
    // day's render applies the filter (and keeps its auto-scroll).
    if (dayShown()) { pendingScroll = null; render(); } else renderVenueChips();
  });

  // ── tooltip ───────────────────────────────────────────────────────────
  let tipFor = null;
  function tipLine(cls, text) { if (text) tip.appendChild(el('div', cls, text)); }
  function showTip(node) {
    const info = itemInfo.get(node.dataset.k);
    if (!info) return;
    tipFor = node;
    tip.textContent = '';
    const it = info.it;
    if (info.kind === 'sched') {
      tipLine('tl-tip-title', info.show.name);
      tipLine('tl-tip-time', it.point ? it.start_label + ' (no end time)' : it.start_label + ' – ' + it.end_label);
      tipLine('tl-tip-main', it.desc);
      tipLine('tl-tip-notes', it.notes);
      if (it.piano) tipLine('tl-tip-flag', 'Piano tuning (set by the piano tuning manager)');
      if (it.from_prev) tipLine('tl-tip-flag', 'From the previous day’s schedule');
      tipLine('tl-tip-hint', 'Click to open this row on the Schedule tab');
    } else {
      tipLine('tl-tip-title', info.kind === 'oh' ? 'Overhead · ' + it.project : info.show.name);
      tipLine('tl-tip-main', it.position + ' — ' + it.tech);
      const state = it.tech_state === 'unfilled' ? 'Unfilled'
        : (it.tech_state === 'requested' ? 'Requested (not scheduled yet)' : (it.scheduled ? 'Scheduled' : 'Not confirmed'));
      tipLine('tl-tip-flag', state + (it.training ? ' · training shift' : ''));
      tipLine('tl-tip-time', 'In ' + it.start_label + (it.open_end ? ' (no out time)' : ' → Out ' + it.end_label));
      (it.breaks || []).forEach((b, i) => tipLine('tl-tip-notes', 'Lunch' + ((it.breaks.length > 1) ? ' ' + (i + 1) : '') + ' ' + b.label));
      if (it.from_prev) tipLine('tl-tip-flag', 'Shift started the previous day');
      tipLine('tl-tip-hint', info.kind === 'oh' ? 'Click to open Overhead & Project Crew' : 'Click to open the show’s labor');
    }
    tip.hidden = false;
    const r = node.getBoundingClientRect();
    const tw = tip.offsetWidth, th = tip.offsetHeight;
    let x = r.right + 8;
    if (x + tw > window.innerWidth - 8) x = Math.max(8, r.left - tw - 8);
    const sr = scroller.getBoundingClientRect();
    let y = Math.max(r.top, sr.top + (headEl ? headEl.offsetHeight : 0));
    y = Math.min(y, window.innerHeight - th - 8);
    tip.style.left = Math.round(x) + 'px';
    tip.style.top = Math.round(Math.max(8, y)) + 'px';
  }
  function hideTip() { tipFor = null; tip.hidden = true; }

  scroller.addEventListener('mouseover', e => {
    const n = e.target.closest && e.target.closest('.tl-item');
    if (n && n !== tipFor) showTip(n);
  });
  scroller.addEventListener('mouseout', e => {
    const n = e.target.closest && e.target.closest('.tl-item');
    if (n && (!e.relatedTarget || !n.contains(e.relatedTarget))) hideTip();
  });
  scroller.addEventListener('focusin', e => {
    const n = e.target.closest && e.target.closest('.tl-item');
    if (n) showTip(n); else hideTip();
  });
  scroller.addEventListener('focusout', () => hideTip());
  scroller.addEventListener('scroll', () => { if (tipFor) hideTip(); }, { passive: true });
  scroller.addEventListener('toggle', syncHeadHeight, true);   // <details> in a lane header
  // The whole lane header opens the show (its links keep their own target).
  scroller.addEventListener('click', e => {
    const h = e.target.closest('.tl-lanehead');
    if (!h || e.target.closest('a, button, summary, details, input')) return;
    if (h.dataset.href) window.location.href = h.dataset.href;
  });

  // ── sizing ────────────────────────────────────────────────────────────
  // The timeline box fills the window below the header (it scrolls inside,
  // so the axis and lane headers can stick in both directions). On phones it
  // fills the space between the fixed top header and the bottom tab bar —
  // the page scrolls the Home header away to give it the whole screen.
  const mainEl = document.querySelector('.main-content');
  const layoutEl = document.querySelector('.app-layout');
  function fitHeight() {
    if (scroller.hidden) return;
    const pb = mainEl ? parseFloat(getComputedStyle(mainEl).paddingBottom) || 0 : 0;
    let avail;
    if (IS_MOBILE) {
      const pt = layoutEl ? parseFloat(getComputedStyle(layoutEl).paddingTop) || 0 : 0;
      avail = window.innerHeight - pt - pb - 8;
    } else {
      const top = scroller.getBoundingClientRect().top + window.scrollY;
      avail = window.innerHeight - top - pb;
    }
    scroller.style.maxHeight = Math.max(300, Math.floor(avail)) + 'px';
  }
  let resizeRaf = 0, lastW = window.innerWidth;
  window.addEventListener('resize', () => {
    if (resizeRaf) return;
    resizeRaf = requestAnimationFrame(() => {
      resizeRaf = 0;
      fitHeight();
      // Lanes are widened to fill the box — redo that when the width changes.
      if (window.innerWidth !== lastW && data) { lastW = window.innerWidth; render(); }
    });
  });
  // Site-message banners load after the page and push the box down; the
  // lane headers reflow when the fonts arrive. One observer for both.
  const headRO = window.ResizeObserver ? new ResizeObserver(() => { fitHeight(); syncHeadHeight(); }) : null;
  if (headRO) {
    const msgs = document.getElementById('site-msg-container');
    if (msgs) headRO.observe(msgs);
    const ph = document.querySelector('.tl-phead');
    if (ph) headRO.observe(ph);
  }

  // ── controls ──────────────────────────────────────────────────────────
  prevBtn.addEventListener('click', () => go(isoAdd(curDate, -1)));
  nextBtn.addEventListener('click', () => go(isoAdd(curDate, 1)));
  todayBtn.addEventListener('click', () => go(localTodayIso(), { follow: true }));
  dateInput.addEventListener('change', () => { if (ISO_RE.test(dateInput.value)) go(dateInput.value); });
  testCb.addEventListener('change', () => {
    includeTest = testCb.checked;
    lsSet(LS_TEST, includeTest ? '1' : '0');
    dataSig = '';
    load();
  });
  function zoom(d) {
    const z = Math.max(0, Math.min(ZOOMS.length - 1, zoomIdx + d));
    if (z === zoomIdx) return;
    zoomIdx = z;
    lsSet(LS_ZOOM, String(zoomIdx));
    if (data) render(); else syncControls();
  }
  zoomOutBtn.addEventListener('click', () => zoom(-1));
  zoomInBtn.addEventListener('click', () => zoom(1));

  // ── refresh: ONE interval, only while visible ─────────────────────────
  function tick() {
    if (document.hidden) return;
    const today = localTodayIso();
    // A page following "today" moves on to it: from the day before only once
    // the new day's view starts (06:00 — until then the old day's
    // after-midnight hours are the ones in use), from anything else (2+ days
    // behind after a sleep / hidden tab, or a clock change) at once.
    if (followToday && curDate !== today && (isoAdd(curDate, 1) !== today || nowMinutes() >= RANGE_START)) {
      go(today, { follow: true });
      return;
    }
    updateNow();
    renderContext();               // 'Today ·' becomes 'Yesterday ·' at midnight
    if (todayBtn) todayBtn.disabled = curDate === today;
    if (Date.now() - lastFetch >= REFRESH_MS - 1000) load({ silent: true });
  }
  function startTimer() { if (!timer) timer = setInterval(tick, TICK_MS); }
  function stopTimer() { if (timer) { clearInterval(timer); timer = null; } }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopTimer();
    else { startTimer(); tick(); }
  });
  window.addEventListener('pagehide', () => {
    stopTimer();
    if (ctrl) { try { ctrl.abort(); } catch (_) { /* gone */ } }
  });
  window.addEventListener('pageshow', e => { if (e.persisted) { startTimer(); tick(); } });

  syncControls();
  load();
  if (!document.hidden) startTimer();
})();
