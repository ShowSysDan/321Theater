"""ai_module — local Ollama per server, the AI job queue, the weekly post-show
1-sheet and its weekly e-mail (3.55.0).

Never imports app.py: ONE ai_module.register(app, **deps) call wires it in.
The rules (keep every one):

* Ollama has NO authentication, so each server only ever talks to the Ollama
  on its OWN machine (ollama_client.validate_url: localhost / 127.x / ::1).
  `OLLAMA_URL` in .env overrides the shared setting per machine and is held
  to the same rule. The one exception (3.61.0) is an AI NODE (ai_node/): a
  box with no database access running Ollama on its own loopback behind a
  pinned-HTTPS, signed front end. An app server whose .env sets AI_NODE_URL
  / AI_NODE_SECRET / AI_NODE_CERT_SHA256 sends its model calls there; it
  still claims the job, builds the prompt and writes every result itself.
* Each worker PROCESS probes its own Ollama (`probe_refresh`, every 60 s, NOT
  leader-gated: the answer is per machine) and the cluster heartbeat
  publishes it (cluster_instances.ai_state / ai_checked_at). Settings → AI
  lists every server: installed / running / version / model present.
* A server without a working Ollama NEVER calls one. All AI work is a row in
  `ai_jobs`, claimed (`FOR UPDATE SKIP LOCKED`) only by a worker whose own
  probe passes; the browser polls the job. Inference claims also take an
  `ai_sessions` slot in the SAME transaction under
  pg_advisory_xact_lock(hashtext('ai_sessions')) — that is the
  ai_max_sessions limit, cluster-wide, with no count-then-insert race.
  Slots carry test_mode (3.56.2) and every count / prune filters on it, so
  a TEST_MODE server sharing production's DB never uses or prunes
  production's slots.
* No connection or transaction is held across an Ollama call. Every progress
  write is guarded by `claimed_instance = <this process> AND status =
  'running'`, so a worker that lost its claim (stale-recovered) stops.
* A 'running' job whose heartbeat is > 4 min old is re-queued (twice at
  most, then 'interrupted'); a queued job nobody can take errors out
  ('no_ai_server'; 'ai_busy' when a capable server exists but every slot
  stayed busy). Weekly runs may hold at most limit-1 slots and extract is
  claimed first, so long runs never lock out AI Extract. Jobs carry test_mode: a TEST_MODE server only ever
  claims test jobs and production never claims them.
* Prompts, documents, notes, comments and model output never go to syslog,
  audit, a URL, localStorage or an e-mail subject (ids, counts and error
  codes only). Extract payloads/results are cleared when done / within the
  hour; weekly results are kept (re-download never re-runs the model).
* Weekly summary: labor $ only for viewers with labor-report permission (a
  run is created with_labor=1 only for them, and a with_labor run is 404 for
  everyone else); $ and headcounts are never sent to the model; amounts,
  e-mails, phone numbers and known names are scrubbed before the model sees
  text. A run has a 30-min budget: past it the rest falls back (partial),
  it never fails. Model output is shared by model_hash (labor-free), so the
  same week with / without labor (or the e-mail) runs the model once.
* Rental $ (3.58.0): every weekly run's facts carry billed in-house gear and
  external rentals per show (app.py `_report_rental_totals`, the Reports'
  own reader). Never sent to the model; the PDF prints them only when the
  VIEWER may see asset $ (`can_report_assets`), never in the e-mail.
* Transcripts (3.58.0): every model call (`_chat`) is recorded on the job
  (`transcript_json`: what was sent, the answer, time, error) for Settings →
  AI → History, admin only. Trimmed after _TX_KEEP_DAYS, dropped by
  on_show_delete, never logged or put in a URL.
* The weekly e-mail is leader-gated, off by default, claimed once per week
  (ai_weekly_email_log, INSERT … ON CONFLICT DO NOTHING) before anything is
  enqueued, and delivered once (status queued → sending is the claim).
  Recipients are re-checked against the Reports permission at send. Mail
  goes only through the injected send_email (the TEST_MODE chokepoint).
"""
import hashlib
import json
import re
import socket
import threading
import time
from datetime import date, datetime, timedelta, time as dtime

from flask import abort, jsonify, render_template, session, Response

import app_config
import db_adapter
import ollama_client as oc

_d = {}
HOST = socket.gethostname()

KINDS_INFER = ('extract', 'weekly_postshow')
KIND_PULL = 'pull_model'
WEEKLY = 'weekly_postshow'
PROMPT_VERSION = 3          # 3.59.0 context + analysis; 3.60.0 wording
MAX_SHOWS = 40
MAX_RANGE_DAYS = 14
MAX_LIVE_WEEKLY = 3
_WEEKLY_BUDGET_S = 1800       # one weekly run, all model calls together
_REDUCE_RESERVE_S = 90        # kept back from the per-show calls for the reduce
_PROBE_FRESH_S = 150          # a local probe older than this doesn't count
_PEER_FRESH_S = 180           # same for a peer's published probe
_MAX_LOCAL_RUNS = 2           # jobs one worker process runs at once
_WAIT_STAGE = 'Waiting for a free AI slot'
_JOB_COLS = ('id, kind, status, test_mode, requested_by, show_id, target_host, period_start, '
             'period_end, with_labor, auto, stage, progress_done, progress_total, claimed_by, '
             'claimed_instance, model, attempts, ai_session_id, input_hash, prompt_version, '
             'show_ids, payload_json, result_json, partial, error_code, message, cancel_requested, '
             'model_hash')

_STATE_LOCK = threading.Lock()
_STATE = {'enabled': False, 'checked': 0}
_RUN_LOCK = threading.Lock()
_RUNNING = [0]
_ENV_WARNED = set()


class _Cancelled(Exception):
    pass


class _Lost(Exception):
    """This worker no longer owns the job (recovered / cancelled elsewhere)."""


class TooManyShows(Exception):
    def __init__(self, n):
        super().__init__(n)
        self.n = n


def _log():
    return _d['syslog_logger']


def _tm():
    return 1 if _d.get('test_mode') else 0


# ── settings ──────────────────────────────────────────────────────────────────

def _setting(k, default=''):
    return _d['get_app_setting'](k, default)


def enabled():
    try:
        return _setting('ollama_enabled', '0') == '1'
    except Exception:
        return False


def _env_model():
    """This server's .env OLLAMA_MODEL ('' = unset or invalid, logged once)."""
    try:
        m = (app_config.get('OLLAMA_MODEL') or '').strip()
    except Exception:
        return ''
    if m and not oc.valid_model(m):
        if ('m', m) not in _ENV_WARNED:
            _ENV_WARNED.add(('m', m))
            _log().error('CONFIG_INVALID key=OLLAMA_MODEL reason=bad_name')
        return ''
    return m


def model():
    """The model THIS server runs (3.60.0): .env OLLAMA_MODEL, else the
    shared Settings → AI model. Servers may differ (a small model on a test
    box, a larger one in production): a job runs on the claiming server's
    model, stamped in ai_jobs.model, and output is reused only from a job
    that ran on this server's model."""
    env = _env_model()
    if env:
        return env
    m = (_setting('ollama_model', 'llama3.2') or '').strip()
    return m if oc.valid_model(m) else 'llama3.2'


def model_source():
    return 'env' if _env_model() else 'settings'


def _env_url():
    try:
        return (app_config.get('OLLAMA_URL') or '').strip()
    except Exception:
        return ''


_NODE_KEYS = ('AI_NODE_URL', 'AI_NODE_SECRET', 'AI_NODE_CERT_SHA256')


def _node():
    """The AI node this server uses (normalized base URL), or None. Read
    from .env each call (cheap) and pushed into ollama_client.set_node; a
    half-set or invalid configuration is NOT used (logged once per value) —
    the server then falls back to its own Ollama, if any."""
    try:
        url, secret, fp = (str(app_config.get(k) or '').strip() for k in _NODE_KEYS)
    except Exception:
        return None
    if not (url or secret or fp):
        oc.set_node(None, None, None)
        return None
    ok, msg = oc.set_node(url, secret, fp)
    if not ok:
        oc.set_node(None, None, None)
        k = ('n', url, hashlib.sha256(secret.encode()).hexdigest(), fp)
        if k not in _ENV_WARNED:
            _ENV_WARNED.add(k)
            _log().error('CONFIG_INVALID key=AI_NODE reason=' + re.sub(r'[^A-Za-z0-9_]+', '_', msg)[:60])
        return None
    return oc.node_base()


def ollama_url():
    """Where this server's model calls go: the AI node when .env names one
    (3.61.0), else this machine's Ollama: .env OLLAMA_URL (per server) else
    the shared setting; both localhost-only. An invalid value falls back
    (logged once)."""
    node = _node()
    if node:
        return node
    env = _env_url()
    if env:
        ok, _m, norm = oc.validate_url(env)
        if ok:
            return norm
        if env not in _ENV_WARNED:
            _ENV_WARNED.add(env)
            _log().error('CONFIG_INVALID key=OLLAMA_URL reason=not_localhost')
    ok, norm = _stored_url()
    return norm if ok else oc.DEFAULT_URL


def _stored_url():
    """(valid, normalized) for the shared ollama_url setting. A value that
    fails the localhost rule (e.g. a GPU host saved before 3.55.0) is
    ignored — logged once per value and flagged on Settings → AI."""
    raw = _setting('ollama_url', oc.DEFAULT_URL) or oc.DEFAULT_URL
    ok, _m, norm = oc.validate_url(raw)
    if not ok and ('s', raw) not in _ENV_WARNED:
        _ENV_WARNED.add(('s', raw))
        _log().error('CONFIG_INVALID key=ollama_url reason=not_localhost')
    return ok, norm


def stored_url_ignored():
    """True when this server is using the default because the saved
    setting is not a localhost URL (and no .env OLLAMA_URL overrides it)."""
    return url_source() == 'settings' and not _stored_url()[0]


def url_source():
    if _node():
        return 'node'
    env = _env_url()
    return 'env' if env and oc.validate_url(env)[0] else 'settings'


def slot_limit():
    try:
        n = int(str(_setting('ai_max_sessions', '2')).strip())
    except (TypeError, ValueError):
        n = 2
    return min(10, max(1, n))


def weekly_slot_limit():
    """Slots weekly runs may hold at once: all but one (so AI Extract is
    never locked out by long runs), or the only one when the limit is 1."""
    n = slot_limit()
    return n - 1 if n > 1 else 1


# ── local probe (per process) ─────────────────────────────────────────────────

def probe_refresh():
    """Scheduler job, every 60 s in EVERY worker (not leader-gated)."""
    try:
        on = enabled()
        m = model()
        if not on:
            st = {'installed': oc.installed(), 'running': False, 'version': '', 'models': [],
                  'has_model': False, 'err': '', 'ms': None, 'checked': time.time()}
        else:
            base = ollama_url()
            st = oc.probe(base, m)
            st['via_node'] = oc.is_node(base)
        st['enabled'] = on
        st['model'] = m
    except Exception as e:
        st = {'enabled': False, 'running': False, 'err': type(e).__name__, 'checked': time.time()}
    with _STATE_LOCK:
        _STATE.clear()
        _STATE.update(st)
    return st


def local_state():
    with _STATE_LOCK:
        return dict(_STATE)


def local_ok(kind='infer'):
    st = local_state()
    if not st.get('enabled') or not st.get('running'):
        return False
    if time.time() - (st.get('checked') or 0) > _PROBE_FRESH_S:
        return False
    if kind == KIND_PULL:
        return True
    return bool(st.get('has_model')) and st.get('model') == model()


def heartbeat_state():
    """(json text, utc datetime) for cluster_instances — no model list, no
    error text beyond a code."""
    st = local_state()
    if not st.get('checked'):
        return None, None
    doc = {'en': 1 if st.get('enabled') else 0, 'inst': 1 if st.get('installed') else 0,
           'run': 1 if st.get('running') else 0, 'ver': (st.get('version') or '')[:40],
           'model': (st.get('model') or '')[:200], 'has': 1 if st.get('has_model') else 0,
           'n': len(st.get('models') or []), 'err': (st.get('err') or '')[:40], 'ms': st.get('ms'),
           'node': 1 if st.get('via_node') else 0}
    return json.dumps(doc, separators=(',', ':')), datetime.utcfromtimestamp(st['checked'])


def _peer_timeout():
    try:
        return max(5, int(_setting('cluster_peer_timeout_sec', '30')))
    except (TypeError, ValueError):
        return 30


def servers(db):
    """Live servers with their published AI state (freshest probe per server).
    Empty on a TEST_MODE install (it never reads peers)."""
    if _d.get('test_mode'):
        return []
    cutoff = datetime.utcnow() - timedelta(seconds=_peer_timeout())
    groups = {}
    for r in db.execute(
            'SELECT instance_id, ip, hostname, app_version, ai_state, ai_checked_at, last_seen '
            'FROM cluster_instances WHERE last_seen > %s', (cutoff,)).fetchall():
        key = (r['ip'] or '', r['hostname'] or '')
        g = groups.setdefault(key, {'ip': r['ip'], 'hostname': r['hostname'] or '', 'workers': 0,
                                    'is_self': False, 'app_version': r['app_version'] or '',
                                    'ai': None, 'ai_checked_at': None})
        g['workers'] += 1
        if r['instance_id'] == _d['instance_id']:
            g['is_self'] = True
        at = r['ai_checked_at']
        if r['ai_state'] and (g['ai_checked_at'] is None or (at and at > g['ai_checked_at'])):
            try:
                g['ai'] = json.loads(r['ai_state'])
                g['ai_checked_at'] = at
            except ValueError:
                pass
    out = sorted(groups.values(), key=lambda g: (g['hostname'], g['ip'] or ''))
    now = datetime.utcnow()
    for g in out:
        at = g.pop('ai_checked_at')
        g['checked_ago_s'] = int((now - at).total_seconds()) if at else None
        g['fresh'] = at is not None and (now - at).total_seconds() <= _PEER_FRESH_S
    return out


def any_capable(db, kind='infer', target_host=None):
    """Can some live server run this job? (This process, or a peer's
    published probe.) A TEST_MODE install only counts itself."""
    if local_ok(kind) and (target_host is None or target_host == HOST):
        return True
    for s in servers(db):
        a = s.get('ai') or {}
        if not s['fresh'] or not a.get('en') or not a.get('run'):
            continue
        if target_host and s['hostname'] != target_host:
            continue
        # Each server runs jobs on its own model (3.60.0), so a peer counts
        # when ITS model is ready, whatever this server's model is.
        if kind == KIND_PULL or a.get('has'):
            return True
    return False


# ── AI session slots (the ai_max_sessions limit) ──────────────────────────────

# Slots are per test_mode (3.56.2): a TEST_MODE server sharing production's
# database neither counts, takes nor prunes production's slots (or v.v.).
_PRUNE_SQL = ("UPDATE ai_sessions SET status='timeout', ended_at=NOW() "
              "WHERE status='running' AND test_mode = %s "
              "AND COALESCE(heartbeat_at, started_at) < NOW() - INTERVAL '5 minutes'")


def count_active():
    db = _d['get_db']()
    try:
        db.execute(_PRUNE_SQL, (_tm(),))
        n = db.execute("SELECT COUNT(*) AS n FROM ai_sessions WHERE status='running' "
                       "AND test_mode = %s", (_tm(),)).fetchone()['n']
        db.commit()
        return int(n or 0)
    finally:
        db.close()


def _release_session(sid):
    if not sid:
        return
    try:
        db = _d['get_db']()
        try:
            db.execute("UPDATE ai_sessions SET status='done', ended_at=NOW() "
                       "WHERE id=%s AND status='running'", (sid,))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        _log().warning(f'AI_SLOT_RELEASE_FAILED err={type(e).__name__}')


# ── the queue ─────────────────────────────────────────────────────────────────

def enqueue(db, kind, *, requested_by=None, show_id=None, target_host=None, period=None,
            with_labor=0, auto=0, input_hash=None, payload=None, show_ids=None, model_name=None,
            stage='Queued', model_hash=None):
    """INSERT one queued job; the CALLER commits (then calls kick())."""
    ps, pe = period or (None, None)
    return db.execute(
        "INSERT INTO ai_jobs (kind, status, test_mode, requested_by, show_id, target_host, "
        " period_start, period_end, with_labor, auto, stage, input_hash, prompt_version, "
        " show_ids, payload_json, model, model_hash, queued_at) "
        "VALUES (%s, 'queued', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()) "
        "RETURNING id",
        (kind, _tm(), requested_by, show_id, target_host, ps, pe, 1 if with_labor else 0,
         1 if auto else 0, stage, input_hash, PROMPT_VERSION if kind == WEEKLY else None,
         json.dumps(show_ids) if show_ids is not None else None,
         json.dumps(payload, default=str) if payload is not None else None, model_name,
         model_hash)
    ).fetchone()['id']


def kick():
    """Start a claim attempt right away when THIS process can do AI work."""
    if local_ok('infer') or local_ok(KIND_PULL):
        threading.Thread(target=_claim_and_run, daemon=True, name='ai-kick').start()


def worker_tick():
    """Scheduler job, every 10 s in EVERY worker (not leader-gated: the claim
    is atomic and only a worker whose own probe passes claims). With AI off
    it does nothing unless jobs are still queued / running (one cheap read)."""
    try:
        if not enabled() and not _jobs_pending():
            return
        _recover()
    except db_adapter.DatabaseUnavailable:
        _log().error('AI_WORKER_SKIPPED reason=database_unavailable')
        return
    except Exception as e:
        _log().warning(f'AI_RECOVER_FAILED err={type(e).__name__}')
    _claim_and_run()


def _jobs_pending():
    db = _d['get_db']()
    try:
        return db.execute("SELECT EXISTS (SELECT 1 FROM ai_jobs WHERE status IN ('queued', 'running') "
                          "AND test_mode=%s) AS x", (_tm(),)).fetchone()['x']
    finally:
        db.close()


def _recover():
    db = _d['get_db']()
    try:
        rows = db.execute(
            "UPDATE ai_jobs SET "
            " status = CASE WHEN attempts < 2 AND cancel_requested = 0 THEN 'queued' ELSE 'interrupted' END, "
            " stage = CASE WHEN attempts < 2 AND cancel_requested = 0 THEN 'Restarting (a server stopped)' "
            "              ELSE 'Interrupted' END, "
            " error_code = CASE WHEN attempts < 2 AND cancel_requested = 0 THEN NULL ELSE 'interrupted' END, "
            " message = CASE WHEN attempts < 2 AND cancel_requested = 0 THEN NULL "
            "                ELSE 'The server working on this stopped. Try again.' END, "
            " finished_at = CASE WHEN attempts < 2 AND cancel_requested = 0 THEN NULL ELSE NOW() END, "
            " payload_json = CASE WHEN attempts < 2 AND cancel_requested = 0 THEN payload_json ELSE NULL END, "
            " queued_at = NOW(), claimed_by = NULL, claimed_instance = NULL "
            "WHERE status = 'running' AND test_mode = %s "
            "  AND heartbeat_at < NOW() - INTERVAL '4 minutes' "
            "RETURNING id, ai_session_id, status", (_tm(),)).fetchall()
        sids = [r['ai_session_id'] for r in rows if r['ai_session_id']]
        if sids:
            db.execute("UPDATE ai_sessions SET status='timeout', ended_at=NOW() "
                       "WHERE id = ANY(%s) AND status='running'", (sids,))
        db.execute("UPDATE ai_jobs SET status='cancelled', stage='Cancelled', finished_at=NOW(), "
                   "payload_json=NULL WHERE status='queued' AND cancel_requested=1 AND test_mode=%s",
                   (_tm(),))
        gone = [dict(r, code='no_ai_server') for r in db.execute(
            "UPDATE ai_jobs SET status='error', error_code='no_ai_server', stage='Not started', "
            " message='No server with the AI model took this job. Check Settings → AI.', "
            " finished_at=NOW(), payload_json=NULL "
            "WHERE status='queued' AND test_mode=%s "
            " AND kind='pull_model' AND queued_at < NOW() - INTERVAL '10 minutes' "
            "RETURNING id", (_tm(),)).fetchall()]
        # A capable server exists → the job is only waiting for a free slot
        # (other jobs are running): give it longer, and say so if it expires.
        if any_capable(db, 'infer'):
            code, msg, ext, wk = ('ai_busy', 'The AI was busy with other work for too long. '
                                  'Try again in a few minutes.', '15 minutes', '120 minutes')
        else:
            code, msg, ext, wk = ('no_ai_server', 'No server with the AI model took this job. '
                                  'Check Settings → AI.', '3 minutes', '30 minutes')
        gone += [dict(r, code=code) for r in db.execute(
            "UPDATE ai_jobs SET status='error', error_code=%s, stage='Not started', message=%s, "
            " finished_at=NOW(), payload_json=NULL "
            "WHERE status='queued' AND test_mode=%s AND ("
            " (kind='extract' AND queued_at < NOW() - %s::interval) OR "
            " (kind='weekly_postshow' AND queued_at < NOW() - %s::interval)) "
            "RETURNING id", (code, msg, _tm(), ext, wk)).fetchall()]
        db.commit()
    finally:
        db.close()
    for r in rows:
        _log().warning(f"AI_JOB_RECOVERED job={r['id']} status={r['status']}")
    for r in gone:
        _log().error(f"AI_JOB_FAILED job={r['id']} stage=queued err={r['code']}")


_CLAIM_SQL = (
    "UPDATE ai_jobs SET status='running', claimed_by=%s, claimed_instance=%s, "
    " started_at=NOW(), heartbeat_at=NOW(), attempts=attempts+1, stage='Starting', "
    " model=COALESCE(model, %s) "
    "WHERE id = (SELECT id FROM ai_jobs WHERE status='queued' AND test_mode=%s "
    "            AND cancel_requested=0 AND {where} "
    "            ORDER BY (kind = 'extract') DESC, id LIMIT 1 FOR UPDATE SKIP LOCKED) "
    f"RETURNING {_JOB_COLS}")


def claim(infer_ok, pull_ok):
    """Atomically claim one job this process can run, or None."""
    db = _d['get_db']()
    try:
        if pull_ok:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('ai_pull:' + HOST,))
            row = db.execute(_CLAIM_SQL.format(
                where="kind='pull_model' AND target_host=%s AND NOT EXISTS ("
                      "SELECT 1 FROM ai_jobs r WHERE r.status='running' AND r.kind='pull_model' "
                      "AND r.target_host=%s)"),
                (HOST, _d['instance_id'], model(), _tm(), HOST, HOST)).fetchone()
            db.commit()
            if row:
                return dict(row)
        if infer_ok:
            db.execute("SELECT pg_advisory_xact_lock(hashtext('ai_sessions'))")
            db.execute(_PRUNE_SQL, (_tm(),))
            n = db.execute("SELECT COUNT(*) AS n FROM ai_sessions WHERE status='running' "
                           "AND test_mode = %s", (_tm(),)).fetchone()['n']
            if int(n or 0) >= slot_limit():
                db.execute("UPDATE ai_jobs SET stage=%s WHERE status='queued' AND test_mode=%s "
                           "AND kind = ANY(%s) AND stage IS DISTINCT FROM %s",
                           (_WAIT_STAGE, _tm(), list(KINDS_INFER), _WAIT_STAGE))
                db.commit()
                return None
            # Weekly runs hold a slot for up to 30 min: they may use at most
            # limit-1 slots, so interactive AI Extract always has one.
            wk = db.execute("SELECT COUNT(*) AS n FROM ai_sessions WHERE status='running' "
                            "AND kind=%s AND test_mode = %s", (WEEKLY, _tm())).fetchone()['n']
            kinds = list(KINDS_INFER) if int(wk or 0) < weekly_slot_limit() else ['extract']
            row = db.execute(_CLAIM_SQL.format(where='kind = ANY(%s)'),
                             (HOST, _d['instance_id'], model(), _tm(), kinds)).fetchone()
            if not row:
                if WEEKLY not in kinds:
                    db.execute("UPDATE ai_jobs SET stage=%s WHERE status='queued' AND test_mode=%s "
                               "AND kind=%s AND stage IS DISTINCT FROM %s",
                               (_WAIT_STAGE, _tm(), WEEKLY, _WAIT_STAGE))
                db.commit()
                return None
            job = dict(row)
            sid = db.execute(
                "INSERT INTO ai_sessions (user_id, show_id, status, kind, heartbeat_at, test_mode) "
                "VALUES (%s, %s, 'running', %s, NOW(), %s) RETURNING id",
                (job['requested_by'], job['show_id'], job['kind'], _tm())).fetchone()['id']
            db.execute('UPDATE ai_jobs SET ai_session_id=%s WHERE id=%s', (sid, job['id']))
            db.commit()
            job['ai_session_id'] = sid
            return job
        return None
    finally:
        db.close()


def _claim_and_run():
    if not enabled():
        return
    infer_ok, pull_ok = local_ok('infer'), local_ok(KIND_PULL)
    if not (infer_ok or pull_ok):
        return
    for _ in range(_MAX_LOCAL_RUNS):
        with _RUN_LOCK:
            if _RUNNING[0] >= _MAX_LOCAL_RUNS:
                return
            _RUNNING[0] += 1
        try:
            job = claim(infer_ok, pull_ok)
        except db_adapter.DatabaseUnavailable:
            _log().error('AI_WORKER_SKIPPED reason=database_unavailable')
            job = None
        except Exception as e:
            _log().warning(f'AI_CLAIM_FAILED err={type(e).__name__}')
            job = None
        if not job:
            with _RUN_LOCK:
                _RUNNING[0] -= 1
            return
        threading.Thread(target=_run_job, args=(job,), daemon=True,
                         name=f"ai-job-{job['id']}").start()


def _guard_update(db, job, sets, params):
    """UPDATE the job only while this process still owns it; returns the
    row's cancel_requested or raises _Lost."""
    r = db.execute(
        f"UPDATE ai_jobs SET {sets}, heartbeat_at=NOW() "
        "WHERE id=%s AND claimed_instance=%s AND status='running' RETURNING cancel_requested",
        list(params) + [job['id'], _d['instance_id']]).fetchone()
    if not r:
        raise _Lost()
    if job.get('ai_session_id'):
        db.execute('UPDATE ai_sessions SET heartbeat_at=NOW() WHERE id=%s', (job['ai_session_id'],))
    return r['cancel_requested']


def _progress(job, stage=None, done=None, total=None):
    sets, params = ['stage = COALESCE(%s, stage)'], [stage[:120] if stage else None]
    if done is not None:
        sets.append('progress_done=%s')
        params.append(int(done))
    if total is not None:
        sets.append('progress_total=%s')
        params.append(int(total))
    db = _d['get_db']()
    try:
        cancel = _guard_update(db, job, ', '.join(sets), params)
        db.commit()
    finally:
        db.close()
    if cancel:
        raise _Cancelled()


def _canceller(job, every=5.0):
    """should_cancel() for a streaming call: at most every `every` seconds
    it refreshes the heartbeat and reads cancel_requested."""
    last = [time.monotonic()]

    def check():
        now = time.monotonic()
        if now - last[0] < every:
            return False
        last[0] = now
        db = _d['get_db']()
        try:
            c = _guard_update(db, job, 'stage = stage', [])
            db.commit()
        except _Lost:
            db.rollback()
            return True
        finally:
            db.close()
        return bool(c)
    return check


# ── transcript (3.58.0): what was sent to the model and what it answered ─────
# Kept on the job row (ai_jobs.transcript_json), shown only to admins in
# Settings → AI → History, trimmed after _TX_KEEP_DAYS and dropped with a
# deleted show (on_show_delete). Never logged, never in a URL.
_TX_MSG_MAX = 60000        # characters kept per message / reply
_TX_TOTAL_MAX = 3000000    # characters kept per job
_TX_KEEP_DAYS = 30


def _tx_cut(t):
    t = '' if t is None else str(t)
    return t if len(t) <= _TX_MSG_MAX else t[:_TX_MSG_MAX] + f'\n…[{len(t) - _TX_MSG_MAX} more characters not kept]'


def _tx_add(job, step, messages, reply=None, ms=0, error=None):
    tx = job.setdefault('_tx', [])
    ent = {'step': str(step)[:200], 'ms': int(ms), 'at': datetime.now().isoformat(timespec='seconds'),
           'sent': [{'role': str(m.get('role') or ''), 'content': _tx_cut(m.get('content'))}
                    for m in messages or []],
           'reply': None if reply is None else _tx_cut(reply), 'error': error}
    n = len(json.dumps(ent, default=str))
    if job.get('_tx_size', 0) + n > _TX_TOTAL_MAX:
        ent = {'step': ent['step'], 'ms': ent['ms'], 'at': ent['at'], 'sent': [], 'reply': None,
               'error': error, 'omitted': True}
        n = 200
    job['_tx_size'] = job.get('_tx_size', 0) + n
    tx.append(ent)


def _chat(job, step, messages, **kw):
    """oc.chat for this job, recorded in its transcript (errors too)."""
    t = time.monotonic()
    try:
        raw = oc.chat(ollama_url(), job['model'] or model(), messages,
                      should_cancel=_canceller(job), **kw)
    except oc.OllamaError as e:
        _tx_add(job, step, messages, None, (time.monotonic() - t) * 1000, error=e.code)
        raise
    _tx_add(job, step, messages, raw, (time.monotonic() - t) * 1000)
    return raw


def _finish(job, status, **cols):
    allowed = {'result_json', 'partial', 'error_code', 'message', 'stage', 'progress_done'}
    sets = ["status=%s", "finished_at=NOW()", "payload_json=NULL"]
    params = [status]
    for k, v in cols.items():
        if k in allowed:
            sets.append(f'{k}=%s')
            params.append(v)
    if job.get('_tx'):
        sets.append('transcript_json=%s')
        params.append(json.dumps(job['_tx'], default=str))
    db = _d['get_db']()
    try:
        _guard_update(db, job, ', '.join(sets), params)
        db.commit()
    finally:
        db.close()


_ERR_TEXT = {
    'unreachable': 'The AI service stopped answering on the server doing this.',
    'deadline': 'The AI model took too long.', 'read_timeout': 'The AI model took too long.',
    'first_byte': 'The AI model took too long to start answering (busy or still loading).',
    'reset': 'The AI service dropped the connection. Try again.',
    'truncated': 'The AI answer was cut off before it finished. Try again.',
    'not_found': 'The AI model is not installed on that server.',
    'too_large': 'The AI answer was too long.', 'bad_json': 'The AI returned an unparseable response. Try again or use a different model.',
    # AI node (3.61.0)
    'cert_mismatch': 'The AI node’s certificate does not match AI_NODE_CERT_SHA256 — nothing was sent.',
    'node_busy': 'The AI node was busy for too long. Try again.',
    'ollama_unreachable': 'The AI node is up but its Ollama is not answering.',
    'node_unauthorized': 'The AI node refused the request (AI_NODE_SECRET differs, or the clocks are more than a minute apart).',
    'node_model_not_allowed': 'The AI node does not allow this model (NODE_MODELS on the node).',
}


def _run_job(job):
    t0 = time.monotonic()
    try:
        with _d['app'].app_context():
            _log().info(f"AI_JOB_START job={job['id']} kind={job['kind']} host={HOST}")
            if job['kind'] == 'extract':
                _exec_extract(job)
            elif job['kind'] == KIND_PULL:
                _exec_pull(job)
            elif job['kind'] == WEEKLY:
                _exec_weekly(job, t0)
            else:
                _finish(job, 'error', error_code='unknown_kind', message='Unknown job.')
    except _Lost:
        _log().warning(f"AI_JOB_LOST job={job['id']}")
    except _Cancelled:
        try:
            _finish(job, 'cancelled', stage='Cancelled')
        except Exception:
            pass
        _log().info(f"AI_JOB_CANCELLED job={job['id']}")
    except db_adapter.DatabaseUnavailable:
        _log().error(f"AI_JOB_FAILED job={job['id']} stage=run err=DatabaseUnavailable")
    except Exception as e:
        code = e.code if isinstance(e, oc.OllamaError) else type(e).__name__
        if isinstance(e, oc.OllamaError) and e.code == 'cancelled':
            try:
                _finish(job, 'cancelled', stage='Cancelled')
            except Exception:
                pass
            return
        try:
            _finish(job, 'error', error_code=str(code)[:40], stage='Failed',
                    message=_ERR_TEXT.get(code, 'The AI job failed. Try again.'))
        except Exception:
            pass
        _log().error(f"AI_JOB_FAILED job={job['id']} kind={job['kind']} err={code}")
    finally:
        _release_session(job.get('ai_session_id'))
        with _RUN_LOCK:
            _RUNNING[0] -= 1


# ── AI Extract (show page) ────────────────────────────────────────────────────

def _strip_fences(raw):
    s = (raw or '').strip()
    if s.startswith('```'):
        s = '\n'.join(s.split('\n')[1:])
        if s.rstrip().endswith('```'):
            s = s.rstrip()[:-3]
    return s.strip()


def _exec_extract(job):
    payload = json.loads(job['payload_json'] or '{}')
    fields = payload.get('fields') or {}
    _progress(job, stage='The AI model is reading the document')
    raw = _chat(job, 'AI Extract: ' + (payload.get('document') or 'document'),
                payload.get('messages') or [], deadline_s=240, first_byte_s=200, max_bytes=262144)
    try:
        extracted = json.loads(_strip_fences(raw))
        if not isinstance(extracted, dict):
            raise ValueError
    except ValueError:
        raise oc.OllamaError('bad_json')
    suggestions = {}
    for k, v in extracted.items():
        if k in fields and v is not None and str(v).strip():
            suggestions[k] = {'value': str(v).strip()[:4000], 'label': fields[k]}
    result = {'suggestions': suggestions, 'model': job['model'] or model(),
              'document': payload.get('document') or '', 'field_count': payload.get('field_count') or 0}
    _finish(job, 'done', stage='Done', result_json=json.dumps(result))
    _log().info(f"AI_EXTRACT show_id={job['show_id']} job={job['id']} "
                f"fields_found={len(suggestions)} model={job['model']} by={job['requested_by']}")


def enqueue_extract(show_id, user_id, messages, fields, document, field_count):
    """(response, status) for POST /shows/<id>/ai-extract: 202 with a job id,
    or the 429 / 503 the old synchronous call gave."""
    limit = slot_limit()
    if count_active() >= limit:
        return {'success': False,
                'error': f'All {limit} AI processing slots are busy. Please try again in a moment.'}, 429
    db = _d['get_db']()
    try:
        if not any_capable(db, 'infer'):
            return {'success': False, 'error': _unreachable_text()}, 503
        jid = enqueue(db, 'extract', requested_by=user_id, show_id=show_id,
                      payload={'messages': messages, 'fields': fields, 'document': document,
                               'field_count': field_count})
        db.commit()
    finally:
        db.close()
    kick()
    return {'success': True, 'queued': True, 'job_id': jid}, 202


def _unreachable_text():
    if session.get('user_role') == 'admin':
        return (f"No server can reach the AI model right now (this server: {HOST}). "
                "See Settings → AI.")
    return 'The AI service is not available right now. Ask an administrator to check Settings → AI.'


def _job_out(r, with_result=False):
    out = {'id': r['id'], 'kind': r['kind'], 'status': r['status'], 'stage': r['stage'] or '',
           'done': r['progress_done'] or 0, 'total': r['progress_total'] or 0,
           'message': r['message'] or '', 'partial': bool(r['partial'])}
    if with_result and r['status'] == 'done' and r['result_json']:
        try:
            out['result'] = json.loads(r['result_json'])
        except ValueError:
            pass
    return out


def _job_view(job_id):
    db = _d['get_db']()
    try:
        r = db.execute(f'SELECT {_JOB_COLS} FROM ai_jobs WHERE id=%s', (job_id,)).fetchone()
    finally:
        db.close()
    uid = session.get('user_id')
    if not r or r['kind'] == WEEKLY or r['test_mode'] != _tm():
        abort(404)
    is_admin = session.get('user_role') == 'admin'
    if not (r['requested_by'] == uid or (r['kind'] == KIND_PULL and is_admin)):
        abort(404)
    return jsonify(_job_out(r, with_result=(r['kind'] == 'extract' and r['requested_by'] == uid)))


def _job_cancel_view(job_id):
    uid = session.get('user_id')
    is_admin = session.get('user_role') == 'admin'
    db = _d['get_db']()
    try:
        r = db.execute(
            "UPDATE ai_jobs SET cancel_requested=1 WHERE id=%s AND test_mode=%s "
            "AND status IN ('queued', 'running') AND (requested_by=%s OR %s) RETURNING id, kind",
            (job_id, _tm(), uid, is_admin)).fetchone()
        db.commit()
    finally:
        db.close()
    if not r:
        return jsonify({'success': False, 'error': 'That job is not running.'}), 404
    _log().info(f'AI_JOB_CANCEL_REQUEST job={job_id} by={uid}')
    return jsonify({'success': True})


# ── model pull (Settings → AI) ────────────────────────────────────────────────

def _exec_pull(job):
    name = job['model'] or model()
    last_t = [0.0]

    def prog(status, done, total):
        now = time.monotonic()
        if now - last_t[0] < 1.0:
            return
        last_t[0] = now
        _progress(job, stage=status or 'Downloading', done=done // 1048576, total=total // 1048576)

    _progress(job, stage=f'Pulling {name}', done=0, total=0)
    last = oc.pull(ollama_url(), name, prog, should_cancel=_canceller(job))
    if last != 'success':
        raise oc.OllamaError('pull_incomplete')
    _finish(job, 'done', stage='Done', message='Model ready.')
    _log().info(f"AI_MODEL_PULLED job={job['id']} host={HOST}")
    probe_refresh()


# ══ Weekly post-show summary ══════════════════════════════════════════════════

_NOTE_KEYS = (('show_notes_tour', 'Show Notes (Tour/Client)'), ('house_notes', 'House Notes'),
              ('equipment_issues', 'Equipment / Maintenance Issues'),
              ('miscellaneous', 'Miscellaneous'))
_NOTE_MAX = 1500
_COMMENT_MAX = 400
_COMMENTS_PER_SHOW = 15
_SHOW_PROMPT_MAX = 6000
_REDUCE_MAX = 12000

# Context both prompts share (3.59.0): the model has to know it is reading a
# performing-arts venue's notes, or "Used a 22 kick instead of an 18" comes
# back as "Used a 22".
_WS_CONTEXT = (
    "Context: the venue is a performing-arts center (concerts, Broadway tours, orchestra, dance, "
    "comedy, school shows, galas, private events). The notes are written in production shorthand. "
    "Read them that way: kick / snare / tom / cymbal = drum-kit parts and a number before them is a "
    "drum size in inches when it reads as one (\"22 kick\" = 22-inch kick drum; \"2 toms\" is a "
    "count); backline = band instruments and amps the "
    "venue supplies; FOH = front of house (the mix position, or the audience side); monitors / "
    "wedges / IEMs = performers' sound; DI, mic, console, desk, PA = sound gear; LX = lighting; "
    "SM = stage manager; PM = production manager (\"7:30 PM\" is a time; times stay as written); TD = technical director; A1 / A2 / L1 / V1 = "
    "audio / lighting / video operators; load-in / load-out = setting up / striking; hold = a "
    "delayed start; house = the audience or front-of-house staff; rider = the artist's "
    "requirements; advance = pre-show planning with the artist; re-advance = planning it again.\n"
    "Writing rules: report only what the text says; never invent facts, names, numbers, times or "
    "causes. Keep the concrete specifics the note gives (sizes, models, quantities, times, what "
    "was expected versus what happened) and expand the shorthand so a reader who was not there "
    "understands it, e.g. \"Used a 22-inch kick drum instead of the 18-inch\", never \"Used a "
    "22\". Initials or abbreviations you cannot expand with certainty (often a person's initials) "
    "stay exactly as written; never guess what they stand for. Refer to people by role, not by "
    "name. Never state dollar amounts, labor costs or headcounts; those are printed separately.\n"
    "Severity: high = safety, injury, damage, a stopped or held show, or a client complaint; "
    "medium = it noticeably affected the performance, the audience or the schedule, or needed a "
    "workaround; low = minor or internal, no audience impact.\n")

_WS_SHOW_PROMPT = (
    "You summarize one show's post-show report for a performing-arts center's production "
    "leadership. You receive the show's post-show notes and staff comments.\n" + _WS_CONTEXT +
    "Return ONLY a JSON object:\n"
    '{"one_liner": string (<=160 chars, how the show went, with its most important specific),\n'
    ' "highlights": [string] (0-3, <=140 chars each, what went well or was notable),\n'
    ' "issues": [{"text": string (<=160 chars, what happened and its effect), '
    '"severity": "high"|"medium"|"low", '
    '"category": "equipment"|"safety"|"house"|"crew"|"client"|"schedule"|"other"}] (0-4),\n'
    ' "follow_ups": [string] (0-3, <=140 chars, open actions the text explicitly mentions; say '
    'what is to be done AND what it is about, e.g. "TP to find out why the video playback had no '
    'audio", never just "TP looking into why")}\n'
    "Use empty lists when nothing applies.")

_WS_WEEK_PROMPT = (
    "You are an analyst writing the weekly executive summary for a performing-arts center's "
    "production leadership, from per-show summaries. The input lists only the shows that had "
    "post-show notes; how many had none is in counts and is printed separately, so never make "
    "missing notes a highlight, issue or pattern. A show marked summary_unavailable has only the "
    "first line of its notes; use what it says and don't guess the rest.\n" + _WS_CONTEXT +
    "Analyse the week, don't just repeat it: say what mattered most, connect problems that share "
    "a cause, a venue, a kind of gear or a kind of show, and say what leadership should act on. "
    "Every claim must be supported by the input. Refer to shows only by numeric id in show_ids. "
    "Return ONLY JSON:\n"
    '{"headline": string (<=120, the single most important takeaway),\n'
    ' "summary": string (<=600, 3-4 sentences: how the week went overall, the most significant '
    'problems and their effect, and what stood out),\n'
    ' "highlights": [{"text": string <=160, "show_ids": [int]}] (0-4, things that went well),\n'
    ' "issues": [{"text": string <=180, "severity": "high"|"medium"|"low", "show_ids": [int]}] '
    '(0-6, most severe first, each one problem with its effect),\n'
    ' "patterns": [{"text": string <=180, "show_ids": [int]}] (0-4, trends or repeated problems '
    'across two or more shows, venues or gear; [] when nothing repeats),\n'
    ' "recommendations": [{"text": string <=180, "show_ids": [int]}] (0-4, concrete, practical '
    'actions leadership could take to prevent or fix what the week showed; grounded in the '
    'input, never generic advice),\n'
    ' "labor_note": string (<=200, qualitative only; "" if none),\n'
    ' "follow_ups": [{"text": string <=160, "show_ids": [int]}] (0-6, open actions from the shows, '
    'each listed once, saying what is to be done and what it is about)}')

_RETRY_MSG = 'Your previous answer was not valid JSON for the schema. Answer again with JSON only.'

_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
_PHONE_RE = re.compile(r'(?<![\w])(?:\+?1[\s.\-]?)?(?:\(\d{3}\)|\d{3})[\s.\-]?\d{3}[\s.\-]?\d{4}(?![\w])')
_MENTION_RE = re.compile(r'(?<![\w@])@[A-Za-z][\w.\-]*')
_CTRL_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f]')
_MONEY_RE = re.compile(r'[$€£]\s?\d')
_AMOUNT_RE = re.compile(
    r'(?:[$€£]|\b(?:USD|EUR|GBP)\s?)\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:[kKmM]|million|thousand)\b)?'
    r'|\b\d[\d,]*(?:\.\d+)?\s?(?:[kKmM]\s?)?(?:dollars|bucks|usd|euros?)\b', re.I)
_SEVERITIES = ('high', 'medium', 'low')
_CATEGORIES = ('equipment', 'safety', 'house', 'crew', 'client', 'schedule', 'other')


def scrub(text, names=()):
    """E-mail addresses / phone numbers → [contact]; money amounts →
    [amount]; @mentions and known names → [person]. Applied before the model
    AND before the PDF ($ is never sent to the model)."""
    t = str(text or '')
    t = _AMOUNT_RE.sub('[amount]', t)
    t = _EMAIL_RE.sub('[contact]', t)
    t = _PHONE_RE.sub('[contact]', t)
    t = _MENTION_RE.sub('[person]', t)
    for n in sorted({' '.join(str(x).split()) for x in names if x}, key=len, reverse=True):
        if len(n) >= 3:
            t = re.sub(r'(?<!\w)' + re.escape(n) + r'(?!\w)', '[person]', t, flags=re.I)
    return t


def _strip_money(t):
    if not _MONEY_RE.search(t):
        return t
    parts = re.split(r'(?<=[.!?])\s+', t)
    return ' '.join(p for p in parts if not _MONEY_RE.search(p)).strip()


def _clean(v, n):
    if not isinstance(v, str):
        return ''
    v = ' '.join(_CTRL_RE.sub(' ', v).split())
    v = scrub(_strip_money(v))           # drop a sentence the model gave a $ in
    if len(v) > n:
        v = v[:n - 1].rstrip() + '…'
    return v


def _cut(text, n):
    text = (text or '').strip()
    return text if len(text) <= n else text[:n].rstrip() + ' …[trimmed]'


def _first_sentence(text, n=140):
    t = ' '.join(str(text or '').split())
    if not t:
        return ''
    m = re.match(r'(.+?[.!?])(\s|$)', t)
    s = m.group(1) if m else t
    return s if len(s) <= n else s[:n - 1].rstrip() + '…'


def _parse(raw):
    s = _strip_fences(raw)
    i, j = s.find('{'), s.rfind('}')
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(s[i:j + 1])
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _as_list(v):
    return v if isinstance(v, list) else []


def validate_show(d):
    if not isinstance(d, dict):
        return None
    one = _clean(d.get('one_liner'), 160)
    if not one:
        return None
    issues = []
    for x in _as_list(d.get('issues')):
        if isinstance(x, dict):
            text = _clean(x.get('text'), 160)
            sev = x.get('severity') if x.get('severity') in _SEVERITIES else 'low'
            cat = x.get('category') if x.get('category') in _CATEGORIES else 'other'
        else:
            text, sev, cat = _clean(x, 160), 'low', 'other'
        if text:
            issues.append({'text': text, 'severity': sev, 'category': cat})
    return {'one_liner': one,
            'highlights': _uniq(_clean(x, 140) for x in _as_list(d.get('highlights')))[:3],
            'issues': issues[:4],
            'follow_ups': _uniq(_clean(x, 140) for x in _as_list(d.get('follow_ups')))[:3],
            'ai': 1}


def _key(t):
    """Duplicate test for model lists: case, spacing and punctuation-blind."""
    return re.sub(r'[\W_]+', ' ', str(t).lower()).strip()


def _uniq(texts):
    out, seen = [], set()
    for t in texts:
        k = _key(t)
        if t and k not in seen:
            seen.add(k)
            out.append(t)
    return out


def _ids(v, valid):
    out = []
    for x in _as_list(v):
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i in valid and i not in out:
            out.append(i)
    return out[:10]


def validate_week(d, valid_ids):
    if not isinstance(d, dict):
        return None
    head = _clean(d.get('headline'), 120)
    if not head:
        return None

    def items(key, n, cap, sev=False):
        out, seen = [], {}
        for x in _as_list(d.get(key)):
            if isinstance(x, dict):
                text, ids = _clean(x.get('text'), cap), _ids(x.get('show_ids'), valid_ids)
                s = x.get('severity') if x.get('severity') in _SEVERITIES else 'low'
            else:
                text, ids, s = _clean(x, cap), [], 'low'
            if not text:
                continue
            k = _key(text)
            if k in seen:                       # the model repeated itself: merge
                it = seen[k]
                it['show_ids'] = (it['show_ids'] + [i for i in ids if i not in it['show_ids']])[:10]
                if sev and _SEVERITIES.index(s) < _SEVERITIES.index(it['severity']):
                    it['severity'] = s
                continue
            it = {'text': text, 'show_ids': ids}
            if sev:
                it['severity'] = s
            seen[k] = it
            out.append(it)
        if sev:
            out.sort(key=lambda i: _SEVERITIES.index(i['severity']))
        return out[:n]
    return {'headline': head, 'summary': _clean(d.get('summary'), 600),
            'highlights': items('highlights', 4, 160), 'issues': items('issues', 6, 180, True),
            'patterns': items('patterns', 4, 180),
            'recommendations': items('recommendations', 4, 180),
            'labor_note': _clean(d.get('labor_note'), 200),
            'follow_ups': items('follow_ups', 6, 160), 'ai': 1}


def weekly_window(end=None, start=None):
    """(start, end) dates: default the 7 days ending yesterday."""
    end = end or (date.today() - timedelta(days=1))
    start = start or (end - timedelta(days=6))
    return start, end


def weekly_shows(db, start, end, access=None):
    """Shows that took place in [start, end] (a performance, the show date,
    or a load-in..load-out overlap), any status, never test / cancelled."""
    rows = db.execute("""
        SELECT s.id, s.name, s.venue, s.show_date, s.load_in_date, s.load_out_date,
               (SELECT MIN(p.perf_date) FROM show_performances p
                 WHERE p.show_id = s.id AND p.perf_date BETWEEN %s AND %s) AS first_perf,
               (SELECT MIN(p.perf_date) FROM show_performances p WHERE p.show_id = s.id) AS min_perf,
               (SELECT MAX(p.perf_date) FROM show_performances p WHERE p.show_id = s.id) AS max_perf
          FROM shows s
         WHERE COALESCE(s.is_test, 0) = 0 AND COALESCE(s.is_cancelled, 0) = 0
           AND (EXISTS (SELECT 1 FROM show_performances p
                         WHERE p.show_id = s.id AND p.perf_date BETWEEN %s AND %s)
                OR s.show_date BETWEEN %s AND %s
                OR (s.load_in_date IS NOT NULL AND s.load_in_date <= %s
                    AND COALESCE(s.load_out_date, s.load_in_date) >= %s))
    """, (start, end, start, end, start, end, end, start)).fetchall()
    as_date = _d['as_date']
    out = []
    for r in rows:
        if access is not None and r['id'] not in access:
            continue
        cands = [as_date(r['first_perf'])]
        sd = as_date(r['show_date'])
        if sd and start <= sd <= end:
            cands.append(sd)
        li = as_date(r['load_in_date'])
        if li:
            cands.append(max(li, start))
        first = min([c for c in cands if c] or [end])
        out.append((first, (r['name'] or '').lower(), dict(r)))
    out.sort(key=lambda x: (x[0], x[1], x[2]['id']))
    return [(f, r) for f, _n, r in out]


def _fmt_d(d):
    return f"{d.strftime('%b')} {d.day}" if d else ''


def fmt_range(a, b):
    if not a:
        return ''
    if not b or a == b:
        return f'{_fmt_d(a)}, {a.year}'
    if a.year == b.year:
        return f'{_fmt_d(a)} – {_fmt_d(b)}, {b.year}'
    return f'{_fmt_d(a)}, {a.year} – {_fmt_d(b)}, {b.year}'


def build_inputs(db, start, end, with_labor, access=None):
    """(show_ids, facts, digests, counts, input_hash) — pure DB reads; no
    request needed (the weekly e-mail job calls it too). Raises TooManyShows."""
    as_date = _d['as_date']
    picked = weekly_shows(db, start, end, access)
    if len(picked) > MAX_SHOWS:
        raise TooManyShows(len(picked))
    ids = [r['id'] for _f, r in picked]
    facts, digests = {}, []
    if not ids:
        return ids, facts, digests, {'shows': 0, 'performances': 0, 'settled': 0, 'unsettled': 0,
                                     'with_notes': 0}, ''
    internal = _d['postnotes_internal_keys']
    notes = {}
    for r in db.execute('SELECT show_id, field_key, field_value FROM post_show_notes '
                        'WHERE show_id = ANY(%s)', (ids,)).fetchall():
        if r['field_key'] not in internal:
            notes.setdefault(r['show_id'], {})[r['field_key']] = r['field_value'] or ''
    perfs = {}
    for r in db.execute('SELECT show_id, perf_date, perf_time FROM show_performances '
                        'WHERE show_id = ANY(%s) AND perf_date BETWEEN %s AND %s '
                        'ORDER BY perf_date, perf_time', (ids, start, end)).fetchall():
        d = as_date(r['perf_date'])
        perfs.setdefault(r['show_id'], []).append([d.isoformat() if d else '', (r['perf_time'] or '')[:20]])
    comments, ncomments = {}, {}
    for r in db.execute("""
        SELECT c.show_id, c.body, c.created_at, c.author_name, u.display_name, u.username
          FROM show_comments c LEFT JOIN users u ON u.id = c.user_id
         WHERE c.show_id = ANY(%s) AND c.deleted_at IS NULL
           AND c.created_at >= %s::date - INTERVAL '3 days'
           AND c.created_at < %s::date + INTERVAL '1 day'
         ORDER BY c.created_at DESC, c.id DESC""", (ids, start, end)).fetchall():
        ncomments[r['show_id']] = ncomments.get(r['show_id'], 0) + 1
        lst = comments.setdefault(r['show_id'], [])
        if len(lst) < _COMMENTS_PER_SHOW:
            lst.append(r)
    names = {}
    for r in db.execute("""
        SELECT lr.show_id, cm.name FROM labor_requests lr
          JOIN crew_members cm ON cm.id = lr.scheduled_crew_member_id WHERE lr.show_id = ANY(%s)
        UNION
        SELECT p.show_id, cm.name FROM post_show_labor p
          JOIN crew_members cm ON cm.id = p.crew_member_id WHERE p.show_id = ANY(%s)""",
            (ids, ids)).fetchall():
        names.setdefault(r['show_id'], set()).add(r['name'])
    unsettled = _d['unsettled_show_ids'](db, ids)
    try:
        gear = _d['asset_totals'](db, ids)
    except Exception as e:
        gear = {}
        _log().warning(f'AI_SUMMARY_ASSETS_FAILED err={type(e).__name__}')
    pms = _d['pm_contacts_many'](db, ids)
    spot_on = _d['module_enabled']('spot')
    total_perfs = 0
    for _first, s in picked:
        sid = s['id']
        n = notes.get(sid, {})
        pm = ((pms.get(sid) or {}).get('show_pm') or {}).get('name') or ''
        dates = [as_date(x) for x in (s['load_in_date'], s['load_out_date'], s['show_date'],
                                      s['min_perf'], s['max_perf'])]
        dates = [x for x in dates if x]
        settled = sid not in unsettled
        f = {'id': sid, 'name': s['name'] or f'Show #{sid}', 'venue': s['venue'] or '',
             'dates': fmt_range(min(dates), max(dates)) if dates else '',
             'perfs': perfs.get(sid, []), 'pm': pm,
             'crew_called': (n.get('crew_called') or '')[:20],
             'cast_count': (n.get('cast_count') or '')[:10],
             'crew_count': (n.get('crew_count') or '')[:10],
             'comments': ncomments.get(sid, 0), 'settled': settled}
        total_perfs += len(f['perfs'])
        if with_labor and settled:
            try:
                lines, total = _d['calc_post_show_labor_cost'](db, sid)
                f['labor_total'] = round(float(total or 0), 2)
                f['labor_hours'] = round(sum(float(l.get('hours') or 0) for l in lines
                                             if not l.get('is_billable_extra')
                                             and not l.get('is_day_subtotal')), 2)
            except Exception as e:
                _log().warning(f'AI_SUMMARY_LABOR_FAILED show_id={sid} err={type(e).__name__}')
        # Rental $ (3.58.0): billed in-house gear and external rentals, from
        # the same reader the Reports use. Facts only, never sent to the
        # model; the PDF prints them only for viewers who can see asset $.
        a_in, a_ext = gear.get(sid, (0.0, 0.0))
        f['assets_in'], f['assets_ext'] = round(a_in, 2), round(a_ext, 2)
        if spot_on:
            try:
                import spot_module
                peak = None
                for c in spot_module.captures_for_show(db, sid):
                    if not (start.isoformat() <= (c.get('date') or '') <= end.isoformat()):
                        continue
                    for m in c.get('monitors') or []:
                        st = m.get('stats') if isinstance(m.get('stats'), dict) else m
                        v = st.get('leq') if isinstance(st, dict) else None
                        if isinstance(v, (int, float)) and (peak is None or v > peak):
                            peak = v
                if peak is not None:
                    f['peak_leq'] = round(float(peak), 1)
            except Exception as e:
                _log().warning(f'AI_SUMMARY_SOUND_FAILED show_id={sid} err={type(e).__name__}')
        facts[str(sid)] = f
        # ── text for the model (scrubbed; no names, no $, no headcounts) ──
        known = set(names.get(sid, ()))
        if pm:
            known.add(pm)
        for c in comments.get(sid, []):
            for k in ('display_name', 'username', 'author_name'):
                if c[k]:
                    known.add(c[k])
        sections = []
        for key, label in _NOTE_KEYS:
            txt = scrub(_cut(n.get(key, ''), _NOTE_MAX), known)
            if txt.strip():
                sections.append(f'[{label}]\n{txt}')
        clines = []
        for c in comments.get(sid, []):
            cd = as_date(c['created_at'])
            body = scrub(_cut(' '.join(str(c['body'] or '').split()), _COMMENT_MAX), known)
            if body:
                clines.append(f"- Staff comment ({cd.isoformat() if cd else ''}): {body}")
        head = (f"Show: {f['name']} | Venue: {f['venue'] or '—'} | Dates: {f['dates'] or '—'} | "
                f"Performances this week: {len(f['perfs'])}")
        if f['crew_called']:
            head += f"\nCrew called: {f['crew_called']}"

        def _msg(cl):
            parts = [head] + sections
            if cl:
                parts.append('[Comments, newest first]\n' + '\n'.join(cl))
            return '\n'.join(parts)
        msg = _msg(clines)
        while len(msg) > _SHOW_PROMPT_MAX and clines:
            clines.pop()                       # oldest first
            msg = _msg(clines)
        if len(msg) > _SHOW_PROMPT_MAX:
            msg = msg[:_SHOW_PROMPT_MAX] + ' …[trimmed]'
        has_text = bool(sections or clines)
        fb = _first_sentence(scrub(n.get('show_notes_tour') or n.get('house_notes') or '', known))
        digests.append({'show_id': sid, 'has_text': has_text, 'user': msg, 'fallback': fb})
    n_unsettled = len([i for i in ids if i in unsettled])
    counts = {'shows': len(ids), 'performances': total_perfs,
              'settled': len(ids) - n_unsettled, 'unsettled': n_unsettled,
              'with_notes': sum(1 for dg in digests if dg['has_text'])}
    h = hashlib.sha256(json.dumps({'pv': PROMPT_VERSION, 'model': model(), 's': start.isoformat(),
                                   'e': end.isoformat(), 'wl': 1 if with_labor else 0,
                                   'f': facts, 'd': digests}, sort_keys=True,
                                  default=str).encode()).hexdigest()
    return ids, facts, digests, counts, h


def model_hash(start, end, facts, digests, counts):
    """Hash of exactly what the MODEL is given (prompts, show ids, names /
    venues and counts of the reduce input) — never labor, so a with-labor and
    a without-labor run of the same week share the model's output."""
    return hashlib.sha256(json.dumps({
        'pv': PROMPT_VERSION, 'model': model(), 's': start.isoformat(), 'e': end.isoformat(),
        'd': [[d['show_id'], d['has_text'], d['user'], d['fallback']] for d in digests],
        # Every fact _reduce_input sends must be here (3.60.0: peak sound,
        # dates, performance count reach the model).
        'n': {k: [f.get('name', ''), f.get('venue', ''), f.get('dates', ''),
                  len(f.get('perfs') or []), f.get('peak_leq')] for k, f in facts.items()},
        'c': {k: counts.get(k) for k in ('shows', 'performances', 'settled', 'unsettled')},
        'p': [_WS_SHOW_PROMPT, _WS_WEEK_PROMPT]}, sort_keys=True, default=str).encode()).hexdigest()


def _reusable_result(db, mhash, exclude_id=None):
    """The newest complete (not partial) weekly result made from the same
    model input, or None."""
    if not mhash:
        return None
    # model = the model the job actually RAN on (the claimer's), which can
    # differ from the hashed one when servers run different models.
    r = db.execute("SELECT id, result_json FROM ai_jobs WHERE kind=%s AND status='done' "
                   "AND model_hash=%s AND test_mode=%s AND partial=0 AND result_json IS NOT NULL "
                   "AND model=%s AND id <> %s ORDER BY id DESC LIMIT 1",
                   (WEEKLY, mhash, _tm(), model(), exclude_id or 0)).fetchone()
    if not r:
        return None
    try:
        res = json.loads(r['result_json'])
    except ValueError:
        return None
    if not isinstance(res, dict) or not isinstance(res.get('shows'), dict) or not res.get('week'):
        return None
    res['_id'] = r['id']
    return res


def _derived_result(src, payload, with_labor):
    """A run's result built from another run's model output plus THIS
    run's own facts (its labor $ only if it was made with labor)."""
    digests = payload.get('digests') or []
    return {'v': 1, 'start': payload.get('start'), 'end': payload.get('end'),
            'with_labor': bool(with_labor), 'model': src.get('model') or model(),
            'order': [dg['show_id'] for dg in digests], 'facts': payload.get('facts') or {},
            'counts': payload.get('counts') or {}, 'shows': src['shows'], 'week': src['week'],
            'partial': 0, 'generated_at': src.get('generated_at') or
            datetime.now().isoformat(timespec='minutes'), 'reused_from': src['_id']}


def _complete_from(db, jid, src, payload, with_labor):
    """Finish a just-enqueued (still queued, same transaction) weekly job
    from an existing result — no model call. The caller commits."""
    res = _derived_result(src, payload, with_labor)
    n = len(payload.get('digests') or []) + 1
    db.execute("UPDATE ai_jobs SET status='done', stage='Done', result_json=%s, partial=0, "
               "progress_done=%s, progress_total=%s, payload_json=NULL, model=%s, "
               "started_at=NOW(), finished_at=NOW() WHERE id=%s AND status='queued'",
               (json.dumps(res, default=str), n, n, res['model'], jid))


def _ask(job, messages, validator, deadline, step='AI call'):
    """(validated dict or None, calls). One retry on bad JSON, inside the
    same `deadline` seconds. An Ollama error or timeout on this call → None
    (the caller falls back); unreachable (nothing delivered) or cancelled
    propagate."""
    calls = 0
    end = time.monotonic() + deadline
    try:
        raw = _chat(job, step, messages, deadline_s=deadline, max_bytes=65536)
        calls += 1
        v = validator(_parse(raw))
        if v:
            return v, calls
        left = end - time.monotonic()
        if left < 15:
            return None, calls
        msgs = messages + [{'role': 'assistant', 'content': (raw or '')[:4000]},
                           {'role': 'user', 'content': _RETRY_MSG}]
        raw = _chat(job, step + ' (retry: the first answer was not usable)', msgs,
                    deadline_s=left, max_bytes=65536)
        calls += 1
        return validator(_parse(raw)), calls
    except oc.OllamaUnavailable:
        raise
    except oc.OllamaError as e:
        if e.code == 'cancelled':
            raise _Cancelled()
        return None, calls + 1


def _reduce_input(order, shows, facts, counts):
    """The weekly (reduce) call's input: only the shows that had notes —
    a show whose own summary failed is kept with its fallback line (the
    model reads the rest as a count — 3.59.0, it used to make "no notes for
    12 shows" the week's highlight), each with the non-$ facts that help
    the analysis (venue, performances, peak sound level)."""
    def build(drop_hl, drop_fu):
        lst = []
        for sid in order:
            s, f = shows.get(str(sid)) or {}, facts.get(str(sid)) or {}
            if not s.get('ai') and not s.get('ai_failed'):
                continue                   # no notes at all
            it = {'show_id': sid, 'name': f.get('name', ''), 'venue': f.get('venue', ''),
                  'dates': f.get('dates', ''), 'performances': len(f.get('perfs') or []),
                  'one_liner': s.get('one_liner', ''), 'issues': s.get('issues', [])}
            if s.get('ai_failed'):         # had notes; its own summary failed
                it['summary_unavailable'] = True
            if f.get('peak_leq') is not None:
                it['peak_sound_leq_db'] = f['peak_leq']
            if not drop_fu:
                it['follow_ups'] = s.get('follow_ups', [])
            if not drop_hl:
                it['highlights'] = s.get('highlights', [])
            lst.append(it)
        n = counts.get('shows', 0)
        return json.dumps({'counts': {'shows': n, 'performances': counts.get('performances', 0),
                                      'shows_with_notes': len(lst),
                                      'shows_without_notes': max(0, n - len(lst)),
                                      'labor_settled': counts.get('settled', 0),
                                      'labor_not_settled': counts.get('unsettled', 0)},
                           'shows': lst}, ensure_ascii=False)
    for dh, df in ((False, False), (True, False), (True, True)):
        txt = build(dh, df)
        if len(txt) <= _REDUCE_MAX:
            return txt
    return txt[:_REDUCE_MAX]


def _exec_weekly(job, t0):
    payload = json.loads(job['payload_json'] or '{}')
    facts, digests, counts = payload.get('facts') or {}, payload.get('digests') or [], payload.get('counts') or {}
    n = len(digests)
    total = n + 1
    if not payload.get('fresh'):
        # Someone finished a run of the same model input meanwhile (e.g. the
        # same week with / without labor): reuse its output, no model call.
        db = _d['get_db']()
        try:
            src = _reusable_result(db, job.get('model_hash'), job['id'])
        finally:
            db.close()
        if src:
            res = _derived_result(src, payload, job['with_labor'])
            _finish(job, 'done', stage='Done', progress_done=total, partial=0,
                    result_json=json.dumps(res, default=str))
            _log().info(f"AI_SUMMARY_REUSED job={job['id']} from={src['_id']}")
            return

    def left():
        return _WEEKLY_BUDGET_S - (time.monotonic() - t0)
    shows, calls, partial = {}, 0, 0
    for i, dg in enumerate(digests):
        _progress(job, stage=f'Summarising show {i + 1} of {n}', done=i, total=total)
        sid = dg['show_id']
        if not dg.get('has_text'):
            shows[str(sid)] = {'one_liner': 'No post-show notes or comments recorded.',
                               'highlights': [], 'issues': [], 'follow_ups': [], 'ai': 0}
            continue
        # Out of time → the remaining shows get the fallback text (partial),
        # never a failed run that throws away the summaries already made.
        budget = min(150.0, left() - _REDUCE_RESERVE_S)
        res, c = (None, 0) if budget < 20 else _ask(
            job, [{'role': 'system', 'content': _WS_SHOW_PROMPT},
                  {'role': 'user', 'content': dg['user']}], validate_show, budget,
            step='Show summary: ' + str((facts.get(str(sid)) or {}).get('name') or f'Show #{sid}'))
        calls += c
        if not res:
            partial = 1
            res = {'one_liner': _clean(dg.get('fallback') or '', 140) or 'Summary unavailable.',
                   'highlights': [], 'issues': [], 'follow_ups': [], 'ai': 0, 'ai_failed': 1}
        shows[str(sid)] = res
    _progress(job, stage='Writing the weekly summary', done=n, total=total)
    order = [dg['show_id'] for dg in digests]
    valid = set(order)
    week = None
    if any(s.get('ai') for s in shows.values()) and left() >= 30:
        week, c = _ask(job, [{'role': 'system', 'content': _WS_WEEK_PROMPT},
                             {'role': 'user', 'content': _reduce_input(order, shows, facts, counts)}],
                       lambda d: validate_week(d, valid), min(180.0, left()),
                       step='Weekly summary (all shows)')
        calls += c
    if not week:
        if any(s.get('ai') for s in shows.values()):
            partial = 1
        highs = []
        for sid in order:
            for it in shows[str(sid)].get('issues', []):
                if it.get('severity') == 'high':
                    highs.append({'text': it['text'], 'severity': 'high', 'show_ids': [sid]})
        n_issues = sum(len(shows[str(s)].get('issues', [])) for s in order)
        week = {'headline': f"{counts.get('shows', 0)} shows, {counts.get('performances', 0)} "
                            f"performances: {n_issues} issues flagged",
                'summary': '', 'highlights': [], 'issues': highs[:6], 'patterns': [],
                'recommendations': [], 'labor_note': '', 'follow_ups': [], 'ai': 0}
    result = {'v': 1, 'start': payload.get('start'), 'end': payload.get('end'),
              'with_labor': bool(job['with_labor']), 'model': job['model'] or model(),
              'order': order, 'facts': facts, 'counts': counts, 'shows': shows, 'week': week,
              'partial': partial, 'generated_at': datetime.now().isoformat(timespec='minutes')}
    _finish(job, 'done', stage='Done', progress_done=total, partial=partial,
            result_json=json.dumps(result, default=str))
    _log().info(f"AI_SUMMARY_DONE job={job['id']} secs={int(time.monotonic() - t0)} "
                f"calls={calls} partial={partial}")


# ── weekly: request side ──────────────────────────────────────────────────────

def _need_reports():
    if not enabled():
        abort(404)
    if not _d['can_view_reports']():
        abort(404)


def _parse_day(v):
    v = (v or '').strip()[:10] if isinstance(v, str) else ''
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ValueError('Dates must be YYYY-MM-DD.')


def _access():
    acc = _d['get_accessible_shows'](session.get('user_id'))
    return None if acc is None else set(acc)


def _weekly_start_view():
    _need_reports()
    body = _d['json_obj']()
    try:
        end = _parse_day(body.get('end_date'))
        start = _parse_day(body.get('start_date'))
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    start, end = weekly_window(end, start)
    if end > date.today():
        return jsonify({'error': 'The week can’t end after today.'}), 400
    if start > end or (end - start).days >= MAX_RANGE_DAYS:
        return jsonify({'error': f'Pick a range of 1–{MAX_RANGE_DAYS} days.'}), 400
    force = bool(body.get('force'))
    wl = 1 if _d['can_report_labor']() else 0
    uid = session.get('user_id')
    db = _d['get_db']()
    try:
        try:
            ids, facts, digests, counts, h = build_inputs(db, start, end, wl, _access())
        except TooManyShows as e:
            return jsonify({'error': f'Too many shows ({e.n}) — pick a shorter range '
                                     f'(at most {MAX_SHOWS}).'}), 400
        if not ids:
            return jsonify({'error': 'No shows took place in that range.'}), 400
        db.execute("SELECT pg_advisory_xact_lock(hashtext('ai_jobs:weekly'))")
        if not force:
            r = db.execute("SELECT id FROM ai_jobs WHERE kind=%s AND status='done' AND input_hash=%s "
                           "AND test_mode=%s AND with_labor=%s AND model=%s ORDER BY id DESC LIMIT 1",
                           (WEEKLY, h, _tm(), wl, model())).fetchone()
            if r:
                db.commit()
                return jsonify({'run_id': r['id'], 'reused': True})
        r = db.execute("SELECT id FROM ai_jobs WHERE kind=%s AND status IN ('queued', 'running') "
                       "AND period_start=%s AND period_end=%s AND with_labor=%s AND test_mode=%s "
                       "ORDER BY id DESC LIMIT 1", (WEEKLY, start, end, wl, _tm())).fetchone()
        if r:
            db.commit()
            return jsonify({'run_id': r['id'], 'existing': True}), 202
        mh = model_hash(start, end, facts, digests, counts)
        payload = {'start': start.isoformat(), 'end': end.isoformat(), 'facts': facts,
                   'digests': digests, 'counts': counts}
        src = None if force else _reusable_result(db, mh)
        if src:
            jid = enqueue(db, WEEKLY, requested_by=uid, period=(start, end), with_labor=wl,
                          input_hash=h, show_ids=ids, payload=payload, model_hash=mh)
            _complete_from(db, jid, src, payload, wl)
            db.commit()
            _log().info(f"AI_SUMMARY_REUSED job={jid} from={src['_id']} by={uid}")
            return jsonify({'run_id': jid, 'reused': True, 'derived': True})
        live = db.execute("SELECT COUNT(*) AS n FROM ai_jobs WHERE kind=%s AND test_mode=%s "
                          "AND status IN ('queued', 'running')", (WEEKLY, _tm())).fetchone()['n']
        if live >= MAX_LIVE_WEEKLY:
            db.rollback()
            return jsonify({'error': 'Summaries are already being generated — try again shortly.'}), 409
        if session.get('user_role') != 'admin' and db.execute(
                "SELECT 1 FROM ai_jobs WHERE kind=%s AND test_mode=%s AND requested_by=%s "
                "AND status IN ('queued', 'running') LIMIT 1", (WEEKLY, _tm(), uid)).fetchone():
            db.rollback()
            return jsonify({'error': 'You already have a summary being generated — '
                                     'wait for it to finish.'}), 409
        if not any_capable(db, 'infer'):
            db.rollback()
            return jsonify({'error': 'No server can reach the AI service right now.'}), 503
        if force:
            payload['fresh'] = True
        jid = enqueue(db, WEEKLY, requested_by=uid, period=(start, end), with_labor=wl,
                      input_hash=h, show_ids=ids, payload=payload, model_hash=mh)
        db.commit()
    finally:
        db.close()
    _log().info(f'AI_SUMMARY_QUEUED job={jid} shows={len(ids)} by={uid}')
    kick()
    return jsonify({'run_id': jid}), 202


def _run_visible(r):
    if not r or r['kind'] != WEEKLY or r['test_mode'] != _tm():
        return False
    if r['with_labor'] and not _d['can_report_labor']():
        return False
    acc = _access()
    if acc is not None:
        try:
            ids = json.loads(r['show_ids'] or '[]')
        except ValueError:
            return False
        if any(i not in acc for i in ids):
            return False
    return True


_RUN_COLS = ('id, kind, status, test_mode, requested_by, period_start, period_end, with_labor, auto, '
             'stage, progress_done, progress_total, partial, message, show_ids, claimed_by, '
             'created_at::timestamptz AS created_at, finished_at::timestamptz AS finished_at')


def _run_out(r):
    is_admin = session.get('user_role') == 'admin'
    try:
        n = len(json.loads(r['show_ids'] or '[]'))
    except ValueError:
        n = 0
    out = {'id': r['id'], 'status': r['status'], 'stage': r['stage'] or '',
           'done': r['progress_done'] or 0, 'total': r['progress_total'] or 0,
           'partial': bool(r['partial']), 'message': r['message'] or '',
           'start': r['period_start'].isoformat() if r['period_start'] else '',
           'end': r['period_end'].isoformat() if r['period_end'] else '',
           'label': fmt_range(r['period_start'], r['period_end']),
           'with_labor': bool(r['with_labor']), 'auto': bool(r['auto']), 'shows': n,
           'pdf': pdf_url(r) if r['status'] == 'done' else '',
           'mine': r['requested_by'] == session.get('user_id'),
           'created_at': _d['ts_out'](r['created_at']), 'finished_at': _d['ts_out'](r['finished_at'])}
    if is_admin:
        out['claimed_by'] = r['claimed_by'] or ''
    return out


def _weekly_runs_view():
    _need_reports()
    db = _d['get_db']()
    try:
        rows = db.execute(f"SELECT {_RUN_COLS} FROM ai_jobs WHERE kind=%s AND test_mode=%s "
                          "AND (with_labor=0 OR %s) ORDER BY id DESC LIMIT 60",
                          (WEEKLY, _tm(), bool(_d['can_report_labor']()))).fetchall()
    finally:
        db.close()
    out = [_run_out(r) for r in rows if _run_visible(r)][:20]
    return jsonify({'runs': out, 'default_end': weekly_window()[1].isoformat()})


def _load_run(run_id, cols=_RUN_COLS):
    db = _d['get_db']()
    try:
        r = db.execute(f'SELECT {cols} FROM ai_jobs WHERE id=%s', (run_id,)).fetchone()
    finally:
        db.close()
    if not _run_visible(r):
        abort(404)
    return r


def _weekly_run_view(run_id):
    _need_reports()
    return jsonify(_run_out(_load_run(run_id)))


def _weekly_cancel_view(run_id):
    _need_reports()
    r = _load_run(run_id)
    uid = session.get('user_id')
    if not (r['requested_by'] == uid or session.get('user_role') == 'admin'):
        abort(404)
    db = _d['get_db']()
    try:
        db.execute("UPDATE ai_jobs SET cancel_requested=1 WHERE id=%s AND status IN ('queued','running')",
                   (run_id,))
        db.execute("UPDATE ai_jobs SET status='cancelled', stage='Cancelled', finished_at=NOW(), "
                   "payload_json=NULL WHERE id=%s AND status='queued'", (run_id,))
        db.commit()
    finally:
        db.close()
    _log().info(f'AI_SUMMARY_CANCEL job={run_id} by={uid}')
    return jsonify({'success': True})


def pdf_filename(start, end, with_labor=False, run_id=None):
    """A descriptive download name (3.59.0), e.g.
    Weekly-Post-Show-Summary_2026-11-06_to_2026-11-12_with-labor.pdf.
    ASCII only (it goes into a URL and a Content-Disposition header)."""
    as_date = _d['as_date']
    a, b = as_date(start), as_date(end)
    if a and b:
        span = a.isoformat() if a == b else f'{a.isoformat()}_to_{b.isoformat()}'
    else:
        span = f'run-{int(run_id or 0)}'
    return f"Weekly-Post-Show-Summary_{span}{'_with-labor' if with_labor else ''}.pdf"


def pdf_url(r):
    """/reports/weekly-summary/<id>/<descriptive name>.pdf — the browser's
    PDF viewer saves under the URL's last segment, so the name lives there
    too. The name part is cosmetic; the id decides (the old <id>.pdf URL
    still works)."""
    return (f"/reports/weekly-summary/{int(r['id'])}/"
            f"{pdf_filename(r['period_start'], r['period_end'], r['with_labor'], r['id'])}")


def _weekly_pdf_view(run_id, slug=None):
    _need_reports()
    r = _load_run(run_id, _RUN_COLS + ', result_json')
    if r['status'] != 'done' or not r['result_json']:
        abort(404)
    pdf = build_pdf(json.loads(r['result_json']), show_assets=_d['can_report_assets']())
    _log().info(f"AI_SUMMARY_PDF job={run_id} by={session.get('user_id')}")
    fname = pdf_filename(r['period_start'], r['period_end'], r['with_labor'], run_id)
    return Response(pdf, mimetype='application/pdf',
                    headers={'Content-Disposition': f'inline; filename="{fname}"',
                             'Cache-Control': 'private, no-store'})


# ── PDF ───────────────────────────────────────────────────────────────────────

def _pdf_context(res, show_assets=False):
    as_date = _d['as_date']
    facts, shows, week = res.get('facts') or {}, res.get('shows') or {}, res.get('week') or {}
    order = [str(i) for i in res.get('order') or []]
    wl = bool(res.get('with_labor'))
    names = {int(k): (f.get('name') or f'Show #{k}') for k, f in facts.items()}

    def label(ids):
        return ', '.join(names.get(i, f'#{i}') for i in ids or [])
    rows = []
    sev = {'high': 0, 'medium': 0, 'low': 0}
    labor_total, labor_hours = 0.0, 0.0
    # Rental $ only where the stored facts carry it (runs from 3.58.0 on).
    show_assets = bool(show_assets) and any('assets_in' in (facts.get(k) or {}) for k in order)
    gear_total, ext_total, billed_total = 0.0, 0.0, 0.0

    def money(v):
        return f'${v:,.2f}'
    for sid in order:
        f, s = facts.get(sid) or {}, shows.get(sid) or {}
        for it in s.get('issues', []):
            sev[it.get('severity', 'low')] = sev.get(it.get('severity', 'low'), 0) + 1
        if wl and f.get('settled') and f.get('labor_total') is not None:
            labor_total += float(f['labor_total'])
            labor_hours += float(f.get('labor_hours') or 0)
        lab = float(f['labor_total']) if (wl and f.get('labor_total') is not None) else None
        gin, gext = float(f.get('assets_in') or 0), float(f.get('assets_ext') or 0)
        billed = (gin + gext if show_assets else 0.0) + (lab or 0.0)
        if show_assets:
            gear_total += gin
            ext_total += gext
        billed_total += billed
        parts = []
        if show_assets:
            parts.append(f'Gear {money(gin)}')
            if gext:
                parts.append(f'External {money(gext)}')
        if wl:
            parts.append(f'Labor {money(lab)}' if lab is not None
                         else ('Labor not settled' if not f.get('settled') else 'Labor —'))
        rows.append({'billed': money(billed), 'billed_parts': parts,
                     'name': f.get('name', ''), 'venue': f.get('venue', ''), 'dates': f.get('dates', ''),
                     'perfs': len(f.get('perfs') or []), 'pm': f.get('pm', ''),
                     'settled': f.get('settled'),
                     'labor': (f"${float(f['labor_total']):,.2f}" if f.get('labor_total') is not None
                               else ('Not settled' if not f.get('settled') else '—')),
                     'one_liner': s.get('one_liner', ''), 'ai_failed': s.get('ai_failed'),
                     'no_notes': not s.get('ai') and not s.get('ai_failed'),
                     'issues': s.get('issues', []), 'peak_leq': f.get('peak_leq')})
    counts = res.get('counts') or {}
    sd, ed = as_date(res.get('start')), as_date(res.get('end'))
    return {
        'title': 'Post-Show Executive Summary', 'period': fmt_range(sd, ed),
        'generated': res.get('generated_at', '').replace('T', ' '), 'model': res.get('model', ''),
        'week': week, 'rows': rows, 'with_labor': wl, 'sev': sev, 'counts': counts,
        'labor_total': f'${labor_total:,.2f}', 'labor_hours': f'{labor_hours:,.1f}',
        'show_assets': show_assets, 'show_money': show_assets or wl,
        'gear_total': money(gear_total), 'ext_total': money(ext_total), 'billed_total': money(billed_total),
        'unsettled': [r['name'] for r in rows if not r['settled']],
        'issues': [dict(i, shows=label(i.get('show_ids'))) for i in week.get('issues', [])],
        'patterns': [dict(i, shows=label(i.get('show_ids'))) for i in week.get('patterns') or []],
        'recommendations': [dict(i, shows=label(i.get('show_ids')))
                            for i in week.get('recommendations') or []],
        # Notes coverage: runs before 3.59.0 have no count — work it out.
        'with_notes': counts.get('with_notes', sum(
            1 for k in order if (shows.get(k) or {}).get('ai') or (shows.get(k) or {}).get('ai_failed'))),
        'highlights': [dict(i, shows=label(i.get('show_ids'))) for i in week.get('highlights', [])],
        'follow_ups': [dict(i, shows=label(i.get('show_ids'))) for i in week.get('follow_ups', [])],
        'partial': bool(res.get('partial')),
        'venues': sorted({r['venue'] for r in rows if r['venue']}),
    }


def build_pdf(res, show_assets=False):
    """One-page Letter PDF from a stored result (no model call, nothing
    written). Condenses in up to three steps, then lets the show table
    continue on page 2 — never drops anything. show_assets: print rental $
    (the viewer may see asset $; never for the weekly e-mail)."""
    from weasyprint import HTML
    ctx = _pdf_context(res, show_assets)
    venue = ctx['venues'][0] if len(ctx['venues']) == 1 else ''
    db = _d['get_db']()
    try:
        ctx['logo'] = _d['get_logo_for_venue'](db, venue)
        ctx['vc'] = _d['get_venue_pdf_colors'](db, venue) if venue else None
    finally:
        db.close()
    fc = _d['wp_font_config']()
    doc = None
    for level in range(3):
        html = render_template('pdf/weekly_summary_pdf.html', cz=level, **ctx)
        doc = HTML(string=html, base_url=_d['app'].root_path).render(font_config=fc)
        if len(doc.pages) <= 1:
            break
    return doc.write_pdf()


# ══ weekly e-mail (leader-gated) ══════════════════════════════════════════════

def email_settings():
    try:
        rec = json.loads(_setting('weekly_summary_email_recipients', '[]') or '[]')
        rec = [int(x) for x in rec if str(x).isdigit()][:200]
    except (ValueError, TypeError):
        rec = []

    def _int(k, d, lo, hi):
        try:
            v = int(_setting(k, str(d)))
        except (TypeError, ValueError):
            v = d
        return v if lo <= v <= hi else d
    return {'enabled': _setting('weekly_summary_email_enabled', '0') == '1',
            'day': _int('weekly_summary_email_day', 0, 0, 6),
            'hour': _int('weekly_summary_email_hour', 7, 0, 23), 'recipients': rec}


def _recipient_rows(db, ids):
    if not ids:
        return []
    return db.execute(f"SELECT u.id, u.email FROM users u WHERE u.id = ANY(%s) "
                      f"AND COALESCE(u.email, '') <> '' AND {_d['recipient_sql']()}",
                      (list(ids),)).fetchall()


def weekly_email_tick(now=None):
    """Scheduler job (every 15 min). Leader-gated: enqueues the week's
    summary once, at/after the configured day + hour (within 24 h), and
    mails it once a capable worker has generated it."""
    if not _d['am_i_leader']():
        return
    try:
        _weekly_email_work(now or datetime.now())
    except db_adapter.DatabaseUnavailable:
        _log().error('AI_WEEKLY_EMAIL_SKIPPED reason=database_unavailable')


def _weekly_email_work(now):
    cfg = email_settings()
    if not enabled() or not cfg['enabled']:
        return
    back = (now.weekday() - cfg['day']) % 7
    slot = datetime.combine(now.date() - timedelta(days=back), dtime(cfg['hour']))
    if slot > now:
        slot -= timedelta(days=7)
    db = _d['get_db']()
    try:
        if now - slot < timedelta(hours=24):
            end = slot.date() - timedelta(days=1)
            start = end - timedelta(days=6)
            got = db.execute("INSERT INTO ai_weekly_email_log (period_end, period_start, status) "
                             "VALUES (%s, %s, 'preparing') ON CONFLICT (period_end) DO NOTHING "
                             "RETURNING period_end", (end, start)).fetchone()
            if got:
                try:
                    ids, facts, digests, counts, h = build_inputs(db, start, end, 0, None)
                except TooManyShows as e:
                    ids = None
                    db.execute("UPDATE ai_weekly_email_log SET status='failed', message=%s "
                               "WHERE period_end=%s", (f'Too many shows ({e.n}).', end))
                if ids == []:
                    db.execute("UPDATE ai_weekly_email_log SET status='skipped', "
                               "message='No shows that week.' WHERE period_end=%s", (end,))
                elif ids:
                    mh = model_hash(start, end, facts, digests, counts)
                    payload = {'start': start.isoformat(), 'end': end.isoformat(),
                               'facts': facts, 'digests': digests, 'counts': counts}
                    jid = enqueue(db, WEEKLY, requested_by=None, period=(start, end), with_labor=0,
                                  auto=1, input_hash=h, show_ids=ids, payload=payload,
                                  model_hash=mh)
                    src = _reusable_result(db, mh)
                    if src:          # someone already generated this week: no model work
                        _complete_from(db, jid, src, payload, 0)
                    db.execute("UPDATE ai_weekly_email_log SET status='queued', job_id=%s "
                               "WHERE period_end=%s", (jid, end))
                    _log().info(f'AI_WEEKLY_EMAIL_QUEUED job={jid} shows={len(ids)} '
                                f"reused={src['_id'] if src else 0}")
            db.commit()
        failed = db.execute(
            "UPDATE ai_weekly_email_log l SET status='failed', message='The summary could not be generated.' "
            "FROM ai_jobs j WHERE l.job_id = j.id AND l.status='queued' "
            "AND j.status IN ('error', 'cancelled', 'interrupted') RETURNING l.job_id").fetchall()
        due = db.execute(
            "UPDATE ai_weekly_email_log l SET status='sending' FROM ai_jobs j "
            "WHERE l.job_id = j.id AND l.status='queued' AND j.status='done' "
            "RETURNING l.period_end, l.job_id").fetchall()
        db.commit()
    finally:
        db.close()
    for r in failed:
        _log().error(f"AI_WEEKLY_EMAIL_FAILED job={r['job_id']} reason=generation")
    if due:
        kick()
    for r in due:
        _send_weekly(r['period_end'], r['job_id'])


def _send_weekly(period_end, job_id):
    status, msg, n = 'failed', '', 0
    try:
        db = _d['get_db']()
        try:
            row = db.execute('SELECT result_json FROM ai_jobs WHERE id=%s', (job_id,)).fetchone()
            rec = _recipient_rows(db, email_settings()['recipients'])
        finally:
            db.close()
        emails = sorted({r['email'] for r in rec})
        if not row or not row['result_json']:
            msg = 'Summary missing.'
        elif not emails:
            msg = 'No recipient may view Reports.'
        else:
            res = json.loads(row['result_json'])
            with _d['app'].app_context():
                pdf = build_pdf(res)
                ctx = _pdf_context(res)
                wk = res.get('week') or {}
                blocks = []
                if wk.get('summary'):
                    blocks.append({'text': wk['summary']})
                if ctx['issues']:
                    blocks.append({'heading': 'Issues', 'items': [
                        f"{i['severity'].upper()}: {i['text']}" + (f" ({i['shows']})" if i['shows'] else '')
                        for i in ctx['issues']]})
                if ctx['patterns']:
                    blocks.append({'heading': 'Patterns', 'items': [i['text'] for i in ctx['patterns']]})
                if ctx['recommendations']:
                    blocks.append({'heading': 'Recommendations',
                                   'items': [i['text'] for i in ctx['recommendations']]})
                if ctx['follow_ups']:
                    blocks.append({'heading': 'Follow-ups', 'items': [i['text'] for i in ctx['follow_ups']]})
                text, html = _d['mail'](
                    'Weekly post-show summary', eyebrow='Reports', intro=wk.get('headline', ''),
                    facts=[('Week', ctx['period']), ('Shows', str(ctx['counts'].get('shows', 0))),
                           ('Performances', str(ctx['counts'].get('performances', 0)))],
                    blocks=blocks, button=('Open Reports', _d['mail_url']('/reports?tab=weekly')),
                    note='AI-assisted draft. Verify against each show’s Post-Show tab.')
                ok, smsg = _d['send_email'](
                    f"Weekly post-show summary · {ctx['period']}", emails, text, html,
                    attachments=[{'filename': pdf_filename(res.get('start'), period_end), 'data': pdf,
                                  'mimetype': 'application/pdf'}],
                    error_context={'purpose': 'weekly_summary', 'triggered_by': 'schedule'})
            n = len(emails)
            status = 'sent' if ok else 'failed'
            msg = '' if ok else 'Sending failed (see Email Send Errors).'
    except Exception as e:
        msg = 'Sending failed.'
        _log().error(f'AI_WEEKLY_EMAIL_FAILED job={job_id} err={type(e).__name__}')
    db = _d['get_db']()
    try:
        db.execute("UPDATE ai_weekly_email_log SET status=%s, message=%s, recipients=%s, "
                   "sent_at=CASE WHEN %s='sent' THEN NOW() ELSE sent_at END WHERE period_end=%s",
                   (status, msg, n, status, period_end))
        db.commit()
    finally:
        db.close()
    _log().info(f'AI_WEEKLY_EMAIL status={status} job={job_id} recipients={n}')


# ── a show is deleted (app.py _purge_show: delete + merge) ────────────────────

def on_show_delete(db, show_id):
    """Forget AI output built from a show that is being deleted (3.56.2).
    A stored weekly result keeps the show's name, venue, PM and the model's
    summary of its notes / comments; ai_jobs.show_id going NULL does not
    touch that. Every job of ANY test_mode that names the show loses its
    payload + result; a finished one reads as an error ("run it again"), a
    queued / running one is cancelled (the worker's guarded writes then stop
    it and its slot is released). Its hashes go too, so nothing reuses it.
    Caller commits (inside the purge's transaction)."""
    sid = int(show_id)
    # show_ids is a JSON list of ints: a digit-bounded match, never a
    # ::jsonb cast (one malformed row must not fail a show delete).
    hit = "(show_id = %s OR show_ids ~ %s)"
    args = (sid, r'(^|[^0-9])' + str(sid) + r'([^0-9]|$)')
    db.execute(
        "UPDATE ai_jobs SET status='cancelled', stage='Cancelled', finished_at=NOW(), "
        " cancel_requested=1, payload_json=NULL, result_json=NULL, input_hash=NULL, "
        " model_hash=NULL, transcript_json=NULL, message='A show in this run was deleted.' "
        f"WHERE status IN ('queued', 'running') AND {hit}", args)
    db.execute(
        "UPDATE ai_jobs SET status=CASE WHEN status='done' THEN 'error' ELSE status END, "
        " error_code=CASE WHEN status='done' THEN 'show_deleted' ELSE error_code END, "
        " message=CASE WHEN status='done' THEN 'A show in this summary was deleted. Run it again.' "
        "              ELSE message END, "
        " payload_json=NULL, result_json=NULL, input_hash=NULL, model_hash=NULL, transcript_json=NULL "
        f"WHERE status NOT IN ('queued', 'running') AND {hit} "
        " AND (result_json IS NOT NULL OR payload_json IS NOT NULL OR status='done' "
        "      OR input_hash IS NOT NULL OR model_hash IS NOT NULL OR transcript_json IS NOT NULL)", args)


# ── housekeeping (from app.py's hourly maintenance) ───────────────────────────

def hourly_trim(db):
    db.execute("UPDATE ai_jobs SET transcript_json=NULL WHERE transcript_json IS NOT NULL "
               "AND COALESCE(finished_at, created_at) < NOW() - %s * INTERVAL '1 day'", (_TX_KEEP_DAYS,))
    db.execute("DELETE FROM ai_sessions WHERE started_at < NOW() - INTERVAL '30 days'")
    db.execute("UPDATE ai_jobs SET payload_json=NULL WHERE payload_json IS NOT NULL "
               "AND status NOT IN ('queued', 'running')")
    db.execute("UPDATE ai_jobs SET result_json=NULL WHERE kind='extract' AND result_json IS NOT NULL "
               "AND finished_at < NOW() - INTERVAL '1 hour'")
    db.execute("DELETE FROM ai_jobs WHERE kind IN ('extract', 'pull_model') "
               "AND status NOT IN ('queued', 'running') AND created_at < NOW() - INTERVAL '7 days'")
    db.execute("DELETE FROM ai_jobs WHERE kind='weekly_postshow' AND status NOT IN ('queued', 'running') "
               "AND id NOT IN (SELECT id FROM ai_jobs WHERE kind='weekly_postshow' "
               "ORDER BY id DESC LIMIT 100)")


# ── Settings → AI (admin) ─────────────────────────────────────────────────────

def install_command():
    return f"cd {_d['app_dir']} && sudo bash install.sh --with-ollama"


# ── Settings → AI → History (3.58.0, admin) ──────────────────────────────────

_HIST_KINDS = {'extract': 'AI Extract', WEEKLY: 'Weekly summary'}


def _hist_row(r):
    def secs(a, b):
        try:
            return int((b - a).total_seconds()) if a and b else None
        except TypeError:
            return None
    what = (r['show_name'] or (f"Show #{r['show_id']}" if r['show_id'] else '')) if r['kind'] == 'extract' \
        else fmt_range(r['period_start'], r['period_end'])
    return {'id': r['id'], 'kind': r['kind'], 'kind_label': _HIST_KINDS.get(r['kind'], r['kind']),
            'what': what, 'show_id': r['show_id'], 'status': r['status'], 'partial': bool(r['partial']),
            'auto': bool(r['auto']), 'with_labor': bool(r['with_labor']),
            'by': r['by_name'] or ('Weekly e-mail' if r['auto'] else ''),
            'server': r['claimed_by'] or '', 'model': r['model'] or '',
            'error': r['error_code'] or '', 'message': r['message'] or '',
            'has_transcript': bool(r['has_tx']),
            'created_at': _d['ts_out'](r['created_at']),
            'took_s': secs(r['started_at'], r['finished_at'])}


_HIST_SQL = ("SELECT j.id, j.kind, j.status, j.partial, j.auto, j.with_labor, j.show_id, "
             "j.period_start, j.period_end, j.claimed_by, j.model, j.error_code, j.message, "
             "j.created_at::timestamptz AS created_at, j.started_at, j.finished_at, "
             "(j.transcript_json IS NOT NULL) AS has_tx, s.name AS show_name, "
             "COALESCE(NULLIF(u.display_name, ''), u.username) AS by_name "
             "FROM ai_jobs j LEFT JOIN shows s ON s.id = j.show_id "
             "LEFT JOIN users u ON u.id = j.requested_by ")


def _history_view():
    kinds = list(_HIST_KINDS)
    from flask import request
    k = request.args.get('kind', '')
    if k in _HIST_KINDS:
        kinds = [k]
    db = _d['get_db']()
    try:
        rows = db.execute(_HIST_SQL + "WHERE j.test_mode = %s AND j.kind = ANY(%s) "
                          "ORDER BY j.id DESC LIMIT 100", (_tm(), kinds)).fetchall()
    finally:
        db.close()
    return jsonify({'jobs': [_hist_row(r) for r in rows], 'keep_days': _TX_KEEP_DAYS})


def _history_job_view(job_id):
    db = _d['get_db']()
    try:
        r = db.execute(_HIST_SQL + "WHERE j.id = %s AND j.test_mode = %s AND j.kind = ANY(%s)",
                       (job_id, _tm(), list(_HIST_KINDS))).fetchone()
        extra = db.execute('SELECT transcript_json, result_json FROM ai_jobs WHERE id = %s',
                           (job_id,)).fetchone() if r else None
    finally:
        db.close()
    if not r:
        abort(404)
    out = _hist_row(r)
    try:
        out['steps'] = json.loads(extra['transcript_json']) if extra['transcript_json'] else []
    except ValueError:
        out['steps'] = []
    res = {}
    try:
        res = json.loads(extra['result_json']) if extra['result_json'] else {}
    except ValueError:
        pass
    if r['kind'] == WEEKLY:
        out['reused_from'] = res.get('reused_from')
        out['pdf'] = pdf_url(r) if r['status'] == 'done' and extra['result_json'] else ''
    else:
        sug = res.get('suggestions') or {}
        out['found'] = [{'label': v.get('label') or k, 'value': v.get('value') or ''}
                        for k, v in sug.items()][:200]
    _log().info(f"AI_HISTORY_VIEW job={job_id} by={session.get('user_id')}")
    return jsonify(out)


def _status_view():
    st = local_state()
    db = _d['get_db']()
    try:
        srv = servers(db)
        pulls = db.execute(
            "SELECT id, status, stage, progress_done, progress_total, target_host, model, message "
            "FROM ai_jobs WHERE kind='pull_model' AND test_mode=%s "
            "AND (status IN ('queued', 'running') OR finished_at > NOW() - INTERVAL '10 minutes') "
            "ORDER BY id DESC LIMIT 10", (_tm(),)).fetchall()
        log = db.execute("SELECT period_start, period_end, status, recipients, message, "
                         "sent_at::timestamptz AS sent_at FROM ai_weekly_email_log "
                         "ORDER BY period_end DESC LIMIT 5").fetchall()
    finally:
        db.close()
    local = {'hostname': HOST, 'enabled': bool(st.get('enabled')), 'installed': bool(st.get('installed')),
             'running': bool(st.get('running')), 'version': st.get('version') or '',
             'has_model': bool(st.get('has_model')), 'models': (st.get('models') or [])[:50],
             'err': st.get('err') or '', 'via_node': bool(st.get('via_node')),
             'checked_ago_s': int(time.time() - st['checked']) if st.get('checked') else None}
    return jsonify({
        'local': local, 'servers': srv, 'model': model(), 'url': ollama_url(),
        'url_source': url_source(), 'stored_url_ignored': stored_url_ignored(),
        'model_source': model_source(),
        'node': oc.node_base() if url_source() == 'node' else '',
        'test_mode': bool(_d.get('test_mode')),
        'install_command': install_command(),
        'pull_command': f'ollama pull {model()}',
        'slots': {'limit': slot_limit()},
        'pulls': [{'id': p['id'], 'status': p['status'], 'stage': p['stage'] or '',
                   'done': p['progress_done'] or 0, 'total': p['progress_total'] or 0,
                   'host': p['target_host'] or '', 'model': p['model'] or '',
                   'message': p['message'] or ''} for p in pulls],
        'email_log': [{'start': r['period_start'].isoformat() if r['period_start'] else '',
                       'end': r['period_end'].isoformat(), 'status': r['status'],
                       'recipients': r['recipients'] or 0, 'message': r['message'] or '',
                       'sent_at': _d['ts_out'](r['sent_at'])} for r in log],
    })


def _probe_now_view():
    st = probe_refresh()
    return jsonify({'success': True, 'running': bool(st.get('running')),
                    'has_model': bool(st.get('has_model'))})


def _pull_view():
    if not enabled():
        return jsonify({'success': False, 'error': 'Turn AI on first.'}), 400
    body = _d['json_obj']()
    host = body.get('hostname') if isinstance(body.get('hostname'), str) else HOST
    host = host.strip()[:255] or HOST
    name = model()
    db = _d['get_db']()
    try:
        if host != HOST:                # that server's own model (3.60.0)
            for sv in servers(db):
                pm = (sv.get('ai') or {}).get('model') or ''
                if sv['hostname'] == host and oc.valid_model(pm):
                    name = pm
                    break
        if not any_capable(db, KIND_PULL, target_host=host):
            return jsonify({'success': False,
                            'error': 'Ollama is not running on that server.'}), 409
        r = db.execute("SELECT id FROM ai_jobs WHERE kind='pull_model' AND target_host=%s "
                       "AND test_mode=%s AND status IN ('queued', 'running') LIMIT 1",
                       (host, _tm())).fetchone()
        if r:
            db.commit()
            return jsonify({'success': True, 'job_id': r['id'], 'existing': True})
        jid = enqueue(db, KIND_PULL, requested_by=session.get('user_id'), target_host=host,
                      model_name=name, stage=f'Waiting for {host}')
        db.commit()
    finally:
        db.close()
    _log().info(f"AI_MODEL_PULL_QUEUED job={jid} host={host} by={session.get('username')}")
    kick()
    return jsonify({'success': True, 'job_id': jid}), 202


def _email_get_view():
    db = _d['get_db']()
    try:
        people = db.execute(f"SELECT u.id, COALESCE(NULLIF(u.display_name, ''), u.username) AS name, "
                            f"COALESCE(u.email, '') <> '' AS has_email FROM users u "
                            f"WHERE {_d['recipient_sql']()} ORDER BY 2").fetchall()
    finally:
        db.close()
    return jsonify({'settings': email_settings(),
                    'people': [{'id': p['id'], 'name': p['name'], 'has_email': bool(p['has_email'])}
                               for p in people]})


def _email_save_view():
    body = _d['json_obj']()
    vals = {}
    if 'enabled' in body:
        vals['weekly_summary_email_enabled'] = '1' if body.get('enabled') in (True, 1, '1') else '0'
    for k, lo, hi in (('day', 0, 6), ('hour', 0, 23)):
        if k in body:
            try:
                v = int(body.get(k))
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': f'Invalid {k}.'}), 400
            if not lo <= v <= hi:
                return jsonify({'success': False, 'error': f'Invalid {k}.'}), 400
            vals[f'weekly_summary_email_{k}'] = str(v)
    if 'recipients' in body:
        rec = body.get('recipients')
        if not isinstance(rec, list) or len(rec) > 200:
            return jsonify({'success': False, 'error': 'Invalid recipient list.'}), 400
        ids = []
        for x in rec:
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': 'Invalid recipient list.'}), 400
        db = _d['get_db']()
        try:
            ok = {r['id'] for r in db.execute(
                f"SELECT u.id FROM users u WHERE u.id = ANY(%s) AND {_d['recipient_sql']()}",
                (ids,)).fetchall()}
        finally:
            db.close()
        bad = [i for i in ids if i not in ok]
        if bad:
            return jsonify({'success': False,
                            'error': 'Only people who can open Reports can receive the summary.'}), 400
        vals['weekly_summary_email_recipients'] = json.dumps(sorted(set(ids)))
    if not vals:
        return jsonify({'success': False, 'error': 'Nothing to save.'}), 400
    db = _d['get_db']()
    try:
        before = email_settings()
        for k, v in vals.items():
            db.execute('INSERT INTO app_settings (key, value) VALUES (%s, %s) '
                       'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value', (k, v))
        _d['log_audit'](db, 'SETTINGS_CHANGE', 'app_settings', None,
                        detail='weekly_summary_email', before=before, after=vals)
        db.commit()
    finally:
        db.close()
    _log().info(f"SETTINGS_CHANGE key=weekly_summary_email by={session.get('username')}")
    return jsonify({'success': True, 'settings': email_settings()})


# ── wiring ────────────────────────────────────────────────────────────────────

def register(app, **deps):
    """deps: get_db, get_app_setting, syslog_logger,
    login_required, admin_required, test_mode, am_i_leader, json_obj, ts_out,
    as_date, can_view_reports, can_report_labor, can_report_assets, asset_totals,
    get_accessible_shows,
    calc_post_show_labor_cost, unsettled_show_ids, pm_contacts_many,
    module_enabled, wp_font_config, get_logo_for_venue, get_venue_pdf_colors,
    mail, mail_url, send_email, recipient_sql, postnotes_internal_keys,
    instance_id, log_audit, app_dir."""
    _d.update(deps, app=app)
    login, admin = deps['login_required'], deps['admin_required']
    app.add_url_rule('/api/ai/jobs/<int:job_id>', 'ai_job_status', login(_job_view))
    app.add_url_rule('/api/ai/jobs/<int:job_id>/cancel', 'ai_job_cancel', login(_job_cancel_view),
                     methods=['POST'])
    app.add_url_rule('/api/reports/weekly-summary', 'weekly_summary_start', login(_weekly_start_view),
                     methods=['POST'])
    app.add_url_rule('/api/reports/weekly-summary/runs', 'weekly_summary_runs', login(_weekly_runs_view))
    app.add_url_rule('/api/reports/weekly-summary/runs/<int:run_id>', 'weekly_summary_run',
                     login(_weekly_run_view))
    app.add_url_rule('/api/reports/weekly-summary/runs/<int:run_id>/cancel', 'weekly_summary_cancel',
                     login(_weekly_cancel_view), methods=['POST'])
    app.add_url_rule('/reports/weekly-summary/<int:run_id>.pdf', 'weekly_summary_pdf',
                     login(_weekly_pdf_view))
    app.add_url_rule('/reports/weekly-summary/<int:run_id>/<slug>.pdf', 'weekly_summary_pdf_named',
                     login(_weekly_pdf_view))
    app.add_url_rule('/settings/ai/status', 'ai_settings_status', admin(_status_view))
    app.add_url_rule('/settings/ai/history', 'ai_settings_history', admin(_history_view))
    app.add_url_rule('/settings/ai/history/<int:job_id>', 'ai_settings_history_job',
                     admin(_history_job_view))
    app.add_url_rule('/settings/ai/probe', 'ai_settings_probe', admin(_probe_now_view), methods=['POST'])
    app.add_url_rule('/settings/ai/pull', 'ai_settings_pull', admin(_pull_view), methods=['POST'])
    app.add_url_rule('/settings/ai/weekly-email', 'ai_weekly_email_get', admin(_email_get_view))
    app.add_url_rule('/settings/ai/weekly-email', 'ai_weekly_email_save', admin(_email_save_view),
                     methods=['POST'])
