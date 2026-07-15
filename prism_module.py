# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
Prism FM integration module — SANDBOXED.

3·2·1→Theater can pull the building's schedule (shows & events) from Prism,
the venue's primary scheduling system. This module is deliberately isolated
from the rest of the app:

  * It only WRITES to its own staging tables (`prism_events`, `prism_sync_log`,
    `prism_venues`) and its own `prism_*` keys in `app_settings`.
  * It touches main-app data (`shows`, `show_performances`, `advance_data`)
    only through import_staged_events() — manual import on /prism, or every
    pending NEW event when the opt-in prism_auto_import_enabled setting is
    on — plus ONE sanctioned sync write-through: shows.prism_status (the
    Hold/Confirmed tag on the homepage) is kept current for linked shows.
  * app.py's only knowledge of this module is `register(app, deps)` plus one
    scheduler job — delete this file + the /prism nav link and the app runs
    unchanged.

ARCHITECTURE
------------
Prism ships an official Node.js SDK (GraphQL under the hood — no public REST
surface worth reimplementing), so we reuse the bridge pattern validated in
the PrismSDKTest project: short-lived `node` subprocesses in prism_bridge/
call the SDK and print JSON to stdout. See prism_bridge/README.md for the
one-time SDK install.

SYNC MODEL
----------
A sync (manual button or daily scheduled job) fetches every event from today
to `prism_lookahead_days` (default 365) ahead and upserts into
`prism_events`, keyed on Prism's own event id — so re-syncing never
duplicates. Rows track an import_state:

    new      – found in Prism, not yet acted on
    imported – an admin created a 321T show from it (linked via
               imported_show_id); re-syncs keep updating the staged copy and
               flag it if Prism changed it after import
    ignored  – an admin dismissed it (kept so it doesn't bounce back as new)

Every sync writes a `prism_sync_log` row with counts and a capped debug log —
the /prism page surfaces these for troubleshooting.

BACKGROUND-JOB SAFETY (see CLAUDE.md)
-------------------------------------
The scheduled job is leader-gated and — like the scheduled-email job —
refuses to act when the configured backend is PostgreSQL but the active
connection silently fell back to the SQLite bootstrap (stale settings).
"""

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from datetime import date, datetime, timedelta

from flask import jsonify, redirect, render_template, request, session, url_for

# Dependencies injected by register() — keeps this module import-safe and
# avoids a circular import with app.py.
_d = {}

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BRIDGE_DIR = os.path.join(_BASE_DIR, 'prism_bridge')

# One sync at a time per worker process; cross-worker overlap is handled by
# the 'running' row check in run_prism_sync().
_sync_lock = threading.Lock()

# Prism EventStatus values (from the SDK)
EVENT_STATUSES = {
    '0': 'Hold',
    '2': 'Confirmed',
    '3': 'In Settlement',
    '4': 'Settled',
}

SETTING_DEFAULTS = {
    'prism_enabled':            '0',
    'prism_token':              '',
    'prism_auto_sync_enabled':  '0',
    # When '1', each sync also auto-imports future-dated NEW events (hidden
    # venues excluded — they arrive pre-ignored) as 321T shows, exactly as if
    # an admin had selected everything and clicked Import.
    'prism_auto_import_enabled': '0',
    'prism_sync_hour':          '5',
    'prism_lookahead_days':     '365',
    'prism_event_statuses':     '2',     # CSV of EVENT_STATUSES keys
    'prism_node_timeout':       '120',   # seconds per bridge call
    'prism_bridge_dir':         '',      # blank = <app>/prism_bridge
    # JSON array of stage/venue names hidden from the events list. Prism
    # reports non-venues like "Holidays" as venues — hiding one filters its
    # events from view AND newly synced events from it arrive pre-ignored.
    'prism_hidden_stages':      '[]',
}

NO_VENUE_TOKEN = '(no venue)'


def _hidden_set(settings):
    """Hidden stage/venue names as a lowercase set."""
    try:
        names = json.loads(settings.get('prism_hidden_stages') or '[]')
        return {str(n).strip().lower() for n in names if str(n).strip()}
    except (TypeError, json.JSONDecodeError):
        return set()


def _event_tokens(stage_names, venue_name):
    """The venue-filter tokens an event row carries: its stage names, falling
    back to the venue name (Prism pseudo-venues like "Holidays" have no
    stages), falling back to a catch-all so blank rows stay filterable."""
    toks = [s.strip() for s in (stage_names or '').split(',') if s.strip()]
    if not toks and (venue_name or '').strip():
        toks = [venue_name.strip()]
    return toks or [NO_VENUE_TOKEN]

# Cap stored debug logs so a chatty sync can't bloat the table.
_DEBUG_LOG_MAX_CHARS = 60_000


# ─── Settings helpers ─────────────────────────────────────────────────────────

def get_prism_settings(db):
    """Return all prism_* settings as a dict, with defaults filled in.
    The LIKE pattern is bound as a parameter — a literal % in SQL would be
    eaten by psycopg2's placeholder interpolation on PostgreSQL."""
    rows = db.execute(
        'SELECT key, value FROM app_settings WHERE key LIKE ?', ('prism_%',)
    ).fetchall()
    found = {r['key']: r['value'] for r in rows}
    return {k: (found.get(k) if found.get(k) not in (None, '') else v)
            for k, v in SETTING_DEFAULTS.items()} | {
        # token may legitimately be '' — don't let the default-filler mask that
        'prism_token': found.get('prism_token', ''),
    }


def _save_setting(db, key, value):
    db.execute('INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)',
               (key, str(value)))


# ─── Small coercion helpers ───────────────────────────────────────────────────

def _norm_date(v):
    """Coerce a Prism date value (ISO string / datetime / date) to 'YYYY-MM-DD' or ''."""
    if v in (None, ''):
        return ''
    if isinstance(v, (date, datetime)):
        return v.strftime('%Y-%m-%d')
    s = str(v).strip()
    m = re.match(r'(\d{4}-\d{2}-\d{2})', s)
    return m.group(1) if m else ''


def _norm_time(v):
    """Coerce a Prism time value to 'HH:MM' or ''. Accepts '19:30', '19:30:00',
    full ISO datetimes, and datetime objects."""
    if v in (None, ''):
        return ''
    if isinstance(v, datetime):
        return v.strftime('%H:%M')
    s = str(v).strip()
    m = re.search(r'[T ](\d{2}:\d{2})', s)      # ISO datetime
    if m:
        return m.group(1)
    m = re.match(r'^(\d{1,2}):(\d{2})', s)      # bare time
    if m:
        return f'{int(m.group(1)):02d}:{m.group(2)}'
    return ''


def _norm_str(v):
    return '' if v is None else str(v)


# ─── Node bridge ──────────────────────────────────────────────────────────────

class PrismBridgeError(Exception):
    """Raised when a prism_bridge Node script fails. .detail carries debug info."""

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail or {}


def _bridge_dir(settings):
    return settings.get('prism_bridge_dir') or DEFAULT_BRIDGE_DIR


def _bridge_call(script_name, args=None, *, settings, timeout=None, debug_sink=None):
    """
    Run a prism_bridge Node script and return parsed JSON from stdout.
    Mirrors the subprocess pattern validated in PrismSDKTest.

    `debug_sink` (callable taking a string) receives a trace of the exchange —
    request args, timing, response size, and the SDK's stderr chatter — so
    sync logs and the Raw API Fetch tool can show exactly what went over the
    wire. The token itself is never written to the sink.
    """
    def note(msg):
        if debug_sink is not None:
            debug_sink(msg)

    bridge_dir = _bridge_dir(settings)
    script_path = os.path.join(bridge_dir, script_name)
    if not os.path.isfile(script_path):
        raise PrismBridgeError(f'Bridge script not found: {script_path}')

    try:
        node_timeout = timeout or int(settings.get('prism_node_timeout') or 120)
    except (TypeError, ValueError):
        node_timeout = 120

    env = {**os.environ, 'PRISM_TOKEN': settings.get('prism_token', '')}
    cmd = ['node', script_name, json.dumps(args or {})]
    note(f'→ {script_name} args={json.dumps(args or {})[:1500]}')

    t0 = time.monotonic()
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=node_timeout, env=env, cwd=bridge_dir,
        )
    except subprocess.TimeoutExpired:
        note(f'✗ {script_name} timed out after {node_timeout}s')
        raise PrismBridgeError(
            f'{script_name} timed out after {node_timeout}s')
    except FileNotFoundError:
        note('✗ node executable not found')
        raise PrismBridgeError(
            'node executable not found — install Node.js ≥ 18 on this server '
            '(see prism_bridge/README.md)')

    elapsed = time.monotonic() - t0
    stderr_txt = (result.stderr or '').strip()

    if result.returncode != 0:
        note(f'✗ {script_name} exit={result.returncode} after {elapsed:.1f}s')
        if stderr_txt:
            note(f'stderr: {stderr_txt[-2000:]}')
        try:
            err = json.loads(stderr_txt.splitlines()[-1])
            raise PrismBridgeError(err.get('error') or stderr_txt,
                                   detail={'stderr': stderr_txt[-4000:]})
        except (json.JSONDecodeError, IndexError):
            raise PrismBridgeError(
                stderr_txt[-1000:] or f'{script_name} exited with code {result.returncode}',
                detail={'stderr': stderr_txt[-4000:]})

    stdout = (result.stdout or '').strip()
    note(f'← {script_name} {len(stdout)} bytes in {elapsed:.1f}s')
    if stderr_txt:
        note(f'SDK chatter (stderr): {stderr_txt[-2000:]}')
    if not stdout:
        raise PrismBridgeError(f'{script_name} produced no output')
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        note(f'✗ stdout is not JSON: {stdout[:300]}')
        raise PrismBridgeError(f'Could not parse {script_name} output as JSON: {exc}',
                               detail={'stdout': stdout[:2000]})


def environment_check(settings, *, db=None):
    """
    Debug aid for the /prism page: report every prerequisite of the bridge
    pipeline so a misconfigured server is diagnosable at a glance.
    """
    checks = []

    def add(name, ok, detail):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})

    bridge_dir = _bridge_dir(settings)
    add('Bridge scripts', os.path.isfile(os.path.join(bridge_dir, 'get_events.js')),
        bridge_dir)

    node_ver = None
    try:
        r = subprocess.run(['node', '--version'], capture_output=True,
                           text=True, timeout=10)
        node_ver = (r.stdout or r.stderr or '').strip()
        add('Node.js', r.returncode == 0, node_ver or 'no output')
    except FileNotFoundError:
        add('Node.js', False, 'node not found on PATH — install Node.js ≥ 18')
    except Exception as e:
        add('Node.js', False, str(e))

    if node_ver:
        try:
            sdk = _bridge_call('check_sdk.js', settings=settings, timeout=30)
            add('Prism SDK', bool(sdk.get('ok')),
                sdk.get('detail') or f"v{sdk.get('version', '?')} @ {sdk.get('path', '?')}")
        except PrismBridgeError as e:
            add('Prism SDK', False, str(e))
    else:
        add('Prism SDK', False, 'skipped — Node.js unavailable')

    add('API token', bool(settings.get('prism_token')),
        'configured' if settings.get('prism_token')
        else 'not set — generate one in Prism → Settings → Developer '
             '(scopes: read-events, read-venues)')

    configured = _d['db_adapter'].read_db_settings(_d['DATABASE']).get('db_type', 'sqlite')
    active = getattr(db, 'db_type', None) if db is not None else None
    db_ok = (configured != 'postgres') or (active == 'postgres') or (db is None)
    add('Database', db_ok,
        f'configured={configured}' + (f', active={active}' if active else ''))

    return checks


# ─── Sync engine ──────────────────────────────────────────────────────────────

def _refresh_venues(db, settings, dbg):
    """
    Pull the venue/stage catalog from Prism into prism_venues (upsert by
    Prism's venue id). Best-effort: a failure here is logged in the sync
    debug log but never aborts the events sync.
    """
    try:
        venues = _bridge_call('get_venues.js', {'includeInactive': True},
                              settings=settings, timeout=60, debug_sink=dbg)
        if not isinstance(venues, list):
            dbg(f'venue refresh: unexpected response shape '
                f'({type(venues).__name__}) — skipped')
            return 0
        n = 0
        for v in venues:
            vid = v.get('id')
            if vid is None:
                continue
            name = _norm_str(v.get('name'))[:200]
            city = _norm_str(v.get('city') or v.get('venueCity'))[:100]
            state = _norm_str(v.get('state') or v.get('venueState'))[:50]
            capacity = v.get('capacity') if isinstance(v.get('capacity'), int) else None
            inactive = bool(v.get('isInactive')) or v.get('active') is False
            stages = [{'name': _norm_str(s.get('name'))[:200],
                       'capacity': s.get('capacity')}
                      for s in (v.get('stages') or []) if s.get('name')]
            row = db.execute('SELECT id FROM prism_venues WHERE prism_venue_id=?',
                             (vid,)).fetchone()
            if row is None:
                db.execute("""
                    INSERT INTO prism_venues
                      (prism_venue_id, name, city, state, capacity, is_active,
                       stages_json, raw_json, last_synced_at)
                    VALUES (?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)
                """, (vid, name, city, state, capacity, 0 if inactive else 1,
                      json.dumps(stages), json.dumps(v)))
            else:
                db.execute("""
                    UPDATE prism_venues SET name=?, city=?, state=?, capacity=?,
                       is_active=?, stages_json=?, raw_json=?,
                       last_synced_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (name, city, state, capacity, 0 if inactive else 1,
                      json.dumps(stages), json.dumps(v), row['id']))
            n += 1
        dbg(f'venue refresh: {n} venue(s) catalogued')
        return n
    except PrismBridgeError as e:
        dbg(f'venue refresh FAILED (events sync continues): {e}')
        _log(f'Prism venue refresh failed: {e}', error=True)
        return 0

def _content_hash(ev):
    """Hash the fields we care about so re-syncs can tell changed from
    unchanged. Deliberately excludes Prism's last-updated stamp — we only
    want to flag changes that alter what an operator would see/import."""
    basis = {
        'name': _norm_str(ev.get('name')),
        'status': _norm_str(ev.get('event_status')),
        'first_date': _norm_date(ev.get('first_date')),
        'last_date': _norm_date(ev.get('last_date')),
        'venue': _norm_str(ev.get('venue_name')),
        'stages': _norm_str(ev.get('stage_names')),
        'tour': _norm_str(ev.get('tour_name')),
        'shows': ev.get('number_of_shows') or 0,
        'dates': [
            {'d': _norm_date(d.get('date')), 's': _norm_time(d.get('startTime')),
             'e': _norm_time(d.get('endTime')), 'st': _norm_str(d.get('stageName')),
             'a': bool(d.get('allDay'))}
            for d in (ev.get('dates') or [])
        ],
    }
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True).encode('utf-8')).hexdigest()


def _stage_names_str(ev):
    v = ev.get('stage_names')
    if isinstance(v, (list, tuple)):
        return ', '.join(str(s) for s in v if s)
    return _norm_str(v)


def run_prism_sync(trigger='manual', triggered_by=''):
    """
    Pull events from today → lookahead and upsert into prism_events.
    Returns a summary dict (also persisted as a prism_sync_log row).
    Raises nothing — all failures are captured in the summary/log row.
    """
    log_lines = []

    def dbg(msg):
        log_lines.append(f"{datetime.now().strftime('%H:%M:%S')} {msg}")

    summary = {'ok': False, 'log_id': None, 'fetched': 0, 'new': 0,
               'updated': 0, 'unchanged': 0, 'auto_ignored': 0,
               'auto_imported': 0, 'status_synced': 0, 'error': ''}

    if not _sync_lock.acquire(blocking=False):
        summary['error'] = 'A sync is already running in this worker.'
        return summary
    try:
        try:
            db = _d['get_db']()
        except Exception as e:
            summary['error'] = f'Could not open a database connection: {e}'
            _log(f'Prism sync aborted — {summary["error"]}', error=True)
            return summary
        try:
            # ── Preflight: guards + claim a 'running' log row. Wrapped so any
            # failure here (DB hiccup etc.) comes back as a summary error and
            # never escapes as an exception — this function's contract.
            try:
                # Same stale-bootstrap guard as the scheduled-email job: if
                # we're configured for PostgreSQL but silently fell back to
                # SQLite, every setting we read here (token!) is stale —
                # refuse to act.
                configured = _d['db_adapter'].read_db_settings(_d['DATABASE']).get('db_type', 'sqlite')
                if configured == 'postgres' and getattr(db, 'db_type', None) != 'postgres':
                    summary['error'] = ('Refusing to sync: configured for postgres but the '
                                        'active connection is SQLite (stale bootstrap).')
                    _log(f"Prism sync REFUSED — {summary['error']}", error=True)
                    return summary

                settings = get_prism_settings(db)
                if settings['prism_enabled'] != '1':
                    summary['error'] = 'Prism module is disabled in settings.'
                    return summary
                if not settings['prism_token']:
                    summary['error'] = 'No Prism API token configured.'
                    return summary

                # Cross-worker guard: bail if another instance has a live run.
                # First expire zombie rows (worker died mid-sync) so they
                # can't block syncs forever. NOTE: the message is bound as a
                # parameter — db_adapter rewrites every literal '?' in SQL
                # text to %s for PostgreSQL, prose included.
                try:
                    node_timeout = int(settings.get('prism_node_timeout') or 120)
                except (TypeError, ValueError):
                    node_timeout = 120
                stale_cutoff = (datetime.now() - timedelta(seconds=node_timeout * 3 + 60))
                db.execute("UPDATE prism_sync_log SET status='error', "
                           "error_text=?, finished_at=CURRENT_TIMESTAMP "
                           "WHERE status='running' AND started_at < ?",
                           ('Marked stale — sync never finished (worker died?)',
                            stale_cutoff.strftime('%Y-%m-%d %H:%M:%S')))
                running = db.execute(
                    "SELECT COUNT(*) AS n FROM prism_sync_log WHERE status='running'"
                ).fetchone()
                if running and running['n']:
                    db.commit()
                    summary['error'] = 'A sync is already running on another instance.'
                    return summary

                try:
                    lookahead = int(settings.get('prism_lookahead_days') or 365)
                except (TypeError, ValueError):
                    lookahead = 365
                win_start = date.today()
                win_end = win_start + timedelta(days=lookahead)
                statuses = [int(s) for s in
                            str(settings.get('prism_event_statuses') or '2').split(',')
                            if s.strip().isdigit()]

                cur = db.execute(
                    "INSERT INTO prism_sync_log (trigger_type, triggered_by, "
                    "window_start, window_end, status) VALUES (?, ?, ?, ?, 'running')",
                    (trigger, triggered_by, win_start.isoformat(), win_end.isoformat()))
                log_id = summary['log_id'] = cur.lastrowid
                db.commit()
            except Exception as e:
                summary['error'] = f'Sync preflight failed: {e}'
                try:
                    db.rollback()
                except Exception:
                    pass
                _log(f'Prism sync preflight FAILED ({trigger}): {e}', error=True)
                return summary

            dbg(f'sync start trigger={trigger} by={triggered_by or "?"} '
                f'window={win_start}→{win_end} statuses={statuses or "all"}')

            try:
                _refresh_venues(db, settings, dbg)

                args = {'startDate': win_start.isoformat(),
                        'endDate': win_end.isoformat()}
                if statuses:
                    args['eventStatus'] = statuses
                events = _bridge_call('get_events.js', args, settings=settings,
                                      debug_sink=dbg)
                if not isinstance(events, list):
                    raise PrismBridgeError(
                        f'Expected a JSON array of events, got {type(events).__name__}')
                summary['fetched'] = len(events)
                dbg(f'bridge returned {len(events)} event(s)')

                hidden = _hidden_set(settings)
                seen_ids = set()
                for ev in events:
                    pid = ev.get('id')
                    if pid is None:
                        dbg(f'SKIP event with no id: {str(ev)[:120]}')
                        continue
                    if pid in seen_ids:
                        dbg(f'SKIP duplicate id {pid} in API response')
                        continue
                    seen_ids.add(pid)
                    _upsert_event(db, ev, summary, dbg, hidden)

                # Shows imported before the status tag existed (or whose tag
                # was cleared) get it filled in from their staged event.
                cur = db.execute("""
                    UPDATE shows SET prism_status = (
                        SELECT e.event_status FROM prism_events e
                        WHERE e.imported_show_id = shows.id
                          AND e.import_state = 'imported'
                        ORDER BY e.last_synced_at DESC LIMIT 1)
                    WHERE prism_status IS NULL
                      AND id IN (SELECT imported_show_id FROM prism_events
                                 WHERE import_state = 'imported'
                                   AND imported_show_id IS NOT NULL)
                """)
                if cur.rowcount:
                    dbg(f'backfilled Prism status tag on {cur.rowcount} '
                        f'previously imported show(s)')

                if settings.get('prism_auto_import_enabled') == '1':
                    _auto_import(db, summary, dbg, win_start)

                db.execute(
                    "UPDATE prism_sync_log SET status='ok', finished_at=CURRENT_TIMESTAMP, "
                    "events_fetched=?, events_new=?, events_updated=?, events_unchanged=?, "
                    "debug_log=? WHERE id=?",
                    (summary['fetched'], summary['new'], summary['updated'],
                     summary['unchanged'],
                     '\n'.join(log_lines)[:_DEBUG_LOG_MAX_CHARS], log_id))
                db.commit()
                summary['ok'] = True
                _log(f"Prism sync OK ({trigger}): fetched={summary['fetched']} "
                     f"new={summary['new']} updated={summary['updated']} "
                     f"unchanged={summary['unchanged']}")
            except PrismBridgeError as e:
                summary['error'] = str(e)
                dbg(f'ERROR {e}')
                if e.detail:
                    dbg(f'detail: {json.dumps(e.detail)[:2000]}')
                db.rollback()
                db.execute(
                    "UPDATE prism_sync_log SET status='error', finished_at=CURRENT_TIMESTAMP, "
                    "error_text=?, debug_log=? WHERE id=?",
                    (str(e)[:2000], '\n'.join(log_lines)[:_DEBUG_LOG_MAX_CHARS], log_id))
                db.commit()
                _log(f'Prism sync FAILED ({trigger}): {e}', error=True)
            except Exception as e:
                summary['error'] = f'Unexpected error: {e}'
                dbg(f'UNEXPECTED {type(e).__name__}: {e}')
                db.rollback()
                db.execute(
                    "UPDATE prism_sync_log SET status='error', finished_at=CURRENT_TIMESTAMP, "
                    "error_text=?, debug_log=? WHERE id=?",
                    (str(e)[:2000], '\n'.join(log_lines)[:_DEBUG_LOG_MAX_CHARS], log_id))
                db.commit()
                _log(f'Prism sync CRASHED ({trigger}): {e}', error=True)
        finally:
            db.close()
    finally:
        _sync_lock.release()
    return summary


def _upsert_event(db, ev, summary, dbg, hidden=frozenset()):
    """Insert or update one staged event row, keyed on Prism's event id.
    Brand-new events whose every venue token is hidden arrive pre-ignored so
    junk venues (e.g. Prism's "Holidays") never pile up as NEW."""
    pid = ev.get('id')
    name = _norm_str(ev.get('name'))[:300]
    status_code = _norm_str(ev.get('event_status'))
    status_str = _norm_str(ev.get('event_status_string')) or \
        EVENT_STATUSES.get(status_code, status_code)
    first_date = _norm_date(ev.get('first_date')) or None
    last_date = _norm_date(ev.get('last_date')) or None
    venue_name = _norm_str(ev.get('venue_name'))[:200]
    stage_names = _stage_names_str(ev)[:300]
    tour_name = _norm_str(ev.get('tour_name'))[:300]
    n_shows = ev.get('number_of_shows') or 0
    is_rental = 1 if ev.get('is_rental') else 0
    dates_json = json.dumps(ev.get('dates') or [])
    raw_json = json.dumps(ev)
    last_updated = _norm_str(ev.get('event_last_updated'))
    h = _content_hash(ev)

    row = db.execute(
        'SELECT id, content_hash, import_state, imported_show_id, event_status '
        'FROM prism_events WHERE prism_event_id=?',
        (pid,)).fetchone()

    if row is None:
        tokens = _event_tokens(stage_names, venue_name)
        auto_ignore = bool(hidden) and all(t.lower() in hidden for t in tokens)
        initial_state = 'ignored' if auto_ignore else 'new'
        db.execute("""
            INSERT INTO prism_events
              (prism_event_id, name, event_status, event_status_code, first_date,
               last_date, venue_name, stage_names, tour_name, number_of_shows,
               is_rental, dates_json, raw_json, content_hash, prism_last_updated,
               import_state, first_seen_at, last_synced_at, last_changed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """, (pid, name, status_str, status_code, first_date, last_date,
              venue_name, stage_names, tour_name, n_shows, is_rental,
              dates_json, raw_json, h, last_updated, initial_state))
        summary['new'] += 1
        if auto_ignore:
            summary['auto_ignored'] = summary.get('auto_ignored', 0) + 1
            dbg(f'NEW #{pid} "{name}" {first_date or "?"} [{status_str}] '
                f'{stage_names or venue_name} → auto-ignored (hidden venue)')
        else:
            dbg(f'NEW #{pid} "{name}" {first_date or "?"} [{status_str}] '
                f'{stage_names or venue_name}')
    elif row['content_hash'] != h:
        db.execute("""
            UPDATE prism_events SET
               name=?, event_status=?, event_status_code=?, first_date=?,
               last_date=?, venue_name=?, stage_names=?, tour_name=?,
               number_of_shows=?, is_rental=?, dates_json=?, raw_json=?,
               content_hash=?, prism_last_updated=?,
               last_synced_at=CURRENT_TIMESTAMP, last_changed_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (name, status_str, status_code, first_date, last_date, venue_name,
              stage_names, tour_name, n_shows, is_rental, dates_json, raw_json,
              h, last_updated, row['id']))
        summary['updated'] += 1
        dbg(f'UPDATED #{pid} "{name}" (state={row["import_state"]})')
        # Sanctioned write-through: keep the linked show's Prism status tag
        # (Hold / Confirmed / ... on the homepage) current. The ONLY field
        # the sync ever writes to a main-app table.
        if row['import_state'] == 'imported' and row['imported_show_id'] \
                and status_str and status_str != row['event_status']:
            db.execute('UPDATE shows SET prism_status=? WHERE id=?',
                       (status_str, row['imported_show_id']))
            summary['status_synced'] += 1
            dbg(f'  ↳ status {row["event_status"] or "?"} → {status_str} '
                f'flowed to show {row["imported_show_id"]}')
    else:
        db.execute("""
            UPDATE prism_events SET raw_json=?, prism_last_updated=?,
               last_synced_at=CURRENT_TIMESTAMP WHERE id=?
        """, (raw_json, last_updated, row['id']))
        summary['unchanged'] += 1


def run_prism_auto_sync():
    """
    APScheduler job (registered in app.py's start_scheduler). Fires hourly;
    does work only during the configured hour, only on the cluster leader,
    only when the module + auto-sync are enabled, and never on a stale
    SQLite fallback (run_prism_sync re-checks that last one too).
    """
    if not _d['am_i_leader']():
        return
    db = _d['get_db']()
    try:
        configured = _d['db_adapter'].read_db_settings(_d['DATABASE']).get('db_type', 'sqlite')
        if configured == 'postgres' and getattr(db, 'db_type', None) != 'postgres':
            _log('Prism auto-sync: configured for postgres but active connection '
                 'is SQLite (stale bootstrap) — skipping this run.', error=True)
            return
        settings = get_prism_settings(db)
    finally:
        db.close()

    if settings['prism_enabled'] != '1' or settings['prism_auto_sync_enabled'] != '1':
        return
    try:
        sync_hour = int(settings.get('prism_sync_hour') or 5)
    except (TypeError, ValueError):
        sync_hour = 5
    if datetime.now().hour != sync_hour:
        return

    # All-time once-a-day dedup: skip if a successful sync already ran today
    # (e.g. an admin clicked Sync Now this morning, or a late misfire re-ran).
    db = _d['get_db']()
    try:
        today_start = date.today().strftime('%Y-%m-%d 00:00:00')
        done = db.execute(
            "SELECT COUNT(*) AS n FROM prism_sync_log "
            "WHERE status='ok' AND trigger_type='scheduled' AND started_at >= ?",
            (today_start,)).fetchone()
        if done and done['n']:
            return
    finally:
        db.close()

    _log(f'Prism auto-sync: starting (hour={sync_hour})')
    run_prism_sync(trigger='scheduled', triggered_by='system')


def _auto_import(db, summary, dbg, today):
    """
    Opt-in (prism_auto_import_enabled): import every future-dated NEW staged
    event as a 321T show, exactly like a manual Import of everything pending.
    Hidden-venue events never qualify (they arrive pre-ignored), and the
    name+date duplicate guard auto-ignores instead of retrying forever.
    Capped per run — anything beyond the cap is picked up by the next sync.
    """
    rows = db.execute(
        "SELECT id FROM prism_events WHERE import_state='new' "
        "AND first_date >= ? ORDER BY first_date LIMIT 200",
        (today.isoformat(),)).fetchall()
    if not rows:
        return
    dbg(f'auto-import: {len(rows)} candidate(s)')
    results = import_staged_events(db, [r['id'] for r in rows],
                                   user_id=None, username='auto-import',
                                   auto=True)
    for res in results:
        dbg(('  ✓ ' if res['ok'] else '  ✗ ') + res['msg'])
    summary['auto_imported'] = sum(1 for r in results if r['ok'])


# ─── Import into 321Theater ───────────────────────────────────────────────────

def _venue_for_event(db, ev_row):
    """
    Map a Prism stage/venue name onto the 321T venue list (app_settings
    venue_list). Falls back to the raw Prism name so nothing is lost.
    """
    try:
        raw = db.execute(
            "SELECT value FROM app_settings WHERE key='venue_list'").fetchone()
        venue_list = json.loads(raw['value']) if raw and raw['value'] else []
    except Exception:
        venue_list = []

    def norm(s):
        return re.sub(r'[^a-z0-9]', '', (s or '').lower())

    candidates = [s.strip() for s in (ev_row['stage_names'] or '').split(',') if s.strip()]
    candidates.append(ev_row['venue_name'] or '')
    for cand in candidates:
        nc = norm(cand)
        if not nc:
            continue
        for v in venue_list:
            nv = norm(v)
            if nv and (nv == nc or nv in nc or nc in nv):
                return v
    return next((c for c in candidates if c), '')


def import_staged_events(db, ids, user_id, username, auto=False):
    """
    Create 321T shows from staged Prism events — the import gateway between
    the sandbox and main-app tables. Returns a per-event result list.

    `auto=True` (auto-import during sync) differs in one way: events caught
    by the name+date duplicate guard are moved to IGNORED instead of being
    left NEW, so they don't retry on every sync (Restore to New re-arms one).
    """
    results = []
    for eid in ids:
        row = db.execute('SELECT * FROM prism_events WHERE id=?', (eid,)).fetchone()
        if row is None:
            results.append({'id': eid, 'ok': False, 'msg': 'staged event not found'})
            continue
        label = f"#{row['prism_event_id']} {row['name']}"
        if row['import_state'] == 'imported' and row['imported_show_id']:
            results.append({'id': eid, 'ok': False,
                            'msg': f'{label}: already imported (show {row["imported_show_id"]})'})
            continue

        # Per-date schedule from Prism; fall back to the event's first_date.
        try:
            pdates = json.loads(row['dates_json'] or '[]')
        except (TypeError, json.JSONDecodeError):
            pdates = []
        perfs = sorted(
            {(_norm_date(d.get('date')), _norm_time(d.get('startTime')))
             for d in pdates if _norm_date(d.get('date'))})
        first_date = perfs[0][0] if perfs else (_norm_date(row['first_date']) or None)
        first_time = perfs[0][1] if perfs else ''

        # Duplicate guard against the main shows table (matches by name +
        # first date, so a manually-created show isn't doubled).
        if first_date:
            dup = db.execute(
                "SELECT id FROM shows WHERE LOWER(name)=LOWER(?) AND ("
                " show_date=? OR id IN (SELECT show_id FROM show_performances"
                "  WHERE perf_date=?))",
                (row['name'], first_date, first_date)).fetchone()
            if dup:
                msg = (f'{label}: a show with this name and date already '
                       f'exists (show {dup["id"]}) — skipped')
                if auto:
                    db.execute("UPDATE prism_events SET import_state='ignored' "
                               "WHERE id=?", (eid,))
                    msg += ' and auto-ignored (Restore to New to retry)'
                results.append({'id': eid, 'ok': False, 'msg': msg})
                continue

        venue = _venue_for_event(db, row)
        cur = db.execute(
            "INSERT INTO shows (name, show_date, show_time, venue, prism_status, "
            "created_by) VALUES (?, ?, ?, ?, ?, ?)",
            (row['name'], first_date, first_time, venue,
             row['event_status'] or None, user_id))
        show_id = cur.lastrowid

        for key, val in [('show_name', row['name']), ('show_date', first_date or ''),
                         ('show_time', first_time), ('venue', venue)]:
            if val:
                db.execute(
                    'INSERT OR REPLACE INTO advance_data (show_id, field_key, field_value) '
                    'VALUES (?, ?, ?)', (show_id, key, val))

        if perfs:
            for i, (pd, pt) in enumerate(perfs):
                db.execute(
                    'INSERT INTO show_performances (show_id, perf_date, perf_time, sort_order) '
                    'VALUES (?, ?, ?, ?)', (show_id, pd, pt, i))
        elif first_date:
            db.execute(
                'INSERT INTO show_performances (show_id, perf_date, perf_time, sort_order) '
                'VALUES (?, ?, ?, 0)', (show_id, first_date, first_time))

        db.execute("""
            UPDATE prism_events SET import_state='imported', imported_show_id=?,
                   imported_at=CURRENT_TIMESTAMP, imported_by=? WHERE id=?
        """, (show_id, user_id, eid))

        _d['log_audit'](db, 'SHOW_CREATE', 'show', show_id, show_id=show_id,
                        after={'name': row['name'], 'show_date': first_date,
                               'venue': venue, 'source': 'prism',
                               'prism_event_id': row['prism_event_id']},
                        detail=f"Imported from Prism event #{row['prism_event_id']}")
        _log(f"PRISM_IMPORT show_id={show_id} prism_event={row['prism_event_id']} "
             f'name={row["name"]} by={username}')
        results.append({'id': eid, 'ok': True, 'show_id': show_id,
                        'msg': f'{label}: created show {show_id} '
                               f'({len(perfs) or (1 if first_date else 0)} performance(s))'})
    return results


# ─── Logging shim ─────────────────────────────────────────────────────────────

def _log(msg, error=False):
    app = _d.get('app')
    logger = getattr(app, 'logger', None) if app else None
    if logger:
        (logger.error if error else logger.info)(msg)
    sl = _d.get('syslog_logger')
    if sl:
        try:
            (sl.error if error else sl.info)(msg)
        except Exception:
            pass


# ─── Routes ───────────────────────────────────────────────────────────────────

def _page_view():
    db = _d['get_db']()
    try:
        settings = get_prism_settings(db)
        env = environment_check(settings, db=db)

        last_ok = db.execute(
            "SELECT * FROM prism_sync_log WHERE status='ok' "
            "ORDER BY started_at DESC LIMIT 1").fetchone()
        history = db.execute(
            "SELECT * FROM prism_sync_log ORDER BY started_at DESC LIMIT 15"
        ).fetchall()

        counts = {r['import_state']: r['n'] for r in db.execute(
            "SELECT import_state, COUNT(*) AS n FROM prism_events "
            "GROUP BY import_state").fetchall()}

        last_ok_started = str(last_ok['started_at']) if last_ok else None
        rows = db.execute("""
            SELECT e.*, s.name AS show_name_321
            FROM prism_events e
            LEFT JOIN shows s ON s.id = e.imported_show_id
            ORDER BY e.first_date ASC NULLS LAST, e.name
            LIMIT 5000
        """).fetchall()

        hidden = _hidden_set(settings)
        token_counts = {}   # lowercase token → staged-event count
        token_labels = {}   # lowercase token → original-case label

        today = date.today().isoformat()
        events = []
        for r in rows:
            d = dict(r)
            for k in ('first_date', 'last_date', 'first_seen_at', 'last_synced_at',
                      'last_changed_at', 'imported_at'):
                d[k] = str(d[k]) if d[k] is not None else None
            d['is_past'] = bool((d['last_date'] or d['first_date'] or '9999')[:10] < today)
            # Flags the UI surfaces as badges:
            d['missing_in_prism'] = bool(
                last_ok_started and d['last_synced_at']
                and d['last_synced_at'] < last_ok_started)
            d['changed_since_import'] = bool(
                d['import_state'] == 'imported' and d['imported_at']
                and d['last_changed_at'] and d['last_changed_at'] > d['imported_at'])
            d['venue_tokens'] = _event_tokens(d['stage_names'], d['venue_name'])
            for t in d['venue_tokens']:
                tl = t.lower()
                token_counts[tl] = token_counts.get(tl, 0) + 1
                token_labels.setdefault(tl, t)
            events.append(d)

        # ── Venue/stage catalog panel + filter rows ──────────────────────────
        # One row per filterable token: every stage of every catalogued venue,
        # venue-level tokens (stage-less pseudo-venues like "Holidays"), and
        # anything seen in events that the venues API didn't report.
        venue_filter = []
        covered = set()
        for v in db.execute('SELECT * FROM prism_venues ORDER BY name').fetchall():
            try:
                stages = json.loads(v['stages_json'] or '[]')
            except (TypeError, json.JSONDecodeError):
                stages = []
            for s in stages:
                nm = (s.get('name') or '').strip()
                if not nm or nm.lower() in covered:
                    continue
                covered.add(nm.lower())
                venue_filter.append({
                    'label': nm, 'parent': v['name'],
                    'capacity': s.get('capacity'),
                    'count': token_counts.get(nm.lower(), 0),
                    'hidden': nm.lower() in hidden,
                    'inactive': not v['is_active'],
                })
            nmv = (v['name'] or '').strip()
            if nmv and nmv.lower() not in covered and \
                    (not stages or nmv.lower() in token_counts):
                covered.add(nmv.lower())
                venue_filter.append({
                    'label': nmv, 'parent': '',
                    'capacity': v['capacity'],
                    'count': token_counts.get(nmv.lower(), 0),
                    'hidden': nmv.lower() in hidden,
                    'inactive': not v['is_active'],
                })
        for tl in sorted(set(token_counts) - covered):
            venue_filter.append({
                'label': token_labels[tl], 'parent': '(seen in events only)',
                'capacity': None, 'count': token_counts[tl],
                'hidden': tl in hidden, 'inactive': False,
            })

        try:
            hidden_list = [str(n) for n in
                           json.loads(settings.get('prism_hidden_stages') or '[]')]
        except (TypeError, json.JSONDecodeError):
            hidden_list = []

        hist = []
        for r in history:
            d = dict(r)
            for k in ('started_at', 'finished_at', 'window_start', 'window_end'):
                d[k] = str(d[k]) if d[k] is not None else None
            hist.append(d)

        return render_template(
            'prism.html',
            user=_d['get_current_user'](),
            settings=settings,
            has_token=bool(settings.get('prism_token')),
            env_checks=env,
            events=events,
            counts=counts,
            history=hist,
            last_ok=dict(last_ok) if last_ok else None,
            is_leader=_d['am_i_leader'](),
            event_statuses=EVENT_STATUSES,
            today=today,
            venue_filter=venue_filter,
            hidden_list=hidden_list,
        )
    finally:
        db.close()


def _sync_now_view():
    user = _d['get_current_user']()
    summary = run_prism_sync(trigger='manual',
                             triggered_by=(user or {}).get('username', ''))
    return jsonify(summary), (200 if summary['ok'] else 400)


def _test_view():
    """Connection test: env check plus a cheap live API call (venues)."""
    db = _d['get_db']()
    try:
        settings = get_prism_settings(db)
        env = environment_check(settings, db=db)
    finally:
        db.close()
    log_lines = []
    out = {'env': env, 'api_ok': False, 'api_detail': '', 'log': log_lines}
    if all(c['ok'] for c in env):
        try:
            venues = _bridge_call('get_venues.js', settings=settings, timeout=60,
                                  debug_sink=log_lines.append)
            names = [v.get('name', '?') for v in venues][:10] \
                if isinstance(venues, list) else []
            out['api_ok'] = True
            out['api_detail'] = (f'Fetched {len(venues)} venue(s): ' + ', '.join(names)) \
                if isinstance(venues, list) else 'Unexpected response shape'
        except PrismBridgeError as e:
            out['api_detail'] = str(e)
    else:
        out['api_detail'] = 'Environment checks failed — fix the items above first.'
    return jsonify(out)


def _debug_fetch_view():
    """
    Troubleshooting tool ("Raw API Fetch" on /prism): run a short live events
    fetch and return the exact request arguments, the bridge/SDK exchange
    trace, and the raw JSON response — WITHOUT writing anything to staging.
    """
    db = _d['get_db']()
    try:
        settings = get_prism_settings(db)
    finally:
        db.close()
    if settings['prism_enabled'] != '1':
        return jsonify({'ok': False, 'error': 'Prism module is disabled in settings.'})
    if not settings['prism_token']:
        return jsonify({'ok': False, 'error': 'No Prism API token configured.'})

    body = request.get_json(silent=True) or {}
    try:
        days = max(1, min(31, int(body.get('days', 7))))
    except (TypeError, ValueError):
        days = 7

    win_start = date.today()
    win_end = win_start + timedelta(days=days)
    statuses = [int(s) for s in
                str(settings.get('prism_event_statuses') or '2').split(',')
                if s.strip().isdigit()]
    args = {'startDate': win_start.isoformat(), 'endDate': win_end.isoformat()}
    if statuses:
        args['eventStatus'] = statuses

    log_lines = []
    out = {'ok': False, 'request': {'script': 'get_events.js', 'args': args},
           'log': log_lines}
    try:
        events = _bridge_call('get_events.js', args, settings=settings,
                              debug_sink=log_lines.append)
        out['ok'] = True
        out['event_count'] = len(events) if isinstance(events, list) else None
        raw = json.dumps(events, indent=2)
        out['response_truncated'] = len(raw) > 80_000
        out['response'] = raw[:80_000]
    except PrismBridgeError as e:
        out['error'] = str(e)
        if e.detail:
            out['detail'] = e.detail
    return jsonify(out)


def _save_settings_view():
    db = _d['get_db']()
    try:
        _save_setting(db, 'prism_enabled',
                      '1' if request.form.get('prism_enabled') else '0')
        _save_setting(db, 'prism_auto_sync_enabled',
                      '1' if request.form.get('prism_auto_sync_enabled') else '0')
        _save_setting(db, 'prism_auto_import_enabled',
                      '1' if request.form.get('prism_auto_import_enabled') else '0')

        # Blank token field = leave the stored token unchanged.
        token = request.form.get('prism_token', '')
        if token.strip():
            _save_setting(db, 'prism_token', token.strip())
        elif request.form.get('prism_token_clear'):
            _save_setting(db, 'prism_token', '')

        for key, lo, hi, dflt in [('prism_sync_hour', 0, 23, '5'),
                                  ('prism_lookahead_days', 1, 1095, '365'),
                                  ('prism_node_timeout', 30, 600, '120')]:
            try:
                v = int(request.form.get(key, dflt))
                v = max(lo, min(hi, v))
            except (TypeError, ValueError):
                v = int(dflt)
            _save_setting(db, key, str(v))

        statuses = [s for s in request.form.getlist('prism_event_statuses')
                    if s in EVENT_STATUSES]
        _save_setting(db, 'prism_event_statuses', ','.join(statuses) or '2')

        bridge_dir = request.form.get('prism_bridge_dir', '').strip()
        _save_setting(db, 'prism_bridge_dir', bridge_dir)

        _d['log_audit'](db, 'SETTINGS_UPDATE', 'setting', 'prism',
                        detail='Prism integration settings updated')
        db.commit()
    finally:
        db.close()
    return redirect(url_for('prism_page'))


def _venue_visibility_view():
    """Persist the set of hidden stage/venue names (prism_hidden_stages).

    Hiding a venue filters its events from the list, makes newly synced
    events from it arrive pre-ignored, AND moves its already-staged NEW
    events to ignored in the same click (imported/ignored rows untouched) —
    so junk like Prism's "Holidays" disappears in one action. Everything
    stays staged and reversible (Show hidden venues → select → Restore)."""
    data = request.get_json(silent=True) or {}
    names = data.get('hidden')
    if not isinstance(names, list) or len(names) > 300:
        return jsonify({'error': 'hidden must be a list of names (max 300)'}), 400
    clean, seen = [], set()
    for n in names:
        s = str(n).strip()[:200]
        if s and s.lower() not in seen:
            seen.add(s.lower())
            clean.append(s)
    db = _d['get_db']()
    try:
        prev = _hidden_set(get_prism_settings(db))
        newly_hidden = seen - prev

        ignored_now = 0
        if newly_hidden:
            rows = db.execute(
                "SELECT id, stage_names, venue_name FROM prism_events "
                "WHERE import_state='new'").fetchall()
            for r in rows:
                tokens = _event_tokens(r['stage_names'], r['venue_name'])
                if all(t.lower() in seen for t in tokens) and \
                        any(t.lower() in newly_hidden for t in tokens):
                    db.execute("UPDATE prism_events SET import_state='ignored' "
                               "WHERE id=?", (r['id'],))
                    ignored_now += 1

        _save_setting(db, 'prism_hidden_stages', json.dumps(clean))
        _d['log_audit'](db, 'SETTINGS_UPDATE', 'setting', 'prism_hidden_stages',
                        detail=f'Prism venue visibility updated — {len(clean)} hidden, '
                               f'{ignored_now} staged event(s) auto-ignored')
        db.commit()
    finally:
        db.close()
    return jsonify({'ok': True, 'hidden': clean, 'ignored_now': ignored_now})


def _set_state_view():
    """Mark staged events ignored / back to new. Never touches main tables."""
    data = request.get_json(silent=True) or {}
    ids = [i for i in (data.get('ids') or []) if isinstance(i, int)]
    state = data.get('state')
    if state not in ('ignored', 'new') or not ids:
        return jsonify({'error': 'state must be "ignored" or "new", with ids[]'}), 400
    db = _d['get_db']()
    try:
        n = 0
        for eid in ids:
            cur = db.execute(
                "UPDATE prism_events SET import_state=? "
                "WHERE id=? AND import_state != 'imported'", (state, eid))
            n += cur.rowcount or 0
        db.commit()
        return jsonify({'ok': True, 'updated': n})
    finally:
        db.close()


def _import_view():
    data = request.get_json(silent=True) or {}
    ids = [i for i in (data.get('ids') or []) if isinstance(i, int)]
    if not ids:
        return jsonify({'error': 'ids[] required'}), 400
    if len(ids) > 200:
        return jsonify({'error': 'Too many events in one import (max 200).'}), 400
    user = _d['get_current_user']()
    db = _d['get_db']()
    try:
        results = import_staged_events(db, ids, user['id'], user['username'])
        db.commit()
    except Exception as e:
        db.rollback()
        return jsonify({'error': f'Import failed, nothing was created: {e}'}), 500
    finally:
        db.close()
    return jsonify({'ok': True, 'results': results,
                    'created': sum(1 for r in results if r['ok'])})


def _raw_view(eid):
    db = _d['get_db']()
    try:
        row = db.execute(
            'SELECT prism_event_id, name, raw_json FROM prism_events WHERE id=?',
            (eid,)).fetchone()
    finally:
        db.close()
    if row is None:
        return jsonify({'error': 'not found'}), 404
    try:
        payload = json.loads(row['raw_json'] or '{}')
    except json.JSONDecodeError:
        payload = {'_unparsed': row['raw_json']}
    return jsonify({'prism_event_id': row['prism_event_id'],
                    'name': row['name'], 'payload': payload})


# ─── Registration ─────────────────────────────────────────────────────────────

def register(app, **deps):
    """
    Wire the module into the Flask app. `deps` must provide:
      get_db, get_current_user, am_i_leader, admin_required, log_audit,
      db_adapter, DATABASE — and optionally syslog_logger.
    Called once from app.py; everything else in this file is self-contained.
    """
    _d.update(deps, app=app)
    admin = deps['admin_required']

    app.add_url_rule('/prism', 'prism_page', admin(_page_view))
    app.add_url_rule('/prism/sync', 'prism_sync_now', admin(_sync_now_view),
                     methods=['POST'])
    app.add_url_rule('/prism/test', 'prism_test', admin(_test_view),
                     methods=['POST'])
    app.add_url_rule('/prism/debug-fetch', 'prism_debug_fetch',
                     admin(_debug_fetch_view), methods=['POST'])
    app.add_url_rule('/prism/settings', 'prism_save_settings',
                     admin(_save_settings_view), methods=['POST'])
    app.add_url_rule('/prism/events/state', 'prism_set_state',
                     admin(_set_state_view), methods=['POST'])
    app.add_url_rule('/prism/venues/visibility', 'prism_venue_visibility',
                     admin(_venue_visibility_view), methods=['POST'])
    app.add_url_rule('/prism/import', 'prism_import', admin(_import_view),
                     methods=['POST'])
    app.add_url_rule('/prism/events/<int:eid>/raw', 'prism_event_raw',
                     admin(_raw_view))
