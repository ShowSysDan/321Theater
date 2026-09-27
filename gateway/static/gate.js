/* Gateway status pill (3.5.0). Keeps the server-rendered "Gateway online /
   offline" indicator current while the page sits open, so someone waiting on
   the email or code form sees the outage (or the recovery) without
   reloading. /__gate/uplink only reads the gateway's cached state and never
   triggers a probe, so polling it costs nothing over the tunnel. */
(function () {
  'use strict';
  var pill = document.getElementById('gate-uplink');
  if (!pill || !window.fetch) return;
  var text = pill.querySelector('.gate-uplink-text');
  var notice = document.getElementById('gate-offline');
  var STATES = ['online', 'offline', 'unknown'];
  var state = pill.getAttribute('data-state');
  var timer = null;

  function apply(next) {
    if (STATES.indexOf(next) < 0) next = 'unknown';
    if (next === state) return;
    state = next;
    STATES.forEach(function (s) {
      pill.classList.toggle('gate-uplink--' + s, s === next);
    });
    pill.setAttribute('data-state', next);
    text.textContent = pill.getAttribute('data-label-' + next) || next;
    if (notice) notice.hidden = next !== 'offline';
  }

  // Slow while healthy, quick while down (to show recovery), quickest while
  // the gateway itself is still on its first probe after a restart.
  function delay() {
    return state === 'online' ? 30000 : state === 'offline' ? 10000 : 3000;
  }

  function schedule() {
    clearTimeout(timer);
    if (!document.hidden) timer = setTimeout(poll, delay());
  }

  function poll() {
    clearTimeout(timer);
    fetch('/__gate/uplink', {
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { apply(d && d.state); },
            function () { apply('unknown'); })  // our own connection blipped
      .then(schedule, schedule);
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) clearTimeout(timer);
    else poll();
  });
  schedule();
})();
