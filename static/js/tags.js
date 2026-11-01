/*
  3·2·1→Theater — #tags on the client (3.35.0).
  © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
  MIT Licensed — see LICENSE for details.

  Loaded on every page right after app.js (base.html). Three jobs:
    1. The tag grammar + renderers (window.Tags, window.renderTaggedText):
       Tags.html(text)      escaped text, each #tag a link to /tags/<tag>
       Tags.richHtml(text)  comments: URL | #tag | @mention tokenized on the
                            RAW text, each segment escaped (never a regex over
                            escaped HTML); URLs stay plain text as before.
    2. Show page: a chip strip of #tag links under every free-text control
       that holds tags (inputs can't contain links), the header "Tags" strip
       (#show-tags-strip, from /api/shows/<id>/tags) and
    3. the deep-link handler: /shows/<id>?tab=<tab>&focus=<kind>:<ref> opens
       the tab / section / schedule day, waits for lazy tabs, scrolls to the
       spot, flashes it and drops `focus` from the URL.

  GRAMMAR — keep identical to find_module.TAG_RE / TAG_PG_PATTERN: '#' + a
  letter + [A-Za-z0-9_]* + '-[A-Za-z0-9_]+' groups, preceded by the start or
  a char NOT in [A-Za-z0-9_&/#], lowercased, ≤ 50 chars. No regex lookbehind
  anywhere in this file — Safari < 16.4 rejects the whole script.

  Chip strips are <div class="tag-chips"> siblings placed right AFTER the
  control. They hold only links: never data-key / .adv-field / .sched-cell /
  .notes-field / .field-presence-row, so no save, sync or presence code can
  read or collect them.
*/
(function () {
  'use strict';

  var TAG_MAX = 50;
  var TAG_NAME = /^[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*$/;
  function tagRe() { return /(^|[^A-Za-z0-9_&\/#])#([A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*)/g; }
  // Comments: URL first (so a '#' inside a link is never a tag), then the
  // tag rule, then the @mention look the comment renderer always had.
  function richRe() {
    return /(https?:\/\/[^\s<>"']+)|(^|[^A-Za-z0-9_&\/#])#([A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*)|@([A-Za-z0-9_][\w.'-]*?)(?=\s|$)/g;
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  }
  function href(tag) { return '/tags/' + encodeURIComponent(tag); }

  function normalize(s) {
    s = String(s == null ? '' : s).trim().replace(/^#/, '').toLowerCase();
    return s && s.length <= TAG_MAX && TAG_NAME.test(s) ? s : null;
  }

  /** Distinct lowercase tags in order of first appearance; stops after `max`
      when given. Dedupes with a prototype-less hash (not indexOf), so it
      stays linear however many tags one box holds. */
  function extract(text, max) {
    var out = [], seen = Object.create(null), re = tagRe(), m, s = String(text == null ? '' : text);
    if (s.indexOf('#') < 0) return out;
    while ((m = re.exec(s))) {
      var t = m[2].toLowerCase();
      if (t.length > TAG_MAX || seen[t]) continue;
      seen[t] = 1;
      out.push(t);
      if (max && out.length >= max) break;
    }
    return out;
  }

  /** Escaped text with every #tag a link to its tag page (no <br>; the
      callers' cells already use white-space: pre-wrap where it matters). */
  function html(text) {
    var s = String(text == null ? '' : text);
    if (s.indexOf('#') < 0) return esc(s);
    var re = tagRe(), m, pos = 0, out = '';
    while ((m = re.exec(s))) {
      var start = m.index + m[1].length;
      var t = m[2].toLowerCase();
      if (t.length > TAG_MAX) continue;
      out += esc(s.slice(pos, start)) +
        '<a class="tag-link" href="' + href(t) + '">#' + esc(m[2]) + '</a>';
      pos = start + 1 + m[2].length;
    }
    return out + esc(s.slice(pos));
  }

  function textHtml(s) { return esc(s).replace(/\n/g, '<br>'); }

  /** Comment body: URL | #tag | @mention on the raw text; newlines → <br>. */
  function richHtml(text) {
    var s = String(text == null ? '' : text);
    var re = richRe(), m, pos = 0, out = '';
    while ((m = re.exec(s))) {
      if (m[0] === '') { re.lastIndex++; continue; }
      if (m[1]) {                       // URL: plain text, as before
        out += textHtml(s.slice(pos, m.index + m[0].length));
      } else if (m[3] !== undefined) {  // #tag (m[2] = the consumed prefix char)
        var t = m[3].toLowerCase();
        out += textHtml(s.slice(pos, m.index + m[2].length));
        out += t.length <= TAG_MAX
          ? '<a class="tag-link" href="' + href(t) + '">#' + esc(m[3]) + '</a>'
          : textHtml('#' + m[3]);
      } else {                          // @mention
        out += textHtml(s.slice(pos, m.index)) +
          '<span class="comment-mention">@' + esc(m[4]) + '</span>';
      }
      pos = m.index + m[0].length;
    }
    return out + textHtml(s.slice(pos));
  }

  window.Tags = {extract: extract, normalize: normalize, html: html, richHtml: richHtml, href: href};
  window.renderTaggedText = html;

  /* ── Show page ─────────────────────────────────────────────────────────── */

  function onShowPage() {
    return !!document.getElementById('advance-form') && typeof SHOW_ID !== 'undefined' && !!SHOW_ID;
  }

  // Free-text controls that get a chip strip (the indexed sources).
  var CTRL_SEL = [
    '#advance-form textarea.adv-field[data-key]',
    '#advance-form input.adv-field[type="text"][data-key]',
    '#schedule-form .sched-meta[data-key]',
    '#postnotes-form textarea.notes-field[data-key]',
    '#show-labor-notes',
    '#labor-days-container .labor-day-notes'
  ].join(', ');

  function isSchedTextCell(el) {
    if (!el.classList || !el.classList.contains('sched-cell')) return false;
    var tr = el.closest('#schedule-form tr.schedule-row');
    if (!tr || tr.classList.contains('piano-sched-row')) return false;
    var c = tr.querySelectorAll('.sched-cell');
    return el === c[2] || el === c[3];        // Description, Notes
  }
  function isTagControl(el) {
    return !!(el && el.nodeType === 1 && (el.matches(CTRL_SEL) || isSchedTextCell(el)));
  }
  function allControls() {
    var list = Array.prototype.slice.call(document.querySelectorAll(CTRL_SEL));
    document.querySelectorAll('#schedule-form tr.schedule-row:not(.piano-sched-row)').forEach(function (tr) {
      var c = tr.querySelectorAll('.sched-cell');
      if (c[2]) list.push(c[2]);
      if (c[3]) list.push(c[3]);
    });
    return list;
  }

  var chipState = new WeakMap();   // control → {val, sig, strip}; dies with the control
  var CHIP_MAX = 30;               // chips under one control; more → a '+ more' chip

  /** Redraw one control's chip strip; returns true when it changed. */
  function refreshChips(ctrl) {
    var val = ctrl.value;
    var st = chipState.get(ctrl);
    var strip = st && st.strip;
    var placed = !strip || (strip.isConnected && strip.previousElementSibling === ctrl);
    if (st && st.val === val && placed) return false;   // untouched since the last pass
    var tags = extract(val, CHIP_MAX + 1);
    var more = tags.length > CHIP_MAX;
    if (more) tags = tags.slice(0, CHIP_MAX);
    var sig = tags.join(' ') + (more ? ' +' : '');
    if (st && st.sig === sig && placed) { st.val = val; return false; }
    if (!tags.length) {
      if (strip) strip.remove();
      chipState.set(ctrl, {val: val, sig: '', strip: null});
      return !!(st && st.sig);
    }
    if (!strip) {
      strip = document.createElement('div');
      strip.className = 'tag-chips';
    }
    strip.innerHTML = tags.map(function (t) {
      return '<a class="tag-chip" href="' + href(t) + '" title="Everything tagged #' + esc(t) + '">#' + esc(t) + '</a>';
    }).join('') + (more ? '<a class="tag-chip tag-chip-more" href="/tags" title="This box holds more tags — see every tag">+ more</a>' : '');
    if (ctrl.nextElementSibling !== strip) ctrl.insertAdjacentElement('afterend', strip);
    chipState.set(ctrl, {val: val, sig: sig, strip: strip});
    return !st || st.sig !== sig;
  }

  // ── Header strip ──────────────────────────────────────────────────────────
  var stripSeq = 0, stripTimer = null, pageSig = null;
  function loadHeaderStrip() {
    var host = document.getElementById('show-tags-strip');
    if (!host || !onShowPage()) return;
    var my = ++stripSeq;
    fetch('/api/shows/' + encodeURIComponent(SHOW_ID) + '/tags', {headers: {'X-Requested-With': 'XMLHttpRequest'}})
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (!d || my !== stripSeq) return;
        // The server sends the 40 most used (find_module.SHOW_STRIP_MAX) +
        // `total` distinct tags.
        var tags = (d.tags || []).slice(0, 40);
        var rest = Math.max(+d.total || 0, tags.length) - tags.length;
        if (!tags.length) { host.hidden = true; host.innerHTML = ''; return; }
        host.innerHTML = '<span class="show-tags-label">Tags</span>' + tags.map(function (t) {
          return '<a class="tag-chip" href="' + href(t.tag) + '" title="' + t.n + ' mention' + (t.n === 1 ? '' : 's') +
            ' in this show — open the tag page">#' + esc(t.tag) + (t.n > 1 ? ' <span class="tag-chip-n">' + t.n + '</span>' : '') + '</a>';
        }).join('') + (rest > 0 ? '<a class="tag-chip tag-chip-more" href="/tags">+' + rest + '</a>' : '');
        host.hidden = false;
      })
      .catch(function () {});
  }
  function queueHeaderStrip() {
    clearTimeout(stripTimer);
    // After the field's own debounced autosave (1.5 s) has had time to land;
    // while a save is still pending (app.js _isDirty) look again later.
    stripTimer = setTimeout(function () {
      if (typeof _isDirty !== 'undefined' && _isDirty) { queueHeaderStrip(); return; }
      loadHeaderStrip();
    }, 3000);
  }

  /** One pass over every control (+ the comment list's tags); cheap. */
  function sweep() {
    var tagsOnPage = [];
    allControls().forEach(function (c) {
      refreshChips(c);
      var st = chipState.get(c);
      if (st && st.sig) tagsOnPage.push(st.sig);
    });
    document.querySelectorAll('#comments-list .comment-item:not(.comment-deleted) .tag-link').forEach(function (a) {
      tagsOnPage.push(a.getAttribute('href'));
    });
    var sig = tagsOnPage.sort().join('|');
    if (pageSig !== null && sig !== pageSig) queueHeaderStrip();
    pageSig = sig;
  }

  function initChips() {
    document.addEventListener('input', function (e) {
      if (isTagControl(e.target) && refreshChips(e.target)) queueHeaderStrip();
    }, true);
    // Labor notes / day notes save on blur / change, not on a debounce.
    document.addEventListener('change', function (e) {
      if (isTagControl(e.target)) queueHeaderStrip();
    }, true);
    // Programmatic changes (live-sync merges, imports, templates, copy-day,
    // AI extract, history restore) fire no `input`: one cheap sweep every
    // 2 s catches them. The only interval this file runs; idle while hidden.
    setInterval(function () { if (!document.hidden) sweep(); }, 2000);
    sweep();
    loadHeaderStrip();
  }

  // ── Deep links: ?focus=<kind>:<ref> ───────────────────────────────────────
  function q1(sel) { return document.querySelector(sel); }
  function cssv(v) { return window.CSS && CSS.escape ? CSS.escape(v) : String(v).replace(/["\\]/g, '\\$&'); }
  function byAttr(sel, attr, v) { return q1(sel + '[' + attr + '="' + cssv(v) + '"]'); }
  function focusCell(v) { var el = byAttr('', 'data-tag-focus', v); return el ? (el.closest('tr') || el) : null; }

  var FOCUS = {
    adv:        {tab: 'advance',   find: function (r) { return byAttr('#advance-form .adv-field', 'data-key', r); }},
    piano:      {tab: 'advance',   find: function (r) { return focusCell('piano:' + r); },
                 fallback: function () { return q1('#piano-tuning-section'); }},
    sched:      {tab: 'schedule',  find: function (r) { return byAttr('#schedule-form tr.schedule-row', 'data-row-id', r); }},
    schedmeta:  {tab: 'schedule',  find: function (r) { return byAttr('#schedule-form .sched-meta', 'data-key', r); }},
    labornotes: {tab: 'staffing',  find: function () { return q1('#show-labor-notes'); }},
    laborday:   {tab: 'staffing',  find: function (r) {
                   var b = byAttr('#labor-days-container .labor-day', 'data-work-date', r);
                   return b ? (b.querySelector('.labor-day-notes') || b) : null; }},
    laborreq:   {tab: 'staffing',  find: function (r) { return byAttr('#labor-days-container tr', 'data-rid', r); }},
    asset:      {tab: 'assets',    find: function (r) { return focusCell('asset:' + r); }},
    rental:     {tab: 'assets',    find: function (r) { return focusCell('rental:' + r); }},
    postnotes:  {tab: 'postnotes', find: function (r) { return byAttr('#postnotes-form .notes-field', 'data-key', r); }},
    pslabor:    {tab: 'postnotes', find: function (r) { return byAttr('#ps-labor-tbody tr', 'data-id', r); }},
    comment:    {tab: 'comments',  find: function (r) { return byAttr('#comments-list .comment-item', 'data-id', r); }},
    file:       {tab: 'export',    find: function (r) {
                   var a = q1('#attachments-list a[href$="/attachments/' + cssv(r) + '/download"]');
                   return a ? (a.closest('.attachment-item') || a) : null; }}
  };

  function clearFocusParam() {
    try {
      var u = new URL(window.location.href);
      if (!u.searchParams.has('focus')) return;
      u.searchParams.delete('focus');
      history.replaceState(history.state, '', u);
    } catch (_) {}
  }

  /** Make an element visible: open its collapsed advance section / notes
      toggle, switch to its schedule day. */
  function reveal(el) {
    var sec = el.closest('.form-section.collapsed');
    if (sec) sec.classList.remove('collapsed');       // not remembered: a visit, not a preference
    var nb = el.closest('.notes-body.hidden');
    if (nb) {
      var btn = nb.previousElementSibling;
      if (btn && btn.classList.contains('notes-toggle') && typeof toggleNotes === 'function') toggleNotes(btn);
      else nb.classList.remove('hidden');
    }
    var pane = el.closest('.sched-day-pane');
    if (pane && (pane.classList.contains('hidden') || pane.style.display === 'none') && typeof switchSchedDay === 'function') {
      var key = pane.dataset.dayKey || '';
      var tabBtn = byAttr('.sched-day-tab', 'data-day-key', key);
      if (tabBtn) switchSchedDay(key, tabBtn);
    }
  }

  function land(el) {
    reveal(el);
    var target = el.matches('tr') ? el : (el.closest('.comment-item, .attachment-item') || el);
    target.scrollIntoView({block: 'center', behavior: 'smooth'});
    target.classList.add('tag-focus-target');
    setTimeout(function () { target.classList.remove('tag-focus-target'); }, 2600);
    if (typeof _flashField === 'function') _flashField(target);
  }

  function handleFocus() {
    var f;
    try { f = new URLSearchParams(window.location.search).get('focus'); } catch (_) { return; }
    if (!f || !onShowPage()) return;
    var i = f.indexOf(':');
    var kind = i < 0 ? f : f.slice(0, i), ref = i < 0 ? '' : f.slice(i + 1);
    // Own keys only: ?focus=constructor / __proto__ must not find Object's.
    var spec = Object.prototype.hasOwnProperty.call(FOCUS, kind) ? FOCUS[kind] : null;
    if (!spec ||!/^[A-Za-z0-9_.:-]{0,100}$/.test(ref)) { clearFocusParam(); return; }
    if (typeof activeTab !== 'undefined' && activeTab !== spec.tab && typeof switchTab === 'function') switchTab(spec.tab);
    // Lazy tabs (labor, comments, assets, files, the piano panel) render
    // asynchronously: look again every 150 ms for up to ~9 s.
    var tries = 0;
    (function attempt() {
      var el = spec.find(ref);
      if (!el && spec.fallback && tries >= 20) el = spec.fallback();
      if (el) { land(el); clearFocusParam(); return; }
      if (++tries >= 60) { clearFocusParam(); return; }
      setTimeout(attempt, 150);
    })();
  }

  function initShowPage() {
    if (!onShowPage()) return;
    initChips();
    handleFocus();
  }
  // show.html's own scripts (initShow → switchTab) run after this file but
  // before DOMContentLoaded fires; start once they have.
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { setTimeout(initShowPage, 0); });
  } else {
    setTimeout(initShowPage, 0);
  }
})();
