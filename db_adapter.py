# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
PostgreSQL database layer for 3·2·1→Theater.

The app is PostgreSQL-native (3.0.0+). There is no SQLite backend, no SQL
dialect translation and no fallback connection:

- SQL is passed to psycopg2 VERBATIM. Write native PostgreSQL: `%s`
  placeholders, `INSERT … ON CONFLICT …`, `NOW() - INTERVAL '…'`,
  `INSERT … RETURNING id` for new ids.
- Because psycopg2 interpolates `%` markers whenever params are passed, a
  literal `%` in SQL that also has params must be written `%%` (or, better,
  bind the pattern as a parameter). With no params, SQL is sent untouched.
- If PostgreSQL can't be reached, connect() RAISES DatabaseUnavailable. It
  never silently hands back some other database — the old silent SQLite
  fallback made background jobs act on stale bootstrap data.

Connection settings come from db_config.ini ([postgresql] section) next to
this file, or from the path in the THEATER_DB_CONFIG environment variable.
"""
import configparser
import logging
import os
import re
import time

import psycopg2
import psycopg2.errors
import psycopg2.extras

_log = logging.getLogger('showadvance')

CONFIG_PATH = os.environ.get('THEATER_DB_CONFIG') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'db_config.ini')

DEFAULT_APP_SCHEMA = 'theater321'
DEFAULT_SHARED_SCHEMA = 'shared'


class DatabaseUnavailable(RuntimeError):
    """PostgreSQL is not configured or not reachable."""


class DBIntegrityError(Exception):
    """A unique-constraint violation (psycopg2 UniqueViolation), re-raised as
    one app-level type so callers can answer 'already exists' cleanly.
    Other integrity errors (FK, NOT NULL, CHECK) propagate as psycopg2's own."""


# ─── Query Timing Hook ─────────────────────────────────────────────────────────
# app.py assigns a callable here (db_adapter.query_timer_hook = fn) that is
# invoked with (sql, duration_seconds) after every execute()/executemany() —
# including ones that raised. Feeds the per-page performance stats behind the
# admin /admin/performance page. The hook must be cheap and must never raise;
# the call site swallows exceptions anyway so a broken collector can't break
# query execution.
query_timer_hook = None


# ─── Config (db_config.ini) ────────────────────────────────────────────────────
# read_db_settings() is called on every get_db(); cache the parsed ini for
# 30 s so each connection doesn't re-read the file.
_settings_cache: dict = {}
_settings_ts: float = 0.0
_CACHE_TTL = 30  # seconds


def clear_settings_cache():
    """Invalidate the config cache immediately (call after editing db_config.ini)."""
    global _settings_cache, _settings_ts
    _settings_cache = {}
    _settings_ts = 0.0


def read_db_settings(config_path=None):
    """
    Parse the [postgresql] section of db_config.ini into pg_* keys.
    Returns {} when the file or section is missing.

    Two schemas are used:
      pg_app_schema    – theater-specific data (shows, schedules, etc.)
      pg_shared_schema – user/auth data shared across apps
    Legacy 'schema' key maps to pg_app_schema for backward compatibility.
    """
    global _settings_cache, _settings_ts
    path = config_path or CONFIG_PATH
    use_cache = path == CONFIG_PATH
    if use_cache and _settings_cache and (time.time() - _settings_ts) < _CACHE_TTL:
        return _settings_cache
    result = {}
    if os.path.exists(path):
        try:
            cp = configparser.ConfigParser()
            cp.read(path, encoding='utf-8')
            if 'postgresql' in cp:
                sec = cp['postgresql']
                legacy_schema = sec.get('schema', '')
                result = {
                    'pg_host':          sec.get('host',     'localhost'),
                    'pg_port':          sec.get('port',     '5432'),
                    'pg_dbname':        sec.get('dbname',   '321theater'),
                    'pg_user':          sec.get('user',     ''),
                    'pg_password':      sec.get('password', ''),
                    'pg_app_schema':    (sec.get('app_schema', '') or legacy_schema
                                         or DEFAULT_APP_SCHEMA),
                    'pg_shared_schema': sec.get('shared_schema', '') or DEFAULT_SHARED_SCHEMA,
                }
        except Exception as e:
            _log.error(f'db_config.ini could not be parsed ({path}): {e}')
            result = {}
    if use_cache:
        _settings_cache = result
        _settings_ts = time.time()
    return result


def is_configured(settings=None):
    """True when db_config.ini has a usable [postgresql] section."""
    s = settings if settings is not None else read_db_settings()
    return bool(s.get('pg_host') or s.get('pg_dbname'))


_SAFE_IDENTIFIER_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


def _validate_identifier(name, label='identifier'):
    """Validate that a SQL identifier (schema/table name) is safe."""
    if not name or not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f'Invalid {label}: {name!r}. Must match [a-zA-Z_][a-zA-Z0-9_]*')
    return name


def schemas(settings=None):
    """(app_schema, shared_schema), validated."""
    s = settings if settings is not None else read_db_settings()
    app_schema = s.get('pg_app_schema') or s.get('pg_schema') or DEFAULT_APP_SCHEMA
    shared_schema = s.get('pg_shared_schema') or DEFAULT_SHARED_SCHEMA
    return (_validate_identifier(app_schema, 'app_schema'),
            _validate_identifier(shared_schema, 'shared_schema'))


def raw_connect(settings=None, search_path=True, connect_timeout=10):
    """A plain psycopg2 connection (no wrapper). With search_path=True the
    session's search_path is set to "<app>", "<shared>" at connect time via
    the startup `options` — no extra round-trip per connection."""
    s = settings if settings is not None else read_db_settings()
    if not is_configured(s):
        raise DatabaseUnavailable(
            f'PostgreSQL is not configured — create {CONFIG_PATH} '
            f'(see db_config.ini.example)')
    kwargs = dict(
        host=s.get('pg_host', 'localhost'),
        port=int(s.get('pg_port', 5432) or 5432),
        dbname=s.get('pg_dbname', '321theater'),
        user=s.get('pg_user', ''),
        password=s.get('pg_password', ''),
        connect_timeout=connect_timeout,
    )
    if search_path:
        app_schema, shared_schema = schemas(s)
        kwargs['options'] = f'-c search_path="{app_schema}","{shared_schema}"'
    return psycopg2.connect(**kwargs)


class DBConnection:
    """Thin wrapper over a psycopg2 connection.

    - execute()/executemany() return a psycopg2 DictCursor (rows support
      row['col'], row[0], .get(), dict(row)).
    - Any error rolls the transaction back before re-raising: PostgreSQL
      aborts the whole transaction on an error, so this keeps the connection
      usable for the caller's next statement.
    - UniqueViolation is re-raised as DBIntegrityError.
    """

    def __init__(self, conn, schema=None):
        self._conn = conn
        self._schema = schema

    @property
    def raw(self):
        """The underlying psycopg2 connection."""
        return self._conn

    def execute(self, sql, params=None):
        if query_timer_hook is None:
            return self._execute(sql, params)
        t0 = time.perf_counter()
        try:
            return self._execute(sql, params)
        finally:
            try:
                query_timer_hook(sql, time.perf_counter() - t0)
            except Exception:
                pass

    def _execute(self, sql, params=None):
        # psycopg2 only skips %-interpolation when vars is None; an empty
        # tuple/list still triggers it. Map "no params" to None so a literal %
        # in a param-less statement is sent as-is.
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        try:
            cur.execute(sql, params if params else None)
            return cur
        except psycopg2.errors.UniqueViolation as e:
            self._conn.rollback()
            raise DBIntegrityError(str(e)) from e
        except Exception:
            self._conn.rollback()
            raise

    def executemany(self, sql, params_list):
        if query_timer_hook is None:
            return self._executemany(sql, params_list)
        t0 = time.perf_counter()
        try:
            return self._executemany(sql, params_list)
        finally:
            try:
                query_timer_hook(sql, time.perf_counter() - t0)
            except Exception:
                pass

    def _executemany(self, sql, params_list):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        try:
            cur.executemany(sql, params_list)
            return cur
        except psycopg2.errors.UniqueViolation as e:
            self._conn.rollback()
            raise DBIntegrityError(str(e)) from e
        except Exception:
            self._conn.rollback()
            raise

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def connect(settings=None):
    """Open a DBConnection to PostgreSQL. Raises DatabaseUnavailable when PG
    is unconfigured or unreachable — there is no fallback database."""
    s = settings if settings is not None else read_db_settings()
    try:
        conn = raw_connect(s)
    except DatabaseUnavailable:
        raise
    except Exception as e:
        _log.error(f'PostgreSQL connection FAILED: {e}')
        raise DatabaseUnavailable(f'PostgreSQL connection failed: {e}') from e
    return DBConnection(conn, schema=schemas(s)[0])


def ensure_schemas(conn, app_schema, shared_schema):
    """CREATE SCHEMA IF NOT EXISTS for both schemas (autocommit)."""
    _validate_identifier(app_schema, 'app_schema')
    _validate_identifier(shared_schema, 'shared_schema')
    prev = conn.autocommit
    conn.autocommit = True
    try:
        cur = conn.cursor()
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{app_schema}"')
        cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{shared_schema}"')
        cur.close()
    finally:
        conn.autocommit = prev


def test_postgres_connection(host, port, dbname, user, password,
                             schema=None, app_schema=None, shared_schema=None):
    """Test a PostgreSQL connection. Returns (True, None) or (False, error_message).
    Accepts either legacy 'schema' or the dual-schema keys. Read-only: checks
    the schemas are reachable without creating anything."""
    app_sch = app_schema or schema or DEFAULT_APP_SCHEMA
    shared_sch = shared_schema or DEFAULT_SHARED_SCHEMA
    try:
        _validate_identifier(app_sch, 'app_schema')
        _validate_identifier(shared_sch, 'shared_schema')
    except ValueError as e:
        return False, str(e)
    try:
        conn = psycopg2.connect(
            host=host, port=int(port or 5432), dbname=dbname, user=user,
            password=password, connect_timeout=5,
            options=f'-c search_path="{app_sch}","{shared_sch}"',
        )
        try:
            cur = conn.cursor()
            cur.execute('SELECT nspname FROM pg_namespace WHERE nspname IN (%s, %s)',
                        (app_sch, shared_sch))
            found = {r[0] for r in cur.fetchall()}
            conn.rollback()
        finally:
            conn.close()
        missing = [s for s in (app_sch, shared_sch) if s not in found]
        if missing:
            return False, (f'Connected, but schema(s) {", ".join(missing)} do not exist yet — '
                           f'run: python3 init_db.py')
        return True, None
    except Exception as e:
        return False, str(e)
