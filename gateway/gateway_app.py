"""3·2·1→Theater companion gateway.

Runs on the public VPS (cyclorama) — NOT on the internal app server. This is
the auth half of the public entrance: Caddy terminates TLS and asks this app,
via forward_auth, whether each request carries a valid gate cookie. Requests
without one land on the email → one-time-code flow below; the code itself is
generated, stored, and checked by the MAIN app over the WireGuard tunnel
(/internal/gateway/otp/*), so this process holds no database and no SMTP
credentials — only two signing/shared secrets from the environment.

Deliberately dependency-light (flask + requests) and stateless: the only
"session" is a signed cookie, so there is nothing on the VPS to replicate,
back up, or steal beyond the two secrets. See README.md in this directory for
the full theory of operation and install runbook.
"""

import collections
import ipaddress
import itertools
import logging
import os
import re
import sys
import threading
import time
import traceback

from urllib.parse import quote

import requests
from flask import (Flask, make_response, redirect, render_template, request)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from urllib3.exceptions import NewConnectionError

# ── Configuration (environment only — no config files, no secrets on disk) ───

GATE_SECRET_KEY = os.environ.get('GATE_SECRET_KEY', '')
GATE_SHARED_SECRET = os.environ.get('GATE_SHARED_SECRET', '')
if not GATE_SECRET_KEY or not GATE_SHARED_SECRET:
    sys.stderr.write(
        'FATAL: GATE_SECRET_KEY and GATE_SHARED_SECRET must both be set.\n'
        'Generate each with:  python3 -c "import secrets; print(secrets.token_hex(32))"\n'
        '(GATE_SHARED_SECRET must equal GATEWAY_SHARED_SECRET on the app server.)\n'
    )
    sys.exit(1)

# One or more 321T servers, in preference order. GATE_APP_INTERNAL_URLS
# (comma-separated) lists every installation for primary/secondary
# redundancy; the single-URL GATE_APP_INTERNAL_URL is honored when the
# list isn't set. With multiple servers the gateway polls each one's
# /internal/cluster/primary probe and sends OTP traffic to whichever
# currently holds the primary role (the app's own leader election).
_urls_env = (os.environ.get('GATE_APP_INTERNAL_URLS', '')
             or os.environ.get('GATE_APP_INTERNAL_URL',
                               'http://10.201.2.101:5400'))
APP_INTERNAL_URLS = [u.strip().rstrip('/') for u in _urls_env.split(',')
                     if u.strip()]
UPSTREAM_CACHE_SECONDS = 10   # how long a primary answer is trusted
SESSION_HOURS = int(os.environ.get('GATE_SESSION_HOURS', '12'))
COOKIE_NAME = os.environ.get('GATE_COOKIE_NAME', '__Host-321gate')
PENDING_COOKIE_NAME = COOKIE_NAME + '-pending'
PENDING_MINUTES = 10          # how long the email→code handoff stays valid
INTERNAL_TIMEOUT = 10         # seconds to wait on the tunnel API
CONNECT_TIMEOUT = 4           # seconds to establish a connection over it
                              # (tunnel healthy = milliseconds; tunnel down =
                              # packets vanish, so don't make visitors wait
                              # the full read timeout to find out)

app = Flask(__name__, static_url_path='/__gate/static')
# The only bodies this service accepts are a one-field email or code form.
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024
logging.basicConfig(level=logging.INFO, format='%(message)s')
log = app.logger

# Two independent signers so a pending cookie can never be replayed as a
# session cookie: same key, different salt/purpose.
_session_signer = URLSafeTimedSerializer(GATE_SECRET_KEY, salt='gate-session')
_pending_signer = URLSafeTimedSerializer(GATE_SECRET_KEY, salt='gate-pending')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client_ip():
    """Real client IP, as Caddy saw it. Caddy is the only thing that can
    reach this process (it binds 127.0.0.1) and it puts the connecting
    address LAST in X-Forwarded-For. Anything to its left came from the
    client and can be forged (Caddy >= 2.5 drops it unless trusted_proxies
    is configured; don't depend on that). Validated as an IP address so a
    forged value can never reach a log line that fail2ban acts on."""
    fwd = request.headers.get('X-Forwarded-For', '')
    candidate = fwd.rsplit(',', 1)[-1].strip() if fwd else ''
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return request.remote_addr or ''


_CONTROL_CHARS = re.compile(r'[\x00-\x1f\x7f]')


def _safe_next(raw):
    """Only same-site relative paths. Anything else collapses to /:
    '//host' (protocol-relative); any backslash (browsers may read '\\' as
    '/'); any control character, because browsers silently STRIP tab/CR/LF
    from URLs, so '/<TAB>/evil.com' passes the '//' test here and then
    lands on evil.com (Chromium-verified; open redirect before 3.5.0, and a
    newline also 500'd the redirect after the code was already spent); and
    paths back into the gate itself."""
    if not raw or not raw.startswith('/') or raw.startswith('//'):
        return '/'
    if '\\' in raw or _CONTROL_CHARS.search(raw) or raw.startswith('/__gate'):
        return '/'
    return raw


def _tunnel():
    """HTTP session for everything sent to a 321T server. trust_env=False
    means no proxy variables and no ~/.netrc, so the shared secret and the
    visitor's email/code can only go straight to the app server. Every call
    also passes allow_redirects=False: a redirect is never a valid answer,
    and requests would carry X-Gateway-Secret along to wherever it points
    (it only strips Authorization on a cross-host redirect)."""
    s = requests.Session()
    s.trust_env = False
    return s


def _never_delivered(exc):
    """True only when a request failed while CONNECTING, so the server
    cannot have received the payload: a connect timeout, or refused / no
    route / name failure (urllib3 NewConnectionError). A connection dropped
    AFTER sending ('Connection aborted') is also a ConnectionError, but the
    app may have acted on it, so it doesn't count."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, requests.exceptions.ConnectionError):
        inner = exc.args[0] if exc.args else None
        return isinstance(getattr(inner, 'reason', None), NewConnectionError)
    return False


def _failure_label(exc):
    """Short, STABLE label for the journal. Raw exception text carries
    object addresses (which would make every probe look like a change) and
    internal addresses (which must never reach a visitor)."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return 'no-answer'     # packets vanish: the usual tunnel-down case
    if _never_delivered(exc):
        if 'refused' in str(exc).lower():
            return 'refused'   # tunnel up, app not listening
        return 'unreachable'
    if isinstance(exc, requests.exceptions.ConnectionError):
        return 'dropped'       # connected, then the connection died
    if isinstance(exc, requests.exceptions.Timeout):
        return 'slow'          # connected, no reply within the read timeout
    return 'error-' + type(exc).__name__


# ── Upstream selection: ONE app instance at a time ────────────────────────────
# With several 321T servers the gateway talks to exactly one: the current
# primary, per the app's own cluster election. The only thing that touches
# the others is discovery: the unauthenticated, read-only
# /internal/cluster/primary boolean, asked in list order and stopping at
# the first server that claims the role. The shared secret and every
# visitor payload (email, code, client IP) go to the primary alone.
#
# A request fails over to another server only when its payload was never
# delivered (_never_delivered). Re-sending one that reached a server would
# mail a second code (voiding the first) or spend a second guess against
# the 5-attempt cap. Before 3.5.0 any error, even an HTTP 500 after the
# app had acted, re-sent the payload to every other server in turn.
#
# Nobody claiming primary (mid-failover, database down, heartbeat broken,
# TEST_MODE servers) means no server should take public traffic: the app's
# probe answers 503 exactly then. The gateway sends nothing and says so,
# instead of picking one anyway as it did before.

_upstream_lock = threading.Lock()
_upstream_cache = {'url': None, 'at': 0.0, 'logged': None}


def _probe_primary(url):
    """'primary', 'standby' (answered 503: up but not the primary, can't see
    the database, or a TEST_MODE server) or a failure label."""
    try:
        with _tunnel() as s:
            r = s.get(url + '/internal/cluster/primary', timeout=(3, 3),
                      allow_redirects=False)
    except Exception as e:
        return _failure_label(e)
    if r.status_code == 200:
        return 'primary'
    if r.status_code == 503:
        return 'standby'
    return f'http-{r.status_code}'


def _discover_primary():
    """(url, None) for the first server in list order that claims primary
    (cached for UPSTREAM_CACHE_SECONDS), or (None, per-server summary).
    Journals only when the answer changes."""
    parts = []
    found = None
    for url in APP_INTERNAL_URLS:
        label = _probe_primary(url)
        if label == 'primary':
            found = url
            break
        parts.append(f'{url}={label}')
    with _upstream_lock:
        changed = found != _upstream_cache['logged']
        _upstream_cache.update(url=found, at=time.time(), logged=found)
    if changed:
        if found:
            log.info('GATE_UPSTREAM primary=%s', found)
        else:
            log.warning('GATE_UPSTREAM no server claims primary: %s',
                        ' '.join(parts))
    return found, (None if found else ' '.join(parts))


def _forget_upstream():
    with _upstream_lock:
        _upstream_cache.update(url=None, at=0.0)


def _pick_upstream():
    """The one server to talk to right now, or None when no server claims
    the primary role. Single-server config: always that server, no
    discovery (its health endpoint already covers the database)."""
    if len(APP_INTERNAL_URLS) == 1:
        return APP_INTERNAL_URLS[0]
    with _upstream_lock:
        url, at = _upstream_cache['url'], _upstream_cache['at']
    if url and time.time() - at < UPSTREAM_CACHE_SECONDS:
        return url
    return _discover_primary()[0]


class UplinkDown(Exception):
    """The payload could not be delivered to any 321T server: tunnel or
    network down, or no server holds the primary role.

    Safe to tell the visitor: it's decided before any app sees the payload,
    so it can't depend on whether the email has an account or the code is
    right. Anything that DID reach an app (an HTTP error, a read timeout, a
    dropped connection, a garbled body) stays generic, as before."""


def _call_internal(path, payload):
    """POST a visitor's request to the ONE current app instance. Returns the
    parsed JSON dict, or None when that instance answered but not usefully
    (callers stay generic toward the visitor then). Raises UplinkDown when
    the payload couldn't be delivered. Fails over at most once, to a freshly
    discovered primary, and only when the first server never received the
    payload (covers the just-failed-over window)."""
    url = _pick_upstream()
    tried = set()
    while url and url not in tried:
        tried.add(url)
        try:
            with _tunnel() as s:
                r = s.post(url + path, json=payload,
                           headers={'X-Gateway-Secret': GATE_SHARED_SECRET},
                           timeout=(CONNECT_TIMEOUT, INTERNAL_TIMEOUT),
                           allow_redirects=False)
        except Exception as e:
            _forget_upstream()
            if _never_delivered(e):
                log.error('internal API %s on %s unreachable: %s',
                          path, url, _failure_label(e))
                url = (_discover_primary()[0]
                       if len(APP_INTERNAL_URLS) > 1 else None)
                continue
            log.error('internal API %s on %s failed after sending: %s',
                      path, url, _failure_label(e))
            return None
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                _uplink_note_reached()
                return data
            log.error('internal API %s on %s returned a non-JSON body',
                      path, url)
        else:
            log.error('internal API %s on %s returned HTTP %s',
                      path, url, r.status_code)
        _forget_upstream()
        return None
    if not tried:
        log.error('internal API %s: no server claims primary, nothing sent',
                  path)
    _uplink_note_unreachable()
    raise UplinkDown()


# ── Uplink status (the public "Gateway online / offline" indicator) ──────────
# When the building's internet drops, the WireGuard tunnel drops with it and
# every code request dies before reaching the app. That used to look exactly
# like success (the code page shows either way), so people waited for an
# email that was never coming. A background thread now probes the app's
# authenticated /internal/gateway/health endpoint and every gate page shows
# the result.
#
# SECURITY: the public side of this is ONE word: 'online', 'offline' or
# 'unknown'. Server URLs, exception text, HTTP codes and server counts go to
# the journal only (VPS-local, root-readable, same as gateway.env itself).
# Nothing a visitor does can make the gateway probe: /__gate/uplink and the
# pages only read the cached state, so it can't be used to hammer the tunnel.

UPLINK_INTERVAL_ONLINE = 30    # seconds between probes while online
UPLINK_INTERVAL_OFFLINE = 10   # ...while offline, to spot recovery quickly
UPLINK_MIN_GAP = 3             # floor between probes when woken early
UPLINK_READ_TIMEOUT = 5

_uplink_lock = threading.Lock()
_uplink = {'state': 'unknown', 'reason': None}
_uplink_wake = threading.Event()


def _uplink_state():
    with _uplink_lock:
        return _uplink['state']


def _set_uplink(online, reason=None):
    """Record a result; journal a line only when the state or reason
    changes, so a long outage is two lines, not one every 10 s."""
    state = 'online' if online else 'offline'
    with _uplink_lock:
        changed = (state, reason) != (_uplink['state'], _uplink['reason'])
        _uplink.update(state=state, reason=reason)
    if not changed:
        return
    if online:
        log.info('GATE_UPLINK state=online')
    else:
        hint = (' (404 = GATE_SHARED_SECRET does not match the app\'s '
                'GATEWAY_SHARED_SECRET, or the app server is older than '
                '3.5.0)') if reason and 'http-404' in reason else ''
        log.warning('GATE_UPLINK state=offline %s%s', reason, hint)


def _probe_uplink():
    """One end-to-end check of the ONE instance the gateway would use:
    discover the primary (multi-server only; this also refreshes the
    routing cache), then call that server's authenticated health endpoint.
    (True, None) when it answers {"ok": true}; else (False, reason)."""
    if len(APP_INTERNAL_URLS) == 1:
        url = APP_INTERNAL_URLS[0]
    else:
        url, summary = _discover_primary()
        if url is None:
            return False, 'no-primary ' + summary
    try:
        with _tunnel() as s:
            r = s.get(url + '/internal/gateway/health',
                      headers={'X-Gateway-Secret': GATE_SHARED_SECRET},
                      timeout=(CONNECT_TIMEOUT, UPLINK_READ_TIMEOUT),
                      allow_redirects=False)
    except Exception as e:
        return False, f'{url}={_failure_label(e)}'
    if r.status_code != 200:
        return False, f'{url}=http-{r.status_code}'
    try:
        if r.json().get('ok') is True:
            return True, None
    except Exception:
        pass
    return False, f'{url}=bad-response'


def _uplink_monitor():
    while True:
        started = time.monotonic()
        try:
            _set_uplink(*_probe_uplink())
        except Exception as e:      # the monitor must never die
            log.error('GATE_UPLINK probe crashed: %s', e)
        online = _uplink_state() == 'online'
        _uplink_wake.wait(UPLINK_INTERVAL_ONLINE if online
                          else UPLINK_INTERVAL_OFFLINE)
        _uplink_wake.clear()
        elapsed = time.monotonic() - started
        if elapsed < UPLINK_MIN_GAP:
            time.sleep(UPLINK_MIN_GAP - elapsed)


_threads_lock = threading.Lock()
_threads = {}


def _ensure_background_threads():
    """Start the uplink monitor and the log relay on the first request, in
    the serving process. Lazy on purpose: importing the module (tests, a
    future --preload) must not open tunnel connections, and a thread
    started before a fork would not survive into the worker. is_alive()
    also restarts one that died."""
    for name, target in (('uplink-monitor', _uplink_monitor),
                         ('log-relay', _relay_sender)):
        t = _threads.get(name)
        if t is not None and t.is_alive():
            continue
        with _threads_lock:
            t = _threads.get(name)
            if t is None or not t.is_alive():
                t = threading.Thread(target=target, name=name, daemon=True)
                t.start()
                _threads[name] = t


def _uplink_note_unreachable():
    """An OTP call found no server at all: flip the indicator now (the
    visitor is about to be told) and have the monitor confirm right away.
    Already offline: leave the monitor's reason alone, or every retry during
    an outage would journal two reason flips."""
    if _uplink_state() != 'offline':
        _set_uplink(False, 'otp-call=no-server-reachable')
    _uplink_wake.set()


def _uplink_note_reached():
    """An OTP call got an answer. That doesn't prove the database (the OTP
    API answers generically when PostgreSQL is down), so rather than flip
    to online, ask for a fresh probe if the indicator says otherwise."""
    if _uplink_state() != 'online':
        _uplink_wake.set()


# ── Log relay to the app's syslog server (3.5.0) ─────────────────────────────
# Every event this service logs (GATE_OTP_*, GATE_SESSION_EXPIRED,
# GATE_UPLINK, GATE_UPSTREAM, errors) also goes to the remote syslog server
# the main app uses. The VPS can't reach that server itself (the WireGuard
# firewall rule confines it to the app's port, and it must stay that way),
# so events ride the tunnel to the ONE primary instance, which writes them
# to its own syslog logger (the journal there, plus the remote server set in
# Settings → Server & Logs). This journal keeps its own copy regardless.
#
# Request threads only append to a bounded in-memory queue; one sender
# thread delivers batches. While the link is down, events wait (oldest
# dropped past RELAY_MAX_QUEUE, and the loss itself is reported) and go out
# with their original timestamps when it returns, so an outage's own
# GATE_UPLINK lines still reach syslog. Delivery is at-least-once: a batch
# whose reply was lost is sent again. The gunicorn access log (one line per
# forward_auth check) is not relayed.

RELAY_MAX_QUEUE = 2000
RELAY_BATCH = 100
RELAY_COALESCE = 1.0    # seconds to gather a burst into one request
RELAY_BACKOFF = 15      # seconds after a failed delivery

_relay_lock = threading.Lock()
_relay_queue = collections.deque()
_relay_state = {'seq': 0, 'dropped': 0}
_relay_wake = threading.Event()
# Relay trouble is noted here, NOT on `log`: logging it there would queue
# another event per failure and grow the queue it's failing to empty.
_relay_note = logging.getLogger('gateway_relay')


class _RelayHandler(logging.Handler):
    """Queues each record for the app's syslog. Never blocks, never raises."""

    def emit(self, record):
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += ' | ' + ''.join(traceback.format_exception_only(
                    *record.exc_info[:2])).strip()
            with _relay_lock:
                if len(_relay_queue) >= RELAY_MAX_QUEUE:
                    _relay_queue.popleft()
                    _relay_state['dropped'] += 1
                _relay_state['seq'] += 1
                _relay_queue.append({'seq': _relay_state['seq'],
                                     'at': record.created,
                                     'level': record.levelname,
                                     'msg': msg[:2000]})
            _relay_wake.set()
        except Exception:
            pass


def _relay_deliver(events):
    """POST one batch to the current primary. True only on a 200."""
    url = _pick_upstream()
    if not url:
        return False
    try:
        with _tunnel() as s:
            r = s.post(url + '/internal/gateway/log', json={'events': events},
                       headers={'X-Gateway-Secret': GATE_SHARED_SECRET},
                       timeout=(CONNECT_TIMEOUT, INTERNAL_TIMEOUT),
                       allow_redirects=False)
    except Exception as e:
        if _never_delivered(e):
            _forget_upstream()
        return False
    return r.status_code == 200


def _relay_sender():
    failing = False
    while True:
        _relay_wake.wait()
        time.sleep(RELAY_COALESCE)
        _relay_wake.clear()
        while True:
            try:
                with _relay_lock:
                    batch = [dict(e) for e in
                             itertools.islice(_relay_queue, RELAY_BATCH)]
                    dropped = _relay_state['dropped']
                if not batch and not dropped:
                    break
                events = [{k: e[k] for k in ('at', 'level', 'msg')}
                          for e in batch]
                if dropped:
                    events.append({
                        'at': time.time(), 'level': 'WARNING',
                        'msg': f'GATE_LOG_RELAY dropped={dropped} oldest '
                               f'events (queue full while undeliverable)'})
                if not _relay_deliver(events):
                    if not failing:
                        failing = True
                        _relay_note.warning(
                            'GATE_LOG_RELAY paused: app not reachable; '
                            'queueing events for syslog')
                    time.sleep(RELAY_BACKOFF)
                    continue
                if failing:
                    failing = False
                    _relay_note.info('GATE_LOG_RELAY resumed')
                last = batch[-1]['seq'] if batch else 0
                with _relay_lock:
                    while _relay_queue and _relay_queue[0]['seq'] <= last:
                        _relay_queue.popleft()
                    _relay_state['dropped'] -= dropped
            except Exception as e:      # the sender must never die
                _relay_note.error('GATE_LOG_RELAY error: %s', e)
                time.sleep(RELAY_BACKOFF)


log.addHandler(_RelayHandler(level=logging.INFO))


def _set_cookie(resp, name, value, max_age):
    resp.set_cookie(
        name, value,
        max_age=max_age,
        secure=True,        # required by the __Host- prefix
        httponly=True,
        samesite='Lax',
        path='/',
    )


def _clear_cookie(resp, name):
    # The deletion Set-Cookie must itself satisfy the __Host- prefix rules
    # (Secure, Path=/, no Domain) — browsers silently DROP a __Host- cookie
    # write that lacks Secure, which would make sign-out a no-op while
    # still showing the "signed out" page.
    resp.delete_cookie(name, path='/', secure=True, httponly=True,
                       samesite='Lax')


def _wants_json():
    """AJAX/fetch callers should get a clean 401, not an HTML redirect."""
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    accept = request.headers.get('Accept', '')
    return 'application/json' in accept and 'text/html' not in accept


# ── Cheap in-process rate limiting (belt-and-suspenders) ─────────────────────
# The authoritative limits live in the main app's database; this bucket just
# keeps one rude client from hammering the tunnel. Single gunicorn worker, so
# in-memory is fine.

_bucket_lock = threading.Lock()
_buckets = {}   # ip -> [timestamps]
_BUCKET_MAX = 12          # requests
_BUCKET_WINDOW = 60.0     # per seconds


def _rate_limited(ip):
    now = time.time()
    with _bucket_lock:
        stamps = [t for t in _buckets.get(ip, []) if now - t < _BUCKET_WINDOW]
        if len(stamps) >= _BUCKET_MAX:
            _buckets[ip] = stamps
            return True
        stamps.append(now)
        _buckets[ip] = stamps
        if len(_buckets) > 10000:   # memory backstop under address churn
            _buckets.clear()
        return False


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/__gate/login', methods=['GET', 'POST'])
def gate_login():
    next_url = _safe_next(request.values.get('next', ''))
    if request.method == 'POST':
        ip = _client_ip()
        if _rate_limited(ip):
            log.info('GATE_OTP_FAIL ip=%s reason=local-throttle', ip)
            return render_template('gate_email.html', next=next_url,
                                   error='Too many requests — wait a minute '
                                         'and try again.'), 429
        email = (request.form.get('email') or '').strip().lower()
        if not email or '@' not in email or len(email) > 254:
            return render_template('gate_email.html', next=next_url,
                                   error='Enter a valid email address.')
        # Fire the request; the response is identical whether or not the
        # email has an account, and we proceed to the code page regardless
        # — unless the request never left the VPS (see UplinkDown).
        try:
            _call_internal('/internal/gateway/otp/request',
                           {'email': email, 'client_ip': ip})
        except UplinkDown:
            log.warning('GATE_OTP_UNSENT ip=%s reason=uplink-down', ip)
            return render_template(
                'gate_email.html', next=next_url, email=email,
                error='No code was sent: the 3·2·1→Theater gateway is '
                      'offline right now. Please try again in a few '
                      'minutes.'), 503
        token = _pending_signer.dumps({'e': email, 'n': next_url})
        resp = make_response(redirect('/__gate/code'))
        _set_cookie(resp, PENDING_COOKIE_NAME, token, PENDING_MINUTES * 60)
        return resp
    return render_template('gate_email.html', next=next_url, error=None)


@app.route('/__gate/code', methods=['GET', 'POST'])
def gate_code():
    raw = request.cookies.get(PENDING_COOKIE_NAME, '')
    try:
        pending = _pending_signer.loads(raw, max_age=PENDING_MINUTES * 60)
    except (BadSignature, SignatureExpired):
        return redirect('/__gate/login')
    email = pending.get('e', '')
    next_url = _safe_next(pending.get('n', '/'))

    if request.method == 'POST':
        ip = _client_ip()
        if _rate_limited(ip):
            log.info('GATE_OTP_FAIL ip=%s reason=local-throttle', ip)
            return render_template('gate_code.html', email=email,
                                   error='Too many attempts — wait a minute '
                                         'and try again.'), 429
        code = (request.form.get('code') or '').strip().replace(' ', '')
        try:
            result = _call_internal('/internal/gateway/otp/verify',
                                    {'email': email, 'code': code,
                                     'client_ip': ip})
        except UplinkDown:
            # Not a GATE_OTP_FAIL: the app never saw the guess (no attempt
            # was used up), and fail2ban must not ban people for retrying
            # during an outage.
            log.warning('GATE_OTP_UNCHECKED ip=%s reason=uplink-down', ip)
            return render_template(
                'gate_code.html', email=email,
                error='Your code couldn’t be checked: the 3·2·1→Theater '
                      'gateway is offline right now. Try again in a few '
                      'minutes (a code stays valid for 10 minutes after it '
                      'was sent).'), 503
        if result and result.get('valid'):
            log.info('GATE_OTP_OK ip=%s', ip)
            token = _session_signer.dumps({'e': email, 'v': 1})
            resp = make_response(redirect(next_url))
            _set_cookie(resp, COOKIE_NAME, token, SESSION_HOURS * 3600)
            _clear_cookie(resp, PENDING_COOKIE_NAME)
            return resp
        # Wrong, expired, burned, or unknown email — one generic message,
        # and a fail2ban-friendly log line.
        log.info('GATE_OTP_FAIL ip=%s', ip)
        return render_template('gate_code.html', email=email,
                               error='That code is invalid or has expired.')
    return render_template('gate_code.html', email=email, error=None)


@app.route('/__gate/check')
def gate_check():
    """Caddy's forward_auth target. 200 = let the request through to the app;
    anything else is returned to the browser (302 to the login form, or 401
    for fetch/XHR so in-app JavaScript fails cleanly instead of following a
    redirect into HTML)."""
    raw = request.cookies.get(COOKIE_NAME, '')
    if raw:
        try:
            data = _session_signer.loads(raw, max_age=SESSION_HOURS * 3600)
            resp = make_response('', 200)
            # Informational only — the app must never treat this as auth.
            # ASCII-sanitized: a non-latin-1 header value would raise while
            # building the response and turn a valid check into a 500.
            resp.headers['X-Gate-Email'] = (
                data.get('e', '').encode('ascii', 'ignore').decode('ascii'))
            return resp
        except SignatureExpired:
            # A previously-valid session just ran out (as opposed to a
            # visitor with no/garbage cookie). Log page navigations only —
            # a dead tab's fetch polls would spam one line per poll.
            if not _wants_json():
                log.info('GATE_SESSION_EXPIRED ip=%s', _client_ip())
        except BadSignature:
            pass
    if _wants_json():
        return ('', 401)
    original_uri = request.headers.get('X-Forwarded-Uri', '/')
    resp = make_response('', 302)
    # quote() the next value or a deep link with its own query string
    # (/shows/5?tab=labor&day=2) gets split at the first '&' when the login
    # page parses it, silently dropping parameters after auth.
    resp.headers['Location'] = (
        '/__gate/login?next=' + quote(_safe_next(original_uri), safe='/'))
    return resp


@app.route('/__gate/status')
def gate_status():
    """Machine-readable countdown for the app's session-expiry watchdog
    (static/js/app.js in the main repo). The gate cookie is HttpOnly and its
    12-hour deadline lives in the itsdangerous signing timestamp, so page
    JavaScript has no way to know when the gate will slam shut — this reports
    seconds remaining without touching anything. Pre-auth by design (it sits
    under /__gate/, outside forward_auth): an expired or missing cookie
    answers authenticated=false instead of a redirect, which is exactly the
    signal the watchdog needs. Read-only — it never re-issues the cookie;
    re-verifying the email code is the only way to restart the 12 h clock."""
    payload = {'authenticated': False, 'seconds_remaining': 0,
               'lifetime_seconds': SESSION_HOURS * 3600}
    raw = request.cookies.get(COOKIE_NAME, '')
    if raw:
        try:
            _, ts = _session_signer.loads(raw, max_age=SESSION_HOURS * 3600,
                                          return_timestamp=True)
            remaining = int(ts.timestamp() + SESSION_HOURS * 3600 - time.time())
            if remaining > 0:
                payload.update(authenticated=True,
                               seconds_remaining=remaining)
        except (BadSignature, SignatureExpired):
            pass
    resp = make_response(payload)
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/__gate/uplink')
def gate_uplink():
    """Live value for the gate pages' "Gateway online / offline" pill
    (static/gate.js). Pre-auth by necessity (the login page needs it),
    which is why it answers exactly one word and reads only the monitor's
    cached state: no URLs, no reasons, no server count, and no way for a
    caller to trigger a probe. Also a clean target for an external uptime
    monitor (alert when the body stops saying "online")."""
    resp = make_response({'state': _uplink_state()})
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.route('/__gate/signout')
def gate_signout():
    resp = make_response(render_template('gate_signout.html'))
    _clear_cookie(resp, COOKIE_NAME)
    _clear_cookie(resp, PENDING_COOKIE_NAME)
    return resp


@app.route('/__gate/healthz')
def gate_healthz():
    """Liveness only — deliberately does NOT probe the tunnel, so monitoring
    can tell 'gateway down' apart from 'app unreachable' (the tunnel's own
    status is /__gate/uplink)."""
    return {'ok': True}


@app.before_request
def _start_background_threads():
    _ensure_background_threads()


@app.context_processor
def _inject_uplink():
    return {'uplink_state': _uplink_state()}


# Gate pages load only their own CSS/JS/icon and post only to themselves.
# Nothing may frame them (no clickjacking the email/code forms), and no
# inline script can run even if something were ever reflected into a page.
# Applies to this service's responses only; the proxied app is untouched.
_SECURITY_HEADERS = {
    'Content-Security-Policy': (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; connect-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"),
    'X-Frame-Options': 'DENY',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'same-origin',
    # Belt-and-suspenders with the Caddyfile's X-Robots-Tag: nothing this
    # service emits should land in a search index or an AI training set.
    'X-Robots-Tag': 'noindex, nofollow, noarchive, nosnippet, noai, noimageai',
}


@app.after_request
def _security_headers(resp):
    for name, value in _SECURITY_HEADERS.items():
        resp.headers.setdefault(name, value)
    return resp
