/* Projects module — the two hooks that run on EVERY page of someone who has
   module access (base.html loads this file right after app.js, only for
   them; it is served by the gated /projects/assets/ route, so nobody else's
   browser ever receives it — that is why this text is not in app.js).

   1. window.NavSearchExtra — app.js's sidebar search asks it for extra
      result groups. We answer with MY projects and their tasks matching the
      words, from the membership-scoped POST /api/projects/search (POST so
      the words never sit in a URL: perf log, history, referrers).
   2. The bell's label for this module's notification kind.

   No DOM is built here: app.js renders the groups with textContent and
   accepts only same-site hrefs. Hrefs are built from integers. */
(function () {
  'use strict';

  var id = function (v) { return (typeof v === 'number' && isFinite(v) && v > 0) ? Math.floor(v) : 0; };

  window.NavSearchExtra = function (q, signal) {
    q = String(q || '').trim().slice(0, 200);
    if (q.length < 2) return Promise.resolve(null);
    return fetch('/api/projects/search', {
      method: 'POST', credentials: 'same-origin', cache: 'no-store', signal: signal,
      headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
      body: JSON.stringify({q: q}),
    }).then(function (r) {
      // Anything but JSON (access removed → the plain 404 page; session
      // ended → the login page) just means "no extra results".
      if (!r.ok || (r.headers.get('Content-Type') || '').indexOf('application/json') < 0) return null;
      return r.json();
    }).then(function (j) {
      var res = (j && Array.isArray(j.results)) ? j.results : [];
      var items = [];
      res.forEach(function (x) {
        var pid = id(x && x.project_id);
        if (!pid || typeof x.title !== 'string' || !x.title) return;
        var tid = x.type === 'task' ? id(x.task_id) : 0;
        if (x.type === 'task' && !tid) return;
        items.push({
          href: '/projects/' + pid + (tid ? '?tab=tasks&task=' + tid : ''),
          title: x.title,
          sub: (tid ? 'Task · ' : 'Project · ') + (typeof x.sub === 'string' ? x.sub : ''),
        });
      });
      return items.length ? {groups: [{head: 'Projects', items: items}]} : null;
    });
  };

  // `_NOTIF_KIND_LABELS` is a top-level const of app.js (same script scope).
  try {
    if (typeof _NOTIF_KIND_LABELS === 'object' && _NOTIF_KIND_LABELS) _NOTIF_KIND_LABELS.project = 'Project';
  } catch (e) { /* app.js changed shape: the bell then shows the raw kind */ }
})();
