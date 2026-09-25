# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
File storage redundancy — S3 (SeaweedFS) <-> PostgreSQL (3.1.0).

Every file the app stores lives on a row of one of five tables, each of which
already has BOTH an inline BYTEA column and an S3 key column:

  kind           table                   bytes column  S3 key column
  attachments    show_attachments        file_data     s3_key
  exports        export_log              pdf_data      s3_key
  asset_photos   asset_types             photo         photo_s3_key
  rentals        show_external_rentals   pdf_data      s3_key
  pdf_templates  pdf_templates           pdf_data      s3_key

The row itself carries the show association (show_id), the file type
(mime_type / photo_mime / always-PDF) and name, and — new in 3.1.0 — a
`content_sha256` of the file's (uncompressed) content. A file can therefore
exist in S3, in the database, or in BOTH. This module owns the rules for that:

  * Read order (`file_read_preference` app_setting, 's3' default | 'db'):
    `read_bytes()` tries the preferred copy first and falls back to the other
    one, logging FILE_READ_FALLBACK — so with both copies present an S3 outage
    no longer takes file downloads down with it.
  * Dual-write (`file_dual_write_enabled`, default '0'): when on, uploads and
    generated PDFs keep their database copy after the S3 upload succeeds
    instead of clearing it (`keep_db_copy()`, consulted by app.py's write
    paths). Off = exactly the pre-3.1.0 behavior.
  * Migration tool (Settings → System → Database → File Redundancy): copies
    files S3 → DB or DB → S3, or verifies both copies, in a background thread.
    COPY ONLY — a run never deletes or clears the source copy. Re-runs are
    duplicate-aware: a row whose target copy already exists is skipped, every
    copy is checked against SHA-256 (DB→S3 reads the object back), and a
    conditional UPDATE refuses to write if the row changed mid-copy.
    Progress lives in `file_migration_runs` so any worker/instance can report
    it; one run at a time cluster-wide (PostgreSQL advisory lock, released
    automatically if the worker dies — the run then reads as 'interrupted'
    and simply re-running resumes, since finished rows are skipped).

Archived show attachments are held gzip-compressed in the database
(`is_compressed`, 2.36.0): an S3 → DB copy of an archived row is stored
compressed to match, and a DB → S3 copy always uploads the DECOMPRESSED bytes
(compressed bytes must never reach S3 as-is). SHA-256 is always over the
uncompressed content, so the two copies compare equal.

Like s3_storage, this module never imports app.py: app.py wires it with one
`register(app, **deps)` call. Syslog events: FILE_READ_FALLBACK,
FILE_READ_FAILED, FILE_STORAGE_SETTINGS, FILE_MIGRATE_START, FILE_MIGRATE_ITEM,
FILE_MIGRATE_PROGRESS, FILE_MIGRATE_DONE, FILE_MIGRATE_CANCEL.
"""

import gzip
import hashlib
import json
import threading
import time
from collections import OrderedDict

from flask import jsonify, request, session

import s3_storage

_d = {}   # dependencies injected by register()

# ─── File kinds ────────────────────────────────────────────────────────────────
# `meta` is the SELECT list used by the migration engine (never the blob): it
# must yield show_id, name, mime, is_compressed, archived for every kind.

KINDS = OrderedDict([
    ('attachments', {
        'label': 'Show attachments',
        'table': 'show_attachments', 'blob': 'file_data', 'key': 's3_key',
        'compressible': True,
        'meta': ("show_id, filename AS name, "
                 "COALESCE(NULLIF(mime_type,''),'application/octet-stream') AS mime, "
                 "COALESCE(is_compressed,0) AS is_compressed, "
                 "(deleted_at IS NOT NULL) AS archived"),
        's3_key_for': lambda r: f"attachments/{r['show_id']}/{r['id']}/{r['name'] or 'file'}",
    }),
    ('exports', {
        'label': 'Exported PDF versions',
        'table': 'export_log', 'blob': 'pdf_data', 'key': 's3_key',
        'compressible': False,
        'meta': ("show_id, export_type, version, "
                 "COALESCE(NULLIF(filename,''), export_type || '_v' || CAST(version AS TEXT) || '.pdf') AS name, "
                 "'application/pdf' AS mime, 0 AS is_compressed, FALSE AS archived"),
        's3_key_for': lambda r: (f"exports/{r['show_id'] if r['show_id'] is not None else 'orphan'}/"
                                 f"{r['export_type'] or 'export'}/v{r['version']}.pdf"),
    }),
    ('asset_photos', {
        'label': 'Asset type photos',
        'table': 'asset_types', 'blob': 'photo', 'key': 'photo_s3_key',
        'compressible': False,
        'meta': ("NULL::INTEGER AS show_id, name, "
                 "COALESCE(NULLIF(photo_mime,''),'image/jpeg') AS mime, "
                 "0 AS is_compressed, FALSE AS archived"),
        's3_key_for': lambda r: f"asset-photos/{r['id']}",
    }),
    ('rentals', {
        'label': 'External rental PDFs',
        'table': 'show_external_rentals', 'blob': 'pdf_data', 'key': 's3_key',
        'compressible': False,
        'meta': ("show_id, COALESCE(NULLIF(pdf_filename,''),'rental.pdf') AS name, "
                 "'application/pdf' AS mime, 0 AS is_compressed, FALSE AS archived"),
        's3_key_for': lambda r: f"external-rentals/{r['id']}/{r['name'] or 'rental.pdf'}",
    }),
    ('pdf_templates', {
        'label': 'PDF form templates',
        'table': 'pdf_templates', 'blob': 'pdf_data', 'key': 's3_key',
        'compressible': False,
        'meta': ("NULL::INTEGER AS show_id, name, 'application/pdf' AS mime, "
                 "0 AS is_compressed, FALSE AS archived"),
        's3_key_for': lambda r: f"pdf-templates/{r['id']}.pdf",
    }),
])

DIRECTIONS = {
    's3_to_db': 'Copy S3 → database',
    'db_to_s3': 'Copy database → S3',
    'verify':   'Verify both copies',
}

MIGRATION_LOCK_KEY = 3217301   # pg advisory lock id: one migration run cluster-wide
_BATCH = 100                   # metadata rows fetched per page (never the blobs)
_FLUSH_EVERY_S = 1.0           # progress row update cadence
_PROGRESS_LOG_EVERY = 100      # FILE_MIGRATE_PROGRESS syslog cadence (items)
_MAX_ERRORS = 200              # errors kept on the run row (all go to syslog)


class FileReadError(Exception):
    """Base for read_bytes() failures."""


class FileMissing(FileReadError, LookupError):
    """The row has no stored copy anywhere (→ 404)."""


class FileUnavailable(FileReadError, RuntimeError):
    """Copies exist but none could be read right now (→ 503)."""


# ─── Settings (cached briefly — read_bytes() is on every download path) ──────

_SETTINGS_TTL = 10.0
_settings = {'ts': 0.0, 'dual': False, 'pref': 's3'}


def _log():
    return _d['syslog_logger']


def clear_settings_cache():
    _settings['ts'] = 0.0


def _load_settings():
    now = time.monotonic()
    if now - _settings['ts'] < _SETTINGS_TTL:
        return _settings
    get = _d['get_app_setting']
    _settings['dual'] = str(get('file_dual_write_enabled', '0')).strip() == '1'
    pref = str(get('file_read_preference', 's3')).strip().lower()
    _settings['pref'] = pref if pref in ('s3', 'db') else 's3'
    _settings['ts'] = now
    return _settings


def keep_db_copy():
    """True when dual-write is on: write paths keep the database copy after a
    successful S3 upload instead of clearing it."""
    try:
        return _load_settings()['dual']
    except Exception:
        return False


def read_preference():
    try:
        return _load_settings()['pref']
    except Exception:
        return 's3'


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


# ─── Reading ───────────────────────────────────────────────────────────────────

def _rget(row, col):
    try:
        return row.get(col)
    except AttributeError:
        try:
            return row[col]
        except (KeyError, IndexError):
            return None


_ABSENT = object()


def in_db_col(kind, alias=''):
    """SELECT-list fragment to use INSTEAD of a kind's BYTEA column when the
    row is headed for read_bytes(): `in_db` says whether a database copy
    exists without fetching it (octet_length() of a TOASTed value reads only
    its header). read_bytes() then loads the bytes only if it actually serves
    the database copy — with the default 's3' preference and dual-write on,
    selecting the blob up front meant pulling every file out of PostgreSQL
    and then downloading it from S3 anyway."""
    p = f'{alias}.' if alias else ''
    return f"(COALESCE(octet_length({p}{KINDS[kind]['blob']}), 0) > 0) AS in_db"


def _load_db_copy_by_id(spec, rid):
    """Lazy read of one row's database copy on its own connection (the
    caller has usually closed its own by now). Decompressed bytes or None."""
    db = _d['get_db']()
    try:
        return _load_db_copy(db, spec, rid)[1]
    finally:
        db.close()


def read_bytes(kind, row):
    """Return the file bytes for a row of KINDS[kind], honouring the admin's
    read preference and falling back to the other copy when the preferred one
    is missing or unreadable. The row must include the kind's key column and
    `id`, plus EITHER the bytes column (and is_compressed for attachments,
    when it can be archived) OR the `in_db` flag from in_db_col() — then the
    database copy is fetched only if it is the copy served.

    Raises FileMissing (no copy stored) or FileUnavailable (copies exist but
    every one failed)."""
    spec = KINDS[kind]
    key = _rget(row, spec['key'])
    try:
        blob = row[spec['blob']]
    except (KeyError, IndexError):
        blob = _ABSENT
    lazy = blob is _ABSENT
    sides = []
    if key:
        sides.append('s3')
    if (bool(_rget(row, 'in_db')) if lazy
            else blob is not None and len(blob) > 0):
        sides.append('db')
    if not sides:
        raise FileMissing(f'{kind} id={_rget(row, "id")} has no stored copy')
    if read_preference() == 'db':
        sides.reverse()
    errors = []
    for side in sides:
        try:
            if side == 's3':
                data = s3_storage.download_file(key)
            elif lazy:
                data = _load_db_copy_by_id(spec, _rget(row, 'id'))
                if not data:
                    raise FileReadError('database copy was removed since the row was read')
            else:
                data = bytes(blob)
                if _rget(row, 'is_compressed'):
                    data = gzip.decompress(data)
            if errors:
                _log().warning(
                    f"FILE_READ_FALLBACK kind={kind} id={_rget(row, 'id')} "
                    f"served_from={side} failed={errors[0][0]} error={errors[0][1]}")
            return data
        except Exception as e:
            errors.append((side, e))
    _log().error(
        f"FILE_READ_FAILED kind={kind} id={_rget(row, 'id')} "
        + ' '.join(f'{s}_error={e}' for s, e in errors))
    raise FileUnavailable('; '.join(f'{s}: {e}' for s, e in errors))


# ─── Migration engine ──────────────────────────────────────────────────────────

def _candidate_where(spec, direction):
    blob, key = spec['blob'], spec['key']
    if direction == 's3_to_db':
        return f'{key} IS NOT NULL'
    if direction == 'db_to_s3':
        return f'{blob} IS NOT NULL'
    return f'({key} IS NOT NULL OR {blob} IS NOT NULL)'


def _iter_candidates(db, kind, direction):
    """Yield metadata rows (no blobs) in id order, a page at a time, so a
    run over thousands of files never holds more than one file in memory."""
    spec = KINDS[kind]
    where = _candidate_where(spec, direction)
    last_id = 0
    while True:
        rows = db.execute(
            f"SELECT id, {spec['meta']}, ({spec['blob']} IS NOT NULL) AS in_db, "
            f"{spec['key']} AS s3_key, content_sha256 FROM {spec['table']} "
            f"WHERE id > %s AND {where} ORDER BY id LIMIT %s",
            (last_id, _BATCH)).fetchall()
        db.commit()   # don't sit idle-in-transaction between pages
        if not rows:
            return
        for r in rows:
            yield dict(r)
        last_id = rows[-1]['id']


def _load_db_copy(db, spec, rid):
    """(stored_bytes, raw_bytes) of a row's database copy, or (None, None)."""
    comp = 'COALESCE(is_compressed,0)' if spec['compressible'] else '0'
    row = db.execute(
        f"SELECT {spec['blob']} AS blob, {comp} AS is_compressed "
        f"FROM {spec['table']} WHERE id=%s", (rid,)).fetchone()
    if not row or row['blob'] is None:
        return None, None
    stored = bytes(row['blob'])
    raw = gzip.decompress(stored) if row['is_compressed'] else stored
    return stored, raw


def _record_sha(db, spec, rid, sha):
    db.execute(f"UPDATE {spec['table']} SET content_sha256=%s "
               f"WHERE id=%s AND content_sha256 IS NULL", (sha, rid))
    db.commit()


class _Run:
    """Counters + persistence for one migration run."""

    def __init__(self, db, run_id, direction, kinds):
        self.db = db
        self.id = run_id
        self.direction = direction
        self.kinds = kinds
        self.total = 0
        self.c = {'processed': 0, 'copied': 0, 'skipped': 0, 'verified': 0,
                  'mismatched': 0, 'failed': 0, 'bytes': 0}
        self.per_kind = {k: {'processed': 0, 'copied': 0, 'skipped': 0, 'verified': 0,
                             'mismatched': 0, 'failed': 0, 'bytes': 0, 'single_copy': 0}
                         for k in kinds}
        self.errors = []
        self.current = ''
        self.cancel = False
        self._last_flush = 0.0
        self.started = time.monotonic()

    def bump(self, kind, field, n=1):
        self.c[field] = self.c.get(field, 0) + n
        self.per_kind[kind][field] = self.per_kind[kind].get(field, 0) + n

    def problem(self, kind, row, field, msg):
        """Record a failed/mismatched item on the run and in syslog."""
        self.bump(kind, field)
        entry = {'kind': kind, 'id': row['id'], 'show_id': row.get('show_id'),
                 'name': row.get('name') or '', 'outcome': field, 'error': str(msg)[:500]}
        if len(self.errors) < _MAX_ERRORS:
            self.errors.append(entry)
        _log().error(
            f"FILE_MIGRATE_ITEM run={self.id} kind={kind} id={row['id']} "
            f"show_id={row.get('show_id')} action={field} error={entry['error']}")

    def flush(self, force=False, status=None, message=None):
        now = time.monotonic()
        if not force and now - self._last_flush < _FLUSH_EVERY_S:
            return
        self._last_flush = now
        sets = ("total=%s, processed=%s, copied=%s, skipped=%s, verified=%s, "
                "mismatched=%s, failed=%s, bytes_copied=%s, current_item=%s, "
                "per_kind_json=%s, errors_json=%s, updated_at=NOW()")
        params = [self.total, self.c['processed'], self.c['copied'], self.c['skipped'],
                  self.c['verified'], self.c['mismatched'], self.c['failed'],
                  self.c['bytes'], self.current[:300],
                  json.dumps(self.per_kind), json.dumps(self.errors, default=str)]
        if status:
            sets += ", status=%s, finished_at=NOW()"
            params.append(status)
        if message is not None:
            sets += ", message=%s"
            params.append(message[:1000])
        params.append(self.id)
        cur = self.db.execute(
            f"UPDATE file_migration_runs SET {sets} WHERE id=%s RETURNING cancel_requested",
            tuple(params))
        row = cur.fetchone()
        self.db.commit()
        if row and row['cancel_requested']:
            self.cancel = True


def _copy_s3_to_db(db, kind, spec, r, run):
    if r['in_db']:
        if not r['content_sha256']:
            _stored, raw = _load_db_copy(db, spec, r['id'])
            if raw is not None:
                _record_sha(db, spec, r['id'], sha256_hex(raw))
        run.bump(kind, 'skipped')   # already in the database — duplicate-aware re-run
        return
    data = s3_storage.download_file(r['s3_key'])
    sha = sha256_hex(data)
    if r['content_sha256'] and r['content_sha256'] != sha:
        run.problem(kind, r, 'mismatched',
                    f'S3 object {r["s3_key"]} does not match the recorded SHA-256 — not copied')
        return
    stored, compressed = data, 0
    if spec['compressible'] and r['archived']:
        # Archived attachments are held gzip-compressed in the DB (2.36.0).
        packed = gzip.compress(data)
        if len(packed) < len(data):
            stored, compressed = packed, 1
    sets = f"{spec['blob']}=%s, content_sha256=%s"
    params = [stored, sha]
    if spec['compressible']:
        sets += ", is_compressed=%s"
        params.append(compressed)
    params += [r['id'], r['s3_key']]
    # Conditional: if a user replaced/removed the file mid-copy, write nothing.
    cur = db.execute(
        f"UPDATE {spec['table']} SET {sets} "
        f"WHERE id=%s AND {spec['key']}=%s AND {spec['blob']} IS NULL", tuple(params))
    if cur.rowcount != 1:
        db.rollback()
        run.bump(kind, 'skipped')
        return
    chk = db.execute(f"SELECT octet_length({spec['blob']}) AS n FROM {spec['table']} WHERE id=%s",
                     (r['id'],)).fetchone()
    if not chk or chk['n'] != len(stored):
        db.rollback()
        run.problem(kind, r, 'failed', 'database copy failed its length check — rolled back')
        return
    db.commit()
    run.bump(kind, 'copied')
    run.bump(kind, 'bytes', len(data))
    _log().info(
        f"FILE_MIGRATE_ITEM run={run.id} kind={kind} id={r['id']} show_id={r.get('show_id')} "
        f"action=copied direction=s3_to_db bytes={len(data)} sha256={sha[:16]}")


def _copy_db_to_s3(db, kind, spec, r, run):
    if r['s3_key']:
        if not r['content_sha256']:
            _stored, raw = _load_db_copy(db, spec, r['id'])
            if raw is not None:
                _record_sha(db, spec, r['id'], sha256_hex(raw))
        run.bump(kind, 'skipped')   # already in S3 — duplicate-aware re-run
        return
    stored, raw = _load_db_copy(db, spec, r['id'])
    db.commit()
    if raw is None:
        run.bump(kind, 'skipped')   # cleared since the page was listed
        return
    sha = sha256_hex(raw)
    if r['content_sha256'] and r['content_sha256'] != sha:
        run.problem(kind, r, 'mismatched',
                    'database copy does not match the recorded SHA-256 — not copied')
        return
    key = spec['s3_key_for'](r)
    s3_storage.upload_file(key, raw, r.get('mime') or 'application/octet-stream')
    back = s3_storage.download_file(key)
    if sha256_hex(back) != sha:
        run.problem(kind, r, 'failed', f'S3 read-back of {key} did not match — row left DB-only')
        return
    stored_md5 = hashlib.md5(stored).hexdigest()
    cur = db.execute(
        f"UPDATE {spec['table']} SET {spec['key']}=%s, content_sha256=%s "
        f"WHERE id=%s AND {spec['key']} IS NULL AND md5({spec['blob']})=%s",
        (key, sha, r['id'], stored_md5))
    if cur.rowcount != 1:
        db.rollback()
        run.bump(kind, 'skipped')   # row changed mid-copy; the next run re-checks it
        return
    db.commit()
    run.bump(kind, 'copied')
    run.bump(kind, 'bytes', len(raw))
    _log().info(
        f"FILE_MIGRATE_ITEM run={run.id} kind={kind} id={r['id']} show_id={r.get('show_id')} "
        f"action=copied direction=db_to_s3 key={key} bytes={len(raw)} sha256={sha[:16]}")


def _verify(db, kind, spec, r, run):
    sha_db = sha_s3 = None
    unreadable = []
    if r['in_db']:
        try:
            _stored, raw = _load_db_copy(db, spec, r['id'])
            db.commit()
            sha_db = sha256_hex(raw) if raw is not None else None
        except Exception as e:
            db.rollback()
            unreadable.append(f'database copy unreadable: {e}')
    if r['s3_key']:
        try:
            sha_s3 = sha256_hex(s3_storage.download_file(r['s3_key']))
        except Exception as e:
            unreadable.append(f'S3 copy {r["s3_key"]} unreadable: {e}')
    if unreadable:
        run.problem(kind, r, 'failed', '; '.join(unreadable))
        return
    if sha_db and sha_s3 and sha_db != sha_s3:
        run.problem(kind, r, 'mismatched', 'S3 and database copies differ')
        return
    actual = sha_db or sha_s3
    if r['content_sha256'] and actual and r['content_sha256'] != actual:
        run.problem(kind, r, 'mismatched', 'content differs from the recorded SHA-256')
        return
    if actual and not r['content_sha256']:
        _record_sha(db, spec, r['id'], actual)
    run.bump(kind, 'verified')
    if not (sha_db and sha_s3):
        run.per_kind[kind]['single_copy'] += 1


_HANDLERS = {'s3_to_db': _copy_s3_to_db, 'db_to_s3': _copy_db_to_s3, 'verify': _verify}


def _run_migration(run, lock_db):
    db = lock_db
    status, message = 'completed', ''
    try:
        run.total = sum(
            db.execute(f"SELECT COUNT(*) AS n FROM {KINDS[k]['table']} "
                       f"WHERE {_candidate_where(KINDS[k], run.direction)}").fetchone()['n']
            for k in run.kinds)
        db.commit()
        run.flush(force=True)
        _log().info(f"FILE_MIGRATE_PROGRESS run={run.id} phase=counted total={run.total}")
        handler = _HANDLERS[run.direction]
        for kind in run.kinds:
            spec = KINDS[kind]
            for r in _iter_candidates(db, kind, run.direction):
                if run.cancel:
                    break
                run.current = f"{spec['label']} #{r['id']} {r.get('name') or ''}".strip()
                try:
                    handler(db, kind, spec, r, run)
                except Exception as e:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    run.problem(kind, r, 'failed', e)
                run.bump(kind, 'processed')
                if run.c['processed'] % _PROGRESS_LOG_EVERY == 0:
                    _log().info(
                        f"FILE_MIGRATE_PROGRESS run={run.id} processed={run.c['processed']}/{run.total} "
                        f"copied={run.c['copied']} skipped={run.c['skipped']} "
                        f"verified={run.c['verified']} mismatched={run.c['mismatched']} "
                        f"failed={run.c['failed']}")
                run.flush()
            if run.cancel:
                break
        if run.cancel:
            status, message = 'cancelled', 'Cancelled by an admin; re-run to continue — finished rows are skipped.'
    except Exception as e:
        status, message = 'failed', f'Run aborted: {e}'
        _log().error(f"FILE_MIGRATE_ITEM run={run.id} action=aborted error={e}")
    finally:
        run.current = ''
        elapsed = time.monotonic() - run.started
        try:
            run.flush(force=True, status=status, message=message)
        except Exception as e:
            # Connection lost mid-run — record the outcome on a fresh one.
            _log().error(f"FILE_MIGRATE_ITEM run={run.id} action=final_flush_failed error={e}")
            try:
                d2 = _d['get_db']()
                try:
                    d2.execute("UPDATE file_migration_runs SET status=%s, message=%s, "
                               "finished_at=NOW(), updated_at=NOW() WHERE id=%s",
                               ('failed', (message or str(e))[:1000], run.id))
                    d2.commit()
                finally:
                    d2.close()
            except Exception:
                pass
        _log().info(
            f"FILE_MIGRATE_DONE run={run.id} status={status} direction={run.direction} "
            f"processed={run.c['processed']}/{run.total} copied={run.c['copied']} "
            f"skipped={run.c['skipped']} verified={run.c['verified']} "
            f"mismatched={run.c['mismatched']} failed={run.c['failed']} "
            f"bytes={run.c['bytes']} elapsed={elapsed:.1f}s")
        try:
            lock_db.execute('SELECT pg_advisory_unlock(%s)', (MIGRATION_LOCK_KEY,))
            lock_db.commit()
        except Exception:
            pass
        try:
            lock_db.close()
        except Exception:
            pass


def _lock_is_held(db):
    """True while some session (any worker, any instance) runs a migration.
    Read-only pg_locks probe — never takes the lock itself."""
    row = db.execute(
        "SELECT 1 FROM pg_locks WHERE locktype='advisory' AND granted "
        "AND database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
        "AND classid=0 AND objid=%s AND objsubid=1", (MIGRATION_LOCK_KEY,)).fetchone()
    return bool(row)


def start_migration(direction, kinds, user_id=None, username='', instance=''):
    """Start a run in a background thread. Returns (run_id, None) or
    (None, error_message) when one is already running."""
    lock_db = _d['get_db']()
    try:
        got = lock_db.execute('SELECT pg_try_advisory_lock(%s) AS ok',
                              (MIGRATION_LOCK_KEY,)).fetchone()['ok']
        lock_db.commit()
        if not got:
            lock_db.close()
            return None, 'A file migration is already running.'
        # We hold the lock, so any 'running' row is left over from a dead worker.
        lock_db.execute("UPDATE file_migration_runs SET status='interrupted', "
                        "finished_at=COALESCE(finished_at, NOW()), "
                        "message='Worker stopped mid-run (restart/deploy); re-run to resume.' "
                        "WHERE status='running'")
        run_id = lock_db.execute(
            "INSERT INTO file_migration_runs (direction, kinds, status, started_by, "
            "started_by_name, instance) VALUES (%s,%s,'running',%s,%s,%s) RETURNING id",
            (direction, ','.join(kinds), user_id, username or '', instance or '')
        ).fetchone()['id']
        lock_db.commit()
    except Exception:
        try:
            lock_db.execute('SELECT pg_advisory_unlock(%s)', (MIGRATION_LOCK_KEY,))
            lock_db.commit()
        except Exception:
            pass
        lock_db.close()
        raise
    _log().info(
        f"FILE_MIGRATE_START run={run_id} direction={direction} kinds={','.join(kinds)} "
        f"by={username or user_id}")
    run = _Run(lock_db, run_id, direction, kinds)
    threading.Thread(target=_run_migration, args=(run, lock_db),
                     name=f'file-migrate-{run_id}', daemon=True).start()
    return run_id, None


# ─── Status helpers ────────────────────────────────────────────────────────────

def storage_counts(db):
    out = []
    for k, spec in KINDS.items():
        b, key = spec['blob'], spec['key']
        row = db.execute(f"""
            SELECT COUNT(*) FILTER (WHERE {b} IS NOT NULL OR {key} IS NOT NULL) AS total,
                   COUNT(*) FILTER (WHERE {key} IS NOT NULL AND {b} IS NULL)     AS s3_only,
                   COUNT(*) FILTER (WHERE {key} IS NULL AND {b} IS NOT NULL)     AS db_only,
                   COUNT(*) FILTER (WHERE {key} IS NOT NULL AND {b} IS NOT NULL) AS both_copies,
                   COUNT(*) FILTER (WHERE content_sha256 IS NOT NULL
                                    AND ({b} IS NOT NULL OR {key} IS NOT NULL))  AS hashed,
                   COALESCE(SUM(octet_length({b})), 0)                           AS db_bytes
            FROM {spec['table']}
        """).fetchone()
        out.append({'key': k, 'label': spec['label'],
                    **{c: int(row[c] or 0) for c in
                       ('total', 's3_only', 'db_only', 'both_copies', 'hashed', 'db_bytes')}})
    return out


def _iso(v):
    return v.isoformat() if hasattr(v, 'isoformat') else v


def _run_dict(row, full=False):
    d = {c: _iso(row[c]) for c in (
        'id', 'direction', 'kinds', 'status', 'started_by_name', 'instance',
        'started_at', 'updated_at', 'finished_at', 'total', 'processed', 'copied',
        'skipped', 'verified', 'mismatched', 'failed', 'bytes_copied',
        'current_item', 'cancel_requested', 'message')}
    d['direction_label'] = DIRECTIONS.get(d['direction'], d['direction'])
    for col, dflt in (('per_kind_json', {}), ('errors_json', [])):
        try:
            val = json.loads(row[col] or '')
        except Exception:
            val = dflt
        d[col[:-5]] = val if (full or col == 'per_kind_json') else None
    d['error_count'] = len(json.loads(row['errors_json'] or '[]')) if row['errors_json'] else 0
    return d


def _reconcile_dead_runs(db):
    """A 'running' row whose lock nobody holds belongs to a dead worker."""
    if db.execute("SELECT 1 FROM file_migration_runs WHERE status='running' LIMIT 1").fetchone() \
            and not _lock_is_held(db):
        db.execute("UPDATE file_migration_runs SET status='interrupted', "
                   "finished_at=COALESCE(finished_at, NOW()), "
                   "message='Worker stopped mid-run (restart/deploy); re-run to resume.' "
                   "WHERE status='running'")
        db.commit()


# ─── Routes ────────────────────────────────────────────────────────────────────

def _bool(v):
    return v is True or str(v).strip().lower() in ('1', 'true', 'yes', 'on')


def register(app, **deps):
    """Wire the module into the Flask app. Required deps: get_db,
    get_app_setting, admin_required, log_audit, syslog_logger, instance_id."""
    _d.update(deps)
    admin_required = deps['admin_required']

    @app.route('/settings/file-storage', methods=['GET'])
    @admin_required
    def file_storage_status():
        db = _d['get_db']()
        try:
            _reconcile_dead_runs(db)
            counts = storage_counts(db)
            runs = db.execute("SELECT * FROM file_migration_runs "
                              "ORDER BY id DESC LIMIT 10").fetchall()
        finally:
            db.close()
        clear_settings_cache()
        st = _load_settings()
        run_list = [_run_dict(r) for r in runs]
        return jsonify({
            'dual_write': st['dual'],
            'read_preference': st['pref'],
            's3_configured': s3_storage.is_configured(),
            'kinds': counts,
            'directions': DIRECTIONS,
            'running': next((r for r in run_list if r['status'] == 'running'), None),
            'runs': run_list,
        })

    @app.route('/settings/file-storage', methods=['POST'])
    @admin_required
    def file_storage_save():
        data = request.get_json(force=True) or {}
        dual = '1' if _bool(data.get('dual_write')) else '0'
        pref = str(data.get('read_preference') or 's3').strip().lower()
        if pref not in ('s3', 'db'):
            return jsonify({'success': False, 'error': "read_preference must be 's3' or 'db'."}), 400
        db = _d['get_db']()
        try:
            for k, v in (('file_dual_write_enabled', dual), ('file_read_preference', pref)):
                db.execute('INSERT INTO app_settings (key, value) VALUES (%s,%s) '
                           'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value', (k, v))
            _d['log_audit'](db, 'SETTINGS_CHANGE', 'setting', None,
                            detail=f'file_storage dual_write={dual} read_preference={pref}')
            db.commit()
        finally:
            db.close()
        clear_settings_cache()
        _log().info(f"FILE_STORAGE_SETTINGS dual_write={dual} read_preference={pref} "
                    f"by={session.get('username')}")
        return jsonify({'success': True})

    @app.route('/settings/file-storage/migrate', methods=['POST'])
    @admin_required
    def file_storage_migrate():
        data = request.get_json(force=True) or {}
        direction = str(data.get('direction') or '')
        if direction not in DIRECTIONS:
            return jsonify({'success': False, 'error': 'Unknown direction.'}), 400
        wanted = data.get('kinds')
        if not isinstance(wanted, list) or not wanted:
            wanted = list(KINDS)
        kinds = [k for k in KINDS if k in wanted]
        if not kinds:
            return jsonify({'success': False, 'error': 'Pick at least one file type.'}), 400
        if not s3_storage.is_configured():
            return jsonify({'success': False,
                            'error': 'S3 storage is not configured — nothing to copy to or from.'}), 400
        run_id, err = start_migration(direction, kinds, user_id=session.get('user_id'),
                                      username=session.get('username') or '',
                                      instance=_d.get('instance_id') or '')
        if err:
            return jsonify({'success': False, 'error': err}), 409
        db = _d['get_db']()
        try:
            _d['log_audit'](db, 'FILE_MIGRATE', 'file_migration', run_id,
                            detail=f'{direction} kinds={",".join(kinds)}')
            db.commit()
        finally:
            db.close()
        return jsonify({'success': True, 'run_id': run_id})

    @app.route('/settings/file-storage/runs/<int:run_id>', methods=['GET'])
    @admin_required
    def file_storage_run(run_id):
        db = _d['get_db']()
        try:
            _reconcile_dead_runs(db)
            row = db.execute('SELECT * FROM file_migration_runs WHERE id=%s', (run_id,)).fetchone()
        finally:
            db.close()
        if not row:
            return jsonify({'error': 'Not found'}), 404
        return jsonify(_run_dict(row, full=True))

    @app.route('/settings/file-storage/runs/<int:run_id>/cancel', methods=['POST'])
    @admin_required
    def file_storage_cancel(run_id):
        db = _d['get_db']()
        try:
            cur = db.execute("UPDATE file_migration_runs SET cancel_requested=1 "
                             "WHERE id=%s AND status='running'", (run_id,))
            db.commit()
            n = cur.rowcount
        finally:
            db.close()
        if n:
            _log().info(f"FILE_MIGRATE_CANCEL run={run_id} by={session.get('username')}")
        return jsonify({'success': bool(n)})
