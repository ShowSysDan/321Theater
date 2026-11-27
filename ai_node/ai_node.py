#!/usr/bin/env python3
"""3·2·1→Theater AI node (3.61.0) — runs the AI model for the cluster.

A dedicated GPU box runs Ollama, bound to 127.0.0.1, and this small service
in front of it. The 321Theater app servers keep ALL the database work: they
claim AI jobs from the queue, build the prompt, and send only the model call
here. This box holds NO database login, no S3 keys and no 321Theater code,
and keeps nothing it is sent.

Python standard library only. Configuration comes from the environment
(systemd EnvironmentFile=/etc/321ai-node/node.env, see node.env.example):

  NODE_LISTEN          host:port to listen on            (0.0.0.0:11500)
  NODE_SECRET          shared secret, ≥ 32 characters     (required)
  NODE_CERT / NODE_KEY TLS certificate + key (PEM)        (/etc/321ai-node/node.crt|.key)
  NODE_OLLAMA_URL      the local Ollama, loopback only    (http://127.0.0.1:11434)
  NODE_MODELS          models allowed, comma-separated. Empty: chat with any
                       installed model, pulls refused. Set: chat AND pull
                       only these.
  NODE_ALLOW_IPS       app servers allowed to connect (IPs / CIDRs, comma-
                       separated). Empty = any address (a warning is logged).
  NODE_MAX_CONCURRENT  model calls at once                 (2)
  NODE_QUEUE_S         how long a call may wait for a free slot (600)
  NODE_MAX_CTX         largest num_ctx a caller may ask for (32768)

Security model:
  * HTTPS only. The app servers pin this certificate by its SHA-256
    fingerprint (AI_NODE_CERT_SHA256 in their .env), so no CA is involved
    and nothing is sent to a box that isn't this one.
  * Every request carries an HMAC-SHA256 signature over method, path,
    timestamp, nonce and the SHA-256 of the body (X-321-Node-* headers):
    ±60 s clock window, each nonce accepted once. Unsigned / wrong → 401.
  * Only four routes, mirroring the parts of Ollama's API the app uses:
    GET /api/version, GET /api/tags, POST /api/chat, POST /api/pull.
    Ollama's delete / create / copy / push / generate / embed are not
    reachable. Chat bodies are rebuilt from an allow-list (roles, content,
    format json, a few clamped options); pulls only for NODE_MODELS.
  * Logs carry method, path, status, time, client IP and an error code —
    never a prompt, an answer or a model's output.
"""
import hashlib
import hmac
import http.server
import ipaddress
import json
import logging
import os
import select
import socket
import ssl
import sys
import threading
import time
import http.client
import urllib.error
import urllib.request
from urllib.parse import urlparse

VERSION = '1'
SIG_WINDOW_S = 60
NONCE_TTL_S = 2 * SIG_WINDOW_S + 10
BODY_MAX = 2 * 1024 * 1024
CONN_MAX = 48
PER_IP_MAX = 8                 # one app server: 4 workers' probes + a few model calls
PREAUTH_S = 15                 # handshake + headers + body + signature check, in total
FIRST_BYTE_MAX_S = 900
WAIT_DEFAULT_S = 120
STREAM_READ_S = 120            # longest pause between two pieces of a running answer
_LINE_MAX = 65536

log = logging.getLogger('321ai-node')


class ConfigError(Exception):
    pass


def _env(k, default=''):
    return (os.environ.get(k) or default).strip()


def _loopback_url(url):
    p = urlparse(url)
    host = (p.hostname or '').lower()
    if p.scheme != 'http' or p.path not in ('', '/') or p.query or p.username:
        return False
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_config():
    secret = _env('NODE_SECRET')
    if len(secret) < 32:
        raise ConfigError('NODE_SECRET must be at least 32 characters')
    listen = _env('NODE_LISTEN', '0.0.0.0:11500')
    host, _, port = listen.rpartition(':')
    try:
        port = int(port)
        assert 1 <= port <= 65535
    except (ValueError, AssertionError):
        raise ConfigError('NODE_LISTEN must be host:port')
    ollama = _env('NODE_OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/')
    if not _loopback_url(ollama):
        raise ConfigError('NODE_OLLAMA_URL must be http://127.0.0.1:<port> (Ollama has no password)')
    nets = []
    raw_allow = _env('NODE_ALLOW_IPS')
    if not raw_allow:
        raise ConfigError('NODE_ALLOW_IPS is empty: list the app servers (IPs or CIDRs), '
                          'or set it to "any" to accept every address')
    for part in ([] if raw_allow.lower() == 'any' else raw_allow.split(',')):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                raise ConfigError(f'NODE_ALLOW_IPS: not an IP or CIDR: {part[:60]}')

    def _int(k, d, lo, hi):
        try:
            return max(lo, min(hi, int(_env(k, str(d)))))
        except ValueError:
            raise ConfigError(f'{k} must be a number')
    return {
        'secret': secret.encode(), 'host': host.strip('[]') or '0.0.0.0', 'port': port,
        'cert': _env('NODE_CERT', '/etc/321ai-node/node.crt'),
        'key': _env('NODE_KEY', '/etc/321ai-node/node.key'),
        'ollama': ollama, 'allow': nets,
        'models': [m.strip() for m in _env('NODE_MODELS').split(',') if m.strip()],
        'max_concurrent': _int('NODE_MAX_CONCURRENT', 2, 1, 16),
        'queue_s': _int('NODE_QUEUE_S', 600, 1, 3600),
        'max_ctx': _int('NODE_MAX_CTX', 32768, 2048, 262144),
    }


# ── request signing (the app side is ollama_client._node_headers) ─────────────

def signature(secret, method, path, ts, nonce, wait, body):
    msg = '\n'.join((method.upper(), path, str(ts), nonce, str(wait),
                     hashlib.sha256(body or b'').hexdigest())).encode()
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


class NonceCache:
    def __init__(self, cap=20000):
        self._seen = {}
        self._lock = threading.Lock()
        self._cap = cap

    def add(self, nonce, now):
        """True when the nonce is new (and is now remembered)."""
        with self._lock:
            if len(self._seen) > self._cap // 2:
                for k in [k for k, exp in self._seen.items() if exp < now]:
                    del self._seen[k]
            if nonce in self._seen or len(self._seen) >= self._cap:
                return False
            self._seen[nonce] = now + NONCE_TTL_S
            return True


def model_allowed(name, allowed):
    """'llama3.2' allows 'llama3.2' and 'llama3.2:latest'."""
    if not allowed:
        return True
    for a in allowed:
        if name == a or (':' not in a and name == a + ':latest') or \
                (':' not in name and a == name + ':latest'):
            return True
    return False


_MODEL_OK = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:/+-')


def _valid_model(m):
    return isinstance(m, str) and 0 < len(m) <= 200 and m[0].isalnum() and set(m) <= _MODEL_OK


def clean_chat(body, cfg):
    """The /api/chat body Ollama gets: rebuilt from an allow-list. Raises
    ValueError(code) on anything else."""
    if not isinstance(body, dict):
        raise ValueError('bad_body')
    model = body.get('model')
    if not _valid_model(model):
        raise ValueError('bad_model')
    if not model_allowed(model, cfg['models']):
        raise ValueError('model_not_allowed')
    msgs = body.get('messages')
    if not isinstance(msgs, list) or not 0 < len(msgs) <= 50:
        raise ValueError('bad_messages')
    out = []
    for m in msgs:
        if not isinstance(m, dict) or m.get('role') not in ('system', 'user', 'assistant') \
                or not isinstance(m.get('content'), str):
            raise ValueError('bad_messages')
        out.append({'role': m['role'], 'content': m['content']})
    opts_in = body.get('options') if isinstance(body.get('options'), dict) else {}
    opts = {}
    for k, lo, hi, typ in (('temperature', 0.0, 2.0, float), ('top_p', 0.0, 1.0, float),
                           ('num_ctx', 512, cfg['max_ctx'], int), ('seed', 0, 2 ** 31 - 1, int),
                           ('num_predict', 1, 32768, int)):
        v = opts_in.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        opts[k] = typ(max(lo, min(hi, v)))
    payload = {'model': model, 'messages': out, 'stream': True, 'options': opts}
    if body.get('format') == 'json':
        payload['format'] = 'json'
    return payload


# ── upstream (the local Ollama) ───────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


class Upstream(Exception):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code


def ollama_open(cfg, path, payload=None, timeout=10.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(cfg['ollama'] + path, data=data,
                                 method='POST' if data else 'GET',
                                 headers={'Content-Type': 'application/json',
                                          'User-Agent': '321ai-node'})
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            e.close()
        except Exception:
            pass
        raise Upstream(502, f'ollama_http_{e.code}')
    except urllib.error.URLError:
        # Ollama never got the request: the app treats this as "unavailable"
        raise Upstream(503, 'ollama_unreachable')
    except (socket.timeout, TimeoutError):
        raise Upstream(504, 'ollama_timeout')
    except OSError:
        raise Upstream(502, 'ollama_reset')


# ── HTTP server ───────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = '321ai-node/' + VERSION
    sys_version = ''
    protocol_version = 'HTTP/1.0'          # one request per connection; body ends at close

    def setup(self):
        # Until the signature checks out, the whole exchange (TLS handshake,
        # request line, headers, body) gets PREAUTH_S in total — a per-read
        # timeout alone let a client trickle bytes and hold a connection
        # forever without the secret.
        self._fd = self.request.fileno()
        self._preauth = threading.Timer(PREAUTH_S, self._kill)
        self._preauth.daemon = True
        self._preauth.start()
        self.request.settimeout(PREAUTH_S)
        self.request = self.server.tls.wrap_socket(self.request, server_side=True)
        super().setup()

    def _kill(self):
        """Pre-auth deadline passed: cut the connection (any blocked read fails)."""
        try:
            s = socket.socket(fileno=os.dup(self._fd))
            try:
                s.shutdown(socket.SHUT_RDWR)
            finally:
                s.close()
        except OSError:
            pass

    def _authed(self):
        self._preauth.cancel()

    def _client_gone(self):
        """True when the caller hung up (it sends nothing after its request,
        so a readable socket means EOF / close_notify / reset)."""
        try:
            r, _w, _x = select.select([self.request], [], [], 0)
        except (OSError, ValueError):
            return True
        if not r:
            return False
        try:
            self.request.settimeout(0.2)
            return self.request.recv(1) == b''
        except (OSError, ssl.SSLError):
            return True
        finally:
            try:
                self.request.settimeout(300)
            except OSError:
                pass

    def _wait_budget(self):
        try:
            w = int(self.headers.get('X-321-Node-Wait', WAIT_DEFAULT_S))
        except ValueError:
            w = WAIT_DEFAULT_S
        return max(1, min(FIRST_BYTE_MAX_S, w))

    def finish(self):
        try:
            self._preauth.cancel()
        except AttributeError:
            pass
        try:
            super().finish()
        finally:
            try:
                self.request.close()       # the TLS socket (setup() replaced the raw one)
            except OSError:
                pass

    def log_message(self, fmt, *args):     # the stdlib access log: replaced by _done()
        pass

    def _done(self, status, code=''):
        log.info('%s %s %s %dms ip=%s%s', self.command, self.path.split('?', 1)[0][:40], status,
                 int((time.monotonic() - self._t0) * 1000), self.client_address[0],
                 f' code={code}' if code else '')

    def _json(self, status, obj, code=''):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        if code:
            self.send_header('X-321-Node-Error', code)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass
        self._done(status, code)

    def _err(self, status, code):
        self._json(status, {'error': code}, code)

    def _auth(self, body):
        h = self.headers
        ts, nonce, sig = h.get('X-321-Node-Time', ''), h.get('X-321-Node-Nonce', ''), \
            h.get('X-321-Node-Sig', '')
        try:
            t = int(ts)
        except ValueError:
            return False
        now = time.time()
        if abs(now - t) > SIG_WINDOW_S or not (16 <= len(nonce) <= 64) or not nonce.isalnum():
            return False
        want = signature(self.server.cfg['secret'], self.command, self.path, t, nonce,
                         h.get('X-321-Node-Wait', ''), body)
        if not hmac.compare_digest(want, sig):
            return False
        if not self.server.nonces.add(nonce, now):  # only a VALID request spends a nonce
            return False
        self._authed()
        return True

    def _body(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return None
        if n < 0 or n > BODY_MAX:
            return None
        return self.rfile.read(n) if n else b''

    def do_GET(self):
        self._t0 = time.monotonic()
        if self.path not in ('/api/version', '/api/tags'):
            return self._err(404, 'not_found')
        if not self._auth(b''):
            return self._err(401, 'unauthorized')
        try:
            with ollama_open(self.server.cfg, self.path, timeout=10) as r:
                data = r.read(1024 * 1024 + 1)
        except Upstream as e:
            return self._err(e.status, e.code)
        if len(data) > 1024 * 1024:
            return self._err(502, 'too_large')
        try:
            obj = json.loads(data or b'{}')
        except ValueError:
            return self._err(502, 'bad_json')
        if self.path == '/api/tags':
            names = []
            for m in (obj.get('models') if isinstance(obj, dict) else None) or []:
                n = m.get('name') if isinstance(m, dict) else None
                # Only models this node lets the app use, so "has the model"
                # on the app side means a chat will be accepted.
                if isinstance(n, str) and n and len(n) <= 100 and \
                        model_allowed(n, self.server.cfg['models']):
                    names.append({'name': n})
            obj = {'models': names[:100], 'node': {'version': VERSION,
                                                   'allowed': self.server.cfg['models'][:50]}}
        else:
            obj = {'version': str((obj or {}).get('version') or '')[:40]}
        self._json(200, obj)

    def do_POST(self):
        self._t0 = time.monotonic()
        body = self._body()                  # read first, so a refusal isn't a reset
        if body is None:
            return self._err(413, 'too_large')
        if self.path not in ('/api/chat', '/api/pull'):
            return self._err(404, 'not_found')
        if not self._auth(body):
            return self._err(401, 'unauthorized')
        try:
            req = json.loads(body or b'{}')
        except ValueError:
            return self._err(400, 'bad_json')
        cfg = self.server.cfg
        if self.path == '/api/pull':
            m = req.get('model') if isinstance(req, dict) else None
            if not _valid_model(m):
                return self._err(400, 'bad_model')
            if not cfg['models'] or not model_allowed(m, cfg['models']):
                return self._err(403, 'model_not_allowed')
            return self._stream('/api/pull', {'model': m, 'stream': True}, None, 120)
        try:
            payload = clean_chat(req, cfg)
        except ValueError as e:
            code = str(e)
            return self._err(403 if code == 'model_not_allowed' else 400, code)
        # The caller says (signed) how long it will wait for the first byte;
        # the node never waits longer — for a slot or for Ollama — and stops
        # as soon as the caller hangs up, so abandoned calls don't hold slots.
        now = time.monotonic()
        end = now + self._wait_budget()
        # Answer node_busy a few seconds BEFORE the caller's budget runs out,
        # so the app hears it (and re-queues) instead of timing out.
        slot_until = now + max(1.0, min(cfg['queue_s'], end - now - 5))
        sem = self.server.slots
        while not sem.acquire(timeout=1.0):
            if self._client_gone():
                return self._done(499, 'client_gone')
            if time.monotonic() > slot_until:
                return self._err(503, 'node_busy')
        try:
            return self._chat_stream(payload, end)
        finally:
            sem.release()

    def _chat_stream(self, payload, end):
        """POST /api/chat to Ollama on a connection we hold, waiting for the
        first byte in a helper thread while watching the caller: if it hangs
        up (or its budget runs out) the upstream socket is shut, which makes
        Ollama cancel the generation."""
        u = urlparse(self.server.cfg['ollama'])
        conn = http.client.HTTPConnection(u.hostname, u.port or 80,
                                          timeout=max(1.0, end - time.monotonic()))
        box = {}

        def run():
            try:
                conn.request('POST', '/api/chat', body=json.dumps(payload).encode(),
                             headers={'Content-Type': 'application/json',
                                      'User-Agent': '321ai-node'})
                box['resp'] = conn.getresponse()
            except BaseException as e:      # noqa: BLE001 — handed to the caller
                box['err'] = e
        th = threading.Thread(target=run, daemon=True)
        th.start()
        why = ''
        while th.is_alive():
            th.join(0.5)
            if not th.is_alive():
                break
            if self._client_gone():
                why = 'client_gone'
            elif time.monotonic() > end:
                why = 'ollama_timeout'
            if why:
                try:
                    if conn.sock:
                        conn.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                th.join(5)
                conn.close()
                return self._done(499, why) if why == 'client_gone' else self._err(504, why)
        err = box.get('err')
        if err is not None:
            conn.close()
            if isinstance(err, (ConnectionRefusedError, ConnectionError)) and 'resp' not in box:
                return self._err(503, 'ollama_unreachable')
            if isinstance(err, (socket.timeout, TimeoutError)):
                return self._err(504, 'ollama_timeout')
            return self._err(502, 'ollama_reset')
        resp = box['resp']
        if resp.status != 200:
            conn.close()
            return self._err(502, f'ollama_http_{resp.status}')
        # After the first byte, each read of the stream may take up to
        # STREAM_READ_S (http.client has already handed the socket to resp).
        try:
            resp.fp.raw._sock.settimeout(STREAM_READ_S)
        except Exception:
            pass
        return self._relay(resp, conn)

    def _stream(self, path, payload, _sem, first_byte_s):
        """Relay Ollama's NDJSON stream (pull) line by line."""
        try:
            resp = ollama_open(self.server.cfg, path, payload, timeout=first_byte_s)
        except Upstream as e:
            return self._err(e.status, e.code)
        try:
            resp.fp.raw._sock.settimeout(STREAM_READ_S)
        except Exception:
            pass
        return self._relay(resp, None)

    def _relay(self, resp, conn):
        """Copy the NDJSON stream to the caller. A caller that hangs up
        ends the upstream request, which stops the generation."""
        status, code = 200, ''
        stop = threading.Event()
        hung_up = []

        def watch():
            # The caller sends nothing while it reads: its socket turning
            # readable means it hung up. select() only (no read: the SSL
            # object belongs to this thread); then shut the upstream socket so
            # a stalled model can't keep the slot.
            while not stop.wait(1.0):
                try:
                    r, _w, _x = select.select([self.request], [], [], 0)
                except (OSError, ValueError):
                    r = [1]
                if r:
                    hung_up.append(1)
                    try:
                        up = resp.fp.raw._sock
                        s2 = socket.socket(fileno=os.dup(up.fileno()))
                        try:
                            s2.shutdown(socket.SHUT_RDWR)
                        finally:
                            s2.close()
                    except Exception:
                        pass
                    return
        threading.Thread(target=watch, daemon=True).start()
        with resp:
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-ndjson')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.request.settimeout(300)
            try:
                while True:
                    line = resp.readline(_LINE_MAX + 1)
                    if not line:
                        break
                    if len(line) > _LINE_MAX:
                        code = 'line_too_large'
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                code = 'client_gone'
            except (socket.timeout, TimeoutError):
                code = 'ollama_read_timeout'
            except OSError:
                code = 'stream_error'
            finally:
                stop.set()
        if hung_up:
            code = 'client_gone'
        if code and code != 'client_gone':
            # Never end a cut-off stream silently: the app's stream reader
            # turns an {"error": …} line into an error instead of a short answer.
            try:
                self.wfile.write((json.dumps({'error': code}) + '\n').encode())
                self.wfile.flush()
            except OSError:
                pass
        if conn is not None:
            conn.close()
        self._done(status, code)


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, cfg, tls):
        self.cfg, self.tls = cfg, tls
        self.nonces = NonceCache()
        self.slots = threading.BoundedSemaphore(cfg['max_concurrent'])
        self.conns = threading.BoundedSemaphore(CONN_MAX)
        self.per_ip = {}
        self.per_ip_lock = threading.Lock()
        fam = socket.AF_INET6 if ':' in cfg['host'] else socket.AF_INET
        self.address_family = fam
        super().__init__((cfg['host'], cfg['port']), Handler)

    def verify_request(self, request, client_address):
        """Before any TLS: drop addresses outside NODE_ALLOW_IPS."""
        nets = self.cfg['allow']
        if not nets:
            return True
        try:
            ip = ipaddress.ip_address(client_address[0].split('%', 1)[0])
            if ip.version == 6 and ip.ipv4_mapped:
                ip = ip.ipv4_mapped
        except ValueError:
            return False
        if any(ip in n for n in nets):
            return True
        log.warning('REFUSED ip=%s reason=not_allowed', client_address[0])
        return False

    def process_request(self, request, client_address):
        ip = client_address[0]
        with self.per_ip_lock:
            n = self.per_ip.get(ip, 0)
            ok = n < PER_IP_MAX and self.conns.acquire(blocking=False)
            if ok:
                self.per_ip[ip] = n + 1
        if not ok:
            log.warning('REFUSED ip=%s reason=too_many_connections', ip)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(ip)
            raise

    def _release(self, ip):
        with self.per_ip_lock:
            n = self.per_ip.get(ip, 1) - 1
            if n <= 0:
                self.per_ip.pop(ip, None)
            else:
                self.per_ip[ip] = n
            self.conns.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(client_address[0])

    def handle_error(self, request, client_address):
        e = sys.exc_info()[1]
        log.warning('CONN_ERROR ip=%s err=%s', client_address[0] if client_address else '?',
                    type(e).__name__)


def tls_context(cfg):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cfg['cert'], cfg['key'])
    return ctx


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format='%(levelname)s %(message)s')
    try:
        cfg = load_config()
        tls = tls_context(cfg)
    except (ConfigError, OSError, ssl.SSLError) as e:
        log.error('CONFIG_INVALID %s', e)
        sys.exit(2)
    if not cfg['allow']:
        log.warning('NODE_ALLOW_IPS=any: every address may connect (requests still need the secret)')
    srv = Server(cfg, tls)
    log.info('START listen=%s:%s ollama=%s models=%s max_concurrent=%s', cfg['host'], cfg['port'],
             cfg['ollama'], ','.join(cfg['models']) or '(any installed)', cfg['max_concurrent'])
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
