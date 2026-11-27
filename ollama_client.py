"""ollama_client — the ONE way 321Theater talks to Ollama (3.55.0).

Dependency-free (stdlib only) and it never imports app.py. Ollama has no
authentication, so the app only ever talks to the Ollama on ITS OWN machine:

  * validate_url() accepts http(s)://localhost / 127.x.x.x / [::1] only (any
    port), no userinfo / path / query / fragment. A name is re-checked at call
    time (every address it resolves to must be loopback), so an /etc/hosts
    entry can't point "localhost" somewhere else.
  * One opener: no environment proxy (ProxyHandler({})), no redirects (a 3xx
    is an error), response size caps, and an overall time.monotonic()
    deadline on top of the per-read socket timeout.
  * Errors carry a short generic CODE, never the response body or the prompt.
    Nothing here logs prompt or answer text.

A server WITHOUT Ollama never calls it: AI work goes through the ai_jobs queue
(ai_module.py) and is claimed only by a worker whose own probe() passed.

AI node (3.61.0): the ONE exception to "this machine only". A dedicated box
runs Ollama on ITS loopback behind ai_node/ai_node.py, and an app server whose
.env names it (AI_NODE_URL / AI_NODE_SECRET / AI_NODE_CERT_SHA256, set here by
set_node()) sends its model calls there instead:
  * https only, the node's certificate PINNED by SHA-256 — checked right after
    the TLS handshake, before a byte of the request is sent (no CA, no
    hostname check: the fingerprint is the identity);
  * every request signed (HMAC-SHA256 over method, path, time, nonce and the
    body's SHA-256 — ai_node.signature() is the other half);
  * the same opener rules (no env proxy, no redirects, caps, deadlines).
The node's base URL is accepted ONLY when it equals the configured one;
validate_url() stays loopback-only for everything else.
"""
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_URL = 'http://127.0.0.1:11434'
_LINE_MAX = 65536                 # one NDJSON line
_TAGS_MAX = 1024 * 1024           # /api/tags body
_MODEL_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$')
_INSTALL_PATHS = ('/usr/local/bin/ollama', '/usr/bin/ollama', '/bin/ollama')


class OllamaError(Exception):
    """A failure with a short, content-free code ('http_500', 'bad_json'…)."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class OllamaUnavailable(OllamaError):
    """Nothing was delivered: Ollama isn't listening / didn't answer the connect."""


class OllamaTimeout(OllamaError):
    pass


class OllamaTooLarge(OllamaError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):   # noqa: D401
        return None               # urllib then raises HTTPError for the 3xx


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


# ── AI node: pinned HTTPS + signed requests (3.61.0) ──────────────────────────

class _PinMismatch(OSError):
    """The node's certificate is not the pinned one (raised BEFORE sending)."""


class _PinnedConnection(http.client.HTTPSConnection):
    pin = b''

    def connect(self):
        super().connect()
        der = self.sock.getpeercert(binary_form=True)
        if not der or not hmac.compare_digest(hashlib.sha256(der).digest(), self.pin):
            try:
                self.sock.close()
            finally:
                self.sock = None
            raise _PinMismatch('cert_mismatch')


class _PinnedHandler(urllib.request.HTTPSHandler):
    def __init__(self, pin):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False          # identity = the pinned fingerprint
        ctx.verify_mode = ssl.CERT_NONE     # (checked in _PinnedConnection.connect)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(context=ctx)
        self._pin = pin

    def https_open(self, req):
        pin = self._pin

        def conn(host, **kw):
            c = _PinnedConnection(host, **kw)
            c.pin = pin
            return c
        return self.do_open(conn, req, context=self._context)


_NODE_LOCK = threading.Lock()
_NODE = {'key': None, 'base': None, 'secret': b'', 'opener': None}
_FP_RE = re.compile(r'^[0-9a-f]{64}$')


def normalize_fingerprint(fp):
    """'AB:CD:…' / 'abcd…' → 64 lower-case hex chars, or ''."""
    v = re.sub(r'[\s:]', '', str(fp or '')).lower()
    return v if _FP_RE.match(v) else ''


def validate_node_url(url):
    """(ok, message, normalized_base) for AI_NODE_URL: https://host:port."""
    if not isinstance(url, str) or not url.strip() or len(url) > 200:
        return False, 'AI_NODE_URL is empty.', None
    try:
        p = urlparse(url.strip())
        port = p.port
    except ValueError:
        return False, 'AI_NODE_URL is not a valid URL.', None
    if p.scheme != 'https':
        return False, 'AI_NODE_URL must start with https://.', None
    if p.username or p.password or '@' in (p.netloc or ''):
        return False, 'AI_NODE_URL may not contain a user name or password.', None
    if p.query or p.fragment or p.params or (p.path not in ('', '/')):
        return False, 'AI_NODE_URL is just https://address:port (no path).', None
    host = (p.hostname or '').lower()
    if not host:
        return False, 'AI_NODE_URL has no address.', None
    port = port or 443
    h = f'[{host}]' if ':' in host else host
    return True, '', f'https://{h}:{port}'


def set_node(url, secret, fingerprint):
    """Configure (or with url=None clear) the AI node. (ok, message)."""
    if not url:
        with _NODE_LOCK:
            _NODE.update(key=None, base=None, secret=b'', opener=None)
        return True, ''
    ok, msg, base = validate_node_url(url)
    if not ok:
        return False, msg
    if not isinstance(secret, str) or len(secret.strip()) < 32:
        return False, 'AI_NODE_SECRET must be at least 32 characters.'
    fp = normalize_fingerprint(fingerprint)
    if not fp:
        return False, 'AI_NODE_CERT_SHA256 must be the 64-hex-digit SHA-256 fingerprint.'
    key = (base, hashlib.sha256(secret.strip().encode()).hexdigest(), fp)
    with _NODE_LOCK:
        if _NODE['key'] != key:
            _NODE.update(key=key, base=base, secret=secret.strip().encode(),
                         opener=urllib.request.build_opener(
                             urllib.request.ProxyHandler({}), _NoRedirect,
                             _PinnedHandler(bytes.fromhex(fp))))
    return True, ''


def node_base():
    return _NODE['base']


def is_node(base):
    return bool(base) and base == _NODE['base']


def _node_headers(secret, method, path, body):
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    msg = '\n'.join((method, path, ts, nonce, hashlib.sha256(body or b'').hexdigest())).encode()
    return {'X-321-Node-Time': ts, 'X-321-Node-Nonce': nonce,
            'X-321-Node-Sig': hmac.new(secret, msg, hashlib.sha256).hexdigest()}


# ── URL / model validation ────────────────────────────────────────────────────

def _is_loopback_literal(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_url(url):
    """(ok, message, normalized_base). Localhost only — Ollama has no auth."""
    if not isinstance(url, str):
        return False, 'The Ollama URL must be text.', None
    url = url.strip()
    if not url or len(url) > 200:
        return False, 'Enter an Ollama URL such as http://127.0.0.1:11434.', None
    try:
        p = urlparse(url)
        port = p.port
    except ValueError:
        return False, 'That is not a valid URL.', None
    if p.scheme not in ('http', 'https'):
        return False, 'The Ollama URL must start with http:// or https://.', None
    if p.username or p.password or '@' in (p.netloc or ''):
        return False, 'The Ollama URL may not contain a user name or password.', None
    if p.query or p.fragment or p.params or (p.path not in ('', '/')):
        return False, 'The Ollama URL is just the address and port (no path).', None
    host = (p.hostname or '').lower()
    if host != 'localhost' and not _is_loopback_literal(host):
        return False, ('Ollama is reached on this server only (it has no password): '
                       'use localhost, 127.0.0.1 or [::1].'), None
    if port is not None and not (1 <= port <= 65535):
        return False, 'The port must be 1–65535.', None
    port = port or (443 if p.scheme == 'https' else 80)
    h = f'[{host}]' if ':' in host else host
    return True, '', f'{p.scheme}://{h}:{port}'


def valid_model(name):
    return isinstance(name, str) and bool(_MODEL_RE.match(name.strip()))


def model_matches(model, names):
    """True when `model` is among the installed names ('llama3.2' matches
    'llama3.2:latest'; a tagged name must match exactly)."""
    model = (model or '').strip()
    if not model:
        return False
    for n in names or ():
        if n == model:
            return True
        if ':' not in model and n.split(':', 1)[0] == model and n.endswith(':latest'):
            return True
    return False


def installed():
    """Is the ollama binary on this machine? (PATH, then the usual places —
    a systemd service's PATH may not include /usr/local/bin.)"""
    if shutil.which('ollama'):
        return True
    return any(os.path.isfile(p) and os.access(p, os.X_OK) for p in _INSTALL_PATHS)


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _check_host(base):
    """Every address the host resolves to must be loopback."""
    host = urlparse(base).hostname or ''
    if _is_loopback_literal(host):
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise OllamaUnavailable('dns')
    addrs = {i[4][0].split('%', 1)[0] for i in infos}
    if not addrs or not all(_is_loopback_literal(a) for a in addrs):
        raise OllamaError('not_loopback')


def _open(base, path, payload=None, timeout=5.0, read_timeout=None):
    """Open a request. `timeout` covers the connect AND the wait for the
    response headers (Ollama sends none until the model is loaded, the prompt
    evaluated, or a queued request got a runner); `read_timeout`, when given,
    then applies to each later read of the stream.

    OllamaUnavailable ('unreachable') = nothing was delivered (refused /
    connect or send failed). A timeout AFTER the request went out is an
    OllamaTimeout ('first_byte'); a reset after it went out is 'reset'."""
    data = json.dumps(payload).encode() if payload is not None else None
    method = 'POST' if data else 'GET'
    headers = {'Content-Type': 'application/json', 'User-Agent': '321Theater'}
    with _NODE_LOCK:
        node = dict(_NODE) if is_node(base) else None
    if node:
        norm, opener = node['base'], node['opener']
        headers.update(_node_headers(node['secret'], method, path, data))
    else:
        ok, _m, norm = validate_url(base)
        if not ok:
            raise OllamaError('bad_url')
        _check_host(norm)
        opener = _OPENER
    req = urllib.request.Request(norm + path, data=data, method=method, headers=headers)
    try:
        resp = opener.open(req, timeout=max(0.5, float(timeout)))
    except urllib.error.HTTPError as e:
        try:
            e.close()
        except Exception:
            pass
        if 300 <= e.code < 400:
            raise OllamaError('redirect')
        code = ''
        try:
            code = (e.headers.get('X-321-Node-Error') or '') if node else ''
        except Exception:
            pass
        code = re.sub(r'[^a-z0-9_]', '', code.lower())[:40]
        if node and code in ('ollama_unreachable', 'node_busy'):
            # the node answered, but the MODEL never got the request
            raise OllamaUnavailable(code)
        if code:
            raise OllamaError('node_' + code if not code.startswith('node_') else code)
        if e.code == 404:
            raise OllamaError('not_found')
        raise OllamaError(f'http_{e.code}')
    except urllib.error.URLError as e:
        if isinstance(getattr(e, 'reason', None), _PinMismatch):
            raise OllamaUnavailable('cert_mismatch')
        # urllib wraps every connect / send failure (refused, connect
        # timeout): nothing was delivered
        raise OllamaUnavailable('unreachable')
    except (socket.timeout, TimeoutError):
        # raised bare by getresponse(): the request went out, Ollama is busy
        # (loading the model / queued) and did not answer in time
        raise OllamaTimeout('first_byte')
    except (ConnectionError, OSError):
        raise OllamaError('reset')
    if read_timeout is not None:
        try:
            resp.fp.raw._sock.settimeout(max(0.5, float(read_timeout)))
        except Exception:
            pass                  # reads keep `timeout`; the deadline still applies
    return resp


def _read_capped(resp, cap):
    data = resp.read(cap + 1)
    if len(data) > cap:
        raise OllamaTooLarge('too_large')
    return data


def version(base, timeout=3.0):
    with _open(base, '/api/version', timeout=timeout) as r:
        try:
            v = json.loads(_read_capped(r, 4096) or b'{}').get('version')
        except (ValueError, AttributeError):
            raise OllamaError('bad_json')
    return str(v)[:40] if v else ''


def tags(base, timeout=4.0):
    """Installed model names (≤100, each ≤100 chars)."""
    with _open(base, '/api/tags', timeout=timeout) as r:
        try:
            body = json.loads(_read_capped(r, _TAGS_MAX) or b'{}')
        except ValueError:
            raise OllamaError('bad_json')
    out = []
    for m in (body.get('models') if isinstance(body, dict) else None) or []:
        n = m.get('name') if isinstance(m, dict) else None
        if isinstance(n, str) and n and len(n) <= 100:
            out.append(n)
        if len(out) >= 100:
            break
    return out


def probe(base, model):
    """This machine's Ollama: {installed, running, version, models, has_model,
    err (code only), ms, checked}. Never raises."""
    t0 = time.monotonic()
    st = {'installed': False if is_node(base) else installed(), 'running': False, 'version': '', 'models': [],
          'has_model': False, 'err': '', 'ms': None, 'checked': time.time()}
    try:
        names = tags(base)
        st['running'] = True
        st['models'] = names[:50]
        st['has_model'] = model_matches(model, names)
        try:
            st['version'] = version(base)
        except OllamaError:
            pass
        if st['running']:
            st['installed'] = True    # it answers, so it is installed (maybe a container)
    except OllamaError as e:
        st['err'] = e.code
    except Exception as e:            # never raise from a probe
        st['err'] = type(e).__name__
    st['ms'] = int((time.monotonic() - t0) * 1000)
    return st


def _iter_lines(resp, deadline, max_bytes, should_cancel=None):
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise OllamaTimeout('deadline')
        if should_cancel and should_cancel():
            raise OllamaError('cancelled')
        try:
            line = resp.readline(_LINE_MAX + 1)
        except (socket.timeout, TimeoutError):
            raise OllamaTimeout('read_timeout')
        if not line:
            return
        if len(line) > _LINE_MAX:
            raise OllamaTooLarge('line_too_large')
        total += len(line)
        if total > max_bytes:
            raise OllamaTooLarge('too_large')
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            raise OllamaError('bad_stream')
        if not isinstance(obj, dict):
            raise OllamaError('bad_stream')
        if obj.get('error'):
            raise OllamaError('ollama_error')
        yield obj


def chat(base, model, messages, *, fmt='json', options=None, deadline_s=180.0,
         max_bytes=262144, read_timeout=60.0, first_byte_s=None, should_cancel=None):
    """Stream /api/chat and return the concatenated answer text. The first
    byte may take up to `first_byte_s` (default: the whole deadline — a cold
    model load or a queued request answers late); after that each read waits
    ≤ read_timeout and the overall deadline is enforced between chunks."""
    if not valid_model(model):
        raise OllamaError('bad_model')
    opts = {'temperature': 0.2, 'num_ctx': 8192}
    opts.update(options or {})
    payload = {'model': model, 'messages': messages, 'stream': True, 'options': opts}
    if fmt:
        payload['format'] = fmt
    deadline = time.monotonic() + float(deadline_s)
    ttfb = float(deadline_s) if first_byte_s is None else min(float(first_byte_s), float(deadline_s))
    resp = _open(base, '/api/chat', payload, timeout=ttfb,
                 read_timeout=min(float(read_timeout), float(deadline_s)))
    out = []
    size = 0
    with resp:
        for obj in _iter_lines(resp, deadline, max_bytes * 4, should_cancel):
            piece = (obj.get('message') or {}).get('content') or ''
            if piece:
                size += len(piece)
                if size > max_bytes:
                    raise OllamaTooLarge('too_large')
                out.append(piece)
            if obj.get('done'):
                break
    return ''.join(out)


def pull(base, model, progress=None, *, deadline_s=3 * 3600.0, read_timeout=120.0,
         should_cancel=None):
    """Stream /api/pull. progress(status, completed, total) is called for
    each update. Returns the last status ('success' when done)."""
    if not valid_model(model):
        raise OllamaError('bad_model')
    deadline = time.monotonic() + float(deadline_s)
    resp = _open(base, '/api/pull', {'model': model, 'stream': True}, timeout=read_timeout)
    last = ''
    with resp:
        for obj in _iter_lines(resp, deadline, 64 * 1024 * 1024, should_cancel):
            st = str(obj.get('status') or '')[:80]
            last = st or last
            if progress:
                def _n(v):
                    try:
                        return max(0, int(v))
                    except (TypeError, ValueError):
                        return 0
                progress(st, _n(obj.get('completed')), _n(obj.get('total')))
    return last
