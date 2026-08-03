# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
DB snapshot inspection & surgical recovery — SANDBOXED (read-mostly).

The backup scheduler in app.py writes hourly (keep 24) and daily (keep 30)
snapshots of the whole database to BACKUP_DIR/{hourly,daily}/ — compressed
plain-SQL `pg_dump` output (.sql.gz) on PostgreSQL, a file copy (.db) on
SQLite. This module lets an admin answer "what did the data look like at
3pm, and what changed since?" and surgically pull pieces back — without ever
restoring the whole database:

  * INSPECT   — parse a snapshot in pure Python (no scratch database, the
                dump never touches the PostgreSQL server) and list its tables
                with row counts next to live counts.
  * DIFF      — per-table comparison keyed on primary key: rows added /
                removed / changed since the snapshot, field-by-field.
  * RECOVER   — two modes, both preview → confirm → apply in ONE transaction,
                audit-logged with before-images so the restore is itself
                reviewable (and row-restores undoable) in the Audit Log:
                  - per-show: roll one show (its `shows` row + all content
                    child tables) back to the snapshot, including
                    resurrecting a deleted show;
                  - row cherry-pick: restore or delete individual rows
                    selected in the diff view.

SAFETY RAILS
------------
  * All routes are admin-only; state changes are POST (CSRF-protected).
  * Apply re-derives the plan from the current snapshot + live data and
    compares a hash against the one issued at preview time — if anything
    changed in between, the apply is refused (HTTP 409) and the admin must
    re-preview.
  * Restore refuses to run when the configured backend is PostgreSQL but the
    connection silently fell back to the SQLite bootstrap (stale data —
    see CLAUDE.md), and when the snapshot's backend differs from the live
    backend (value formats aren't comparable across backends).
  * RESTORE_BLOCKED tables (auth/session/security/log/infra tables, plus the
    shared `users` directory owned jointly with sister apps) are inspectable
    but never writable from here.
  * On PostgreSQL, sequences are re-synced (setval to MAX(id)) after any
    insert so resurrected rows can't make future inserts collide.

app.py's only knowledge of this module is one `register(app, **deps)` call —
delete this file + the Settings tab link and the app runs unchanged.
"""

import gzip
import hashlib
import json
import os
import re
import sqlite3
import zlib
from collections import Counter
from datetime import date, datetime, time as dt_time
from decimal import Decimal, InvalidOperation

from flask import jsonify, render_template, request

# Filled by register() — mirrors prism_module's dependency-dict pattern.
_d = {}


class SnapshotReadError(Exception):
    """The snapshot file exists but can't be parsed — corrupt, truncated, or
    (for the newest hourly file) still being written by the backup job.
    Views translate this into a friendly 422 instead of a 500."""

# ─── Policy ───────────────────────────────────────────────────────────────────

# Tables that may never be written by a snapshot restore. Everything is still
# inspectable/diffable. Reasons:
#   users / user_pending_registration — shared cross-app directory (see
#       CLAUDE.md); restoring stale rows (old password hashes, old is_app_*
#       flags) could clobber sister-app changes.
#   *_sessions / tokens / otp — security state; restoring old secrets is
#       strictly worse than losing them.
#   audit_log — restoring it would falsify history.
#   email_send_log — the scheduled-email dedup ledger; rewinding it would
#       re-send already-sent advance emails.
#   cluster/perf tables — operational churn, meaningless to restore.
RESTORE_BLOCKED = {
    'users', 'user_pending_registration',
    'app_sessions', 'active_sessions', 'password_reset_tokens',
    'gateway_otp_codes', 'ai_sessions',
    'audit_log', 'email_send_log', 'email_outbox_log', 'export_log',
    'cluster_instances', 'perf_page_stats', 'perf_slow_queries',
    'sqlite_sequence',
}

# Churny operational tables — sorted to the bottom of the tables list so the
# content tables people actually care about stay on top.
SYSTEM_TABLES = RESTORE_BLOCKED | {
    'notifications', 'advance_reads', 'site_message_views',
    'site_message_dismissals', 'email_send_errors', 'field_alert_state',
    'comment_versions', 'prism_sync_log',
}

# Content child tables included in a per-show restore, in apply order (the
# `shows` row itself always goes first so child inserts have their parent).
# Deliberately EXCLUDES logs/ledgers tied to a show (email_send_log,
# export_log, notifications, audit_log, asset_logs, ai_sessions) — rolling a
# show back should not rewind history of what was sent or done.
SHOW_CHILD_TABLES = [
    'advance_data', 'schedule_rows', 'schedule_meta', 'post_show_notes',
    'show_performances', 'show_comments', 'show_attachments',
    'field_alert_state', 'labor_requests', 'show_labor_billable_items',
    'post_show_labor', 'show_labor_days', 'show_assets',
    'show_external_rentals', 'pdf_submissions', 'form_history',
]

DIFF_ROW_CAP = 400        # max rows returned per diff bucket
PREVIEW_SAMPLE_CAP = 25   # sample rows shown per table in a restore preview
AUDIT_ROW_CAP = 200       # max per-row audit entries per apply
DISPLAY_VALUE_CAP = 2000  # long values (form_history blobs...) truncated in UI JSON

_SAFE_FILE_RE = re.compile(r'^advance_[0-9_]+\.(sql\.gz|db)$')
_SAFE_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_$]*$')


# ─── Snapshot files ───────────────────────────────────────────────────────────

def _backup_dir():
    return _d['BACKUP_DIR']


def _snapshot_path(kind, name):
    """Validate (kind, filename) and return the absolute path, or None."""
    if kind not in ('hourly', 'daily'):
        return None
    if not _SAFE_FILE_RE.match(name or ''):
        return None
    path = os.path.join(_backup_dir(), kind, name)
    return path if os.path.isfile(path) else None


def list_snapshots():
    """All snapshot files on THIS server, newest first."""
    out = []
    for kind in ('hourly', 'daily'):
        d = os.path.join(_backup_dir(), kind)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not _SAFE_FILE_RE.match(name):
                continue
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            backend = 'postgres' if name.endswith('.sql.gz') else 'sqlite'
            out.append({
                'kind': kind,
                'name': name,
                'size': st.st_size,
                'mtime': datetime.fromtimestamp(st.st_mtime),
                'backend': backend,
                'healthy': _quick_health(p, backend),
            })
    out.sort(key=lambda s: s['mtime'], reverse=True)
    return out


# ─── pg_dump plain-format parsing ────────────────────────────────────────────
#
# A plain-format dump is a SQL script. The data lives in COPY blocks:
#
#     COPY theater321.shows (id, name, show_date, ...) FROM stdin;
#     1\tHamilton\t2026-08-01\t...
#     \.
#
# COPY text format is strictly line-based (embedded newlines/tabs in values
# are backslash-escaped), so streaming line-by-line is safe. Primary keys
# appear later as ALTER TABLE ... ADD CONSTRAINT ... PRIMARY KEY (...).

_COPY_RE = re.compile(
    r'^COPY\s+(?:"?([A-Za-z0-9_$]+)"?\.)?"?([A-Za-z0-9_$]+)"?\s*'
    r'\(([^)]*)\)\s+FROM\s+stdin;\s*$'
)
_ALTER_ONLY_RE = re.compile(
    r'^ALTER\s+TABLE\s+ONLY\s+(?:"?([A-Za-z0-9_$]+)"?\.)?"?([A-Za-z0-9_$]+)"?\s*(.*)$',
    re.IGNORECASE,
)
_PKEY_RE = re.compile(
    r'ADD\s+CONSTRAINT\s+\S+\s+PRIMARY\s+KEY\s+\(([^)]*)\)', re.IGNORECASE
)

_COPY_SIMPLE_ESCAPES = {
    '\\': '\\', 'b': '\b', 'f': '\f', 'n': '\n',
    'r': '\r', 't': '\t', 'v': '\v',
}


def _copy_unescape(field):
    """Decode one COPY text-format field. '\\N' (the whole field) is NULL."""
    if field == '\\N':
        return None
    if '\\' not in field:
        return field
    out = []
    i, n = 0, len(field)
    while i < n:
        c = field[i]
        if c != '\\':
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            out.append('\\')
            break
        e = field[i]
        if e in _COPY_SIMPLE_ESCAPES:
            out.append(_COPY_SIMPLE_ESCAPES[e])
            i += 1
        elif e == 'x':
            j = i + 1
            hexd = ''
            while j < n and len(hexd) < 2 and field[j] in '0123456789abcdefABCDEF':
                hexd += field[j]
                j += 1
            if hexd:
                out.append(chr(int(hexd, 16)))
                i = j
            else:
                out.append('x')
                i += 1
        elif e in '01234567':
            j = i
            octd = ''
            while j < n and len(octd) < 3 and field[j] in '01234567':
                octd += field[j]
                j += 1
            out.append(chr(int(octd, 8)))
            i = j
        else:
            out.append(e)
            i += 1
    return ''.join(out)


def _split_cols(col_list_sql):
    return [c.strip().strip('"') for c in col_list_sql.split(',') if c.strip()]


def _iter_dump_lines(path):
    """Yield decoded lines (no trailing newline) of the FIRST gzip member of
    a .sql.gz dump, zcat-style: trailing garbage after a complete gzip stream
    is ignored. Python's gzip module instead raises BadGzipFile on such files
    — and torn backup writes (a second writer or a partial copy landing after
    a complete stream) produced exactly that in the wild, turning perfectly
    recoverable dumps into 500s. pg_dump output is always a single member, so
    reading one member never loses data. A stream that ends before the member
    completes still raises EOFError (truncated / mid-write)."""
    dec = zlib.decompressobj(31)  # 31 = gzip header + max window
    pending = b''
    with open(path, 'rb') as f:
        while not dec.eof:
            chunk = f.read(1 << 16)
            if not chunk:
                raise EOFError('compressed stream ends mid-member')
            pending += dec.decompress(chunk)
            if b'\n' not in pending:
                continue
            lines = pending.split(b'\n')
            pending = lines.pop()
            for ln in lines:
                yield ln.decode('utf-8', errors='replace')
    for ln in pending.split(b'\n') if pending else ():
        yield ln.decode('utf-8', errors='replace')


def _scan_pg_dump(path):
    """One streaming pass: table -> columns/row-count/primary-key."""
    tables = {}
    pending_alter = None  # (schema, table) awaiting its ADD CONSTRAINT line
    it = _iter_dump_lines(path)
    for line in it:
        m = _COPY_RE.match(line)
        if m:
            schema, table = (m.group(1) or 'public'), m.group(2)
            cols = _split_cols(m.group(3))
            count = 0
            for data_line in it:
                if data_line == '\\.':
                    break
                count += 1
            tables[(schema, table)] = {'columns': cols, 'rows': count, 'pk': None}
            continue
        am = _ALTER_ONLY_RE.match(line)
        if am:
            rest = am.group(3) or ''
            target = ((am.group(1) or 'public'), am.group(2))
            pm = _PKEY_RE.search(rest)
            if pm:
                if target in tables:
                    tables[target]['pk'] = _split_cols(pm.group(1))
            elif not rest.strip():
                pending_alter = target
            continue
        if pending_alter is not None:
            pm = _PKEY_RE.search(line)
            if pm and pending_alter in tables:
                tables[pending_alter]['pk'] = _split_cols(pm.group(1))
            if line.rstrip().endswith(';'):
                pending_alter = None
    return tables


def _read_pg_dump_table(path, schema, table):
    """Stream the dump to one table's COPY block; return (columns, rows).
    Row values are Python strings or None — exactly the COPY text values."""
    it = _iter_dump_lines(path)
    for line in it:
        m = _COPY_RE.match(line)
        if not m:
            continue
        if (m.group(1) or 'public') != schema or m.group(2) != table:
            # Skip this block's data quickly.
            for data_line in it:
                if data_line == '\\.':
                    break
            continue
        cols = _split_cols(m.group(3))
        rows = []
        for data_line in it:
            if data_line == '\\.':
                break
            rows.append([_copy_unescape(f) for f in data_line.split('\t')])
        return cols, rows
    return None, None


# ─── SQLite snapshot reading (.db backups on SQLite installs) ────────────────

def _sqlite_ro(path):
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


def _scan_sqlite_db(path):
    tables = {}
    conn = _sqlite_ro(path)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name")]
        for t in names:
            if not _SAFE_IDENT_RE.match(t):
                continue
            info = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
            cols = [r[1] for r in info]
            pk = [r[1] for r in sorted((r for r in info if r[5]), key=lambda r: r[5])]
            count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            tables[('main', t)] = {'columns': cols, 'rows': count, 'pk': pk or None}
    finally:
        conn.close()
    return tables


def _read_sqlite_db_table(path, table):
    conn = _sqlite_ro(path)
    try:
        info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        cols = [r[1] for r in info]
        if not cols:
            return None, None
        rows = [list(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
        return cols, rows
    finally:
        conn.close()


# ─── Snapshot facade + scan cache ─────────────────────────────────────────────

_scan_cache = {}  # (path, mtime, size) -> tables dict
_SCAN_CACHE_MAX = 8


class Snapshot:
    def __init__(self, kind, name):
        self.kind = kind
        self.name = name
        self.path = _snapshot_path(kind, name)
        if self.path is None:
            raise FileNotFoundError(f'unknown snapshot {kind}/{name}')
        self.backend = 'postgres' if name.endswith('.sql.gz') else 'sqlite'

    def scan(self):
        st = os.stat(self.path)
        key = (self.path, st.st_mtime, st.st_size)
        hit = _scan_cache.get(key)
        if hit is not None:
            return hit
        try:
            tables = (_scan_pg_dump(self.path) if self.backend == 'postgres'
                      else _scan_sqlite_db(self.path))
        except _READ_ERRORS as e:
            raise SnapshotReadError(self._read_error_detail(e)) from e
        while len(_scan_cache) >= _SCAN_CACHE_MAX:
            _scan_cache.pop(next(iter(_scan_cache)))
        _scan_cache[key] = tables
        return tables

    def read_table(self, schema, table):
        try:
            if self.backend == 'postgres':
                return _read_pg_dump_table(self.path, schema, table)
            return _read_sqlite_db_table(self.path, table)
        except _READ_ERRORS as e:
            raise SnapshotReadError(self._read_error_detail(e)) from e

    def _read_error_detail(self, e):
        if isinstance(e, (gzip.BadGzipFile, zlib.error)):
            if not _quick_health(self.path, self.backend):
                return ('the file does not start with gzip data, so it was '
                        'corrupted when it was WRITTEN (e.g. two processes '
                        'writing the same backup file, or a partial copy)')
            return ('the gzip stream breaks partway through the file — the '
                    'backup was corrupted when it was written')
        if isinstance(e, EOFError):
            return ('the file ends mid-stream — it is truncated, or the '
                    'backup job is writing it right now; try again in a minute')
        return f'{type(e).__name__}: {e}'


# Errors that mean "this snapshot file is unreadable", not "our code is wrong".
# gzip.BadGzipFile subclasses OSError, so OSError also covers permission and
# I/O errors on the file itself.
_READ_ERRORS = (OSError, EOFError, zlib.error, sqlite3.DatabaseError,
                UnicodeError)


def _quick_health(path, backend):
    """Cheap magic-byte check used by the list view and error messages.
    Catches corruption at the head of the file only — a stream that breaks
    mid-file still surfaces as a clean SnapshotReadError at scan time."""
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return False
    if backend == 'postgres':
        return head[:2] == b'\x1f\x8b'
    return head.startswith(b'SQLite format 3\x00')


# ─── Live-side helpers ────────────────────────────────────────────────────────

def _live_backend_info():
    """(configured_type, and whether reads would be a stale SQLite fallback)."""
    settings = _d['db_adapter'].read_db_settings(_d['DATABASE'])
    return settings


def _is_stale_fallback(db):
    configured = _live_backend_info().get('db_type', 'sqlite')
    return configured == 'postgres' and getattr(db, 'db_type', None) != 'postgres'


def _live_schemas(db):
    """Schemas this app owns on the live side, in search-path order."""
    if db.db_type != 'postgres':
        return ['main']
    s = _live_backend_info()
    return [s.get('pg_app_schema') or 'theater321',
            s.get('pg_shared_schema') or 'shared']


def _qi(name):
    if not _SAFE_IDENT_RE.match(name or ''):
        raise ValueError(f'unsafe identifier: {name!r}')
    return f'"{name}"'


def _qtable(db, schema, table):
    if db.db_type == 'postgres':
        return f'{_qi(schema)}.{_qi(table)}'
    return _qi(table)


def _live_tables(db):
    """{(schema, table): approx-owned} for the app's live schemas."""
    out = []
    if db.db_type == 'postgres':
        for schema in _live_schemas(db):
            rows = db.execute(
                'SELECT table_name FROM information_schema.tables '
                'WHERE table_schema = ? AND table_type = ? ORDER BY table_name',
                (schema, 'BASE TABLE')).fetchall()
            out.extend((schema, r['table_name']) for r in rows)
    else:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name").fetchall()
        out.extend(('main', r['name']) for r in rows)
    return out


def _live_columns(db, schema, table):
    if db.db_type == 'postgres':
        rows = db.execute(
            'SELECT column_name FROM information_schema.columns '
            'WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position',
            (schema, table)).fetchall()
        return [r['column_name'] for r in rows]
    rows = db.execute(f'PRAGMA table_info({_qi(table)})').fetchall()
    return [r['name'] for r in rows]


def _live_count(db, schema, table):
    try:
        return db.execute(
            f'SELECT COUNT(*) AS n FROM {_qtable(db, schema, table)}'
        ).fetchone()['n']
    except Exception:
        return None


def _read_live_table(db, schema, table):
    cols = _live_columns(db, schema, table)
    if not cols:
        return None, None
    col_sql = ', '.join(_qi(c) for c in cols)
    rows = db.execute(
        f'SELECT {col_sql} FROM {_qtable(db, schema, table)}').fetchall()
    return cols, [[r[c] for c in cols] for r in (dict(row) for row in rows)]


# ─── Value normalization & diffing ────────────────────────────────────────────
#
# Snapshot values from a pg dump are COPY text; live PostgreSQL values are
# Python objects (bool/int/Decimal/datetime/...). Normalize both to the COPY
# text representation before comparing.

def _norm(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return 't' if v else 'f'
    if isinstance(v, datetime):
        return v.isoformat(sep=' ')
    if isinstance(v, (date, dt_time)):
        return v.isoformat()
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, (bytes, memoryview)):
        return '\\x' + bytes(v).hex()
    return v if isinstance(v, str) else str(v)


def _trunc(v):
    """Display-only truncation for UI JSON — never applied to written values."""
    if isinstance(v, str) and len(v) > DISPLAY_VALUE_CAP:
        return v[:DISPLAY_VALUE_CAP] + f'… [{len(v)} chars total]'
    return v


def _values_equal(a, b):
    """Normalized equality with a numeric fallback so '5', '5.0' and
    Decimal('5.00') don't produce phantom diffs."""
    na, nb = _norm(a), _norm(b)
    if na == nb:
        return True
    if na is None or nb is None:
        return False
    try:
        return Decimal(na) == Decimal(nb)
    except (InvalidOperation, ValueError, ArithmeticError):
        return False


def _pk_for(snap_meta, columns):
    pk = (snap_meta or {}).get('pk')
    if pk and all(c in columns for c in pk):
        return pk
    if 'id' in columns:
        return ['id']
    return None


def _key_of(row_map, pk_cols):
    return json.dumps([_norm(row_map.get(c)) for c in pk_cols])


def _diff_table(snap_cols, snap_rows, live_cols, live_rows, pk_cols):
    """Compare one table. Returns buckets keyed on pk (or on the whole row
    when no usable pk exists — then 'changed' detection is impossible)."""
    common = [c for c in snap_cols if c in live_cols]
    snap_maps = [dict(zip(snap_cols, r)) for r in snap_rows]
    live_maps = [dict(zip(live_cols, r)) for r in live_rows]

    result = {
        'columns': common,
        'snap_only_columns': [c for c in snap_cols if c not in live_cols],
        'live_only_columns': [c for c in live_cols if c not in snap_cols],
        'keyed_by': pk_cols,
        'added': [], 'removed': [], 'changed': [],
        'added_total': 0, 'removed_total': 0, 'changed_total': 0,
        'same_total': 0, 'capped': False,
    }

    if not pk_cols or not all(c in common for c in pk_cols):
        # No stable key — degrade to whole-row set comparison.
        result['keyed_by'] = None
        snap_keys = Counter(json.dumps([_norm(m.get(c)) for c in common]) for m in snap_maps)
        live_keys = Counter(json.dumps([_norm(m.get(c)) for c in common]) for m in live_maps)
        for k, n in (live_keys - snap_keys).items():
            result['added_total'] += n
            if len(result['added']) < DIFF_ROW_CAP:
                result['added'].append({'key': None, 'row': {
                    c: _trunc(v) for c, v in zip(common, json.loads(k))}})
        for k, n in (snap_keys - live_keys).items():
            result['removed_total'] += n
            if len(result['removed']) < DIFF_ROW_CAP:
                result['removed'].append({'key': None, 'row': {
                    c: _trunc(v) for c, v in zip(common, json.loads(k))}})
        result['same_total'] = sum((snap_keys & live_keys).values())
        result['capped'] = (result['added_total'] > len(result['added'])
                            or result['removed_total'] > len(result['removed']))
        return result

    snap_by_key, live_by_key = {}, {}
    for m in snap_maps:
        snap_by_key[_key_of(m, pk_cols)] = m
    for m in live_maps:
        live_by_key[_key_of(m, pk_cols)] = m

    def _display(m):
        return {c: _trunc(_norm(m.get(c))) for c in common}

    for k, m in live_by_key.items():
        if k not in snap_by_key:
            result['added_total'] += 1
            if len(result['added']) < DIFF_ROW_CAP:
                result['added'].append({'key': k, 'row': _display(m)})
    for k, m in snap_by_key.items():
        if k not in live_by_key:
            result['removed_total'] += 1
            if len(result['removed']) < DIFF_ROW_CAP:
                result['removed'].append({'key': k, 'row': _display(m)})
            continue
        lm = live_by_key[k]
        fields = [{'column': c, 'live': _trunc(_norm(lm.get(c))),
                   'snapshot': _trunc(_norm(m.get(c)))}
                  for c in common if not _values_equal(m.get(c), lm.get(c))]
        if fields:
            result['changed_total'] += 1
            if len(result['changed']) < DIFF_ROW_CAP:
                result['changed'].append({
                    'key': k,
                    'pk': {c: _norm(m.get(c)) for c in pk_cols},
                    'fields': fields,
                })
        else:
            result['same_total'] += 1
    result['capped'] = (result['added_total'] > len(result['added'])
                        or result['removed_total'] > len(result['removed'])
                        or result['changed_total'] > len(result['changed']))
    return result


# ─── Restore planning ─────────────────────────────────────────────────────────

def _restore_allowed(db, snap):
    """(ok, reason) — global preconditions for any write-back."""
    if _is_stale_fallback(db):
        return False, ('Configured backend is PostgreSQL but this connection '
                       'fell back to the SQLite bootstrap — refusing to write. '
                       'Fix the PostgreSQL connection first.')
    if snap.backend != db.db_type:
        return False, (f'Snapshot backend ({snap.backend}) differs from the '
                       f'live backend ({db.db_type}) — cross-backend restore '
                       'is not supported.')
    return True, None


def _plan_table(db, snap, schema, table, restore_keys=None, delete_keys=None,
                where_show_id=None):
    """Build the per-table portion of a restore plan.

    restore_keys / delete_keys: explicit key lists from the diff view
    (None = none). where_show_id: per-show mode — make live rows for that
    show match the snapshot's (upsert all snapshot rows, delete live-only).
    """
    meta = snap.scan().get((schema, table))
    if meta is None:
        return None, f'table {table} not present in snapshot'
    snap_cols, snap_rows = snap.read_table(schema, table)
    live_cols = _live_columns(db, schema, table)
    if not live_cols:
        return None, f'table {table} not present in live database'
    _, live_rows = _read_live_table(db, schema, table)
    pk_cols = _pk_for(meta, [c for c in snap_cols if c in live_cols])
    if not pk_cols:
        return None, f'table {table} has no usable primary key'

    common = [c for c in snap_cols if c in live_cols]
    snap_maps = {}
    live_maps = {}
    for r in snap_rows:
        m = dict(zip(snap_cols, r))
        if where_show_id is not None and not _values_equal(m.get('show_id' if table != 'shows' else 'id'), where_show_id):
            continue
        snap_maps[_key_of(m, pk_cols)] = m
    for r in live_rows:
        m = dict(zip(live_cols, r))
        if where_show_id is not None and not _values_equal(m.get('show_id' if table != 'shows' else 'id'), where_show_id):
            continue
        live_maps[_key_of(m, pk_cols)] = m

    if where_show_id is not None:
        restore_keys = list(snap_maps.keys())
        delete_keys = [k for k in live_maps if k not in snap_maps]

    entry = {'schema': schema, 'table': table, 'pk': pk_cols, 'columns': common,
             'inserts': [], 'updates': [], 'deletes': []}

    restore_set = set(restore_keys or [])
    for k in (restore_keys or []):
        sm = snap_maps.get(k)
        if sm is None:
            return None, f'row {k} no longer exists in the snapshot view of {table}'
        row = {c: sm.get(c) for c in common}
        lm = live_maps.get(k)
        if lm is None:
            entry['inserts'].append({'key': k, 'row': row})
        else:
            fields = [c for c in common
                      if c not in pk_cols and not _values_equal(sm.get(c), lm.get(c))]
            if fields:
                entry['updates'].append({
                    'key': k,
                    'pk': {c: _norm(sm.get(c)) for c in pk_cols},
                    'row': row,
                    'set_columns': fields,
                    'before': {c: _norm(lm.get(c)) for c in common},
                })

    for k in (delete_keys or []):
        lm = live_maps.get(k)
        if lm is None:
            continue  # already gone — nothing to do
        if k in restore_set:
            return None, f'row {k} in {table} is marked both restore and delete'
        entry['deletes'].append({
            'key': k,
            'pk': {c: _norm(lm.get(c)) for c in pk_cols},
            'before': {c: _norm(lm.get(c)) for c in common},
        })

    return entry, None


def _plan_hash(tables):
    canon = json.dumps(tables, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode('utf-8')).hexdigest()


def _build_plan(db, snap, payload):
    """Shared by preview and apply so what was previewed is what runs.
    Returns (plan, error). plan = {'tables': [...], 'hash': ..., 'mode': ...}"""
    mode = payload.get('mode')
    tables = []

    if mode == 'rows':
        schema = payload.get('schema') or ''
        table = payload.get('table') or ''
        if not _SAFE_IDENT_RE.match(table) or not _SAFE_IDENT_RE.match(schema):
            return None, 'invalid table'
        if table in RESTORE_BLOCKED:
            return None, f'table {table} is not restorable from snapshots'
        restore_keys = payload.get('restore') or []
        delete_keys = payload.get('delete') or []
        if not restore_keys and not delete_keys:
            return None, 'nothing selected'
        if not all(isinstance(k, str) for k in restore_keys + delete_keys):
            return None, 'malformed row keys'
        entry, err = _plan_table(db, snap, schema, table,
                                 restore_keys=restore_keys, delete_keys=delete_keys)
        if err:
            return None, err
        tables.append(entry)

    elif mode == 'show':
        try:
            show_id = int(payload.get('show_id'))
        except (TypeError, ValueError):
            return None, 'invalid show_id'
        scan = snap.scan()
        shows_key = next(((s, t) for (s, t) in scan if t == 'shows'), None)
        if shows_key is None:
            return None, 'snapshot contains no shows table'
        schema = shows_key[0]
        entry, err = _plan_table(db, snap, schema, 'shows', where_show_id=show_id)
        if err:
            return None, err
        if not (entry['inserts'] or entry['updates']):
            # Show row unchanged — still fine, children may differ.
            pass
        # Refuse to plan against a show id that exists in neither place.
        snap_has = bool(entry['inserts'] or entry['updates']) or _snapshot_has_show(snap, schema, show_id)
        if not snap_has:
            return None, f'show {show_id} not found in this snapshot'
        tables.append(entry)
        snap_tables = {t for (_, t) in scan}
        live = {t for (_, t) in _live_tables(db)}
        for child in SHOW_CHILD_TABLES:
            if child not in snap_tables or child not in live:
                continue
            centry, cerr = _plan_table(db, snap, schema, child, where_show_id=show_id)
            if cerr:
                return None, f'{child}: {cerr}'
            if centry['inserts'] or centry['updates'] or centry['deletes']:
                tables.append(centry)
    else:
        return None, 'unknown mode'

    total = sum(len(t['inserts']) + len(t['updates']) + len(t['deletes'])
                for t in tables)
    if total == 0:
        return None, 'nothing to change — live data already matches the snapshot'
    plan = {'mode': mode, 'tables': tables, 'hash': _plan_hash(tables),
            'total_changes': total}
    return plan, None


def _snapshot_has_show(snap, schema, show_id):
    cols, rows = snap.read_table(schema, 'shows')
    if not cols or 'id' not in cols:
        return False
    idx = cols.index('id')
    return any(_values_equal(r[idx], show_id) for r in rows)


# ─── Restore apply ────────────────────────────────────────────────────────────

def _apply_plan(db, plan, snap):
    """Execute a plan inside the caller's transaction. Raises on any anomaly
    (caller rolls back). Returns per-table counts for the response."""
    log_audit = _d['log_audit']
    audit_rows_logged = 0
    counts = []

    for t in plan['tables']:
        schema, table, pk_cols = t['schema'], t['table'], t['pk']
        q = _qtable(db, schema, table)
        pk_where = ' AND '.join(f'{_qi(c)} = ?' for c in pk_cols)

        for u in t['updates']:
            set_cols = u['set_columns']
            sql = (f'UPDATE {q} SET '
                   + ', '.join(f'{_qi(c)} = ?' for c in set_cols)
                   + f' WHERE {pk_where}')
            params = [u['row'][c] for c in set_cols] + [u['pk'][c] for c in pk_cols]
            cur = db.execute(sql, params)
            if cur.rowcount != 1:
                raise RuntimeError(
                    f'{table}: update matched {cur.rowcount} rows for key '
                    f'{u["key"]} — data changed underneath, aborting')
            if audit_rows_logged < AUDIT_ROW_CAP:
                log_audit(db, 'SNAPSHOT_ROW_UPDATE', 'snapshot_restore',
                          entity_id=u['key'],
                          show_id=plan.get('show_id'),
                          before=u['before'], after=u['row'],
                          detail=f'{table}: restored from {snap.kind}/{snap.name}')
                audit_rows_logged += 1

        cols = t['columns']
        for ins in t['inserts']:
            col_sql = ', '.join(_qi(c) for c in cols)
            ph = ', '.join('?' for _ in cols)
            db.execute(f'INSERT INTO {q} ({col_sql}) VALUES ({ph})',
                       [ins['row'][c] for c in cols])
            if audit_rows_logged < AUDIT_ROW_CAP:
                log_audit(db, 'SNAPSHOT_ROW_INSERT', 'snapshot_restore',
                          entity_id=ins['key'],
                          show_id=plan.get('show_id'),
                          after=ins['row'],
                          detail=f'{table}: restored from {snap.kind}/{snap.name}')
                audit_rows_logged += 1

        for de in t['deletes']:
            cur = db.execute(f'DELETE FROM {q} WHERE {pk_where}',
                             [de['pk'][c] for c in pk_cols])
            if cur.rowcount != 1:
                raise RuntimeError(
                    f'{table}: delete matched {cur.rowcount} rows for key '
                    f'{de["key"]} — data changed underneath, aborting')
            if audit_rows_logged < AUDIT_ROW_CAP:
                log_audit(db, 'SNAPSHOT_ROW_DELETE', 'snapshot_restore',
                          entity_id=de['key'],
                          show_id=plan.get('show_id'),
                          before=de['before'],
                          detail=f'{table}: removed (row absent in '
                                 f'{snap.kind}/{snap.name})')
                audit_rows_logged += 1

        # Re-sync the id sequence on PostgreSQL so resurrected rows can't
        # collide with future inserts. setval(NULL, ...) is a no-op (strict),
        # so tables without a serial id are safe.
        if db.db_type == 'postgres' and t['inserts'] and 'id' in cols:
            db.execute(
                f"SELECT setval(pg_get_serial_sequence('{schema}.{table}', 'id'), "
                f"GREATEST((SELECT COALESCE(MAX(id), 0) FROM {q}), 1))")

        counts.append({'table': table,
                       'inserted': len(t['inserts']),
                       'updated': len(t['updates']),
                       'deleted': len(t['deletes'])})
    return counts, audit_rows_logged


# ─── Views ────────────────────────────────────────────────────────────────────

def _json_error(msg, status=400):
    return jsonify({'success': False, 'error': msg}), status


def _snapshot_read_error(snap, e):
    return _json_error(f'Could not read {snap.kind}/{snap.name}: {e}.', 422)


def _open_snapshot_or_error():
    if request.method == 'POST':
        payload = request.get_json(silent=True) or {}
        kind, name = payload.get('kind'), payload.get('name')
    else:
        kind, name = request.args.get('kind'), request.args.get('name')
    try:
        return Snapshot(kind or '', name or ''), None
    except (FileNotFoundError, ValueError):
        return None, _json_error('Snapshot not found on this server (snapshots '
                                 'are per-server; it may have been pruned).', 404)


def _page_view():
    db = _d['get_db']()
    try:
        stale = _is_stale_fallback(db)
        live_backend = db.db_type
    finally:
        db.close()
    return render_template(
        'snapshots.html',
        snapshots=list_snapshots(),
        live_backend=live_backend,
        stale_fallback=stale,
        now=datetime.now(),
        user=_d['get_current_user'](),
    )


def _tables_view():
    snap, err = _open_snapshot_or_error()
    if err:
        return err
    db = _d['get_db']()
    try:
        scan = snap.scan()
        live = _live_tables(db)
        live_set = set(live)
        schemas = set(_live_schemas(db)) if db.db_type == 'postgres' else {'main'}
        keys = sorted(set(k for k in scan if k[0] in schemas or snap.backend != db.db_type) | live_set,
                      key=lambda k: (k[1] in SYSTEM_TABLES, k[0], k[1]))
        ok, reason = _restore_allowed(db, snap)
        tables = []
        for (schema, table) in keys:
            meta = scan.get((schema, table))
            in_live = (schema, table) in live_set
            tables.append({
                'schema': schema,
                'table': table,
                'snap_rows': meta['rows'] if meta else None,
                'live_rows': _live_count(db, schema, table) if in_live else None,
                'in_snapshot': meta is not None,
                'in_live': in_live,
                'system': table in SYSTEM_TABLES,
                'restorable': (ok and meta is not None and in_live
                               and table not in RESTORE_BLOCKED),
            })
        warnings = []
        if _is_stale_fallback(db):
            warnings.append('Live connection is a stale SQLite fallback — the '
                            '"live" numbers below are NOT your PostgreSQL data. '
                            'Restore is disabled.')
        if snap.backend != db.db_type:
            warnings.append(f'This snapshot is a {snap.backend} backup but the '
                            f'live backend is {db.db_type} — comparison is '
                            'best-effort and restore is disabled.')
        if not ok and reason and reason not in warnings:
            warnings.append(reason)
        return jsonify({'success': True, 'tables': tables, 'warnings': warnings,
                        'restore_ok': ok})
    except SnapshotReadError as e:
        return _snapshot_read_error(snap, e)
    finally:
        db.close()


def _diff_view():
    snap, err = _open_snapshot_or_error()
    if err:
        return err
    schema = request.args.get('schema') or ''
    table = request.args.get('table') or ''
    if not _SAFE_IDENT_RE.match(schema) or not _SAFE_IDENT_RE.match(table):
        return _json_error('Invalid table.')
    try:
        meta = snap.scan().get((schema, table))
    except SnapshotReadError as e:
        return _snapshot_read_error(snap, e)
    if meta is None:
        return _json_error('Table not found in snapshot.', 404)
    db = _d['get_db']()
    try:
        live_n = _live_count(db, schema, table)
        if (meta['rows'] or 0) + (live_n or 0) > 500_000:
            return _json_error('This table is too large to diff in memory '
                               f'({meta["rows"]:,} snapshot + {live_n:,} live '
                               'rows). Inspect it with SQL tooling instead.')
        snap_cols, snap_rows = snap.read_table(schema, table)
        live_cols = _live_columns(db, schema, table)
        if not live_cols:
            return _json_error('Table not found in live database.', 404)
        _, live_rows = _read_live_table(db, schema, table)
        pk_cols = _pk_for(meta, [c for c in snap_cols if c in live_cols])
        diff = _diff_table(snap_cols, snap_rows, live_cols, live_rows, pk_cols)
        ok, _reason = _restore_allowed(db, snap)
        diff['restorable'] = ok and table not in RESTORE_BLOCKED
        diff['blocked'] = table in RESTORE_BLOCKED
        return jsonify({'success': True, 'diff': diff})
    except SnapshotReadError as e:
        return _snapshot_read_error(snap, e)
    finally:
        db.close()


def _shows_view():
    """Shows present in the snapshot, flagged with live existence — feeds the
    per-show restore picker (includes shows since deleted from live)."""
    snap, err = _open_snapshot_or_error()
    if err:
        return err
    try:
        scan = snap.scan()
        shows_key = next(((s, t) for (s, t) in scan if t == 'shows'), None)
        if shows_key is None:
            return _json_error('Snapshot contains no shows table.', 404)
        cols, rows = snap.read_table(*shows_key)
    except SnapshotReadError as e:
        return _snapshot_read_error(snap, e)
    if not cols:
        return _json_error('Could not read shows from snapshot.', 500)
    idx = {c: i for i, c in enumerate(cols)}
    db = _d['get_db']()
    try:
        live_ids = {str(r['id']) for r in db.execute('SELECT id FROM shows').fetchall()}
    finally:
        db.close()
    shows = []
    for r in rows:
        sid = _norm(r[idx['id']]) if 'id' in idx else None
        shows.append({
            'id': sid,
            'name': _norm(r[idx['name']]) if 'name' in idx else '',
            'show_date': _norm(r[idx['show_date']]) if 'show_date' in idx else '',
            'status': _norm(r[idx['status']]) if 'status' in idx else '',
            'exists_live': sid in live_ids,
        })
    shows.sort(key=lambda s: (s['show_date'] or ''), reverse=True)
    return jsonify({'success': True, 'shows': shows, 'schema': shows_key[0]})


def _preview_summary(plan):
    """Trim a plan to what the confirm dialog needs to show."""
    tables = []
    for t in plan['tables']:
        tables.append({
            'table': t['table'],
            'inserts': len(t['inserts']),
            'updates': len(t['updates']),
            'deletes': len(t['deletes']),
            'sample_inserts': [
                {c: _trunc(_norm(v)) for c, v in i['row'].items()}
                for i in t['inserts'][:PREVIEW_SAMPLE_CAP]],
            'sample_updates': [{
                'pk': u['pk'],
                'fields': [{'column': c, 'live': _trunc(u['before'].get(c)),
                            'snapshot': _trunc(_norm(u['row'].get(c)))}
                           for c in u['set_columns']],
            } for u in t['updates'][:PREVIEW_SAMPLE_CAP]],
            'sample_deletes': [
                {c: _trunc(v) for c, v in d['before'].items()}
                for d in t['deletes'][:PREVIEW_SAMPLE_CAP]],
        })
    warnings = []
    for t in plan['tables']:
        if t['table'] == 'shows' and t['deletes']:
            warnings.append('This plan DELETES a shows row — all of its child '
                            'data cascades away with it.')
        if t['table'] == 'show_attachments' and t['inserts']:
            warnings.append('Restoring show_attachments rows restores the '
                            'database records only — if the underlying files '
                            'were deleted from disk, those links will be broken.')
    return {'mode': plan['mode'], 'hash': plan['hash'],
            'total_changes': plan['total_changes'], 'tables': tables,
            'warnings': warnings}


def _preview_view():
    payload = request.get_json(silent=True) or {}
    snap, err = _open_snapshot_or_error()
    if err:
        return err
    db = _d['get_db']()
    try:
        ok, reason = _restore_allowed(db, snap)
        if not ok:
            return _json_error(reason, 409)
        plan, perr = _build_plan(db, snap, payload)
        if perr:
            return _json_error(perr)
        return jsonify({'success': True, 'preview': _preview_summary(plan)})
    except SnapshotReadError as e:
        return _snapshot_read_error(snap, e)
    finally:
        db.close()


def _apply_view():
    payload = request.get_json(silent=True) or {}
    expected_hash = payload.get('hash') or ''
    snap, err = _open_snapshot_or_error()
    if err:
        return err
    db = _d['get_db']()
    try:
        ok, reason = _restore_allowed(db, snap)
        if not ok:
            return _json_error(reason, 409)
        plan, perr = _build_plan(db, snap, payload)
        if perr:
            return _json_error(perr)
        if plan['hash'] != expected_hash:
            return _json_error('The data changed after you previewed — nothing '
                               'was written. Re-run the preview and try again.',
                               409)
        if payload.get('mode') == 'show':
            plan['show_id'] = payload.get('show_id')
        try:
            counts, audited = _apply_plan(db, plan, snap)
            _d['log_audit'](
                db, 'SNAPSHOT_RESTORE', 'snapshot_restore',
                entity_id=f'{snap.kind}/{snap.name}',
                show_id=plan.get('show_id'),
                detail=json.dumps({
                    'mode': plan['mode'],
                    'snapshot': f'{snap.kind}/{snap.name}',
                    'counts': counts,
                    'rows_audited': audited,
                    'rows_total': plan['total_changes'],
                }, default=str))
            db.commit()
        except Exception as e:
            db.rollback()
            _d['app'].logger.exception('snapshot restore failed')
            return _json_error(f'Restore failed and was rolled back: {e}', 500)
        syslog = _d.get('syslog_logger')
        if syslog:
            syslog.info(f'SNAPSHOT_RESTORE mode={plan["mode"]} '
                        f'snapshot={snap.kind}/{snap.name} '
                        f'changes={plan["total_changes"]}')
        return jsonify({'success': True, 'counts': counts,
                        'total_changes': plan['total_changes']})
    except SnapshotReadError as e:
        return _snapshot_read_error(snap, e)
    finally:
        db.close()


# ─── Registration ─────────────────────────────────────────────────────────────

def register(app, **deps):
    """
    Wire the module into the Flask app. `deps` must provide:
      get_db, get_current_user, admin_required, log_audit, db_adapter,
      DATABASE, BACKUP_DIR — and optionally syslog_logger.
    Called once from app.py; everything else in this file is self-contained.
    """
    _d.update(deps, app=app)
    admin = deps['admin_required']

    app.add_url_rule('/admin/snapshots', 'snapshots_page', admin(_page_view))
    app.add_url_rule('/admin/snapshots/api/tables', 'snapshots_tables',
                     admin(_tables_view))
    app.add_url_rule('/admin/snapshots/api/diff', 'snapshots_diff',
                     admin(_diff_view))
    app.add_url_rule('/admin/snapshots/api/shows', 'snapshots_shows',
                     admin(_shows_view))
    app.add_url_rule('/admin/snapshots/api/preview', 'snapshots_preview',
                     admin(_preview_view), methods=['POST'])
    app.add_url_rule('/admin/snapshots/api/apply', 'snapshots_apply',
                     admin(_apply_view), methods=['POST'])
