/* Inbox (3.47.0): the floating envelope button (desktop) / "Inbox" tab
   (mobile) — Messages (chat_module.py) + the notification list that used to
   pop out of the sidebar bell.

   Rules kept here:
   - Every piece of user text goes in with textContent (never innerHTML).
   - No websockets: ONE status poll (30 s, visible tabs only) drives every
     badge; the open conversation polls every 5 s; both stop while hidden.
   - "Also post to the show's comments" is never implicit: the composer
     states it, the send button says it, and every send tells the server
     what this screen showed (post_to_comments) — the server refuses with
     409 when the conversation's setting has changed underneath us.
   - Reading is explicit (POST …/read) and only while the panel is open on
     that conversation in a visible tab. */
(function () {
  'use strict';
  var panel = document.getElementById('inbox-panel');
  if (!panel) return;                       // signed out / document viewer
  var CHAT = panel.dataset.chat === '1';
  var ME = parseInt(panel.dataset.uid, 10) || 0;
  var PAGE_SHOW = panel.dataset.showId
    ? {id: parseInt(panel.dataset.showId, 10), name: panel.dataset.showName || ''} : null;
  var MOBILE = document.documentElement.classList.contains('mobile-view');
  var H = {'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'};
  var STATUS_MS = 30000, THREAD_MS = 5000, LIST_MS = 15000;
  document.documentElement.classList.add('has-inbox');

  var $ = function (id) { return document.getElementById(id); };
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function ls(key, val) {
    try {
      if (val === undefined) return localStorage.getItem(key);
      localStorage.setItem(key, val);
    } catch (e) {}
    return null;
  }
  function when(iso, long) {
    var d = (typeof parseServerTime === 'function') ? parseServerTime(iso) : new Date(iso);
    if (!d || isNaN(d)) return '';
    var now = new Date(), same = d.toDateString() === now.toDateString();
    var t = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    if (same && !long) return t;
    var opt = {month: 'short', day: 'numeric'};
    if (d.getFullYear() !== now.getFullYear()) opt.year = 'numeric';
    var day = d.toLocaleDateString([], opt);
    return long || Math.abs(now - d) < 6 * 86400000 ? day + ', ' + t : day;
  }
  function initials(name) {
    return String(name || '?').trim().split(/\s+/).slice(0, 2).map(function (w) { return w.charAt(0).toUpperCase(); }).join('') || '?';
  }
  function color(name) {
    return (typeof _userColor === 'function') ? _userColor(name) : 'var(--accent)';
  }
  async function api(url, body) {
    var opt = body === undefined ? {headers: H, credentials: 'same-origin'}
      : {method: 'POST', headers: H, credentials: 'same-origin', body: JSON.stringify(body)};
    var r = await fetch(url, opt);
    var d = {};
    try { d = await r.json(); } catch (e) {}
    if (!r.ok) {
      var err = new Error(d.error || ('Request failed (' + r.status + ')'));
      err.status = r.status; err.data = d;
      throw err;
    }
    return d;
  }

  // ── state ────────────────────────────────────────────────────────────────
  var S = {
    open: false, view: CHAT ? 'threads' : 'notifications',
    status: {unread: 0, reminders: 0, notifications: 0, latest: 0},
    threads: [], threadsLoaded: false, listSeq: 0,
    thread: null, msgs: [], more: false, threadSeq: 0, due: [],
    drafts: {}, people: null, sending: false,
    groupByShow: ls('inbox.groupByShow') === '1',
    compose: null
  };
  var timers = {status: null, thread: null, list: null};

  // ── badges ───────────────────────────────────────────────────────────────
  function setBadge(node, n) {
    if (!node) return;
    if (n > 0) { node.textContent = n > 99 ? '99+' : String(n); node.hidden = false; }
    else node.hidden = true;
  }
  function paintBadges() {
    var st = S.status, msg = st.unread + st.reminders;
    setBadge($('inbox-fab-badge'), msg + st.notifications);
    setBadge($('m-inbox-badge'), msg + st.notifications);
    setBadge($('inbox-tab-msg-badge'), msg);
    setBadge($('inbox-tab-notif-badge'), st.notifications);
    setBadge($('notif-bell-badge'), st.notifications);     // sidebar bell
    var fab = $('inbox-fab');
    if (fab) {
      var total = msg + st.notifications;
      fab.title = total ? (msg ? msg + ' unread message' + (msg === 1 ? '' : 's') : '')
        + (msg && st.notifications ? ', ' : '')
        + (st.notifications ? st.notifications + ' notification' + (st.notifications === 1 ? '' : 's') : '')
        : 'Messages and notifications';
      fab.classList.toggle('has-unread', total > 0);
    }
  }
  async function pollStatus() {
    if (document.hidden) return;
    try {
      var d = await api('/api/chat/status');
      var changed = d.latest !== S.status.latest || d.unread !== S.status.unread
        || d.reminders !== S.status.reminders;
      S.status = d;
      paintBadges();
      if (changed && S.open && S.view === 'threads') loadThreads();
    } catch (e) { /* offline / signed out — the session watchdog handles it */ }
  }

  // ── open / close / views ─────────────────────────────────────────────────
  function show(view) {
    S.view = view;
    ['threads', 'notifications', 'thread', 'compose'].forEach(function (v) {
      var n = $('inbox-view-' + v);
      if (n) n.hidden = v !== view;
    });
    var top = view === 'threads' || view === 'notifications';
    $('inbox-tabs').hidden = !top || !CHAT;
    $('inbox-back').hidden = top;
    $('inbox-new').hidden = !CHAT || view === 'compose' || view === 'notifications';
    Array.prototype.forEach.call(document.querySelectorAll('#inbox-tabs [data-view]'), function (b) {
      var on = b.dataset.view === view;
      b.classList.toggle('active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    if (top) $('inbox-title').textContent = CHAT ? 'Inbox' : 'Notifications';
    clearInterval(timers.thread); timers.thread = null;
    clearInterval(timers.list); timers.list = null;
    if (!S.open) return;
    if (view === 'threads') {
      loadThreads();
      timers.list = setInterval(function () { if (!document.hidden) loadThreads(); }, LIST_MS);
    } else if (view === 'notifications') {
      if (typeof _loadNotifPanel === 'function') _loadNotifPanel();
    } else if (view === 'thread') {
      timers.thread = setInterval(pollThread, THREAD_MS);
    }
  }
  function open(view) {
    if (view === 'threads' && !CHAT) view = 'notifications';
    S.open = true;
    panel.hidden = false;
    document.documentElement.classList.add('inbox-open');
    var fab = $('inbox-fab');
    if (fab) fab.setAttribute('aria-expanded', 'true');
    show(view || (S.view === 'thread' && S.thread ? 'thread' : S.view === 'compose' ? 'compose'
      : (CHAT ? 'threads' : 'notifications')));
    if (S.view === 'thread') pollThread();
  }
  function close() {
    S.open = false;
    panel.hidden = true;
    document.documentElement.classList.remove('inbox-open');
    var fab = $('inbox-fab');
    if (fab) fab.setAttribute('aria-expanded', 'false');
    clearInterval(timers.thread); timers.thread = null;
    clearInterval(timers.list); timers.list = null;
    pollStatus();
  }
  function toggle(view) {
    if (S.open && (!view || view === S.view)) close();
    else open(view);
  }

  // ── thread list ──────────────────────────────────────────────────────────
  function threadTitle(t) {
    if (t.is_self) return 'Notes to self';
    // No one else left: the other person's account was deleted.
    return t.members.filter(function (m) { return m.id !== ME; })
      .map(function (m) { return m.name; }).join(', ') || 'Former user';
  }
  async function loadThreads() {
    if (!CHAT) return;
    var seq = ++S.listSeq;
    try {
      var d = await api('/api/chat/threads');
      if (seq !== S.listSeq) return;
      S.threads = d.threads || [];
      S.threadsLoaded = true;
      drawThreads();
    } catch (e) {
      if (seq !== S.listSeq || S.threadsLoaded) return;
      var list = $('inbox-thread-list');
      clear(list);
      list.appendChild(el('div', 'inbox-empty', 'Unable to load messages.'));
    }
  }
  function threadRow(t) {
    var row = el('button', 'inbox-thread' + (t.unread || t.reminders ? ' unread' : ''));
    row.type = 'button';
    row.addEventListener('click', function () { openThread(t.id); });
    var title = threadTitle(t);
    var av = el('span', 'inbox-avatar', t.is_self ? '✎' : (t.members.length > 2 ? String(t.members.length - 1) : initials(title)));
    av.style.background = t.is_self ? 'var(--bg-hover)' : color(title);
    if (t.is_self) av.style.color = 'var(--text)';
    row.appendChild(av);
    var main = el('span', 'inbox-thread-main');
    var l1 = el('span', 'inbox-thread-l1');
    l1.appendChild(el('span', 'inbox-thread-name', title));
    l1.appendChild(el('span', 'inbox-thread-time', when(t.last_at)));
    main.appendChild(l1);
    if (t.show && !S.groupByShow) {
      var sh = el('span', 'inbox-thread-show');
      sh.appendChild(el('span', 'inbox-show-chip', t.show.name));
      if (t.post_to_comments) sh.appendChild(el('span', 'inbox-public-chip', 'posts to comments'));
      main.appendChild(sh);
    } else if (t.post_to_comments) {
      var pc = el('span', 'inbox-thread-show');
      pc.appendChild(el('span', 'inbox-public-chip', 'posts to comments'));
      main.appendChild(pc);
    }
    var l2 = el('span', 'inbox-thread-l2');
    var prev = t.last ? ((t.last.kind === 'system' ? '' : (t.last.mine ? 'You: ' : (t.members.length > 2 ? t.last.author + ': ' : ''))) + t.last.body) : '';
    l2.appendChild(el('span', 'inbox-thread-prev', prev.replace(/\s+/g, ' ')));
    if (t.reminders) l2.appendChild(el('span', 'inbox-remind-flag', '⏰ ' + (t.reminders > 1 ? t.reminders + ' reminders' : 'reminder')));
    if (t.unread) l2.appendChild(el('span', 'inbox-badge inline', t.unread > 99 ? '99+' : String(t.unread)));
    main.appendChild(l2);
    row.appendChild(main);
    return row;
  }
  function drawThreads() {
    var list = $('inbox-thread-list');
    if (!list) return;
    var q = ($('inbox-search').value || '').trim().toLowerCase();
    var rows = S.threads.filter(function (t) {
      if (!q) return true;
      return (threadTitle(t) + ' ' + (t.show ? t.show.name : '') + ' ' + (t.last ? t.last.body : '')).toLowerCase().indexOf(q) !== -1;
    });
    var keep = list.scrollTop;
    clear(list);
    $('inbox-group-btn').classList.toggle('active', S.groupByShow);
    $('inbox-group-btn').setAttribute('aria-pressed', S.groupByShow ? 'true' : 'false');
    if (!rows.length) {
      var empty = el('div', 'inbox-empty');
      empty.appendChild(el('div', null, S.threads.length ? 'No conversation matches.' : 'No messages yet.'));
      if (!S.threads.length) empty.appendChild(el('div', 'inbox-empty-sub', 'Press + New to write to someone — or to yourself, with a reminder.'));
      list.appendChild(empty);
      return;
    }
    if (!S.groupByShow) {
      rows.forEach(function (t) { list.appendChild(threadRow(t)); });
    } else {
      // Groups in order of their newest conversation; "No show" last.
      var groups = [], byKey = {};
      rows.forEach(function (t) {
        var k = t.show ? 's' + t.show.id : 'none';
        if (!byKey[k]) { byKey[k] = {show: t.show, rows: []}; groups.push(byKey[k]); }
        byKey[k].rows.push(t);
      });
      groups.sort(function (a, b) { return (a.show ? 0 : 1) - (b.show ? 0 : 1); });
      groups.forEach(function (g) {
        var h = el('div', 'inbox-group-head');
        if (g.show) {
          var a = el('a', 'no-prefetch', g.show.name);
          a.href = '/shows/' + encodeURIComponent(g.show.id);
          h.appendChild(a);
          if (g.show.date) h.appendChild(el('span', 'inbox-group-date', g.show.date));
          if (g.show.status !== 'active') h.appendChild(el('span', 'inbox-group-date', g.show.status));
        } else h.appendChild(el('span', null, 'No show tagged'));
        list.appendChild(h);
        g.rows.forEach(function (t) { list.appendChild(threadRow(t)); });
      });
    }
    list.scrollTop = keep;
  }

  // ── one conversation ─────────────────────────────────────────────────────
  function lastId() { return S.msgs.length ? S.msgs[S.msgs.length - 1].id : 0; }
  async function openThread(tid, keepDraft, keepReminder) {
    var seq = ++S.threadSeq;
    // Only a LOADED conversation owns the text in the box (a placeholder
    // that never finished loading still shows the previous one's).
    if (S.thread && !S.thread.loading && !keepDraft) S.drafts[S.thread.id] = $('inbox-input').value;
    S.thread = {id: tid, members: [], show: null, post_to_comments: false, loading: true};
    $('inbox-input').value = S.drafts[tid] || '';
    _draftPub = null;
    S.msgs = []; S.due = []; S.more = false;
    if (!S.open) open('thread'); else show('thread');
    $('inbox-title').textContent = 'Loading…';
    clear($('inbox-msgs'));
    $('inbox-thread-meta').hidden = true;
    $('inbox-public-banner').hidden = true;
    try {
      var d = await api('/api/chat/threads/' + tid);
      if (seq !== S.threadSeq) return;
      S.thread = d.thread; S.msgs = d.messages || []; S.more = !!d.more; S.due = d.due || [];
      drawThread(true);
      $('inbox-input').value = S.drafts[tid] || '';
      _draftPub = null;
      noteDraftMode();               // a restored draft counts as written under the mode now shown
      if (!keepReminder) setReminder(null);
      autoGrow();
      toBottom();
      markRead();
      if (!MOBILE) $('inbox-input').focus();
    } catch (e) {
      if (seq !== S.threadSeq) return;
      S.thread = null;
      show('threads');
      flash(e.message);
    }
  }
  async function pollThread() {
    if (!S.open || S.view !== 'thread' || !S.thread || S.thread.loading || document.hidden) return;
    var tid = S.thread.id, seq = S.threadSeq;
    try {
      var d = await api('/api/chat/threads/' + tid + '?after=' + lastId());
      if (seq !== S.threadSeq || !S.thread || S.thread.id !== tid) return;
      var was = S.thread.post_to_comments;
      var stick = nearBottom();
      S.thread = d.thread;
      S.due = d.due || [];
      var have = {};
      S.msgs.forEach(function (m) { have[m.id] = 1; });
      var fresh = (d.messages || []).filter(function (m) { return !have[m.id]; });
      S.msgs = S.msgs.concat(fresh);
      if (fresh.length || was !== d.thread.post_to_comments) {
        // A time picker open under a message would be wiped by the redraw:
        // repaint only what must change now, redraw when the picker closes.
        if ($('inbox-msgs').querySelector('.inbox-timepick')) { S.redrawPending = true; paintPublic(); paintComposer(); }
        else drawThread(stick);
      } else drawDue();
      if (fresh.length) markRead();
    } catch (e) {
      if (e.status === 404 && S.thread && S.thread.id === tid) { S.thread = null; show('threads'); }
    }
  }
  function markRead() {
    if (!S.thread || document.hidden || !S.open || S.view !== 'thread') return;
    var up = lastId();
    if (!up || up <= (S.thread.last_read_id || 0)) return;
    S.thread.last_read_id = up;
    api('/api/chat/threads/' + S.thread.id + '/read', {up_to: up}).then(pollStatus).catch(function () {});
  }
  // After the layout settles (banner / composer heights), not just now.
  function toBottom() {
    var box = $('inbox-msgs');
    box.scrollTop = box.scrollHeight;
    requestAnimationFrame(function () { box.scrollTop = box.scrollHeight; });
  }
  function nearBottom() {
    var box = $('inbox-msgs');
    return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  }
  function publicNow() { return !!(S.thread && S.thread.post_to_comments && S.thread.show); }

  // The "this posts to show comments" banner — always current, even while
  // the message list itself is waiting for a time picker to close.
  function paintPublic() {
    var t = S.thread, ban = $('inbox-public-banner');
    ban.hidden = !publicNow();
    if (publicNow()) {
      $('inbox-public-text').textContent = 'Every message sent here — yours and '
        + (t.is_self ? 'any' : 'theirs') + ' — is also posted to the comments of “' + t.show.name
        + '”. Anyone with access to the show can read it.';
    }
  }
  function drawThread(stick) {
    var t = S.thread;
    if (!t) return;
    $('inbox-title').textContent = threadTitle(t);
    // meta line: the tagged show + the comments switch
    var meta = $('inbox-thread-meta');
    clear(meta);
    if (t.show) {
      meta.hidden = false;
      meta.appendChild(el('span', 'inbox-meta-label', 'About'));
      var a = el('a', 'inbox-show-chip no-prefetch', t.show.name);
      a.href = '/shows/' + encodeURIComponent(t.show.id) + '?tab=comments';
      a.title = 'Open the show';
      meta.appendChild(a);
      if (!t.post_to_comments && t.can_comment) {
        var on = el('button', 'inbox-link-btn', 'Also post to show comments…');
        on.type = 'button';
        on.addEventListener('click', function () { setCommentsMode(true); });
        meta.appendChild(on);
      }
    } else meta.hidden = true;
    paintPublic();
    S.redrawPending = false;
    var box = $('inbox-msgs');
    var prevTop = box.scrollTop;
    clear(box);
    if (S.more) {
      var older = el('button', 'inbox-link-btn inbox-older', 'Show earlier messages');
      older.type = 'button';
      older.addEventListener('click', loadOlder);
      box.appendChild(older);
    }
    var lastDay = '';
    S.msgs.forEach(function (m) {
      var d = (typeof parseServerTime === 'function') ? parseServerTime(m.created_at) : new Date(m.created_at);
      var day = d && !isNaN(d) ? d.toDateString() : '';
      if (day && day !== lastDay) {
        lastDay = day;
        box.appendChild(el('div', 'inbox-day', d.toLocaleDateString([], {weekday: 'short', month: 'short', day: 'numeric'})));
      }
      box.appendChild(msgNode(m, t));
    });
    if (!S.msgs.length) box.appendChild(el('div', 'inbox-empty', 'No messages yet.'));
    drawDue();
    paintComposer();
    if (stick) toBottom();
    else box.scrollTop = prevTop;
  }
  function msgNode(m, t) {
    if (m.kind === 'system') {
      var sys = el('div', 'inbox-sys');
      sys.appendChild(el('span', null, m.body));
      sys.appendChild(el('span', 'inbox-msg-time', when(m.created_at, true)));
      return sys;
    }
    var wrap = el('div', 'inbox-msg' + (m.mine ? ' mine' : ''));
    wrap.dataset.mid = m.id;
    if (!m.mine && t.members.length > 2) wrap.appendChild(el('div', 'inbox-msg-author', m.author));
    var bub = el('div', 'inbox-bubble' + (m.in_comments ? ' public' : ''), m.body);
    wrap.appendChild(bub);
    var foot = el('div', 'inbox-msg-foot');
    foot.appendChild(el('span', 'inbox-msg-time', when(m.created_at)));
    if (m.in_comments) {
      var pub = el('span', 'inbox-public-chip', 'in show comments');
      pub.title = 'This message was also posted to the show’s comments';
      foot.appendChild(pub);
    }
    if (m.mine) {
      if (m.remind_at && !m.remind_done) {
        var chip = el('span', 'inbox-remind-chip' + (m.remind_due ? ' due' : ''),
          '⏰ ' + (m.remind_due ? 'Reminder due' : when(m.remind_at, true)));
        foot.appendChild(chip);
        if (m.remind_due) foot.appendChild(remindBtn('Done', m, {done: true}));
        foot.appendChild(remindBtn(m.remind_due ? 'Later…' : 'Change', m, 'pick'));
        if (!m.remind_due) foot.appendChild(remindBtn('Cancel', m, {remind_at: null}));
      } else {
        foot.appendChild(remindBtn(m.remind_done ? '⏰ Remind again' : '⏰ Remind me', m, 'pick'));
      }
    }
    wrap.appendChild(foot);
    return wrap;
  }
  function remindBtn(label, m, action) {
    var b = el('button', 'inbox-link-btn', label);
    b.type = 'button';
    b.addEventListener('click', function () {
      if (action === 'pick') {
        pickTime(b.closest('.inbox-msg'), function (iso) { saveReminder(m, {remind_at: iso}); });
      } else saveReminder(m, action);
    });
    return b;
  }
  async function saveReminder(m, body) {
    try {
      var d = await api('/api/chat/messages/' + m.id + '/reminder', body);
      S.msgs = S.msgs.map(function (x) { return x.id === m.id ? d.message : x; });
      S.due = S.due.filter(function (x) { return x.id !== m.id; });
      var stick = nearBottom();
      drawThread(stick);
      pollStatus();
    } catch (e) { flash(e.message); }
  }
  // Reminders that came due — pinned above the composer so they're seen
  // even when their message is far up the conversation.
  function drawDue() {
    var host = $('inbox-due');
    var due = S.due.slice();
    S.msgs.forEach(function (m) {
      if (m.mine && m.remind_due && !due.some(function (x) { return x.id === m.id; })) due.push(m);
    });
    // Leave the rows alone while a time is being picked in one of them, and
    // when nothing changed (keeps keyboard focus on Done / Later… between polls).
    var sig = (S.thread ? S.thread.id : 0) + ':' + due.map(function (m) { return m.id; }).join(',');
    if (host.querySelector('.inbox-timepick') || host.dataset.sig === sig) return;
    host.dataset.sig = sig;
    clear(host);
    host.hidden = !due.length;
    due.forEach(function (m) {
      var row = el('div', 'inbox-due-row');
      row.appendChild(el('span', 'inbox-due-ico', '⏰'));
      row.appendChild(el('span', 'inbox-due-text', m.body.replace(/\s+/g, ' ')));
      row.appendChild(remindBtn('Done', m, {done: true}));
      var later = el('button', 'inbox-link-btn', 'Later…');
      later.type = 'button';
      later.addEventListener('click', function () {
        pickTime(row, function (iso) { saveReminder(m, {remind_at: iso}); });
      });
      row.appendChild(later);
      host.appendChild(row);
    });
  }
  async function loadOlder() {
    if (!S.thread || !S.msgs.length) return;
    var tid = S.thread.id, seq = S.threadSeq, box = $('inbox-msgs');
    try {
      var d = await api('/api/chat/threads/' + tid + '?before=' + S.msgs[0].id);
      if (seq !== S.threadSeq) return;
      var h = box.scrollHeight;
      S.msgs = (d.messages || []).concat(S.msgs);
      S.more = !!d.more;
      drawThread(false);
      box.scrollTop = box.scrollHeight - h;
    } catch (e) { flash(e.message); }
  }
  async function setCommentsMode(on) {
    var t = S.thread;
    if (!t || !t.show) return;
    var others = t.members.filter(function (m) { return m.id !== ME; }).map(function (m) { return m.name; }).join(', ');
    var msg = on
      ? 'Post this conversation to the comments of “' + t.show.name + '”?\n\n'
        + 'From now on EVERY message sent here' + (others ? ' — by you or ' + others : '')
        + ' — is also posted to that show’s comments, where anyone with access to the show can read it.\n\n'
        + 'Messages already sent stay private.' + (others ? ' ' + others + ' will see a notice in the conversation.' : '')
      : 'Stop posting this conversation to the comments of “' + t.show.name + '”?\n\n'
        + 'New messages will be private again. Comments already posted stay on the show.';
    if (!window.confirm(msg)) return;
    try {
      await api('/api/chat/threads/' + t.id + '/comments-mode', {on: on});
      await pollThread();
      if (S.thread && S.thread.id === t.id) { S.thread.post_to_comments = on; drawThread(true); }
    } catch (e) { flash(e.message); }
  }

  // ── composer (open conversation) ─────────────────────────────────────────
  // The mode (posts to comments or not) the text in the box was WRITTEN
  // under. If the conversation's mode changes while someone is typing, the
  // poll redraws the banner — but a quick Enter must not publish what was
  // typed as private (or the reverse): send() stops once and says so.
  var _draftPub = null;
  function noteDraftMode() {
    if (!$('inbox-input').value.trim()) _draftPub = null;
    else if (_draftPub === null) _draftPub = publicNow();
  }
  var _remindIso = null;
  function setReminder(iso) {
    _remindIso = iso || null;
    var chip = $('inbox-remind-set');
    chip.hidden = !_remindIso;
    if (_remindIso) $('inbox-remind-set-text').textContent = '⏰ Remind me ' + when(_remindIso, true);
    $('inbox-remind-btn').classList.toggle('active', !!_remindIso);
  }
  function paintComposer() {
    var pub = publicNow(), t = S.thread || {};
    var comp = $('inbox-composer');
    comp.classList.toggle('public', pub);
    var blocked = pub && !t.can_comment;
    $('inbox-input').disabled = blocked;
    $('inbox-send').disabled = blocked || S.sending;
    $('inbox-send').textContent = pub ? 'Send + post to comments' : 'Send';
    var note = $('inbox-composer-note');
    note.hidden = !pub;
    if (pub) {
      note.textContent = blocked
        ? 'This conversation posts to show comments, and your account is read-only — you can read here but not send.'
        : 'Posting to the comments of “' + t.show.name + '”';
    }
    $('inbox-input').placeholder = t.is_self ? 'Note to self…' : 'Message…';
  }
  function autoGrow() {
    var ta = $('inbox-input');
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 140) + 'px';
  }
  async function send() {
    var t = S.thread;
    if (!t || t.loading || S.sending) return;
    var ta = $('inbox-input'), body = ta.value.trim();
    if (!body) return;
    var tid = t.id;
    if (_draftPub !== null && _draftPub !== publicNow()) {
      _draftPub = publicNow();
      flash(publicNow()
        ? 'Not sent — this conversation now posts to the show’s comments. Check the banner, then send again.'
        : 'Not sent — this conversation no longer posts to the show’s comments, so your message would stay private. Send again if that is what you want.');
      return;
    }
    S.sending = true; paintComposer();
    try {
      await api('/api/chat/threads/' + tid + '/messages',
        {body: body, post_to_comments: publicNow(), remind_at: _remindIso});
      S.drafts[tid] = '';            // sent — whatever is on screen by now
      if (S.thread && S.thread.id === tid) { ta.value = ''; _draftPub = null; setReminder(null); autoGrow(); }
      S.sending = false;
      await pollThread();
      toBottom();
      pollStatus();
    } catch (e) {
      S.sending = false;
      if (e.status === 409 && e.data && e.data.reason === 'comments_mode_changed') {
        // The switch moved while this was being written: show the new state
        // and let the writer decide — nothing was sent.
        await pollThread();
        if (S.thread && S.thread.id === tid) { S.thread.post_to_comments = !!e.data.post_to_comments; drawThread(true); }
        _draftPub = publicNow();     // the writer has now been told: the retry goes through
      }
      flash(e.message);
    }
    paintComposer();
  }

  // ── time picker (reminders) ──────────────────────────────────────────────
  function localInput(d) {
    var p = function (n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function quickTimes() {
    var now = new Date();
    var h1 = new Date(now.getTime() + 3600000);
    var eve = new Date(now); eve.setHours(16, 0, 0, 0);
    var tom = new Date(now); tom.setDate(tom.getDate() + 1); tom.setHours(9, 0, 0, 0);
    var mon = new Date(now); mon.setDate(mon.getDate() + ((8 - mon.getDay()) % 7 || 7)); mon.setHours(9, 0, 0, 0);
    var out = [['In 1 hour', h1]];
    if (eve - now > 5400000) out.push(['Today 4 PM', eve]);
    out.push(['Tomorrow 9 AM', tom], ['Monday 9 AM', mon]);
    return out;
  }
  /* A small inline row (quick picks + a date/time box) appended to `host`;
     cb(isoUtc) once a time is chosen. One picker at a time. */
  function pickTime(host, cb) {
    var old = panel.querySelector('.inbox-timepick');
    var flush = function () { if (S.redrawPending && S.view === 'thread' && S.thread && !S.thread.loading) drawThread(nearBottom()); };
    if (old) { var same = old.parentNode === host; old.remove(); if (same) { flush(); drawDue(); return; } }
    if (!host) return;
    var row = el('div', 'inbox-timepick');
    var done = function (d) {
      if (!d || isNaN(d)) return;
      if (d.getTime() < Date.now() - 60000) { flash('Pick a time in the future.'); return; }
      row.remove();
      flush();
      cb(d.toISOString());
    };
    quickTimes().forEach(function (q) {
      var b = el('button', 'btn btn-xs btn-ghost', q[0]);
      b.type = 'button';
      b.addEventListener('click', function () { done(q[1]); });
      row.appendChild(b);
    });
    var inp = el('input', 'field-input inbox-time-input');
    inp.type = 'datetime-local';
    inp.min = localInput(new Date());
    inp.setAttribute('aria-label', 'Reminder date and time');
    row.appendChild(inp);
    var ok = el('button', 'btn btn-xs btn-primary', 'Set');
    ok.type = 'button';
    ok.addEventListener('click', function () { if (inp.value) done(new Date(inp.value)); });
    row.appendChild(ok);
    host.appendChild(row);
    if (row.scrollIntoView) row.scrollIntoView({block: 'nearest'});
  }

  // ── new message ──────────────────────────────────────────────────────────
  function compose(opts) {
    if (!CHAT) return;
    opts = opts || {};
    S.compose = {to: opts.to || [], show: opts.show !== undefined ? opts.show : PAGE_SHOW,
                 pub: false, remind: null};
    if (!S.open) open('compose'); else show('compose');
    $('inbox-title').textContent = 'New message';
    $('inbox-c-body').value = opts.body || '';
    $('inbox-c-to').value = ''; $('inbox-c-show').value = '';
    $('inbox-c-pub').checked = false;
    hideSug();
    paintCompose();
    if (!S.people) {
      api('/api/chat/people').then(function (d) { S.people = d.people || []; }).catch(function () { S.people = []; });
    }
    if (!MOBILE) $('inbox-c-to').focus();
  }
  function paintCompose() {
    var c = S.compose;
    if (!c) return;
    var chips = $('inbox-c-to-chips');
    clear(chips);
    c.to.forEach(function (p) {
      var chip = el('span', 'inbox-chip', p.name);
      var x = el('button', 'inbox-chip-x', '×');
      x.type = 'button';
      x.setAttribute('aria-label', 'Remove ' + p.name);
      x.addEventListener('click', function () {
        c.to = c.to.filter(function (q) { return q.id !== p.id; });
        paintCompose();
      });
      chip.appendChild(x);
      chips.appendChild(chip);
    });
    $('inbox-c-to').placeholder = c.to.length ? 'Add another person…' : 'Name — or leave empty for a note to self';
    $('inbox-c-self').hidden = c.to.length > 0;
    var sh = $('inbox-c-show-chip');
    clear(sh);
    $('inbox-c-show').hidden = !!c.show;
    if (c.show) {
      var chip2 = el('span', 'inbox-chip show', c.show.name);
      var x2 = el('button', 'inbox-chip-x', '×');
      x2.type = 'button';
      x2.setAttribute('aria-label', 'Remove the show tag');
      x2.addEventListener('click', function () { c.show = null; c.pub = false; $('inbox-c-pub').checked = false; paintCompose(); });
      chip2.appendChild(x2);
      sh.appendChild(chip2);
    }
    var canPub = !!c.show && panel.dataset.canComment === '1';
    $('inbox-c-pub').disabled = !canPub;
    if (!canPub) { c.pub = false; $('inbox-c-pub').checked = false; }
    $('inbox-c-pub-row').classList.toggle('disabled', !canPub);
    $('inbox-c-pub-hint').textContent = !c.show ? 'Tag a show first.'
      : (panel.dataset.canComment === '1' ? '' : 'Your account is read-only, so it cannot post comments.');
    var warn = $('inbox-c-pub-warn');
    warn.hidden = !c.pub;
    if (c.pub) {
      var names = c.to.map(function (p) { return p.name; }).join(', ');
      warn.textContent = 'Public: this message — and every reply'
        + (names ? ' ' + names + ' sends' : '') + ' in this conversation — will be posted to the comments of “'
        + c.show.name + '”, where anyone with access to the show can read it. '
        + (names ? names + ' will see this stated at the top of the conversation.' : '');
    }
    $('inbox-c-remind-text').textContent = c.remind ? '⏰ Remind me ' + when(c.remind, true) : '';
    $('inbox-c-remind-clear').hidden = !c.remind;
    $('inbox-c-remind-btn').textContent = c.remind ? 'Change' : '⏰ Remind me…';
    $('inbox-c-send').textContent = c.pub ? 'Send + post to comments' : (c.to.length ? 'Send' : 'Save note');
  }
  function hideSug() {
    ['inbox-c-to-sug', 'inbox-c-show-sug'].forEach(function (id) { var n = $(id); clear(n); n.hidden = true; });
  }
  function sugList(host, items, label, onPick, emptyText) {
    clear(host);
    host.hidden = false;
    if (!items.length) { host.appendChild(el('div', 'inbox-sug-empty', emptyText)); return; }
    items.forEach(function (it, i) {
      var b = el('button', 'inbox-sug-item' + (i === 0 ? ' active' : ''));
      b.type = 'button';
      var parts = label(it);
      b.appendChild(el('span', null, parts[0]));
      if (parts[1]) b.appendChild(el('span', 'inbox-sug-sub', parts[1]));
      b.addEventListener('mousedown', function (e) { e.preventDefault(); onPick(it); });
      host.appendChild(b);
    });
  }
  function sugKeys(input, host, e) {
    if (host.hidden) return false;
    var items = Array.prototype.slice.call(host.querySelectorAll('.inbox-sug-item'));
    if (!items.length) return false;
    var i = items.findIndex(function (b) { return b.classList.contains('active'); });
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      items[i].classList.remove('active');
      i = (i + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length;
      items[i].classList.add('active');
      items[i].scrollIntoView({block: 'nearest'});
      return true;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      items[i].dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
      return true;
    }
    if (e.key === 'Escape') { e.stopPropagation(); hideSug(); return true; }
    return false;
  }
  function peopleSug() {
    var c = S.compose, q = $('inbox-c-to').value.trim().toLowerCase();
    var host = $('inbox-c-to-sug');
    if (!q) { clear(host); host.hidden = true; return; }
    var taken = {};
    c.to.forEach(function (p) { taken[p.id] = 1; });
    var hits = (S.people || []).filter(function (p) { return !taken[p.id] && p.name.toLowerCase().indexOf(q) !== -1; }).slice(0, 8);
    sugList(host, hits, function (p) { return [p.name, p.role]; }, function (p) {
      c.to.push(p);
      $('inbox-c-to').value = '';
      hideSug(); paintCompose();
      $('inbox-c-to').focus();
    }, S.people ? 'No one by that name.' : 'Loading…');
  }
  var _showSeq = 0, _showTimer = null;
  function showSug() {
    var q = $('inbox-c-show').value.trim();
    clearTimeout(_showTimer);
    _showTimer = setTimeout(async function () {
      var seq = ++_showSeq;
      try {
        var d = await api('/api/chat/shows?q=' + encodeURIComponent(q));
        if (seq !== _showSeq || S.view !== 'compose' || document.activeElement !== $('inbox-c-show')) return;
        sugList($('inbox-c-show-sug'), d.shows || [], function (s) {
          return [s.name, [s.date, s.venue].filter(Boolean).join(' · ')];
        }, function (s) {
          S.compose.show = {id: s.id, name: s.name};
          $('inbox-c-show').value = '';
          hideSug(); paintCompose();
        }, 'No active show or event matches.');
      } catch (e) {}
    }, 180);
  }
  async function sendNew() {
    var c = S.compose;
    if (!c || S.sending) return;
    var body = $('inbox-c-body').value.trim();
    if (!body) { flash('Write a message first.'); $('inbox-c-body').focus(); return; }
    if ($('inbox-c-to').value.trim()) { flash('Pick the person from the list, or clear the To box.'); $('inbox-c-to').focus(); return; }
    S.sending = true;
    $('inbox-c-send').disabled = true;
    try {
      var d = await api('/api/chat/threads', {
        user_ids: c.to.map(function (p) { return p.id; }), show_id: c.show ? c.show.id : null,
        post_to_comments: !!c.pub, body: body, remind_at: c.remind});
      S.compose = null;
      $('inbox-c-body').value = '';
      S.sending = false;
      $('inbox-c-send').disabled = false;
      await openThread(d.thread_id, true);
      pollStatus();
      return;
    } catch (e) {
      if (e.status === 409 && e.data && e.data.reason === 'thread_is_public' && e.data.thread_id) {
        // That conversation already posts to comments: nothing was sent.
        // Open it with the text in its composer so the banner is seen first.
        S.drafts[e.data.thread_id] = body;
        var keepRemind = c.remind;
        S.sending = false;
        $('inbox-c-send').disabled = false;
        await openThread(e.data.thread_id, true, true);
        if (S.thread && S.thread.id === e.data.thread_id) setReminder(keepRemind);
        flash('Not sent yet — this conversation posts to the show’s comments. Check the banner, then press Send.');
        return;
      }
      flash(e.message);
    }
    S.sending = false;
    $('inbox-c-send').disabled = false;
  }

  // ── small notices inside the panel ───────────────────────────────────────
  var _flashTimer = null;
  function flash(text) {
    var n = $('inbox-flash');
    n.textContent = text;
    n.hidden = false;
    clearTimeout(_flashTimer);
    _flashTimer = setTimeout(function () { n.hidden = true; }, 7000);
  }

  // ── wiring ───────────────────────────────────────────────────────────────
  var fab = $('inbox-fab');
  if (fab) fab.addEventListener('click', function () { toggle(); });
  var mtab = $('m-inbox-tab');
  if (mtab) mtab.addEventListener('click', function () {
    if (typeof mCloseDrawer === 'function') mCloseDrawer();
    toggle();
  });
  $('inbox-close').addEventListener('click', close);
  $('inbox-back').addEventListener('click', function () {
    if (S.view === 'thread') {
      if (S.thread && !S.thread.loading) S.drafts[S.thread.id] = $('inbox-input').value;
      S.threadSeq++;                 // a fetch still in flight is for a view we left
      S.thread = null;
      $('inbox-input').value = '';
      _draftPub = null;
    }
    show(CHAT ? 'threads' : 'notifications');
  });
  $('inbox-new').addEventListener('click', function () { compose(); });
  Array.prototype.forEach.call(document.querySelectorAll('#inbox-tabs [data-view]'), function (b) {
    b.addEventListener('click', function () { show(b.dataset.view); });
  });
  $('inbox-search').addEventListener('input', drawThreads);
  $('inbox-group-btn').addEventListener('click', function () {
    S.groupByShow = !S.groupByShow;
    ls('inbox.groupByShow', S.groupByShow ? '1' : '0');
    drawThreads();
  });
  $('inbox-public-off').addEventListener('click', function () { setCommentsMode(false); });
  $('inbox-send').addEventListener('click', send);
  $('inbox-input').addEventListener('input', function () { autoGrow(); noteDraftMode(); });
  $('inbox-input').addEventListener('keydown', function (e) {
    // Enter sends on a keyboard; on a phone Enter is a new line.
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && !MOBILE) { e.preventDefault(); send(); }
  });
  $('inbox-remind-btn').addEventListener('click', function () {
    pickTime($('inbox-composer-extra'), function (iso) { setReminder(iso); });
  });
  $('inbox-remind-clear').addEventListener('click', function () { setReminder(null); });
  $('inbox-c-to').addEventListener('input', peopleSug);
  $('inbox-c-to').addEventListener('keydown', function (e) {
    if (sugKeys(this, $('inbox-c-to-sug'), e)) return;
    if (e.key === 'Backspace' && !this.value && S.compose && S.compose.to.length) {
      S.compose.to.pop(); paintCompose();
    }
  });
  $('inbox-c-to').addEventListener('blur', function () { setTimeout(function () { var n = $('inbox-c-to-sug'); clear(n); n.hidden = true; }, 120); });
  $('inbox-c-show').addEventListener('input', showSug);
  $('inbox-c-show').addEventListener('focus', showSug);
  $('inbox-c-show').addEventListener('keydown', function (e) { sugKeys(this, $('inbox-c-show-sug'), e); });
  $('inbox-c-show').addEventListener('blur', function () { setTimeout(function () { var n = $('inbox-c-show-sug'); clear(n); n.hidden = true; }, 120); });
  $('inbox-c-pub').addEventListener('change', function () {
    if (S.compose) { S.compose.pub = this.checked; paintCompose(); }
  });
  $('inbox-c-remind-btn').addEventListener('click', function () {
    pickTime($('inbox-c-remind-row'), function (iso) { S.compose.remind = iso; paintCompose(); });
  });
  $('inbox-c-remind-clear').addEventListener('click', function () { S.compose.remind = null; paintCompose(); });
  $('inbox-c-send').addEventListener('click', sendNew);
  $('inbox-c-cancel').addEventListener('click', function () { S.compose = null; show('threads'); });

  // Esc closes the panel (a suggestion list / the report pickers handle
  // their own Esc first); a click outside closes it on desktop.
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !S.open || e.defaultPrevented) return;
    if (document.querySelector('.modal-overlay:not(.hidden):not([style*="display:none"]):not([style*="display: none"]):not(.pdf-filler-hidden)')) return;
    close();
  });
  document.addEventListener('mousedown', function (e) {
    if (!S.open || MOBILE) return;
    // A suggestion row removes itself while handling this very mousedown —
    // a detached target was inside the panel a moment ago.
    if (!e.target.isConnected) return;
    if (panel.contains(e.target) || (fab && fab.contains(e.target))) return;
    var bell = $('notif-bell-btn');
    if (bell && bell.contains(e.target)) return;
    // Keep it open while the writer has unsent text.
    if (S.view === 'compose' && $('inbox-c-body').value.trim()) return;
    if (S.view === 'thread' && $('inbox-input').value.trim()) return;
    close();
  });
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    pollStatus();
    if (S.open && S.view === 'thread') { pollThread().then(markRead); }
  });
  window.addEventListener('pagehide', function () {
    clearInterval(timers.status); clearInterval(timers.thread); clearInterval(timers.list);
  });

  show(S.view);
  pollStatus();
  timers.status = setInterval(pollStatus, STATUS_MS);

  // Deep link: /anything?inbox=<thread id> opens that conversation.
  try {
    var want = new URLSearchParams(location.search).get('inbox');
    if (want && CHAT && /^\d+$/.test(want)) openThread(parseInt(want, 10));
    else if (want === 'open') open();
  } catch (e) {}

  window.Inbox = {
    open: open, close: close, toggle: toggle, compose: compose, openThread: openThread,
    refresh: pollStatus
  };
})();
