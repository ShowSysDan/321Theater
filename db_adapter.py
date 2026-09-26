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

Connection settings come from the app's .env file (PG_* keys, 3.4.0 — see
app_config.py / .env.example). Any key missing there falls back to the legacy
db_config.ini [postgresql] section (next to this file, or the path in
THEATER_DB_CONFIG), so installs that haven't ported their config keep working.
"""
import logging
import os
import re
import threading
import time

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import psycopg2.extras

import app_config

_log = logging.getLogger('showadvance')

# Legacy config file (deprecated fallback for any PG_* key missing from .env).
CONFIG_PATH = app_config.get('THEATER_DB_CONFIG') or os.path.join(
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


# ─── Config (.env PG_*, falling back to db_config.ini) ─────────────────────────
# read_db_settings() is called on every get_db(); cache the resolved settings
# for 30 s so each connection doesn't re-read the files.
_settings_cache: dict = {}
_settings_sources: dict = {}
_settings_ts: float = 0.0
_CACHE_TTL = 30  # seconds

# (settings key, .env key, legacy db_config.ini [postgresql] key, default).
# app_config.layered() takes each key from .env first, then the ini.
PG_SETTINGS_SPEC = (
    ('pg_host',          'PG_HOST',          'host',          'localhost'),
    ('pg_port',          'PG_PORT',          'port',          '5432'),
    ('pg_dbname',        'PG_DBNAME',        'dbname',        '321theater'),
    ('pg_user',          'PG_USER',          'user',          ''),
    ('pg_password',      'PG_PASSWORD',      'password',      ''),
    # 'schema' is the pre-two-schema ini name for the app schema.
    ('pg_app_schema',    'PG_APP_SCHEMA',    ('app_schema', 'schema'), DEFAULT_APP_SCHEMA),
    ('pg_shared_schema', 'PG_SHARED_SCHEMA', 'shared_schema', DEFAULT_SHARED_SCHEMA),
    ('pg_pool_max_idle', 'PG_POOL_MAX_IDLE', 'pool_max_idle', ''),  # '' = pool default
)


def clear_settings_cache():
    """Invalidate the config cache immediately (call after editing .env / db_config.ini)."""
    global _settings_cache, _settings_ts
    _settings_cache = {}
    _settings_ts = 0.0


def settings_sources():
    """{pg_* key: 'environment' | '.env' | 'db_config.ini' | 'default'} for the
    active settings — the Settings → Database panel lists the keys still
    coming from db_config.ini so they can be ported to .env."""
    read_db_settings()
    return dict(_settings_sources)


def read_db_settings(config_path=None):
    """
    The PostgreSQL connection settings as pg_* keys: each key from .env
    (PG_HOST, PG_PORT, …) or, when missing there, from the legacy
    db_config.ini [postgresql] section. Returns {} when neither configures
    PostgreSQL at all.

    Two schemas are used:
      pg_app_schema    – theater-specific data (shows, schedules, etc.)
      pg_shared_schema – user/auth data shared across apps
    """
    global _settings_cache, _settings_sources, _settings_ts
    path = config_path or CONFIG_PATH
    use_cache = path == CONFIG_PATH
    if use_cache and _settings_cache and (time.time() - _settings_ts) < _CACHE_TTL:
        return _settings_cache
    values, sources, present = app_config.layered(PG_SETTINGS_SPEC, path, 'postgresql')
    result = {}
    if present:
        result = dict(values)
        result['pg_pool_max_idle'] = result['pg_pool_max_idle'] or str(_POOL_DEFAULT_MAX_IDLE)
    if use_cache:
        _settings_cache = result
        _settings_sources = sources if present else {}
        _settings_ts = time.time()
    return result


def is_configured(settings=None):
    """True when .env or db_config.ini configures PostgreSQL."""
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


def raw_connect(settings=None, search_path=True, connect_timeout=10, **extra):
    """A plain psycopg2 connection (no wrapper). With search_path=True the
    session's search_path is set to "<app>", "<shared>" at connect time via
    the startup `options` — no extra round-trip per connection. `extra` is
    passed through to psycopg2.connect (the pool adds TCP keepalives)."""
    s = settings if settings is not None else read_db_settings()
    if not is_configured(s):
        raise DatabaseUnavailable(
            f'PostgreSQL is not configured — set PG_HOST / PG_DBNAME / PG_USER / '
            f'PG_PASSWORD in {app_config.ENV_PATH} (see .env.example)')
    kwargs = dict(
        host=s.get('pg_host', 'localhost'),
        port=int(s.get('pg_port', 5432) or 5432),
        dbname=s.get('pg_dbname', '321theater'),
        user=s.get('pg_user', ''),
        password=s.get('pg_password', ''),
        connect_timeout=connect_timeout,
        **extra,
    )
    if search_path:
        app_schema, shared_schema = schemas(s)
        kwargs['options'] = f'-c search_path="{app_schema}","{shared_schema}"'
    return psycopg2.connect(**kwargs)


class _ClosedConnection:
    """Stands in for the psycopg2 connection after DBConnection.close(). With
    pooling, the real connection may already be lent to another thread, so a
    use-after-close must fail exactly like psycopg2's own closed connection
    instead of silently running on someone else's transaction."""
    closed = 1

    def __getattr__(self, name):
        raise psycopg2.InterfaceError('connection already closed')


_CLOSED = _ClosedConnection()

# Statements that leave state on the SESSION (outliving the transaction). A
# connection that ran one is closed instead of going back to the pool, so the
# next borrower can never inherit a lock or a changed setting. Only the first
# few characters are matched — this runs on every execute().
_SESSION_STATE_RE = re.compile(
    r'\s*(?:SET|RESET|LISTEN|UNLISTEN|PREPARE|DECLARE|DISCARD|LOAD|'
    r'CREATE\s+(?:GLOBAL\s+|LOCAL\s+)?TEMP)', re.IGNORECASE)


def _leaves_session_state(sql):
    # pg_advisory_lock / pg_try_advisory_lock are session-scoped (the _xact_
    # variants are released by the rollback on return and don't match).
    return 'advisory_lock' in sql or _SESSION_STATE_RE.match(sql) is not None


class DBConnection:
    """Thin wrapper over a psycopg2 connection.

    - execute()/executemany() return a psycopg2 DictCursor (rows support
      row['col'], row[0], .get(), dict(row)).
    - Any error rolls the transaction back before re-raising: PostgreSQL
      aborts the whole transaction on an error, so this keeps the connection
      usable for the caller's next statement.
    - UniqueViolation is re-raised as DBIntegrityError.
    - close() hands a pooled connection back to the pool (rolled back first);
      an unpooled one is really closed. Calling close() twice is harmless.
    """

    def __init__(self, conn, schema=None, pool=None, reused=False):
        self._conn = conn
        self._schema = schema
        self._pool = pool          # _ConnectionPool, or None = really close
        self._reusable = pool is not None
        # A connection that sat idle in the pool may have died (PostgreSQL
        # restart, idle-kill). Its first statement gets one transparent
        # reconnect — safe because nothing in that transaction can have
        # committed. Cleared after the first statement succeeds.
        self._first_stmt_retry = reused

    @property
    def raw(self):
        """The underlying psycopg2 connection. The caller may change session
        state through it, so this connection is never reused from the pool."""
        self._reusable = False
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
        return self._run(lambda cur: cur.execute(sql, params if params else None), sql)

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
        return self._run(lambda cur: cur.executemany(sql, params_list), sql)

    def _run(self, op, sql):
        if self._reusable and _leaves_session_state(sql):
            self._reusable = False
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        try:
            op(cur)
        except psycopg2.errors.UniqueViolation as e:
            self._first_stmt_retry = False
            self._safe_rollback()
            raise DBIntegrityError(str(e)) from e
        except Exception:
            if self._first_stmt_retry and self._conn.closed:
                # The pooled connection was dead before we used it: swap in a
                # fresh one (raises DatabaseUnavailable if PG is really down)
                # and run the statement once more.
                self._first_stmt_retry = False
                _log.warning('DB_POOL_RECONNECT an idle pooled connection had died '
                             '(PostgreSQL restart / idle kill); replaced it and retried')
                self._conn = self._pool.fresh_connection()
                cur = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
                try:
                    op(cur)
                    return cur
                except psycopg2.errors.UniqueViolation as e:
                    self._safe_rollback()
                    raise DBIntegrityError(str(e)) from e
                except Exception:
                    self._safe_rollback()
                    raise
            self._first_stmt_retry = False
            self._safe_rollback()
            raise
        self._first_stmt_retry = False
        return cur

    def _safe_rollback(self):
        """Roll back after a failed statement. Re-raises nothing on a dead
        connection, so the caller sees the ORIGINAL error rather than an
        InterfaceError from the rollback."""
        if self._conn.closed:
            self._reusable = False
            return
        try:
            self._conn.rollback()
        except Exception:
            self._reusable = False

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        conn, self._conn = self._conn, _CLOSED
        if conn is _CLOSED:
            return
        if self._pool is not None and self._reusable:
            self._pool.release(conn)
            return
        try:
            conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ─── Connection pool ───────────────────────────────────────────────────────────
# Opening a PostgreSQL connection costs a TCP (+TLS) handshake, SCRAM auth and a
# server-side backend fork — ~10 ms measured, several times the cost of the
# queries most requests run — and a brand-new backend runs its first queries
# against cold catalog caches. A page view calls get_db() several times (the
# session loader, get_app_setting(), helpers), so connect() keeps a small
# per-PROCESS stack of idle connections and reuses them.
#
# Rules that keep a pooled connection indistinguishable from a fresh one:
#   * release() rolls back any open transaction (a plain close() did the same
#     implicitly), so no snapshot, lock or half-done write is ever handed on.
#   * A connection that changed session state (session-level advisory lock,
#     SET, LISTEN, temp table, or `.raw` access) is closed, not pooled.
#   * A dead idle connection is replaced transparently on its first statement
#     (DBConnection._run); connections idle longer than _POOL_IDLE_TTL are
#     closed rather than reused.
#   * Nothing ever waits on the pool: when it's empty a new connection is
#     opened (exactly the old behavior), and when it's full the returned
#     connection is closed. It only caps how many IDLE connections a process
#     keeps: PG_POOL_MAX_IDLE in .env (legacy: [postgresql] pool_max_idle
#     in db_config.ini; default 4 per
#     process, i.e. per Gunicorn worker; 0 = no pooling, the pre-3.3.0
#     connect-per-call behavior).
#   * Fork-safe: a child process never touches its parent's pooled sockets.

_POOL_DEFAULT_MAX_IDLE = 4
_POOL_IDLE_TTL = 300.0        # seconds an idle connection may wait for reuse
_POOL_KEEPALIVE = dict(keepalives=1, keepalives_idle=60,
                       keepalives_interval=10, keepalives_count=3)


class _ConnectionPool:
    def __init__(self, settings, max_idle):
        self.settings = settings
        self.key = _pool_key(settings)
        self.max_idle = max_idle
        self.pid = os.getpid()
        self._idle = []            # [(psycopg2 connection, monotonic released_at)]
        self._lock = threading.Lock()

    def fresh_connection(self):
        try:
            return raw_connect(self.settings, **_POOL_KEEPALIVE)
        except DatabaseUnavailable:
            raise
        except Exception as e:
            _log.error(f'PostgreSQL connection FAILED: {e}')
            raise DatabaseUnavailable(f'PostgreSQL connection failed: {e}') from e

    def acquire(self):
        """(connection, reused?) — an idle pooled connection when one is
        available, else a brand-new one."""
        now = time.monotonic()
        conn, stale = None, []
        with self._lock:
            while self._idle and now - self._idle[0][1] > _POOL_IDLE_TTL:
                stale.append(self._idle.pop(0)[0])      # oldest first
            while self._idle:
                c, _ = self._idle.pop()                 # LIFO: warmest backend
                if not c.closed:
                    conn = c
                    break
        for c in stale:
            _close_quietly(c)
        if conn is not None:
            return conn, True
        return self.fresh_connection(), False

    def release(self, conn):
        if os.getpid() != self.pid:
            _orphaned.append(conn)     # borrowed before a fork: not ours to close
            return
        if conn.closed:
            return
        try:
            status = conn.get_transaction_status()
            if status == psycopg2.extensions.TRANSACTION_STATUS_UNKNOWN:
                return _close_quietly(conn)
            if status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                conn.rollback()
        except Exception:
            return _close_quietly(conn)
        with self._lock:
            if len(self._idle) < self.max_idle:
                self._idle.append((conn, time.monotonic()))
                return
        _close_quietly(conn)

    def drain(self):
        with self._lock:
            idle, self._idle = self._idle, []
        if os.getpid() == self.pid:
            for c, _ in idle:
                _close_quietly(c)
        else:
            _orphaned.extend(c for c, _ in idle)


def _close_quietly(conn):
    try:
        conn.close()
    except Exception:
        pass


def _pool_key(s):
    return tuple(s.get(k) for k in ('pg_host', 'pg_port', 'pg_dbname', 'pg_user',
                                    'pg_password', 'pg_app_schema', 'pg_shared_schema'))


_pool = None
_pool_lock = threading.Lock()
# Idle connections inherited across a fork. Never closed or garbage-collected
# in the child: closing (or freeing) one sends a Terminate on the socket the
# parent still uses.
_orphaned = []


def _get_pool(settings):
    """The process-wide pool for the current connection settings, or None
    when pooling is off. Rebuilt (old idle connections closed) after a fork
    or when the connection settings change."""
    global _pool
    try:
        max_idle = int(settings.get('pg_pool_max_idle', _POOL_DEFAULT_MAX_IDLE))
    except (TypeError, ValueError):
        max_idle = _POOL_DEFAULT_MAX_IDLE
    p = _pool
    if (p is not None and p.pid == os.getpid() and p.key == _pool_key(settings)
            and p.max_idle == max_idle):
        return p if max_idle > 0 else None
    with _pool_lock:
        p = _pool
        if (p is None or p.pid != os.getpid() or p.key != _pool_key(settings)
                or p.max_idle != max_idle):
            if p is not None:
                p.drain()
            p = _pool = _ConnectionPool(settings, max_idle)
    return p if max_idle > 0 else None


def connect(settings=None):
    """Open a DBConnection to PostgreSQL. Raises DatabaseUnavailable when PG
    is unconfigured or unreachable — there is no fallback database.

    With the default settings (settings=None — every get_db()), the
    connection comes from the per-process pool when one is idle; close()
    returns it. Explicit settings always get a dedicated, unpooled
    connection."""
    s = settings if settings is not None else read_db_settings()
    if settings is None and is_configured(s):
        pool = _get_pool(s)
        if pool is not None:
            conn, reused = pool.acquire()
            return DBConnection(conn, schema=schemas(s)[0], pool=pool, reused=reused)
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
