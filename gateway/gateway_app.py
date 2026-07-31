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

import logging
import os
import sys
import threading
import time

import requests
from flask import (Flask, abort, make_response, redirect, render_template,
                   request)
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

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

APP_INTERNAL_URL = os.environ.get(
    'GATE_APP_INTERNAL_URL', 'http://10.201.2.101:5400').rstrip('/')
SESSION_HOURS = int(os.environ.get('GATE_SESSION_HOURS', '12'))
COOKIE_NAME = os.environ.get('GATE_COOKIE_NAME', '__Host-321gate')
PENDING_COOKIE_NAME = COOKIE_NAME + '-pending'
PENDING_MINUTES = 10          # how long the email→code handoff stays valid
INTERNAL_TIMEOUT = 10         # seconds to wait on the tunnel API

app = Flask(__name__, static_url_path='/__gate/static')
logging.basicConfig(level=logging.INFO, format='%(message)s')
log = app.logger

# Two independent signers so a pending cookie can never be replayed as a
# session cookie: same key, different salt/purpose.
_session_signer = URLSafeTimedSerializer(GATE_SECRET_KEY, salt='gate-session')
_pending_signer = URLSafeTimedSerializer(GATE_SECRET_KEY, salt='gate-pending')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client_ip():
    """Real client IP. Caddy is the only thing that can reach this process
    (it binds 127.0.0.1) and always sets X-Forwarded-For; the leftmost entry
    is the connecting client Caddy saw."""
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.remote_addr or ''


def _safe_next(raw):
    """Only same-site relative paths — mirrors the main app's open-redirect
    guard. Anything odd (or a path back into the gate itself) collapses to /."""
    if not raw or not raw.startswith('/') or raw.startswith('//'):
        return '/'
    if raw.startswith('/__gate'):
        return '/'
    return raw


def _call_internal(path, payload):
    """POST to the main app over the tunnel. Returns the parsed JSON dict or
    None on any failure — callers stay generic toward the visitor either way."""
    try:
        r = requests.post(
            APP_INTERNAL_URL + path,
            json=payload,
            headers={'X-Gateway-Secret': GATE_SHARED_SECRET},
            timeout=INTERNAL_TIMEOUT,
        )
        if r.status_code == 200:
            return r.json()
        log.error('internal API %s returned HTTP %s', path, r.status_code)
    except Exception as e:
        log.error('internal API %s unreachable: %s', path, e)
    return None


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
    resp.delete_cookie(name, path='/')


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
        # email has an account, and we proceed to the code page regardless.
        _call_internal('/internal/gateway/otp/request',
                       {'email': email, 'client_ip': ip})
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
        result = _call_internal('/internal/gateway/otp/verify',
                                {'email': email, 'code': code,
                                 'client_ip': ip})
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
            resp.headers['X-Gate-Email'] = data.get('e', '')
            return resp
        except (BadSignature, SignatureExpired):
            pass
    if _wants_json():
        return ('', 401)
    original_uri = request.headers.get('X-Forwarded-Uri', '/')
    resp = make_response('', 302)
    resp.headers['Location'] = '/__gate/login?next=' + _safe_next(original_uri)
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
    can tell 'gateway down' apart from 'app unreachable'."""
    return {'ok': True}
