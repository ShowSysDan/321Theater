# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
Spot sound-level integration — SANDBOXED module (3.27.0).

Spot (github.com/ShowSysDan/Spot) owns the building's sensors ("monitors":
dB meters, …), their readings and the branded level report. 321Theater owns
venues and performances. The two apps talk ONLY over Spot's HTTP API v1
(docs/API.md in the Spot repo); neither touches the other's database.

What this module does:
  * Settings → Spot (admin): Spot URL + API key (write-only), timezone,
    automation knobs, and the venue → monitor mapping (Spot has no idea what
    a 321Theater venue is — API.md recipe 8.1 says we keep the mapping).
  * After every performance ends (+ a grace period so late readings land),
    the leader-gated `run_spot_capture` job PULLS the levels for the
    performance's window from Spot (`GET /stats`, `GET /report.pdf`, with
    Spot's curtain detection when enabled) — recipe 8.2. Pull, not Spot's
    push callback: no unauthenticated inbound endpoint to defend, and
    nothing to do when a performance moves except notice its window changed.
  * The numbers land in `spot_captures` and show on the show page's Post
    Show tab + the Post-Show Notes PDF ("Sound Levels"); the PDF is filed
    into the show's Files as an ordinary show_attachments row (via the
    injected store/archive helpers, so S3 / dual-write / archive rules stay
    in app.py's one implementation).

The performance window ("go off the show dates/times in the advance form"):
  start = show_performances.perf_date + perf_time (the advance form's
  dates/times; a show with no performance rows uses shows.show_date +
  show_time). end = the END of the production-schedule row on that day that
  starts at the performance time (e.g. "19:30–22:15 Show"), else start +
  the default show length. Spot's detection (when on) then finds the actual
  curtain up/down within ±window of those times.

SANDBOX RULES (mirrors prism_module / security_module):
  * Owns `spot_venue_monitors`, `spot_captures` and `spot_*` app_settings.
    The only other write is the show-file attachment, through the injected
    `store_show_attachment` / `archive_show_attachment` helpers.
  * app.py's knowledge of it: one register() call, the scheduler job, the
    Post Show tab section + postnotes PDF section (module-gated), and the
    show merge / snapshot child-table lists.
  * The module flag (Settings → System → Modules, key `spot`, default OFF)
    gates the show-page routes, the job and the show-page UI. The Spot
    settings page stays reachable for admins so it can be configured first.

Multi-instance safety (several 321Theater servers share one PostgreSQL):
  * The job starts with `am_i_leader()` — only the cluster primary pulls.
    TEST_MODE instances are never leader, so a test server never auto-pulls.
  * Every capture first CLAIMS its `spot_captures` row with a conditional
    UPDATE (status 'running', stale after 10 min), so a manual "Pull now"
    on another instance, or a leader hand-off mid-run, can never fetch and
    file the same performance twice at the same time.

Security:
  * The API key is write-only (never sent to a browser; blank = keep). A
    connection test against a URL other than the saved one needs the key
    typed again, so the stored key can't be redirected to another host.
  * All Spot traffic is server-side, http(s) only, ignores env proxies and
    never follows redirects (the key header must not follow a 3xx).
    Responses are size-capped.
  * Spot error detail (can carry internal hosts) is shown to admins only.

Syslog: SPOT_CAPTURE, SPOT_CAPTURE_FAILED, SPOT_SETTINGS_SAVE,
SPOT_MAPPING_SAVE, SPOT_JOB.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import abort, jsonify, render_template, request, session

_d = {}

MODULE_KEY = 'spot'

# app_settings keys → defaults. Strings, like every app_settings value.
SETTINGS_DEFAULTS = {
    'spot_base_url':          '',
    'spot_api_key':           '',
    'spot_timezone':          'America/New_York',
    'spot_auto_pull':         '1',    # job pulls automatically after each show
    'spot_grace_minutes':     '10',   # wait after the end before pulling
    'spot_default_minutes':   '150',  # show length when the schedule has no end
    'spot_use_schedule_end':  '1',    # end time from the production schedule
    'spot_auto_detect':       '1',    # Spot finds the real curtain up/down
    'spot_detect_window':     '15',   # minutes either side (1–60)
    'spot_detect_min_step':   '6',    # dB (0.5–60)
    'spot_attach_pdf':        '1',    # file Spot's PDF under the show's Files
    'spot_lookback_days':     '3',    # how far back the job catches up
    'spot_max_attempts':      '6',    # retries per performance before giving up
}
_INT_LIMITS = {
    'spot_grace_minutes':   (0, 240),
    'spot_default_minutes': (15, 1440),
    'spot_detect_window':   (1, 60),
    'spot_lookback_days':   (0, 30),
    'spot_max_attempts':    (1, 20),
}
_BOOL_KEYS = ('spot_auto_pull', 'spot_use_schedule_end', 'spot_auto_detect',
              'spot_attach_pdf')

MAX_JSON_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 25 * 1024 * 1024
HTTP_TIMEOUT = 30
MAX_MONITORS = 50          # Spot's per-request cap
JOB_BATCH = 20             # captures per job run (the rest go next run)
CLAIM_STALE_MINUTES = 10   # a 'running' claim older than this is abandoned
MANUAL_PULL_COOLDOWN = 60  # seconds between manual pulls of one performance
QUEUE_AHEAD_DAYS = 7       # Settings → Spot queue shows this far ahead

# Stats copied into spot_captures (Spot may add fields; we keep what we show).
_STAT_KEYS = ('count', 'events', 'min', 'min_at', 'max', 'max_at', 'avg',
              'p10', 'p50', 'p90', 'leq', 'l10', 'l50', 'l90')


class SpotError(Exception):
    """A Spot call failed. `public` is safe for any show user; str(e) is the
    admin-only detail."""
    def __init__(self, detail, public='Could not get data from Spot.', status=None):
        super().__init__(detail)
        self.public = public
        self.status = status


# ─── Settings ─────────────────────────────────────────────────────────────────

def _enabled():
    return _d['module_enabled'](MODULE_KEY)


def load_settings(db):
    rows = db.execute('SELECT key, value FROM app_settings WHERE key = ANY(%s)',
                      (list(SETTINGS_DEFAULTS),)).fetchall()
    cfg = dict(SETTINGS_DEFAULTS)
    for r in rows:
        if r['value'] is not None:
            cfg[r['key']] = r['value']
    return cfg


def _int(cfg, key):
    lo, hi = _INT_LIMITS[key]
    try:
        v = int(float(cfg.get(key)))
    except (TypeError, ValueError):
        v = int(SETTINGS_DEFAULTS[key])
    return max(lo, min(hi, v))


def _min_step(cfg):
    try:
        v = float(cfg.get('spot_detect_min_step'))
    except (TypeError, ValueError):
        v = 6.0
    return max(0.5, min(60.0, v))


def _on(cfg, key):
    return str(cfg.get(key, '')) == '1'


def _tz(cfg):
    try:
        return ZoneInfo(cfg.get('spot_timezone') or 'America/New_York')
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo('America/New_York')


def normalize_base_url(raw):
    """'http://spot.internal:6100' (optionally ending in /api/v1) → the
    origin+path without the API suffix, or raise ValueError. http(s) only, a
    host is required, no credentials / query / fragment."""
    s = (raw or '').strip()
    if not s:
        return ''
    if len(s) > 500 or any(ord(c) < 33 for c in s):
        raise ValueError('URL contains spaces or control characters.')
    p = urllib.parse.urlsplit(s)
    if p.scheme not in ('http', 'https') or not p.hostname:
        raise ValueError('Use an http:// or https:// URL with a host name.')
    if p.username or p.password or p.query or p.fragment:
        raise ValueError('Leave credentials, query and fragment out of the URL.')
    path = p.path.rstrip('/')
    if path.endswith('/api/v1'):
        path = path[:-len('/api/v1')]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, path, '', ''))


def is_configured(cfg):
    return bool(cfg.get('spot_base_url') and cfg.get('spot_api_key'))


# ─── HTTP client (server-side only) ───────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None   # → HTTPError: a 3xx is a failure, the key never follows


_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def spot_call(base_url, api_key, path, params=None, want='json'):
    """GET {base}/api/v1{path}. Returns parsed JSON, or (bytes, filename) for
    want='pdf'. Raises SpotError."""
    if not base_url or not api_key:
        raise SpotError('Spot URL or API key not set.', 'Spot is not configured.')
    url = base_url + '/api/v1' + path
    if params:
        url += '?' + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method='GET', headers={
        'Authorization': f'Bearer {api_key}',
        'Accept': 'application/pdf' if want == 'pdf' else 'application/json',
        'User-Agent': '321Theater-Spot/1',
    })
    cap = MAX_PDF_BYTES if want == 'pdf' else MAX_JSON_BYTES
    try:
        with _opener.open(req, timeout=HTTP_TIMEOUT) as r:
            body = r.read(cap + 1)
            ctype = r.headers.get_content_type()
    except urllib.error.HTTPError as e:
        msg = f'HTTP {e.code}'
        try:
            err = json.loads(e.read(65536) or b'{}').get('error')
            if err:
                msg += f': {str(err)[:300]}'
        except Exception:
            pass
        public = ('Spot rejected the API key.' if e.code == 401
                  else 'Spot returned an error.')
        raise SpotError(msg, public, e.code) from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, 'reason', e)
        raise SpotError(f'Cannot reach Spot: {reason}', 'Spot is unreachable.') from None
    if len(body) > cap:
        raise SpotError('Spot response too large.')
    if want == 'pdf':
        if ctype != 'application/pdf' or not body.startswith(b'%PDF'):
            raise SpotError(f'Expected a PDF from Spot, got {ctype}.')
        return body
    try:
        return json.loads(body)
    except ValueError:
        raise SpotError(f'Spot sent non-JSON ({ctype}).') from None


def _call(cfg, path, params=None, want='json'):
    return spot_call(cfg.get('spot_base_url'), cfg.get('spot_api_key'),
                     path, params, want)


# ─── Venue → monitor mapping ──────────────────────────────────────────────────

def _vkey(v):
    return ' '.join((v or '').split()).lower()


def load_mapping(db):
    """{venue_key: {'venue': name, 'monitors': [ids], 'names': {id: name},
    'ref': id|None}}"""
    out = {}
    for r in db.execute(
            'SELECT venue_name, monitor_id, monitor_name, unit, is_detect_ref '
            'FROM spot_venue_monitors ORDER BY venue_name, monitor_id').fetchall():
        e = out.setdefault(_vkey(r['venue_name']), {
            'venue': r['venue_name'], 'monitors': [], 'names': {}, 'units': {},
            'ref': None})
        e['monitors'].append(r['monitor_id'])
        e['names'][r['monitor_id']] = r['monitor_name'] or f"Monitor {r['monitor_id']}"
        e['units'][r['monitor_id']] = r['unit'] or ''
        if r['is_detect_ref']:
            e['ref'] = r['monitor_id']
    return out


# ─── Planner: performances → windows ──────────────────────────────────────────

def _perf_start(d, t, tz):
    """date + 'HH:MM' → aware datetime in the venue zone, or None."""
    hm = _d['parse_time'](t)
    if not d or hm is None:
        return None
    if isinstance(d, str):
        try:
            d = date.fromisoformat(d[:10])
        except ValueError:
            return None
    return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=tz)


def _schedule_end(sched, perf_id, d, start, tz):
    """End of the production-schedule row on that performance's day that
    starts at the performance time; None when there isn't one."""
    best = None
    ds = d.isoformat() if hasattr(d, 'isoformat') else str(d or '')[:10]
    for r in sched:
        on_day = (perf_id is not None and r['perf_id'] == perf_id) or \
            (r['day_date'] or '')[:10] == ds
        if not on_day:
            continue
        st = _d['parse_time'](r['start_time'])
        en = _d['parse_time'](r['end_time'])
        if st is None or en is None or st != (start.hour, start.minute):
            continue
        end = start.replace(hour=en[0], minute=en[1])
        if end <= start:
            end += timedelta(days=1)   # runs past midnight
        if best is None or end > best:
            best = end
    return best


def plan(db, cfg, *, show_id=None, start_date=None, end_date=None, mapping=None):
    """Every performance in range (or of one show) with its window, mapped
    monitors and capture state. Shared by the job, Settings → Spot and the
    show page, so they never disagree."""
    tz = _tz(cfg)
    mapping = load_mapping(db) if mapping is None else mapping
    conds, params = ["COALESCE(s.is_test, 0) = 0",
                     "s.status IN ('active', 'archived')"], []
    if show_id is not None:
        conds.append('s.id = %s')
        params.append(show_id)
    where = ' AND '.join(conds)
    rng, rng_params = '', []
    if start_date and end_date:
        rng = ' AND d BETWEEN %s AND %s'
        rng_params = [start_date, end_date]
    rows = db.execute(f"""
        SELECT * FROM (
          SELECT s.id AS show_id, s.name, s.venue, p.id AS perf_id,
                 p.perf_date AS d, p.perf_time AS t
            FROM shows s JOIN show_performances p ON p.show_id = s.id
           WHERE {where}
          UNION ALL
          SELECT s.id, s.name, s.venue, NULL, s.show_date, s.show_time
            FROM shows s
           WHERE {where}
             AND NOT EXISTS (SELECT 1 FROM show_performances p WHERE p.show_id = s.id)
        ) x WHERE d IS NOT NULL{rng}
        ORDER BY d, t, show_id
    """, tuple(params + params + rng_params)).fetchall()
    if not rows:
        return []
    show_ids = sorted({r['show_id'] for r in rows})
    sched_by_show = {}
    if _on(cfg, 'spot_use_schedule_end'):
        for r in db.execute(
                'SELECT show_id, perf_id, day_date, start_time, end_time '
                'FROM schedule_rows WHERE show_id = ANY(%s)', (show_ids,)).fetchall():
            sched_by_show.setdefault(r['show_id'], []).append(r)
    caps = {}
    for c in db.execute("""
            SELECT id, show_id, perf_key, status, error, attempts, sched_start,
                   sched_end, monitor_ids, attachment_id, captured_by,
                   captured_at::timestamptz AS captured_at,
                   last_attempt_at::timestamptz AS last_attempt_at,
                   claimed_at::timestamptz AS claimed_at
              FROM spot_captures WHERE show_id = ANY(%s)""", (show_ids,)).fetchall():
        caps[c['perf_key']] = c
    default_len = timedelta(minutes=_int(cfg, 'spot_default_minutes'))
    grace = timedelta(minutes=_int(cfg, 'spot_grace_minutes'))
    if _on(cfg, 'spot_auto_detect'):
        # Spot's own rule: let the detection window (+2 min) pass too, so a
        # late curtain-down is in the data before we ask.
        grace += timedelta(minutes=_int(cfg, 'spot_detect_window') + 2)
    out = []
    for r in rows:
        perf_key = f"perf:{r['perf_id']}" if r['perf_id'] else f"show:{r['show_id']}"
        start = _perf_start(r['d'], r['t'], tz)
        m = mapping.get(_vkey(r['venue']))
        item = {
            'show_id': r['show_id'], 'show_name': r['name'], 'venue': r['venue'] or '',
            'perf_id': r['perf_id'], 'perf_key': perf_key,
            'date': r['d'].isoformat() if hasattr(r['d'], 'isoformat') else str(r['d']),
            'time': _hhmm(r['t']),
            'start': start, 'end': None, 'end_source': None, 'due_at': None,
            'monitors': list(m['monitors']) if m else [],
            'monitor_names': dict(m['names']) if m else {},
            'ref': m['ref'] if m else None,
            'capture': caps.get(perf_key),
            'problem': None,
        }
        if start is None:
            item['problem'] = 'No performance time (HH:MM) in the advance.'
        else:
            end = _schedule_end(sched_by_show.get(r['show_id'], []),
                                r['perf_id'], r['d'], start, tz)
            item['end_source'] = 'schedule' if end else 'default'
            item['end'] = end or start + default_len
            item['due_at'] = item['end'] + grace
        if not item['monitors'] and not item['problem']:
            item['problem'] = ('No Spot sensors linked to this venue.' if r['venue']
                               else 'Show has no venue.')
        out.append(item)
    return out


def _hhmm(t):
    hm = _d['parse_time'](t)
    return f'{hm[0]:02d}:{hm[1]:02d}' if hm else (t or '').strip()


def _window_sig(item):
    return (item['start'].isoformat(), item['end'].isoformat(),
            ','.join(str(i) for i in sorted(item['monitors'])))


def _state(item, cfg, now):
    """'unmapped' | 'notime' | 'upcoming' | 'waiting' | 'due' | 'captured'
    | 'stale' (captured, but the window or sensors changed since) | 'retry'
    (failed, waiting for backoff) | 'failed' (gave up) | 'running'."""
    if item['problem']:
        return 'notime' if item['start'] is None else 'unmapped'
    c = item['capture']
    if now < item['start']:
        return 'upcoming'
    if now < item['due_at']:
        return 'waiting'
    if not c or c['status'] == 'pending':
        return 'due'
    if c['status'] == 'running':
        cl = c['claimed_at']
        if cl and now - cl < timedelta(minutes=CLAIM_STALE_MINUTES):
            return 'running'
        return 'due'
    if c['status'] == 'captured':
        sig = (c['sched_start'], c['sched_end'], c['monitor_ids'])
        return 'captured' if sig == _window_sig(item) else 'stale'
    # error / no_data
    if (c['attempts'] or 0) >= _int(cfg, 'spot_max_attempts'):
        return 'failed'
    backoff = timedelta(minutes=min(5 * 2 ** max((c['attempts'] or 1) - 1, 0), 120))
    la = c['last_attempt_at']
    if la and now - la < backoff:
        return 'retry'
    return 'due'


# ─── Capture ──────────────────────────────────────────────────────────────────

def _slim_stats(st):
    return {k: st.get(k) for k in _STAT_KEYS if k in st} if isinstance(st, dict) else {}


def _fmt_title(item):
    t = item['start'].strftime('%a %b %d, %Y %H:%M') if item['start'] else item['date']
    return f"{item['show_name']} — {t}"[:200]


def _safe_name(s):
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', s or '').strip('_')
    return s[:60] or 'show'


def capture(item, cfg, *, by='auto', user_id=None):
    """Pull one performance's levels (+ PDF) from Spot and store them.
    Returns (ok, public_message). Never raises for Spot/network trouble —
    it's recorded on the spot_captures row instead."""
    get_db, log = _d['get_db'], _d['syslog_logger']
    sig = _window_sig(item)
    monitors = item['monitors'][:MAX_MONITORS]
    # 1) Claim the row (short transaction — no lock held across HTTP).
    db = get_db()
    try:
        db.execute("""INSERT INTO spot_captures (show_id, perf_id, perf_key, status)
                      VALUES (%s, %s, %s, 'pending') ON CONFLICT (perf_key) DO NOTHING""",
                   (item['show_id'], item['perf_id'], item['perf_key']))
        row = db.execute(f"""
            UPDATE spot_captures SET status='running', claimed_at=NOW(), show_id=%s
             WHERE perf_key=%s AND (status <> 'running'
                   OR claimed_at IS NULL
                   OR claimed_at < NOW() - INTERVAL '{CLAIM_STALE_MINUTES} minutes')
            RETURNING id, attachment_id, status""",
            (item['show_id'], item['perf_key'])).fetchone()
        db.commit()
    finally:
        db.close()
    if not row:
        return False, 'Already being pulled — try again in a moment.'
    cap_id, old_aid = row['id'], row['attachment_id']

    # 2) Ask Spot.
    params = {'monitors': ','.join(str(i) for i in monitors),
              'start': item['start'].isoformat(), 'end': item['end'].isoformat()}
    if _on(cfg, 'spot_auto_detect'):
        params.update(detect='1', window_minutes=str(_int(cfg, 'spot_detect_window')),
                      min_step=str(_min_step(cfg)))
        if item['ref']:
            params['detect_monitor'] = str(item['ref'])
    pdf = None
    try:
        stats = _call(cfg, '/stats', params)
        if not isinstance(stats, dict):
            raise SpotError('Unexpected /stats response from Spot.')
        mons = [{'id': m.get('id'), 'name': m.get('name'), 'unit': m.get('unit'),
                 'location': m.get('location'), 'stats': _slim_stats(m.get('stats'))}
                for m in (stats.get('monitors') or []) if isinstance(m, dict)]
        has_data = any((m['stats'].get('count') or 0) > 0 for m in mons)
        if has_data and _on(cfg, 'spot_attach_pdf'):
            pdf = _call(cfg, '/report.pdf', dict(params, title=_fmt_title(item)), want='pdf')
    except SpotError as e:
        _fail(cap_id, item, str(e), by)
        return False, e.public

    # 3) Store.
    status = 'captured' if has_data else 'no_data'
    detection = stats.get('detection') if isinstance(stats.get('detection'), dict) else None
    db = get_db()
    try:
        new_aid = old_aid
        if pdf:
            fname = (f"spot_levels_{_safe_name(item['show_name'])}_"
                     f"{item['start'].strftime('%Y%m%d_%H%M')}.pdf")
            desc = f"Spot sound-level report — {item['date']} {item['time']}".strip()
            new_aid = _d['store_show_attachment'](db, item['show_id'], fname,
                                                  'application/pdf', pdf, desc, user_id)
            _d['log_audit'](db, 'FILE_UPLOAD', 'attachment', new_aid,
                            show_id=item['show_id'], detail=f'{fname} (Spot)')
            # A re-pull replaces the earlier report: archive it (never destroy).
            if old_aid and old_aid != new_aid:
                old = _d['archive_show_attachment'](db, old_aid, user_id)
                if old is not None and old['show_id'] == item['show_id']:
                    _d['log_audit'](db, 'FILE_ARCHIVE', 'attachment', old_aid,
                                    show_id=item['show_id'], detail=f"{old['filename']} (Spot re-pull)")
        db.execute("""
            UPDATE spot_captures SET status=%s, error=NULL,
                   attempts=CASE WHEN %s = 'captured' THEN 0 ELSE attempts + 1 END,
                   last_attempt_at=NOW(), claimed_at=NULL,
                   sched_start=%s, sched_end=%s, monitor_ids=%s, end_source=%s,
                   report_start=%s, report_end=%s, stats_json=%s, detection_json=%s,
                   attachment_id=%s, captured_at=NOW(), captured_by=%s
             WHERE id=%s""",
            (status, status, sig[0], sig[1], sig[2], item['end_source'],
             str(stats.get('start') or '')[:64], str(stats.get('end') or '')[:64],
             json.dumps(mons, default=str),
             json.dumps(detection, default=str) if detection else None,
             new_aid, by[:80], cap_id))
        _d['log_audit'](db, 'SPOT_CAPTURE', 'spot_capture', cap_id,
                        show_id=item['show_id'],
                        detail=f"{item['perf_key']} status={status} by={by}")
        db.commit()
    except Exception as e:
        db.close()
        _fail(cap_id, item, f'store failed: {e}', by)
        raise
    db.close()
    log.info(f"SPOT_CAPTURE show_id={item['show_id']} perf={item['perf_key']} "
             f"status={status} monitors={len(mons)} pdf={'yes' if pdf else 'no'} by={by}")
    if status == 'no_data':
        return False, 'Spot has no readings for this performance yet.'
    return True, 'Sound levels pulled from Spot.'


def _fail(cap_id, item, detail, by):
    _d['syslog_logger'].warning(
        f"SPOT_CAPTURE_FAILED show_id={item['show_id']} perf={item['perf_key']} "
        f"by={by} error={detail[:300]}")
    try:
        db = _d['get_db']()
        try:
            db.execute("""UPDATE spot_captures SET status='error', error=%s,
                          attempts=attempts+1, last_attempt_at=NOW(), claimed_at=NULL
                          WHERE id=%s""", (detail[:500], cap_id))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass   # the claim goes stale in CLAIM_STALE_MINUTES and is retried


# ─── Scheduled job ────────────────────────────────────────────────────────────

def run_spot_capture(force=False):
    """APScheduler job (every 5 min). LEADER-GATED: only the cluster primary
    pulls, so N instances × 4 workers never fetch the same performance. Skips
    the run (logged) when PostgreSQL is down. `force` = an admin's Run Now
    (any instance; the per-row claim still prevents doubles). Returns a
    summary dict."""
    app, log = _d['app'], _d['syslog_logger']
    if _d.get('test_mode'):
        # A test server may share production's database: never bulk-pull
        # (not even an admin's Run Now). Single "Pull now" clicks still work.
        return {'skipped': 'test instance'}
    if not force and not _d['am_i_leader']():
        return {'skipped': 'not leader'}
    try:
        db = _d['get_db']()
    except _d['db_adapter'].DatabaseUnavailable as e:
        app.logger.error(f'Spot capture: PostgreSQL unreachable — skipping run: {e}')
        return {'skipped': 'database unavailable'}
    try:
        cfg = load_settings(db)
        if not _enabled():
            return {'skipped': 'module disabled'}
        if not is_configured(cfg):
            return {'skipped': 'not configured'}
        if not force and not _on(cfg, 'spot_auto_pull'):
            return {'skipped': 'auto pull off'}
        tz = _tz(cfg)
        today = datetime.now(tz).date()
        # The day before the lookback too: an overnight show starts then.
        items = plan(db, cfg, start_date=today - timedelta(days=_int(cfg, 'spot_lookback_days') + 1),
                     end_date=today)
    finally:
        db.close()
    now = datetime.now(tz)
    due = [i for i in items if _state(i, cfg, now) in ('due', 'stale')]
    done = failed = 0
    for item in due[:JOB_BATCH]:
        try:
            ok, _msg = capture(item, cfg, by='auto')
        except Exception as e:   # never let one show stop the rest
            app.logger.exception(f'Spot capture crashed for {item["perf_key"]}: {e}')
            ok = False
        done += ok
        failed += not ok
    if due:
        log.info(f'SPOT_JOB due={len(due)} pulled={done} failed={failed} force={int(force)}')
    return {'due': len(due), 'pulled': done, 'failed': failed,
            'deferred': max(0, len(due) - JOB_BATCH)}


# ─── Serialisation ────────────────────────────────────────────────────────────

def _iso(v):
    return v.isoformat() if hasattr(v, 'isoformat') else v


def _item_json(item, cfg, now, *, admin):
    c = item['capture']
    st = _state(item, cfg, now)
    out = {
        'show_id': item['show_id'], 'show_name': item['show_name'],
        'venue': item['venue'], 'perf_key': item['perf_key'],
        'date': item['date'], 'time': item['time'],
        'start': _iso(item['start']), 'end': _iso(item['end']),
        'end_source': item['end_source'], 'due_at': _iso(item['due_at']),
        'monitors': [{'id': i, 'name': item['monitor_names'].get(i)}
                     for i in item['monitors']],
        'state': st, 'problem': item['problem'],
    }
    if c:
        out['capture'] = {
            'status': c['status'], 'attempts': c['attempts'],
            'captured_at': _iso(c['captured_at']), 'captured_by': c['captured_by'],
            'attachment_id': c['attachment_id'],
            'last_attempt_at': _iso(c['last_attempt_at']),
            # Spot's error text can name internal hosts — admins only.
            'error': (c['error'] if admin else
                      ('Could not get data from Spot.' if c['error'] else None)),
        }
    return out


def captures_for_show(db, show_id):
    """Captured sound levels for the Post-Show Notes PDF: [{label, report
    range, detection, monitors}] — [] when nothing's been captured (so the PDF
    is byte-identical to before for every show without Spot data)."""
    rows = db.execute("""
        SELECT c.stats_json, c.detection_json, c.report_start, c.report_end,
               c.sched_start
          FROM spot_captures c
          LEFT JOIN show_performances p ON p.id = c.perf_id
         WHERE c.show_id=%s AND c.status='captured' AND c.stats_json IS NOT NULL
           AND (c.perf_id IS NULL OR p.id IS NOT NULL)
         ORDER BY c.sched_start, c.id""", (show_id,)).fetchall()
    out = []
    for r in rows:
        try:
            mons = json.loads(r['stats_json'] or '[]')
        except ValueError:
            mons = []
        try:
            det = json.loads(r['detection_json']) if r['detection_json'] else None
        except ValueError:
            det = None
        out.append({
            'date': (r['sched_start'] or '')[:10], 'time': _hm(r['sched_start']),
            'report_start': _hm(r['report_start']), 'report_end': _hm(r['report_end']),
            'detection': det, 'monitors': mons,
        })
    return out


def _hm(iso):
    try:
        return datetime.fromisoformat(iso).strftime('%H:%M')
    except (TypeError, ValueError):
        return ''


# ─── Routes ───────────────────────────────────────────────────────────────────

def _module_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _enabled():
            abort(404)
        return f(*args, **kwargs)
    return decorated


def _is_admin():
    u = _d['get_current_user']()
    return bool(u and u['role'] == 'admin')


def _show_levels_view(show_id):
    if not _d['can_access_show'](session['user_id'], show_id):
        abort(403)
    db = _d['get_db']()
    try:
        if not db.execute('SELECT 1 FROM shows WHERE id=%s', (show_id,)).fetchone():
            abort(404)
        cfg = load_settings(db)
        items = plan(db, cfg, show_id=show_id)
        stats = {r['perf_key']: r for r in db.execute(
            'SELECT perf_key, stats_json, detection_json, report_start, report_end '
            'FROM spot_captures WHERE show_id=%s', (show_id,)).fetchall()}
    finally:
        db.close()
    now = datetime.now(_tz(cfg))
    admin = _is_admin()
    perfs = []
    for item in items:
        j = _item_json(item, cfg, now, admin=admin)
        s = stats.get(item['perf_key'])
        if s and s['stats_json']:
            try:
                j['levels'] = json.loads(s['stats_json'])
                j['detection'] = json.loads(s['detection_json']) if s['detection_json'] else None
            except ValueError:
                pass
            j['report_start'] = s['report_start']
            j['report_end'] = s['report_end']
        perfs.append(j)
    return jsonify({'configured': is_configured(cfg), 'performances': perfs,
                    'can_pull': not (session.get('is_readonly') or session.get('is_restricted'))})


def _show_pull_view(show_id):
    if not _d['can_access_show'](session['user_id'], show_id):
        abort(403)
    if session.get('is_readonly') or session.get('is_restricted'):
        return jsonify({'success': False, 'error': 'Read-only access.'}), 403
    perf_key = str((request.get_json(silent=True) or {}).get('perf_key') or '')
    db = _d['get_db']()
    try:
        cfg = load_settings(db)
        items = [i for i in plan(db, cfg, show_id=show_id) if i['perf_key'] == perf_key]
    finally:
        db.close()
    if not is_configured(cfg):
        return jsonify({'success': False, 'error': 'Spot is not configured.'}), 409
    if not items:
        return jsonify({'success': False, 'error': 'Performance not found.'}), 404
    item = items[0]
    if item['problem']:
        return jsonify({'success': False, 'error': item['problem']}), 409
    now = datetime.now(_tz(cfg))
    if now < item['end']:
        return jsonify({'success': False,
                        'error': "This performance hasn't ended yet."}), 409
    c = item['capture']
    if c and c['captured_at'] and (now - c['captured_at']).total_seconds() < MANUAL_PULL_COOLDOWN:
        return jsonify({'success': False, 'error': 'Just pulled — wait a minute.'}), 429
    ok, msg = capture(item, cfg, by=session.get('username') or 'user',
                      user_id=session.get('user_id'))
    return jsonify({'success': ok, 'message': msg, 'error': None if ok else msg})


def _settings_page_view():
    db = _d['get_db']()
    try:
        cfg = load_settings(db)
        venues = _d['get_distinct_venues'](db)
        mapping = load_mapping(db)
    finally:
        db.close()
    # One box per venue however it's spelled on shows ('Walt Disney Theater'
    # vs 'walt disney  theater' match the same sensors): the mapped spelling
    # wins, else the first seen.
    by_key = {_vkey(e['venue']): e['venue'] for e in mapping.values()}
    for v in venues:
        by_key.setdefault(_vkey(v), v)
    venues = sorted((v for k, v in by_key.items() if k), key=str.lower)
    safe_cfg = {k: v for k, v in cfg.items() if k != 'spot_api_key'}
    return render_template('spot.html', user=_d['get_current_user'](),
                           cfg=safe_cfg, has_key=bool(cfg.get('spot_api_key')),
                           venues=venues, module_on=_enabled())


def _settings_save_view():
    data = request.get_json(silent=True) or {}
    updates = {}
    try:
        if 'spot_base_url' in data:
            updates['spot_base_url'] = normalize_base_url(data.get('spot_base_url'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    key = str(data.get('spot_api_key') or '').strip()
    if key:
        if len(key) > 200 or not re.fullmatch(r'[\x21-\x7e]+', key):
            return jsonify({'success': False, 'error': 'API key looks malformed.'}), 400
        updates['spot_api_key'] = key
    if data.get('clear_api_key'):
        updates['spot_api_key'] = ''
    if 'spot_timezone' in data:
        tzname = str(data.get('spot_timezone') or '').strip()
        try:
            ZoneInfo(tzname)
        except (ZoneInfoNotFoundError, ValueError):
            return jsonify({'success': False, 'error': 'Unknown timezone.'}), 400
        updates['spot_timezone'] = tzname
    for k in _BOOL_KEYS:
        if k in data:
            updates[k] = '1' if data.get(k) in (True, '1', 1, 'true', 'on') else '0'
    for k, (lo, hi) in _INT_LIMITS.items():
        if k in data:
            try:
                v = int(data.get(k))
            except (TypeError, ValueError):
                return jsonify({'success': False, 'error': f'{k} must be a whole number.'}), 400
            if not lo <= v <= hi:
                return jsonify({'success': False, 'error': f'{k} must be {lo}–{hi}.'}), 400
            updates[k] = str(v)
    if 'spot_detect_min_step' in data:
        try:
            v = float(data.get('spot_detect_min_step'))
        except (TypeError, ValueError):
            v = -1
        if not 0.5 <= v <= 60:
            return jsonify({'success': False, 'error': 'Minimum step must be 0.5–60 dB.'}), 400
        updates['spot_detect_min_step'] = f'{v:g}'
    db = _d['get_db']()
    try:
        for k, v in updates.items():
            db.execute('INSERT INTO app_settings (key, value) VALUES (%s, %s) '
                       'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value', (k, v))
        changed = ','.join(sorted(updates))
        _d['log_audit'](db, 'SETTINGS_CHANGE', 'settings', None,
                        detail=f'spot: {changed}')
        db.commit()
    finally:
        db.close()
    _d['syslog_logger'].info(f"SPOT_SETTINGS_SAVE keys={changed} by={session.get('username')}")
    return jsonify({'success': True})


def _settings_test_view():
    """Test the connection — with the form's (unsaved) URL/key when given.
    The SAVED key is only ever sent to the SAVED URL."""
    data = request.get_json(silent=True) or {}
    db = _d['get_db']()
    try:
        cfg = load_settings(db)
    finally:
        db.close()
    try:
        url = normalize_base_url(data.get('spot_base_url')) if data.get('spot_base_url') \
            else cfg['spot_base_url']
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)})
    key = str(data.get('spot_api_key') or '').strip()
    if not key:
        if url != cfg['spot_base_url']:
            return jsonify({'success': False, 'error':
                            'Type the API key to test a URL that is not saved yet.'})
        key = cfg['spot_api_key']
    try:
        info = spot_call(url, key, '/')
    except SpotError as e:
        return jsonify({'success': False, 'error': str(e)})
    keep = ('service', 'api', 'version', 'key', 'timezone', 'now')
    return jsonify({'success': True, 'info': {k: info.get(k) for k in keep}})


def _settings_monitors_view():
    db = _d['get_db']()
    try:
        cfg = load_settings(db)
    finally:
        db.close()
    try:
        res = _call(cfg, '/monitors')
    except SpotError as e:
        return jsonify({'success': False, 'error': str(e)})
    mons = []
    for m in res.get('monitors') or []:
        if not isinstance(m, dict) or not isinstance(m.get('id'), int):
            continue
        latest = m.get('latest') if isinstance(m.get('latest'), dict) else None
        mons.append({'id': m['id'], 'name': str(m.get('name') or '')[:200],
                     'unit': str(m.get('unit') or '')[:40],
                     'location': str(m.get('location') or '')[:200],
                     'enabled': bool(m.get('enabled', True)),
                     'latest': latest and {'ts': latest.get('ts'), 'value': latest.get('value')}})
    return jsonify({'success': True, 'monitors': mons})


def _mapping_get_view():
    db = _d['get_db']()
    try:
        mapping = load_mapping(db)
    finally:
        db.close()
    return jsonify({'mapping': [
        {'venue': e['venue'], 'ref': e['ref'],
         'monitors': [{'id': i, 'name': e['names'][i], 'unit': e['units'][i]}
                      for i in e['monitors']]}
        for e in mapping.values()]})


def _mapping_save_view():
    """Replace one venue's sensors: {venue, monitors:[{id,name,unit}], ref}."""
    data = request.get_json(silent=True) or {}
    venue = ' '.join(str(data.get('venue') or '').split())[:200]
    if not venue:
        return jsonify({'success': False, 'error': 'Venue required.'}), 400
    mons, seen = [], set()
    for m in data.get('monitors') or []:
        try:
            mid = int(m.get('id'))
        except (TypeError, ValueError, AttributeError):
            return jsonify({'success': False, 'error': 'Bad monitor id.'}), 400
        if mid <= 0 or mid in seen:
            continue
        seen.add(mid)
        mons.append((mid, str(m.get('name') or '')[:200], str(m.get('unit') or '')[:40]))
    if len(mons) > MAX_MONITORS:
        return jsonify({'success': False, 'error': f'At most {MAX_MONITORS} sensors per venue.'}), 400
    try:
        ref = int(data['ref']) if data.get('ref') not in (None, '') else None
    except (TypeError, ValueError):
        ref = None
    if ref not in seen:
        ref = None
    db = _d['get_db']()
    try:
        # Remove every spelling of this venue (case/space-insensitive), then insert.
        for r in db.execute('SELECT DISTINCT venue_name FROM spot_venue_monitors').fetchall():
            if _vkey(r['venue_name']) == _vkey(venue):
                db.execute('DELETE FROM spot_venue_monitors WHERE venue_name=%s',
                           (r['venue_name'],))
        for mid, name, unit in mons:
            db.execute("""INSERT INTO spot_venue_monitors
                          (venue_name, monitor_id, monitor_name, unit, is_detect_ref)
                          VALUES (%s, %s, %s, %s, %s)""",
                       (venue, mid, name, unit, 1 if mid == ref else 0))
        _d['log_audit'](db, 'SETTINGS_CHANGE', 'settings', None,
                        detail=f'spot mapping: {venue} → {sorted(seen)} ref={ref}')
        db.commit()
    finally:
        db.close()
    _d['syslog_logger'].info(f"SPOT_MAPPING_SAVE venue={venue!r} monitors={sorted(seen)} "
                             f"ref={ref} by={session.get('username')}")
    return jsonify({'success': True})


def _queue_view():
    db = _d['get_db']()
    try:
        cfg = load_settings(db)
        tz = _tz(cfg)
        today = datetime.now(tz).date()
        items = plan(db, cfg, start_date=today - timedelta(days=_int(cfg, 'spot_lookback_days') + 1),
                     end_date=today + timedelta(days=QUEUE_AHEAD_DAYS))
        recent = db.execute("""
            SELECT c.perf_key, c.show_id, s.name AS show_name, c.status, c.error,
                   c.attempts, c.captured_by, c.attachment_id,
                   c.captured_at::timestamptz AS captured_at,
                   c.last_attempt_at::timestamptz AS last_attempt_at
              FROM spot_captures c JOIN shows s ON s.id = c.show_id
             ORDER BY COALESCE(c.last_attempt_at, c.captured_at) DESC NULLS LAST
             LIMIT 50""").fetchall()
    finally:
        db.close()
    now = datetime.now(tz)
    return jsonify({
        'leader': bool(_d['am_i_leader']()),
        'queue': [_item_json(i, cfg, now, admin=True) for i in items],
        'recent': [{k: _iso(r[k]) for k in r.keys()} for r in recent],
    })


def _run_now_view():
    res = run_spot_capture(force=True)
    _d['syslog_logger'].info(f"SPOT_JOB manual run by={session.get('username')} result={res}")
    return jsonify({'success': True, 'result': res})


def _admin_pull_view():
    """Settings → Spot "Pull now" for any show in the queue (admin)."""
    data = request.get_json(silent=True) or {}
    try:
        show_id = int(data.get('show_id'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Bad show id.'}), 400
    perf_key = str(data.get('perf_key') or '')
    db = _d['get_db']()
    try:
        cfg = load_settings(db)
        items = [i for i in plan(db, cfg, show_id=show_id) if i['perf_key'] == perf_key]
    finally:
        db.close()
    if not items:
        return jsonify({'success': False, 'error': 'Performance not found.'}), 404
    item = items[0]
    if item['problem']:
        return jsonify({'success': False, 'error': item['problem']}), 409
    if datetime.now(_tz(cfg)) < item['end']:
        return jsonify({'success': False, 'error': "This performance hasn't ended yet."}), 409
    ok, msg = capture(item, cfg, by=session.get('username') or 'admin',
                      user_id=session.get('user_id'))
    if not ok:
        db = _d['get_db']()
        try:
            r = db.execute('SELECT error FROM spot_captures WHERE perf_key=%s',
                           (perf_key,)).fetchone()
        finally:
            db.close()
        msg = (r and r['error']) or msg
    return jsonify({'success': ok, 'message': msg, 'error': None if ok else msg})


def register(app, **deps):
    """Wire the module in. deps: get_db, get_current_user, login_required,
    admin_required, can_access_show, module_enabled, am_i_leader, log_audit,
    syslog_logger, db_adapter, parse_time, get_distinct_venues,
    store_show_attachment, archive_show_attachment, test_mode."""
    _d.update(deps, app=app)
    login, admin = deps['login_required'], deps['admin_required']

    def show_route(view):
        return login(_module_required(view))

    app.add_url_rule('/shows/<int:show_id>/spot/levels', 'spot_show_levels',
                     show_route(_show_levels_view))
    app.add_url_rule('/shows/<int:show_id>/spot/pull', 'spot_show_pull',
                     show_route(_show_pull_view), methods=['POST'])
    # Settings stay reachable while the module is off, so it can be set up first.
    app.add_url_rule('/settings/spot', 'spot_settings_page', admin(_settings_page_view))
    app.add_url_rule('/settings/spot', 'spot_settings_save', admin(_settings_save_view),
                     methods=['POST'])
    app.add_url_rule('/settings/spot/test', 'spot_settings_test', admin(_settings_test_view),
                     methods=['POST'])
    app.add_url_rule('/settings/spot/monitors', 'spot_settings_monitors',
                     admin(_settings_monitors_view))
    app.add_url_rule('/settings/spot/mapping', 'spot_mapping_get', admin(_mapping_get_view))
    app.add_url_rule('/settings/spot/mapping', 'spot_mapping_save', admin(_mapping_save_view),
                     methods=['POST'])
    app.add_url_rule('/settings/spot/queue', 'spot_queue', admin(_queue_view))
    app.add_url_rule('/settings/spot/run', 'spot_run_now', admin(_run_now_view),
                     methods=['POST'])
    app.add_url_rule('/settings/spot/pull', 'spot_admin_pull', admin(_admin_pull_view),
                     methods=['POST'])
