"""Projects (3.49.0) — private projects (a festival, a PA install…) with
tasks, dependencies, linked shows, contacts and discussion. Budgets, files
and notes live in projects_docs_module.py, which registers itself through
the `core` namespace built here.

SANDBOXED like chat_module / find_module: never imports app.py; ONE
`projects_module.register(app, **deps)` call wires it in. It owns the pm_*
tables ("project" already means Overhead & Project Crew, hence the prefix)
and writes to main-app tables in exactly two places: a show created from a
task / the Overview (through the injected `create_show`, the same inserts
`new_show` makes) and `notifications` (through the injected `notify_users`).

The two rules everything below protects
- HIDDEN. Module key `projects`, default OFF, and even when ON only users
  with a `pm_access` row see anything. For everyone else EVERY url equal to
  or under /projects, /api/projects or /settings/projects — any method,
  routed or not — answers exactly like a url that does not exist: the ONE
  before_request gate aborts 404 before Flask could answer 405 / OPTIONS.
  That is also why the module's CSS/JS are not under static/: they are in
  projects_assets/ and served only by the gated /projects/assets/<name>.
- PRIVATE. A project is readable only by its pm_members rows. NO admin
  bypass anywhere: `ctx()` is the single door, a non-member gets the same
  404 as "no such project", and every child row is fetched with
  `WHERE id = %s AND project_id = %s` (never by id alone), so an id from
  someone else's project is just "Not found". Project content (names,
  titles, bodies, contact details) never reaches the audit log, syslog or
  any log line: `core.syslog()` writes verbs + integer ids only and the
  catch-all in `wrap` logs the exception TYPE, never its text (psycopg2
  messages can quote row values). The project's own history is pm_activity.
  `log_audit` is used only by the admin routes (grant / revoke / orphan
  delete / the module switch), with ids only.

Calling convention (BE-docs uses the same, via `core`)
    @app.route('/api/projects/<int:pid>/tasks', methods=['POST'], endpoint='pm_task_create')
    @core.wrap
    def pm_task_create(db, uid, pid):
        c = core.ctx(db, pid, 'edit', lock=True)     # membership + rights, FOR UPDATE
        ...writes...
        rev = core.touch(db, pid)                    # every write bumps the project rev
        core.activity(db, c, 'created', 'task', tid, detail)
        db.commit()
        core.syslog('PM_TASK_CREATE', pid, task_id=tid)
        return jsonify({'success': True, 'rev': rev, ...})
`wrap` opens / closes the connection and turns `core.Refuse(code, msg)` into
`{'error': msg}`; it never commits. EVERY mutating route passes lock=True
(writes to one project are serialized on its pm_projects row); GETs never
lock and never write (hover prefetch, polls).

Multi-user: no websockets (the app has none). Open pages poll
`GET /api/projects/<pid>/rev` and refetch the state bundle when it moved.

Dependencies are finish-to-start in calendar days (§ "Scheduling" below).
A save may leave a dependent starting too early — that is REPORTED
(`conflict` on the task, a notification) and never auto-fixed: dates move
only through shift-preview → shift, where the person approves the exact
list of moves (the token) first.
"""

import hashlib
import json
import math
import os
import re
import shutil
import types
from datetime import date, timedelta
from functools import wraps

from flask import (abort, g, has_request_context, jsonify, render_template,
                   request, send_file, session)
from jinja2 import TemplateNotFound
from werkzeug.exceptions import HTTPException

MODULE_KEY = 'projects'
_SETTING = 'module_projects_enabled'

# The three url trees the gate owns, and the two that are admin pages (they
# work with the module OFF and for an admin without pm_access, so an admin
# can switch it on and grant themself first).
_GATED = ('/projects', '/api/projects', '/settings/projects')
_ADMIN = ('/settings/projects', '/api/projects/admin')

# Served by /projects/assets/<name> from the repo-root directory
# projects_assets/. A fixed allow-list: the name never reaches the
# filesystem unless it is one of these.
_ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'projects_assets')
_ASSETS = {
    'projects.css': 'text/css', 'projects_gantt.css': 'text/css',
    'projects_docs.css': 'text/css', 'projects.js': 'text/javascript',
    'projects_gantt.js': 'text/javascript', 'projects_docs.js': 'text/javascript',
    'projects_nav.js': 'text/javascript',
}

PROJECT_STATUSES = ('planning', 'active', 'on_hold', 'done', 'archived')
BUDGET_VISIBILITY = ('members', 'editors', 'owner')
TASK_STATUSES = ('todo', 'doing', 'blocked', 'done')
_PROJECT_STATUS_LABEL = {'planning': 'Planning', 'active': 'Active', 'on_hold': 'On hold',
                         'done': 'Done', 'archived': 'Archived'}
_TASK_STATUS_LABEL = {'todo': 'To do', 'doing': 'In progress', 'blocked': 'Blocked', 'done': 'Done'}

# Hard limits: a 400 with a plain message, never a truncation.
LIMITS = types.MappingProxyType({
    'tasks': 2000, 'contacts': 500, 'budget_lines': 1000, 'sources_per_line': 20,
    'files': 2000, 'notes': 500, 'members': 100, 'depends_on': 50, 'owned_projects': 200,
})
# Text caps (characters). Over the cap is a 400 '<Field> is too long.'.
CAPS = types.MappingProxyType({
    'title': 200, 'short': 120, 'email': 254, 'long': 10000, 'comment': 4000,
    'url': 500, 'q': 200, 'activity': 300,
})

_INT_MAX = 2147483647            # PostgreSQL INTEGER: a bigger id can't exist
_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_ENTITY_RE = re.compile(r'^[a-z_]{1,30}$')
# Budget activity is hidden from members who may not see the budget — every
# reader of pm_activity goes through _activity_rows(), which applies this.
BUDGET_ENTITIES = ('budget_line', 'budget_source')

_NAME_SQL = "COALESCE(NULLIF(TRIM(u.display_name), ''), u.username)"


def _access_sql(expr):
    """SQL for "this user id has module access right now": a pm_access row
    and not a document viewer (viewers are confined to /viewer; has_access()
    is False for them whatever the table says). The module switch itself is
    checked separately — it is the same for everyone."""
    return (f"(EXISTS (SELECT 1 FROM pm_access a_ WHERE a_.user_id = {expr}) AND NOT EXISTS "
            f"(SELECT 1 FROM users v_ WHERE v_.id = {expr} AND COALESCE(v_.is_document_viewer, 0) = 1))")


# Someone an admin may grant access to / an owner may add: an approved,
# unlocked account that isn't a document viewer. 321Theater flags only —
# never is_app_*.
_USABLE_USER_SQL = ("COALESCE(u.is_locked, 0) = 0 AND COALESCE(u.pending_approval, 0) = 0 "
                    "AND COALESCE(u.is_document_viewer, 0) = 0")

_NOTIFY_TITLES = {
    'invite': 'Added to a project', 'owner': 'You are now the owner',
    'assign': 'Assigned to you', 'mention': 'Mentioned you',
    'conflict': 'Schedule conflict', 'shift': 'Dates moved', 'due': 'Due soon',
    'overdue': 'Overdue', 'budget': 'Budget line over its estimate',
}

_VIEW_ONLY = 'You have view-only access to this project.'
_OWNER_ONLY = 'Only the project owner can do that.'
_ARCHIVED = 'This project is archived.'
_NOT_FOUND = 'Not found.'

# Every pm_projects column, plus its two timestamps as timestamptz (what the
# browser gets — see CLAUDE.md "Sending timestamps to browser JS").
_PROJECT_COLS = ('p.*, p.created_at::timestamptz AS created_tz, '
                 'p.updated_at::timestamptz AS updated_tz')

_core = None        # the shared namespace (set by register)
_d = {}             # the deps dict register() was given (also core.d)


class Refuse(Exception):
    """Raised anywhere under `wrap` to answer `{'error': message}` with
    `code`. `extra` keys ride along in the JSON (e.g. a 409's current state)."""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class WriteLost(Exception):
    """A helper that swallows its own errors (notify_users) lost the
    transaction. Under `wrap` this is the generic 500 — the person retries;
    nothing was saved and nothing claims otherwise."""


class _Ctx(dict):
    """What ctx() returns: a dict whose keys also read as attributes
    (c.uid / c['uid'])."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


# ─── Access ───────────────────────────────────────────────────────────────────

def _is_under(path, bases):
    return any(path == b or path.startswith(b + '/') for b in bases)


def _module_on(db):
    """The module switch, read straight from app_settings (missing row = off)."""
    r = db.execute('SELECT value FROM app_settings WHERE key = %s', (_SETTING,)).fetchone()
    return bool(r) and r['value'] == '1'


def has_access():
    """True when the module is ON and the signed-in user has a pm_access row.
    False without a request / session user and for document viewers. ONE
    statement, memoized on flask.g for the request (the gate, the nav and
    the route share it); a failure is not memoized. RAISES on a database
    outage — routes must 503, never act on a guess (display code uses the
    context processor's projects_enabled(), which returns False instead)."""
    if _core is None or not has_request_context():
        return False
    uid = session.get('user_id')
    if not uid or session.get('is_document_viewer'):
        return False
    memo = g.get('_pm_access')
    if memo is not None:
        return memo
    db = _d['get_db']()
    try:
        r = db.execute(
            'SELECT (SELECT value FROM app_settings WHERE key = %s) AS on_, '
            'EXISTS (SELECT 1 FROM pm_access WHERE user_id = %s) AS ok',
            (_SETTING, uid)).fetchone()
    finally:
        db.close()
    ok = bool(r) and r['on_'] == '1' and bool(r['ok'])
    g._pm_access = ok
    return ok


def _username():
    if has_request_context():
        return session.get('username') or 'unknown'
    return 'system'


def _log_ids(ids):
    """` k=v` pairs for a log line — integers only. Anything that is not an
    int (or a list of ints) is DROPPED, so a caller can never put project
    text into syslog by passing the wrong variable."""
    out = []
    for k, v in ids.items():
        if isinstance(v, bool):
            out.append(f' {k}={int(v)}')
        elif isinstance(v, int):
            out.append(f' {k}={v}')
        elif isinstance(v, (list, tuple, set)) and all(
                isinstance(x, int) and not isinstance(x, bool) for x in v):
            out.append(f" {k}={','.join(str(x) for x in sorted(v))}")
    return ''.join(out)


def syslog(event, pid=None, **ids):
    """`PM_<VERB> project_id=<pid> <k>=<int> … by=<username>` — ids and verbs
    only (the syslog server and its readers are outside the project)."""
    try:
        head = f'{event} project_id={int(pid)}' if pid is not None else event
        _d['syslog_logger'].info(f'{head}{_log_ids(ids)} by={_username()}')
    except Exception:
        pass        # logging must never break a request


def _log_failure(where, exc, **ids):
    """An unexpected exception: its TYPE and where it was raised (file:line
    inside this package), never str(exc) — a database error message can
    quote the row it choked on."""
    try:
        at = ''
        tb = exc.__traceback__
        while tb is not None:
            fn = os.path.basename(tb.tb_frame.f_code.co_filename)
            if fn.startswith('project'):
                at = f'{fn}:{tb.tb_lineno}'
            tb = tb.tb_next
        _d['syslog_logger'].error(
            f'PM_ERROR at={where} type={type(exc).__name__} where={at or "-"}'
            f'{_log_ids(ids)} by={_username()}')
    except Exception:
        pass


def wrap(fn):
    """The one view decorator: `fn(db, uid, **url_args)` with a connection
    that is always closed. Refuse → its JSON; wrap never commits (a view
    that returns without committing changed nothing). The gate already
    checked access; it is checked again here (memoized, free) so a route
    registered outside the gated prefixes by mistake still 404s."""
    @wraps(fn)
    def view(**kw):
        if not has_access():
            abort(404)
        return _run(fn, kw)
    return view


def wrap_admin(fn):
    """`wrap` for the admin pages: the admin ROLE instead of module access
    (they work with the module off). No project semantics — an admin is not
    a member of anything here."""
    @wraps(fn)
    def view(**kw):
        if not session.get('user_id') or session.get('user_role') != 'admin':
            abort(404)
        return _run(fn, kw)
    return view


def _run(fn, kw):
    uid = session['user_id']
    db = _d['get_db']()
    try:
        return fn(db, uid, **kw)
    except Refuse as r:
        db.rollback()
        return jsonify(dict(r.extra, error=r.message)), r.code
    except (HTTPException, _d['db_unavailable']):
        raise           # abort() and the 503 outage handler keep working
    except Exception as e:
        # Never the app's generic 500 path for project routes: it logs the
        # full traceback (exception text included) and echoes a detail to
        # the browser. Type + location only.
        try:
            db.rollback()
        except Exception:
            pass
        _log_failure(request.endpoint or '-', e)
        return jsonify({'error': 'Something went wrong. Please try again.'}), 500
    finally:
        db.close()


def ctx(db, pid, need='view', lock=False):
    """THE door to a project. Returns
    {pid, uid, name, project (row dict), role, can_edit, can_manage,
     can_comment, can_budget, readonly, archived} or raises Refuse.

    - not a member / no such project → 404 'Not found.' (identical).
    - readonly = the raw session rule (is_readonly or is_restricted — read-only
      ADMINS too): such an account can view only.
    - archived: 'edit' / 'comment' / 'budget_edit' → 403 for everyone. The
      owner's reopen (PUT status) and DELETE do their own checks on 'view' /
      'owner'.
    - 'edit' → owner or editor; 'owner' → the owner; 'comment' → can_edit, or
      a viewer while viewers_can_comment.
    - 'budget' → can_budget, else **404** (a member who may not see the
      budget must not learn it has content). 'budget_edit' = that AND edit.
    - lock=True → FOR UPDATE on the pm_projects row: every mutating route.
      Reads flask.session — never call it from a thread or a job."""
    if need not in ('view', 'edit', 'owner', 'comment', 'budget', 'budget_edit'):
        raise ValueError(need)
    uid = session['user_id']
    if not isinstance(pid, int) or not 0 < pid <= _INT_MAX:
        raise Refuse(404, _NOT_FOUND)
    row = db.execute(
        f"""SELECT {_PROJECT_COLS}, m.role AS my_role, {_NAME_SQL} AS my_name
              FROM pm_projects p
              JOIN pm_members m ON m.project_id = p.id AND m.user_id = %s
              JOIN users u ON u.id = m.user_id
             WHERE p.id = %s {'FOR UPDATE OF p' if lock else ''}""",
        (uid, pid)).fetchone()
    if not row:
        raise Refuse(404, _NOT_FOUND)
    role = row['my_role']
    if lock:
        # The join above read my member row BEFORE waiting for the lock: a
        # removal / role change / ownership transfer that committed while
        # this request waited would be invisible. Read it again now that
        # the project is ours — this is what makes two racing owner actions
        # see each other.
        m = db.execute('SELECT role FROM pm_members WHERE project_id = %s AND user_id = %s',
                       (pid, uid)).fetchone()
        if not m:
            raise Refuse(404, _NOT_FOUND)
        role = m['role']
    p = {k: row[k] for k in row.keys() if k not in ('my_role', 'my_name')}
    readonly = bool(session.get('is_readonly') or session.get('is_restricted'))
    archived = p['status'] == 'archived'
    writer = role in ('owner', 'editor') and not readonly
    vis = p['budget_visibility']
    can_budget = (role == 'owner' or (vis == 'editors' and role == 'editor') or vis == 'members')
    c = _Ctx(
        pid=pid, uid=uid, name=row['my_name'] or '', project=p, role=role,
        readonly=readonly, archived=archived,
        can_edit=writer and not archived,
        can_manage=role == 'owner' and not readonly,
        can_comment=(not readonly and not archived
                     and (role in ('owner', 'editor')
                          or (role == 'viewer' and bool(p['viewers_can_comment'])))),
        can_budget=can_budget)
    if need in ('budget', 'budget_edit') and not can_budget:
        raise Refuse(404, _NOT_FOUND)
    if need in ('edit', 'comment', 'budget_edit'):
        if archived:
            raise Refuse(403, _ARCHIVED)
        if not (c.can_comment if need == 'comment' else writer):
            raise Refuse(403, _VIEW_ONLY)
    if need == 'owner' and not c.can_manage:
        raise Refuse(403, _OWNER_ONLY)
    return c


# ─── Input helpers (each raises Refuse(400, …); bad input is never a 500) ─────

def _body():
    """The request's JSON object. Anything else (no JSON, a list, a string,
    absurd nesting) is a 400."""
    try:
        data = request.get_json(silent=True)
    except RecursionError:
        data = None
    if not isinstance(data, dict):
        raise Refuse(400, 'Bad request.')
    return data


def clean_text(v, max, field='Text', required=False, line=False):
    """A str: stripped, NUL removed (PostgreSQL text can't hold it), CRLF →
    LF; `line=True` also folds all whitespace to single spaces (titles,
    names). Over `max` → 400 '<Field> is too long.' — NEVER truncated: the
    person must know their text did not fit."""
    if v is None:
        v = ''
    if not isinstance(v, str):
        raise Refuse(400, f'{field} must be text.')
    s = v.replace('\x00', '').replace('\r\n', '\n').replace('\r', '\n').strip()
    try:
        s.encode('utf-8')
    except UnicodeEncodeError:      # a lone surrogate from "\ud800" in JSON
        raise Refuse(400, f'{field} has unreadable characters.')
    if line:
        s = ' '.join(s.split())
    if len(s) > max:
        raise Refuse(400, f'{field} is too long.')
    if required and not s:
        raise Refuse(400, f'{field} is required.')
    return s


def parse_date(v, field='Date'):
    """ISO YYYY-MM-DD → date, null / '' → None, anything else 400. Years
    outside 1900–2200 are refused: a typo like 20226 would otherwise stretch
    every timeline across centuries."""
    if v is None or v == '':
        return None
    if isinstance(v, str) and _DATE_RE.match(v):
        try:
            d = date.fromisoformat(v)
            if 1900 <= d.year <= 2200:
                return d
        except ValueError:
            pass
    raise Refuse(400, f'{field} is not a valid date.')


def money(v, field='Amount'):
    """None for null / ''; else a finite float, |v| ≤ 1e12, rounded to cents.
    NaN / Infinity / booleans / text are a 400 (DOUBLE PRECISION would store
    NaN happily and poison every total)."""
    if v is None or v == '':
        return None
    if isinstance(v, bool):
        raise Refuse(400, f'{field} is not a valid amount.')
    if isinstance(v, str):
        try:
            v = float(v.strip().replace(',', ''))
        except ValueError:
            raise Refuse(400, f'{field} is not a valid amount.')
    # A JSON integer of 300+ digits is a valid Python int but overflows a
    # float: compare as an int first so it is a 400, never an OverflowError.
    if isinstance(v, int) and abs(v) > 10 ** 12:
        raise Refuse(400, f'{field} is not a valid amount.')
    if not isinstance(v, (int, float)) or not math.isfinite(v) or abs(v) > 1e12:
        raise Refuse(400, f'{field} is not a valid amount.')
    return round(float(v), 2)


def valid_color(v, field='Colour'):
    """'' or #rrggbb (it is written into a style attribute by the client)."""
    if v is None or v == '':
        return ''
    if isinstance(v, str) and _COLOR_RE.match(v):
        return v.lower()
    raise Refuse(400, f'{field} must look like #rrggbb.')


def to_int(v, field='Value', lo=1, hi=_INT_MAX):
    """An integer from JSON (or a digit string): booleans, floats, NaN and
    out-of-range values are a 400."""
    if isinstance(v, str) and re.fullmatch(r'-?\d{1,10}', v.strip()):
        v = int(v.strip())
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise Refuse(400, f'{field} is not valid.')
    return v


def to_flag(v, field='Value'):
    """A JSON boolean (or 0 / 1) → 0 / 1."""
    if v is True or v is False:
        return int(v)
    if isinstance(v, int) and v in (0, 1):
        return v
    raise Refuse(400, f'{field} is not valid.')


def one_of(v, allowed, field='Value'):
    if not isinstance(v, str) or v not in allowed:
        raise Refuse(400, f'{field} is not valid.')
    return v


def _url_id(v):
    """An id from the URL: one PostgreSQL could not hold is simply not found
    (binding it would raise 'integer out of range' → 500)."""
    if not isinstance(v, int) or not 0 < v <= _INT_MAX:
        raise Refuse(404, _NOT_FOUND)
    return v


def iso(d):
    return d.isoformat() if d is not None else None


def ts(v):
    """A timestamp for browser JS — select the column `::timestamptz`."""
    return _d['ts_out'](v) if v is not None else None


def _like(q):
    """A bound ILIKE pattern for user text (%, _ and \\ taken literally)."""
    return '%' + q.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_') + '%'


def _quote(title):
    return f'“{title}”'


def _same(old, new):
    """Is a validated value what the row already holds? (NULL text = '',
    a NULL / 0 / 1 flag compares as a number.) Partial updates write — and
    log, and bump the rev for — only what really changed."""
    if isinstance(new, str):
        return (old or '') == new
    if isinstance(new, int):        # ids are ≥ 1, so a NULL never equals one
        return (old or 0) == new
    return old == new


# ─── Shared DB helpers ────────────────────────────────────────────────────────

def touch(db, pid):
    """Bump the project's rev (open pages poll it) — every write, once."""
    return db.execute(
        'UPDATE pm_projects SET rev = rev + 1, updated_at = CURRENT_TIMESTAMP '
        'WHERE id = %s RETURNING rev', (pid,)).fetchone()['rev']


def activity(db, c, verb, entity, entity_id, detail):
    """One row of the project's own history. `detail` is a complete plain-text
    fragment that reads after the actor's name ("created task “Hang
    speakers”"); it may hold titles (the table is project-private) but NEVER
    an amount. Over 300 chars it is cut with … (the one sanctioned
    truncation)."""
    detail = ' '.join(str(detail or '').replace('\x00', '').split())
    if len(detail) > CAPS['activity']:
        detail = detail[:CAPS['activity'] - 1] + '…'
    db.execute(
        'INSERT INTO pm_activity (project_id, user_id, actor_name, verb, entity, entity_id, detail) '
        'VALUES (%s, %s, %s, %s, %s, %s, %s)',
        (c['pid'], c['uid'], c['name'], verb, entity, entity_id, detail))


def notify(db, pid, user_ids, code, link, key, actor_id=None, subject=''):
    """A bell notification (kind 'project') for `user_ids` ∩ CURRENT members
    of pid ∩ users who CURRENTLY have module access − the actor. Whoever the
    caller names, nobody outside the project (or without access) can receive
    one — the filter is here, not at the call sites. Never reads the session
    (jobs pass no actor). Caller commits. Returns the ids notified.
    title = the fixed text for `code` (+ ': ' + subject, i.e. a task title),
    body = the project's name; neither is ever logged or put in an email
    subject."""
    want = sorted({int(u) for u in (user_ids or ()) if u} - {actor_id})
    if not want:
        return []
    rows = db.execute(
        f"""SELECT m.user_id, p.name FROM pm_members m
              JOIN pm_projects p ON p.id = m.project_id
             WHERE m.project_id = %s AND m.user_id = ANY(%s::int[])
               AND {_access_sql('m.user_id')}""", (pid, want)).fetchall()
    ids = [r['user_id'] for r in rows]
    if not ids:
        return []
    title = _NOTIFY_TITLES[code]
    if subject:
        title += ': ' + str(subject)[:120]
    # notify_users never raises — but when one of its statements fails, the
    # adapter has already rolled the WHOLE transaction back, taking the
    # caller's own writes with it. Carrying on would commit nothing and
    # still answer "saved". So: same transaction before and after, or stop.
    xid = db.execute('SELECT txid_current() AS x').fetchone()['x']
    _d['notify_users'](db, ids, title, rows[0]['name'], link_url=link, kind='project',
                       coalesce_key=f'pm:{int(pid)}:{key}')
    if db.execute('SELECT txid_current() AS x').fetchone()['x'] != xid:
        raise WriteLost()
    return ids


def _link(pid, tab=None, **ids):
    """A relative link into a project, built from ints and fixed tab names."""
    q = ([f'tab={tab}'] if tab else []) + [f'{k}={int(v)}' for k, v in ids.items()]
    return f'/projects/{int(pid)}' + ('?' + '&'.join(q) if q else '')


def _withdraw(db, pid, key, unread_only=True, user_id=None, keep=None):
    """Delete notification rows of one coalesce key (`pm:<pid>:<key>`)."""
    sql = "DELETE FROM notifications WHERE kind = 'project' AND field_key = %s"
    args = [f'pm:{int(pid)}:{key}']
    if unread_only:
        sql += ' AND read_at IS NULL'
    if user_id is not None:
        sql += ' AND user_id = %s'
        args.append(user_id)
    if keep is not None:
        sql += ' AND NOT (user_id = ANY(%s::int[]))'
        args.append(sorted(keep))
    db.execute(sql, args)


def _scrub(db, pid=None, user_id=None):
    """Remove notifications, READ ones too, when their reader may no longer
    know: a whole project (deleted), one member of it (removed / left), or
    everything a user holds (module access revoked)."""
    sql, args = "DELETE FROM notifications WHERE kind = 'project'", []
    if pid is not None:
        sql += ' AND field_key LIKE %s'
        args.append(f'pm:{int(pid)}:%')
    if user_id is not None:
        sql += ' AND user_id = %s'
        args.append(user_id)
    if not args:
        raise ValueError('scrub needs a project or a user')
    db.execute(sql, args)


def member_ids(db, pid):
    return [r['user_id'] for r in db.execute(
        'SELECT user_id FROM pm_members WHERE project_id = %s ORDER BY user_id', (pid,)).fetchall()]


def user_name(db, uid):
    if not uid:
        return ''
    r = db.execute(f'SELECT {_NAME_SQL} AS n FROM users u WHERE u.id = %s', (uid,)).fetchone()
    return (r['n'] if r else '') or ''


def _owner_id(db, pid):
    r = db.execute("SELECT user_id FROM pm_members WHERE project_id = %s AND role = 'owner'",
                   (pid,)).fetchone()
    return r['user_id'] if r else None


def live_task(db, pid, tid, code=400):
    """The live task `tid` OF THIS PROJECT, or Refuse. For an id named in a
    request BODY (default → 400); routes pass code=404 for a url id. None
    stays None (a nullable reference)."""
    if tid is None:
        return None
    if code == 400:
        tid = to_int(tid, 'Task')
    else:
        _url_id(tid)
    r = db.execute('SELECT * FROM pm_tasks WHERE id = %s AND project_id = %s AND deleted_at IS NULL',
                   (tid, pid)).fetchone()
    if not r:
        raise Refuse(code, _NOT_FOUND if code == 404 else 'That task is not on this project.')
    return r


def live_contact(db, pid, cid, code=400):
    """Same as live_task, for a project contact."""
    if cid is None:
        return None
    if code == 400:
        cid = to_int(cid, 'Contact')
    else:
        _url_id(cid)
    r = db.execute('SELECT * FROM pm_contacts WHERE id = %s AND project_id = %s AND deleted_at IS NULL',
                   (cid, pid)).fetchone()
    if not r:
        raise Refuse(code, _NOT_FOUND if code == 404 else 'That contact is not on this project.')
    return r


def can_see_file_sql(c):
    """For alias f (pm_files): a budget-only file does not exist for a member
    who may not see the budget. A literal, not a parameter, so it can be
    dropped into any query."""
    return f"(f.budget_only = 0 OR {'TRUE' if c['can_budget'] else 'FALSE'})"


def _activity_rows(db, c, limit, entity=None, entity_id=None, before=None):
    """pm_activity for a reader, newest first — budget entities only for
    someone who can see the budget. The ONE reader in this file."""
    sql = ("SELECT id, actor_name, verb, entity, entity_id, detail, "
           "created_at::timestamptz AS at FROM pm_activity WHERE project_id = %s")
    args = [c['pid']]
    if not c['can_budget']:
        sql += ' AND NOT (entity = ANY(%s))'
        args.append(list(BUDGET_ENTITIES))
    if entity:
        sql += ' AND entity = %s'
        args.append(entity)
    if entity_id is not None:
        sql += ' AND entity_id = %s'
        args.append(entity_id)
    if before is not None:
        sql += ' AND id < %s'
        args.append(before)
    sql += ' ORDER BY id DESC LIMIT %s'
    args.append(limit)
    return [{'id': r['id'], 'actor': r['actor_name'] or 'Former user', 'verb': r['verb'],
             'entity': r['entity'], 'entity_id': r['entity_id'], 'detail': r['detail'] or '',
             'at': ts(r['at'])} for r in db.execute(sql, args).fetchall()]


def _members_out(db, pid):
    """[{id, name, role, active}] — owner first. active = still has module
    access (an inactive member keeps their row; they just can't get in)."""
    return [{'id': r['user_id'], 'name': r['name'], 'role': r['role'], 'active': bool(r['active'])}
            for r in db.execute(
                f"""SELECT m.user_id, m.role, {_NAME_SQL} AS name,
                           {_access_sql('m.user_id')} AS active
                      FROM pm_members m JOIN users u ON u.id = m.user_id
                     WHERE m.project_id = %s
                     ORDER BY (m.role <> 'owner'), (m.role <> 'editor'), LOWER({_NAME_SQL}), m.user_id""",
                (pid,)).fetchall()]


def _first_in_line(db, pid):
    """Who may claim an ownerless project: the ONE member first in line —
    editors before viewers, then longest on the project — skipping anyone
    who could not act as owner (no module access, a read-only or locked
    account). None while an owner exists. There is no admin claim."""
    r = db.execute(
        f"""SELECT m.user_id FROM pm_members m JOIN users u ON u.id = m.user_id
             WHERE m.project_id = %s AND m.role IN ('editor', 'viewer')
               AND {_access_sql('m.user_id')}
               AND COALESCE(u.is_readonly, 0) = 0 AND COALESCE(u.is_locked, 0) = 0
               AND NOT EXISTS (SELECT 1 FROM pm_members o
                                WHERE o.project_id = m.project_id AND o.role = 'owner')
             ORDER BY (m.role <> 'editor'), m.added_at, m.user_id LIMIT 1""", (pid,)).fetchone()
    return r['user_id'] if r else None


def _project_out(db, p):
    return {
        'id': p['id'], 'name': p['name'], 'description': p['description'] or '',
        'status': p['status'], 'color': p['color'] or '',
        'start_date': iso(p['start_date']), 'end_date': iso(p['end_date']),
        'budget_visibility': p['budget_visibility'],
        'viewers_can_comment': bool(p['viewers_can_comment']),
        'summary_text': p['summary_text'] or '', 'risks_text': p['risks_text'] or '',
        'rev': p['rev'], 'ownerless': _owner_id(db, p['id']) is None,
        'created_at': ts(p['created_tz']), 'updated_at': ts(p['updated_tz']),
    }


def _can_create_show():
    """The rule new_show enforces for this session (injected from app.py)."""
    return bool(_d['can_create_show']())


def _show_bars(db, show_ids):
    """Linked shows for the bundle / timeline, from app.py's helper (dates
    from load-in / load-out / performances — its rules, not re-derived here)."""
    if not show_ids:
        return []
    return list(_d['show_bars'](db, show_ids))


def _s3_cleanup(keys, pid):
    """Delete stored objects AFTER the commit that removed their rows (a
    rolled-back delete must not lose files). Failures are logged by count
    and type only; the object is then an orphan in the bucket, never a
    dangling row."""
    if not keys:
        return
    s3 = _d['s3_storage']
    failed, kind = 0, ''
    try:
        if not s3.is_configured():
            return
    except Exception:
        return
    for k in keys:
        try:
            s3.delete_file(k)
        except Exception as e:
            failed, kind = failed + 1, type(e).__name__
    if failed:
        try:
            _d['syslog_logger'].warning(
                f'S3_DELETE_FAILED table=pm_file_versions project_id={int(pid)} '
                f'count={failed} type={kind}')
        except Exception:
            pass


# ─── Scheduling: dependencies, conflicts, the approved shift ──────────────────
# Finish-to-start, calendar days. begin = start_date or due_date, finish =
# due_date or start_date. gap = 0 days when the dependent is a milestone,
# else 1 (work starts the day AFTER its dependency finishes; a milestone
# may fall on the finish day). A live, not-done task T is IN CONFLICT with
# a live, not-done dependency D when both have dates and
# T.begin < D.finish + gap. Computed here, never stored.

def _sched(db, pid):
    """(tasks, deps) for one project: every task row's scheduling columns
    (soft-deleted ones too — the loop check needs their edges) and
    {task_id: {depends_on_id, …}}."""
    tasks = {r['id']: r for r in db.execute(
        """SELECT id, title, status, start_date, due_date, is_milestone, sort_order,
                  show_id, assignee_id, (deleted_at IS NOT NULL) AS gone
             FROM pm_tasks WHERE project_id = %s""", (pid,)).fetchall()}
    deps = {}
    for r in db.execute(
            """SELECT d.task_id, d.depends_on_id FROM pm_task_deps d
                 JOIN pm_tasks t ON t.id = d.task_id AND t.project_id = %s
                 JOIN pm_tasks x ON x.id = d.depends_on_id AND x.project_id = %s""",
            (pid, pid)).fetchall():
        deps.setdefault(r['task_id'], set()).add(r['depends_on_id'])
    return tasks, deps


def _begin(t):
    return t['start_date'] or t['due_date']


def _finish(t):
    return t['due_date'] or t['start_date']


def _counts(t):
    """Takes part in conflicts: live and not done."""
    return not t['gone'] and t['status'] != 'done'


def _gap(t):
    return timedelta(days=0 if t['is_milestone'] else 1)


def _conflicts(tasks, deps):
    """{task_id: [ids of the dependencies it starts too early for]}."""
    out = {}
    for tid, ds in deps.items():
        t = tasks.get(tid)
        if not t or not _counts(t) or _begin(t) is None:
            continue
        bad = []
        for d in ds:
            x = tasks.get(d)
            if x and _counts(x) and _finish(x) is not None and _begin(t) < _finish(x) + _gap(t):
                bad.append(d)
        if bad:
            out[tid] = sorted(bad)
    return out


def _would_loop(deps, tid, new_deps):
    """Would `tid` depending on `new_deps` close a loop? Walks ALL of the
    project's edges — soft-deleted tasks included, or restoring one could
    resurrect a cycle."""
    seen, stack = set(), list(new_deps)
    while stack:
        n = stack.pop()
        if n == tid:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(deps.get(n, ()))
    return False


def _shift_plan(tasks, deps, x):
    """The moves that clear every conflict at and after task `x`.
    S = {x} ∪ its transitive dependents, keeping only tasks that can move
    (live, not done, dated). In topological order each moves LATER by the
    fewest whole days (never earlier) that puts its begin at or after
    max(dependency finish + gap) over all its counting, dated dependencies —
    using the already-shifted dates of those in S — keeping its length. So
    when x itself starts too early, x moves first and the rest follow.
    Returns [{id, title, old_start, old_due, new_start, new_due,
    is_milestone, show_id}] for tasks whose dates change."""
    def movable(i):
        t = tasks.get(i)
        return bool(t) and _counts(t) and _begin(t) is not None

    dependents = {}
    for t, ds in deps.items():
        for d in ds:
            dependents.setdefault(d, set()).add(t)
    S, seen, stack = set(), {x}, [x]
    if movable(x):
        S.add(x)
    while stack:
        cur = stack.pop()
        for t in dependents.get(cur, ()):
            if t in seen:
                continue
            seen.add(t)
            if movable(t):          # a done / undated / deleted task doesn't
                S.add(t)            # move, so nothing behind it is pushed
                stack.append(t)
    # Kahn's algorithm over S (edges dependency → dependent). The save-time
    # loop check makes a cycle impossible; if one exists anyway there is no
    # order to move things in — refuse rather than spin.
    indeg = {t: len(deps.get(t, set()) & S) for t in S}
    key = lambda i: (tasks[i]['sort_order'] or 0, i)
    ready = sorted((t for t in S if indeg[t] == 0), key=key)
    order = []
    while ready:
        cur = ready.pop(0)
        order.append(cur)
        nxt = []
        for t in dependents.get(cur, ()):
            if t in S:
                indeg[t] -= 1
                if indeg[t] == 0:
                    nxt.append(t)
        ready = sorted(ready + nxt, key=key)
    if len(order) != len(S):
        raise Refuse(409, 'These tasks depend on each other in a loop — fix the dependencies first.')
    new_finish, moves = {}, []
    for tid in order:
        t = tasks[tid]
        need = None
        for d in deps.get(tid, ()):
            dep = tasks.get(d)
            if not dep or not _counts(dep):
                continue
            fin = new_finish.get(d, _finish(dep))
            if fin is None:
                continue
            cand = fin + _gap(t)
            need = cand if need is None or cand > need else need
        delta = max(0, (need - _begin(t)).days) if need is not None else 0
        step = timedelta(days=delta)
        new_finish[tid] = _finish(t) + step
        if delta:
            moves.append({
                'id': tid, 'title': t['title'],
                'old_start': iso(t['start_date']), 'old_due': iso(t['due_date']),
                'new_start': iso(t['start_date'] + step) if t['start_date'] else None,
                'new_due': iso(t['due_date'] + step) if t['due_date'] else None,
                'is_milestone': bool(t['is_milestone']), 'show_id': t['show_id']})
    return moves


def _shift_token(moves):
    """What the person approved: the exact list of moves. `shift` applies
    only when a fresh plan hashes to the same token."""
    return hashlib.sha256(json.dumps(moves, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode('ascii')).hexdigest()


_TASK_COLS = """t.id, t.title, t.description, t.status, t.priority, t.assignee_id, t.group_name,
       t.start_date, t.due_date, t.is_milestone, t.sort_order, t.show_id, t.contact_id,
       t.completed_at::timestamptz AS completed_at, t.created_at::timestamptz AS created_at,
       t.updated_at::timestamptz AS updated_at, t.updated_name,
       (SELECT COUNT(*) FROM pm_comments c
         WHERE c.project_id = t.project_id AND c.task_id = t.id AND c.deleted_at IS NULL) AS comment_count"""


def _tasks_out(db, pid, ids=None, sched=None):
    """Full TASK objects (live tasks only), in sort order. `ids` narrows to
    some of them; `sched` reuses an already-loaded (tasks, deps)."""
    if ids is not None and not ids:
        return []
    tasks, deps = sched or _sched(db, pid)
    conf = _conflicts(tasks, deps)
    sql = f'SELECT {_TASK_COLS} FROM pm_tasks t WHERE t.project_id = %s AND t.deleted_at IS NULL'
    args = [pid]
    if ids is not None:
        sql += ' AND t.id = ANY(%s::int[])'
        args.append(sorted(ids))
    out = []
    for r in db.execute(sql + ' ORDER BY t.sort_order, t.id', args).fetchall():
        tid = r['id']
        out.append({
            'id': tid, 'title': r['title'], 'description': r['description'] or '',
            'status': r['status'], 'priority': r['priority'], 'assignee_id': r['assignee_id'],
            'group_name': r['group_name'] or '',
            'start_date': iso(r['start_date']), 'due_date': iso(r['due_date']),
            'is_milestone': bool(r['is_milestone']), 'sort_order': r['sort_order'] or 0,
            'show_id': r['show_id'], 'contact_id': r['contact_id'],
            'depends_on': sorted(d for d in deps.get(tid, ())
                                 if d in tasks and not tasks[d]['gone']),
            'comment_count': r['comment_count'],
            'conflict': tid in conf, 'conflict_with': conf.get(tid, []),
            'completed_at': ts(r['completed_at']), 'created_at': ts(r['created_at']),
            'updated_at': ts(r['updated_at']), 'updated_name': r['updated_name'] or ''})
    return out


def _after_task_write(db, c, before, saved=()):
    """The tail of every task write: recompute conflicts, tell the people a
    NEW conflict concerns, withdraw the notice of one that is gone, and
    return (tasks, new_conflicts) for the response — the saved task(s) plus
    every task whose conflict state changed. `before` = _conflicts() taken
    under the lock BEFORE the write."""
    pid = c['pid']
    sched = _sched(db, pid)
    tasks, _deps = sched
    after = _conflicts(*sched)
    changed = {t for t in set(before) | set(after) if before.get(t) != after.get(t)}
    new = []
    owner = None
    for tid in sorted(after):
        fresh = [d for d in after[tid] if d not in before.get(tid, ())]
        if not fresh:
            continue
        new.append({'task_id': tid, 'with': fresh})
        if owner is None:
            owner = _owner_id(db, pid) or 0
        notify(db, pid, [tasks[tid]['assignee_id'], owner], 'conflict',
               _link(pid, 'tasks', task=tid), f'dep:{tid}', actor_id=c['uid'],
               subject=tasks[tid]['title'])
    for tid in before:
        if tid not in after:        # resolved (or the task is gone / done)
            _withdraw(db, pid, f'dep:{tid}')
    return _tasks_out(db, pid, changed | set(saved), sched), new


def _notify_assignee(db, c, tid, title, assignee_id):
    if assignee_id and assignee_id != c['uid']:
        notify(db, c['pid'], [assignee_id], 'assign', _link(c['pid'], 'tasks', task=tid),
               f't:{tid}:assign', actor_id=c['uid'], subject=title)


def _task_fields(db, c, body, cur=None):
    """Validate the task keys PRESENT in `body` → {column: value}. `cur` is
    the stored row on an update (its dates complete the start/due check)."""
    pid, out = c['pid'], {}
    if 'title' in body:
        out['title'] = clean_text(body['title'], CAPS['title'], 'Title', line=True)
        if not out['title']:
            raise Refuse(400, 'Give the task a title.')
    elif cur is None:
        raise Refuse(400, 'Give the task a title.')
    if 'description' in body:
        out['description'] = clean_text(body['description'], CAPS['long'], 'Description')
    if 'status' in body:
        out['status'] = one_of(body['status'], TASK_STATUSES, 'Status')
    if 'priority' in body:
        out['priority'] = to_int(body['priority'], 'Priority', 0, 3)
    if 'group_name' in body:
        out['group_name'] = clean_text(body['group_name'], CAPS['short'], 'Group', line=True)
    if 'start_date' in body:
        out['start_date'] = parse_date(body['start_date'], 'Start date')
    if 'due_date' in body:
        out['due_date'] = parse_date(body['due_date'], 'Due date')
    if 'is_milestone' in body:
        out['is_milestone'] = to_flag(body['is_milestone'], 'Milestone')
    if 'assignee_id' in body:
        a = body['assignee_id']
        if a is not None:
            a = to_int(a, 'Assignee')
            if not db.execute('SELECT 1 FROM pm_members WHERE project_id = %s AND user_id = %s',
                              (pid, a)).fetchone():
                raise Refuse(400, 'Pick someone on the project.')
        out['assignee_id'] = a
    if 'contact_id' in body:
        r = live_contact(db, pid, body['contact_id'])
        out['contact_id'] = r['id'] if r else None
    start = out.get('start_date', cur['start_date'] if cur is not None else None)
    due = out.get('due_date', cur['due_date'] if cur is not None else None)
    if start and due and due < start:
        raise Refuse(400, 'The due date is before the start date.')
    return out


def _dep_ids(db, pid, raw, tid=None):
    """`depends_on` from a body → a sorted list of live task ids of THIS
    project (≤ 50, never the task itself)."""
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise Refuse(400, 'Depends on is not valid.')
    if len(raw) > LIMITS['depends_on']:
        raise Refuse(400, f"A task can depend on at most {LIMITS['depends_on']} others.")
    ids = sorted({to_int(v, 'Depends on') for v in raw})
    if tid is not None and tid in ids:
        raise Refuse(400, "A task can't depend on itself.")
    if ids:
        ok = {r['id'] for r in db.execute(
            'SELECT id FROM pm_tasks WHERE project_id = %s AND deleted_at IS NULL '
            'AND id = ANY(%s::int[])', (pid, ids)).fetchall()}
        if len(ok) != len(ids):
            raise Refuse(400, 'That task is not on this project.')
    return ids


def _set_deps(db, pid, tid, ids):
    """Replace the dependencies of `tid` that point at LIVE tasks. Edges to a
    soft-deleted task are kept: restoring it restores the link."""
    db.execute(
        """DELETE FROM pm_task_deps d USING pm_tasks x
            WHERE d.task_id = %s AND x.id = d.depends_on_id AND x.project_id = %s
              AND x.deleted_at IS NULL AND NOT (d.depends_on_id = ANY(%s::int[]))""",
        (tid, pid, ids))
    for dep in ids:
        db.execute('INSERT INTO pm_task_deps (task_id, depends_on_id) VALUES (%s, %s) '
                   'ON CONFLICT DO NOTHING', (tid, dep))


def _live_task_count(db, pid):
    return db.execute('SELECT COUNT(*) AS n FROM pm_tasks WHERE project_id = %s AND deleted_at IS NULL',
                      (pid,)).fetchone()['n']


def _snippet(text, q, radius=60):
    """±`radius` chars around the first hit of q in text, as one plain line."""
    text = text or ''
    i = text.lower().find(q.lower())
    if i < 0:
        i = 0
    a, b = max(0, i - radius), min(len(text), i + len(q) + radius)
    s = ' '.join(text[a:b].split())
    return ('…' if a > 0 else '') + s + ('…' if b < len(text) else '')


def _new_show_fields(raw):
    """{name, show_date?, venue, show_mode} for a show made from a project.
    '' is a valid venue (the board has a no-venue column)."""
    if not isinstance(raw, dict):
        raise Refuse(400, 'Bad request.')
    name = clean_text(raw.get('name'), CAPS['title'], 'Show name', line=True)
    if not name:
        raise Refuse(400, 'Give the show a name.')
    d = parse_date(raw.get('show_date'), 'Show date')
    venue = clean_text(raw.get('venue'), CAPS['title'], 'Venue', line=True)
    mode = one_of(raw.get('show_mode', 'show'), ('show', 'event'), 'Show type')
    return name, iso(d), venue, mode


def _create_show(db, c, fields):
    """Create a REAL show (visible to everyone in the app) through the
    injected helper — the same inserts and the same SHOW_CREATE audit row
    `new_show` writes, with nothing that names this module — and link it.
    Returns (show_id, name). Caller commits, then calls _show_created_log."""
    if c['readonly'] or not _can_create_show():
        raise Refuse(403, "You can't create shows.")
    name, show_date, venue, mode = fields
    sid = _d['create_show'](db, name, show_date, venue, mode, c['uid'])
    db.execute('INSERT INTO pm_shows (project_id, show_id, linked_by) VALUES (%s, %s, %s)',
               (c['pid'], sid, c['uid']))
    return sid, name


def _show_created_log(sid, name):
    # Byte-for-byte the line new_show writes (a show's name is app-public).
    try:
        _d['syslog_logger'].info(f'SHOW_CREATE show_id={sid} name={name} by={_username()}')
    except Exception:
        pass


# ─── Functions other files call ───────────────────────────────────────────────

def my_work(db, uid):
    """For the My Work page: the user's non-archived projects and the live,
    not-done tasks assigned to them. BOTH lists are restricted to projects
    where (project_id, uid) is in pm_members — an assignee who was removed
    from a project no longer sees its tasks. The caller checks has_access()
    first. Dates are date objects (the template formats them)."""
    today = date.today()
    projects = [{
        'id': r['id'], 'name': r['name'], 'status': r['status'], 'color': r['color'] or '',
        'role': r['role'], 'open': r['open'], 'overdue': r['overdue'], 'next_due': r['next_due'],
    } for r in db.execute(
        """SELECT p.id, p.name, p.status, p.color, m.role, t.open, t.overdue, t.next_due
             FROM pm_projects p
             JOIN pm_members m ON m.project_id = p.id AND m.user_id = %s
             LEFT JOIN LATERAL (
                  SELECT COUNT(*) FILTER (WHERE x.status <> 'done') AS open,
                         COUNT(*) FILTER (WHERE x.status <> 'done' AND x.due_date < %s) AS overdue,
                         MIN(x.due_date) FILTER (WHERE x.status <> 'done') AS next_due
                    FROM pm_tasks x WHERE x.project_id = p.id AND x.deleted_at IS NULL) t ON TRUE
            WHERE p.status <> 'archived'
            ORDER BY LOWER(p.name), p.id LIMIT 200""", (uid, today)).fetchall()]
    tasks = [{
        'id': r['id'], 'project_id': r['project_id'], 'project_name': r['project_name'],
        'title': r['title'], 'status': r['status'], 'priority': r['priority'],
        'due_date': r['due_date'], 'overdue': bool(r['due_date'] and r['due_date'] < today),
        'is_milestone': bool(r['is_milestone']),
    } for r in db.execute(
        """SELECT t.id, t.project_id, p.name AS project_name, t.title, t.status, t.priority,
                  t.due_date, t.is_milestone
             FROM pm_tasks t
             JOIN pm_members m ON m.project_id = t.project_id AND m.user_id = %s
             JOIN pm_projects p ON p.id = t.project_id
            WHERE t.assignee_id = %s AND t.deleted_at IS NULL AND t.status <> 'done'
              AND p.status <> 'archived'
            ORDER BY (t.due_date IS NULL), (t.due_date >= %s), t.due_date, t.priority DESC, t.id
            LIMIT 200""", (uid, uid, today)).fetchall()]
    return {'projects': projects, 'tasks': tasks}


def on_show_merge(db, source_id, keeper_id):
    """Two shows are being merged (app.py calls this BEFORE its move loop —
    deliberately not in _SHOW_MERGE_MOVE_TABLES, whose row counts are shown
    to admins). A project linked to both would break UNIQUE(project_id,
    show_id), so the would-be duplicate goes first; then the remaining
    links and the tasks pointing at the source move to the keeper. The
    affected projects' rev moves so open pages refetch. Caller commits."""
    pids = {r['project_id'] for r in db.execute(
        'SELECT project_id FROM pm_shows WHERE show_id = %s '
        'UNION SELECT project_id FROM pm_tasks WHERE show_id = %s',
        (source_id, source_id)).fetchall()}
    if not pids:
        return
    db.execute(
        'DELETE FROM pm_shows WHERE show_id = %s AND project_id IN '
        '(SELECT k.project_id FROM pm_shows k WHERE k.show_id = %s)', (source_id, keeper_id))
    db.execute('UPDATE pm_shows SET show_id = %s WHERE show_id = %s', (keeper_id, source_id))
    db.execute('UPDATE pm_tasks SET show_id = %s WHERE show_id = %s', (keeper_id, source_id))
    db.execute('UPDATE pm_projects SET rev = rev + 1 WHERE id = ANY(%s::int[])', (sorted(pids),))


def pending_email_items(db, delay_min):
    """Rows for app.py's inbox email job: this module's bell notifications
    left unread for `delay_min` minutes, STAMPED by the one statement that
    returns them — am_i_leader() can be true in several workers at once, and
    this way each row is handed out exactly once. Only for users who still
    have access (someone revoked since must not be mailed a title). [] while
    the module is off. Caller commits; the email it builds is `sensitive`."""
    if not _module_on(db):
        return []
    return [dict(r) for r in db.execute(
        f"""UPDATE notifications n SET emailed_at = CURRENT_TIMESTAMP
             WHERE n.kind = 'project' AND n.read_at IS NULL AND n.emailed_at IS NULL
               AND n.created_at <= NOW() - %s * INTERVAL '1 minute'
               AND {_access_sql('n.user_id')}
         RETURNING n.user_id, n.title, n.body""", (delay_min,)).fetchall()]


def run_hourly():
    """The hourly job (app.py's wrapper is leader-gated and catches
    DatabaseUnavailable). Own connection; never touches flask.session / ctx.
    (a) Due / overdue alerts for live, not-done tasks of non-archived
        projects: `due` when due today or tomorrow, `overdue` once past.
        To the assignee when they are a current member with access, else
        the owner. Each alert is CLAIMED with INSERT … ON CONFLICT DO
        NOTHING RETURNING (ref = the due date, so a re-dated task alerts
        again) in the same transaction as its notification: two workers
        running at once send each alert once, and a crash loses neither.
    (b) projects_docs_module.retry_extractions(), when that module loaded.
    Returns the number of alerts sent."""
    if _core is None:
        return 0
    sent = 0
    db = _d['get_db']()
    try:
        if not _module_on(db):
            return 0
        today = date.today()
        soon = today + timedelta(days=1)
        rows = db.execute(
            f"""SELECT * FROM (
                  SELECT t.id, t.project_id, t.title, t.due_date,
                         CASE WHEN t.due_date < %s THEN 'overdue' ELSE 'due' END AS kind,
                         COALESCE(
                           (SELECT m.user_id FROM pm_members m
                             WHERE m.project_id = t.project_id AND m.user_id = t.assignee_id
                               AND {_access_sql('m.user_id')}),
                           (SELECT m.user_id FROM pm_members m
                             WHERE m.project_id = t.project_id AND m.role = 'owner'
                               AND {_access_sql('m.user_id')})) AS rcpt
                    FROM pm_tasks t JOIN pm_projects p ON p.id = t.project_id
                   WHERE t.deleted_at IS NULL AND t.status <> 'done' AND p.status <> 'archived'
                     AND t.due_date IS NOT NULL AND t.due_date <= %s) q
                 WHERE q.rcpt IS NOT NULL
                   AND NOT EXISTS (SELECT 1 FROM pm_task_alerts a
                                    WHERE a.task_id = q.id AND a.kind = q.kind
                                      AND a.ref = CAST(q.due_date AS TEXT))
                 ORDER BY q.id LIMIT 1000""", (today, soon)).fetchall()
        db.rollback()               # nothing held while we work through them
        for r in rows:
            ref = r['due_date'].isoformat()
            # Claim and notify in ONE transaction. A second worker's INSERT
            # waits on ours, then conflicts and returns nothing.
            got = db.execute(
                'INSERT INTO pm_task_alerts (task_id, kind, ref) VALUES (%s, %s, %s) '
                'ON CONFLICT DO NOTHING RETURNING task_id', (r['id'], r['kind'], ref)).fetchone()
            if got:
                try:
                    told = notify(db, r['project_id'], [r['rcpt']], r['kind'],
                                  _link(r['project_id'], 'tasks', task=r['id']),
                                  f"t:{r['id']}:{r['kind']}:{ref}", subject=r['title'])
                except WriteLost:
                    db.rollback()   # the claim went with it: next hour retries
                    continue
                sent += len(told)
            db.commit()
        if sent:
            syslog('PM_ALERTS', count=sent)
    finally:
        db.close()
    docs = _d.get('docs')
    if docs is not None and hasattr(docs, 'retry_extractions'):
        try:
            docs.retry_extractions()
        except _d['db_unavailable']:
            raise
        except Exception as e:
            _log_failure('retry_extractions', e)
    return sent


# ─── Routes ───────────────────────────────────────────────────────────────────

def _page(template, **kw):
    """Render a module page. Until its template exists (another engineer
    writes them) the route still answers — a bare placeholder with no
    feature text — so the routes can be built and tested first."""
    try:
        return render_template(template, user=_d['get_current_user'](),
                               asset_v=str(_d.get('app_version') or ''), **kw)
    except TemplateNotFound as e:
        if e.name != template:
            raise                   # a missing include is a real error
        return '<!doctype html><meta charset="utf-8"><title>Loading…</title><p>Loading…</p>'


def _routes_pages(app):

    @app.route('/projects', endpoint='pm_index')
    @wrap
    def pm_index(db, uid):
        return _page('projects/index.html')

    @app.route('/projects/<int:pid>', endpoint='pm_project')
    @wrap
    def pm_project(db, uid, pid):
        try:
            ctx(db, pid)
        except Refuse:
            abort(404)              # a page, so the app's own "Page not found"
        return _page('projects/project.html', pid=pid)

    @app.route('/settings/projects', endpoint='pm_settings')
    @wrap_admin
    def pm_settings(db, uid):
        return _page('projects/settings.html')

    @app.route('/projects/assets/<name>', endpoint='pm_asset')
    def pm_asset(name):
        # No connection needed, so not under wrap — but the same check (the
        # gate made it already; memoized).
        if not has_access():
            abort(404)
        mime = _ASSETS.get(name)
        path = os.path.join(_ASSET_DIR, name) if mime else None
        if not path or not os.path.isfile(path):
            abort(404)
        resp = send_file(path, mimetype=mime, conditional=True, etag=True, max_age=None)
        # Revalidate every time (ETag makes that a 304): a deploy must never
        # leave a browser on old module code, and nothing here is public.
        resp.headers['Cache-Control'] = 'private, no-cache'
        return resp


def _routes_projects(app):

    @app.route('/api/projects', methods=['GET'], endpoint='pm_api_list')
    @wrap
    def pm_api_list(db, uid):
        today = date.today()
        rows = db.execute(
            f"""SELECT p.id, p.name, p.status, p.color, p.start_date, p.end_date, m.role,
                       (SELECT {_NAME_SQL} FROM pm_members o JOIN users u ON u.id = o.user_id
                         WHERE o.project_id = p.id AND o.role = 'owner') AS owner_name,
                       (SELECT COUNT(*) FROM pm_members x WHERE x.project_id = p.id) AS members,
                       t.total, t.done, t.open, t.overdue, ms.title AS ms_title, ms.d AS ms_date
                  FROM pm_projects p
                  JOIN pm_members m ON m.project_id = p.id AND m.user_id = %s
                  LEFT JOIN LATERAL (
                       SELECT COUNT(*) AS total,
                              COUNT(*) FILTER (WHERE x.status = 'done') AS done,
                              COUNT(*) FILTER (WHERE x.status <> 'done') AS open,
                              COUNT(*) FILTER (WHERE x.status <> 'done' AND x.due_date < %s) AS overdue
                         FROM pm_tasks x WHERE x.project_id = p.id AND x.deleted_at IS NULL) t ON TRUE
                  LEFT JOIN LATERAL (
                       SELECT x.title, COALESCE(x.due_date, x.start_date) AS d
                         FROM pm_tasks x
                        WHERE x.project_id = p.id AND x.deleted_at IS NULL AND x.is_milestone = 1
                          AND x.status <> 'done' AND COALESCE(x.due_date, x.start_date) >= %s
                        ORDER BY COALESCE(x.due_date, x.start_date), x.id LIMIT 1) ms ON TRUE
                 ORDER BY (p.status = 'archived'), LOWER(p.name), p.id""",
            (uid, today, today)).fetchall()
        return jsonify({'projects': [{
            'id': r['id'], 'name': r['name'], 'status': r['status'], 'color': r['color'] or '',
            'start_date': iso(r['start_date']), 'end_date': iso(r['end_date']), 'role': r['role'],
            'owner_name': r['owner_name'] or '', 'open': r['open'], 'overdue': r['overdue'],
            'done': r['done'], 'total': r['total'],
            'next_milestone': ({'title': r['ms_title'], 'date': iso(r['ms_date'])}
                               if r['ms_title'] is not None else None),
            'members': r['members']} for r in rows]})

    @app.route('/api/projects', methods=['POST'], endpoint='pm_api_create')
    @wrap
    def pm_api_create(db, uid):
        if session.get('is_readonly') or session.get('is_restricted'):
            raise Refuse(403, 'Your account is read-only.')
        body = _body()
        name = clean_text(body.get('name'), CAPS['title'], 'Name', line=True)
        if not name:
            raise Refuse(400, 'Give the project a name.')
        desc = clean_text(body.get('description'), CAPS['long'], 'Description')
        start = parse_date(body.get('start_date'), 'Start date')
        end = parse_date(body.get('end_date'), 'End date')
        if start and end and end < start:
            raise Refuse(400, 'The end date is before the start date.')
        color = valid_color(body.get('color'))
        # Serialize one user's creates (no project row to lock yet) so the
        # cap can't be passed by a burst of parallel requests.
        # The lock is the user's own pm_access row (they have one, or the
        # gate would have 404'd) — never the cross-app users table.
        db.execute('SELECT 1 FROM pm_access WHERE user_id = %s FOR UPDATE', (uid,))
        owned = db.execute("SELECT COUNT(*) AS n FROM pm_members WHERE user_id = %s AND role = 'owner'",
                           (uid,)).fetchone()['n']
        if owned >= LIMITS['owned_projects']:
            raise Refuse(400, f"You already own {LIMITS['owned_projects']} projects — the limit.")
        pid = db.execute(
            'INSERT INTO pm_projects (name, description, start_date, end_date, color, created_by) '
            'VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
            (name, desc, start, end, color, uid)).fetchone()['id']
        db.execute("INSERT INTO pm_members (project_id, user_id, role, added_by) "
                   "VALUES (%s, %s, 'owner', %s)", (pid, uid, uid))
        activity(db, {'pid': pid, 'uid': uid, 'name': user_name(db, uid)},
                 'created', 'project', pid, 'created the project')
        db.commit()
        syslog('PM_PROJECT_CREATE', pid)
        return jsonify({'success': True, 'id': pid})

    @app.route('/api/projects/search', methods=['POST'], endpoint='pm_api_search')
    @wrap
    def pm_api_search(db, uid):
        """The sidebar box: my projects by name and my projects' live tasks by
        title, ≤ 8. POST so the text never sits in a url (perf log, history)."""
        q = clean_text(_body().get('q'), CAPS['q'], 'Search', line=True)
        if len(q) < 2:
            return jsonify({'results': []})
        like = _like(q)
        projects = db.execute(
            """SELECT p.id, p.name, p.status FROM pm_projects p
                 JOIN pm_members m ON m.project_id = p.id AND m.user_id = %s
                WHERE p.name ILIKE %s
                ORDER BY (p.status = 'archived'), p.updated_at DESC, p.id LIMIT 8""",
            (uid, like)).fetchall()
        tasks = db.execute(
            """SELECT t.id, t.project_id, t.title, p.name FROM pm_tasks t
                 JOIN pm_members m ON m.project_id = t.project_id AND m.user_id = %s
                 JOIN pm_projects p ON p.id = t.project_id
                WHERE t.deleted_at IS NULL AND p.status <> 'archived' AND t.title ILIKE %s
                ORDER BY (t.status = 'done'), t.updated_at DESC, t.id LIMIT 8""",
            (uid, like)).fetchall()
        pr = [{'type': 'project', 'project_id': r['id'], 'title': r['name'],
               'sub': _PROJECT_STATUS_LABEL.get(r['status'], '')} for r in projects]
        tk = [{'type': 'task', 'project_id': r['project_id'], 'task_id': r['id'],
               'title': r['title'], 'sub': r['name']} for r in tasks]
        # Projects first, but leave room for tasks when both match.
        head = pr[:max(4, 8 - len(tk))]
        return jsonify({'results': (head + tk)[:8]})

    @app.route('/api/projects/<int:pid>', methods=['GET'], endpoint='pm_api_state')
    @wrap
    def pm_api_state(db, uid, pid):
        """The STATE bundle: everything the project page needs in one read."""
        c = ctx(db, pid)
        p = c.project
        owner = _owner_id(db, pid)
        sched = _sched(db, pid)
        show_ids = [r['show_id'] for r in db.execute(
            'SELECT show_id FROM pm_shows WHERE project_id = %s ORDER BY id', (pid,)).fetchall()]
        show_ids = [s for s in show_ids if _d['can_access_show'](uid, s)]
        shows = _show_bars(db, show_ids)
        counts = db.execute(
            f"""SELECT
                 (SELECT COUNT(*) FROM pm_comments x WHERE x.project_id = %s
                     AND x.task_id IS NULL AND x.deleted_at IS NULL) AS discussion,
                 (SELECT COUNT(*) FROM pm_files f WHERE f.project_id = %s
                     AND f.deleted_at IS NULL AND {can_see_file_sql(c)}) AS files,
                 (SELECT COUNT(*) FROM pm_notes x WHERE x.project_id = %s
                     AND x.deleted_at IS NULL) AS notes,
                 (SELECT COUNT(*) FROM pm_budget_lines x WHERE x.project_id = %s
                     AND x.deleted_at IS NULL) AS budget_lines""", (pid, pid, pid, pid)).fetchone()
        proj = _project_out(db, p)
        return jsonify({
            'project': proj,
            'me': {'id': uid, 'name': c.name, 'role': c.role, 'can_edit': c.can_edit,
                   'can_manage': c.can_manage, 'can_comment': c.can_comment,
                   'can_budget': c.can_budget, 'can_create_show': (not c.readonly) and _can_create_show(),
                   'can_claim': owner is None and _first_in_line(db, pid) == uid and not c.readonly,
                   'readonly': c.readonly, 'archived': c.archived},
            'members': _members_out(db, pid),
            'tasks': _tasks_out(db, pid, sched=sched),
            'shows': shows,
            'contacts': _contacts_out(db, pid),
            'venues': list(_d['get_distinct_venues'](db)),
            'discussion_count': counts['discussion'],
            # A member who may not see the budget must not learn it has lines.
            'counts': {'files': counts['files'], 'notes': counts['notes'],
                       'budget_lines': counts['budget_lines'] if c.can_budget else 0},
            'activity': _activity_rows(db, c, 30),
            'today': date.today().isoformat()})

    @app.route('/api/projects/<int:pid>/rev', methods=['GET'], endpoint='pm_api_rev')
    @wrap
    def pm_api_rev(db, uid, pid):
        # The 10-second poll: ONE statement, no ctx. A removed member gets
        # the 404 that tells the page to close.
        r = db.execute(
            'SELECT p.rev FROM pm_projects p JOIN pm_members m ON m.project_id = p.id '
            'AND m.user_id = %s WHERE p.id = %s', (uid, _url_id(pid))).fetchone()
        if not r:
            raise Refuse(404, _NOT_FOUND)
        return jsonify({'rev': r['rev']})

    @app.route('/api/projects/<int:pid>', methods=['PUT'], endpoint='pm_api_update')
    @wrap
    def pm_api_update(db, uid, pid):
        """Partial update. Editors: name, description, status, color, dates,
        summary_text, risks_text. Owner only: budget_visibility,
        viewers_can_comment, and reopening an archived project."""
        c = ctx(db, pid, 'view', lock=True)
        body, p, new = _body(), c.project, {}
        if 'name' in body:
            new['name'] = clean_text(body['name'], CAPS['title'], 'Name', line=True)
            if not new['name']:
                raise Refuse(400, 'Give the project a name.')
        for k, label in (('description', 'Description'), ('summary_text', 'Summary'),
                         ('risks_text', 'Risks')):
            if k in body:
                new[k] = clean_text(body[k], CAPS['long'], label)
        if 'status' in body:
            new['status'] = one_of(body['status'], PROJECT_STATUSES, 'Status')
        if 'color' in body:
            new['color'] = valid_color(body['color'])
        if 'start_date' in body:
            new['start_date'] = parse_date(body['start_date'], 'Start date')
        if 'end_date' in body:
            new['end_date'] = parse_date(body['end_date'], 'End date')
        if 'budget_visibility' in body:
            new['budget_visibility'] = one_of(body['budget_visibility'], BUDGET_VISIBILITY,
                                              'Budget visibility')
        if 'viewers_can_comment' in body:
            new['viewers_can_comment'] = to_flag(body['viewers_can_comment'], 'Viewers can comment')
        owner_keys = {'budget_visibility', 'viewers_can_comment'} & set(new)
        if c.archived:
            # Read only for everyone — except that its owner may reopen it
            # (and change other fields in that same save).
            if new.get('status', 'archived') == 'archived':
                raise Refuse(403, _ARCHIVED)
            if not c.can_manage:
                raise Refuse(403, _OWNER_ONLY)
        elif c.readonly or c.role not in ('owner', 'editor'):
            raise Refuse(403, _VIEW_ONLY)
        if owner_keys and not c.can_manage:
            raise Refuse(403, _OWNER_ONLY)
        start = new.get('start_date', p['start_date'])
        end = new.get('end_date', p['end_date'])
        if start and end and end < start and ({'start_date', 'end_date'} & set(new)):
            raise Refuse(400, 'The end date is before the start date.')
        changed = {k: v for k, v in new.items() if not _same(p[k], v)}
        if not changed:             # an identical save changes (and logs) nothing
            return jsonify({'success': True, 'rev': p['rev'], 'project': _project_out(db, p)})
        sets = ', '.join(f'{k} = %s' for k in changed)      # keys come from the fixed list above
        db.execute(f'UPDATE pm_projects SET {sets} WHERE id = %s', (*changed.values(), pid))
        rev = touch(db, pid)
        autosave = set(changed) <= {'summary_text', 'risks_text'}
        if autosave:
            # Typing into Summary / Risks saves every few seconds: one
            # history line per person per 10 minutes, not one per keystroke.
            quiet = db.execute(
                "SELECT 1 FROM pm_activity WHERE project_id = %s AND user_id = %s AND verb = 'summary' "
                "AND created_at > NOW() - INTERVAL '10 minutes' LIMIT 1", (pid, uid)).fetchone()
            if not quiet:
                activity(db, c, 'summary', 'project', pid, 'edited the summary and risks')
        else:
            bits = []
            if 'name' in changed:
                bits.append('renamed the project')
            if 'status' in changed:
                bits.append('set the status to ' + _PROJECT_STATUS_LABEL[changed['status']])
            if 'budget_visibility' in changed:
                bits.append('changed who can see the budget')
            if 'viewers_can_comment' in changed:
                bits.append('changed whether viewers can comment')
            if not bits or (set(changed) - {'name', 'status', 'budget_visibility', 'viewers_can_comment'}):
                bits.append('updated the project details')
            activity(db, c, 'updated', 'project', pid, ', '.join(bits))
            quiet = False
        db.commit()
        if not quiet:
            syslog('PM_PROJECT_UPDATE', pid)
        fresh = db.execute(f'SELECT {_PROJECT_COLS} FROM pm_projects p WHERE p.id = %s', (pid,)).fetchone()
        return jsonify({'success': True, 'rev': rev, 'project': _project_out(db, fresh)})

    @app.route('/api/projects/<int:pid>', methods=['DELETE'], endpoint='pm_api_delete')
    @wrap
    def pm_api_delete(db, uid, pid):
        c = ctx(db, pid, 'owner', lock=True)
        if _body().get('confirm') != 'DELETE':
            raise Refuse(400, 'Type DELETE to confirm.')
        keys = _delete_project(db, pid)
        db.commit()
        _s3_cleanup(keys, pid)
        syslog('PM_PROJECT_DELETE', pid)
        return jsonify({'success': True})

    @app.route('/api/projects/<int:pid>/claim-owner', methods=['POST'], endpoint='pm_claim_owner')
    @wrap
    def pm_claim_owner(db, uid, pid):
        """An ownerless project (its owner's account was deleted) is taken
        over by the ONE member first in line. Never by an admin."""
        c = ctx(db, pid, 'view', lock=True)
        if _owner_id(db, pid) is not None:
            raise Refuse(409, 'This project already has an owner.')
        if c.readonly or _first_in_line(db, pid) != uid:
            raise Refuse(403, "You can't take over this project.")
        db.execute("UPDATE pm_members SET role = 'owner' WHERE project_id = %s AND user_id = %s",
                   (pid, uid))
        rev = touch(db, pid)
        activity(db, c, 'owner', 'member', uid, 'became the owner')
        db.commit()
        syslog('PM_OWNER_CLAIM', pid, user_id=uid)
        return jsonify({'success': True, 'rev': rev, 'members': _members_out(db, pid)})


def _delete_project(db, pid):
    """Delete a project and everything in it (FK cascade), and scrub its
    notifications. Returns the S3 keys of its files, for the caller to
    delete AFTER commit."""
    keys = [r['s3_key'] for r in db.execute(
        "SELECT s3_key FROM pm_file_versions WHERE project_id = %s AND s3_key IS NOT NULL "
        "AND s3_key <> ''", (pid,)).fetchall()]
    _scrub(db, pid=pid)
    db.execute('DELETE FROM pm_projects WHERE id = %s', (pid,))
    return keys


def _routes_members(app):

    def addable_sql():
        # May be added to a project: has module access, usable account, and
        # not on it yet. The owner's picker and the add route share this.
        return (f"{_USABLE_USER_SQL} AND {_access_sql('u.id')} AND NOT EXISTS "
                "(SELECT 1 FROM pm_members m WHERE m.project_id = %s AND m.user_id = u.id)")

    def member_row(db, pid, target):
        r = db.execute(
            f"""SELECT m.user_id, m.role, {_NAME_SQL} AS name, COALESCE(u.is_readonly, 0) AS ro,
                       {_access_sql('m.user_id')} AS active
                  FROM pm_members m JOIN users u ON u.id = m.user_id
                 WHERE m.project_id = %s AND m.user_id = %s""", (pid, _url_id(target))).fetchone()
        if not r:
            raise Refuse(404, _NOT_FOUND)
        return r

    @app.route('/api/projects/<int:pid>/people', methods=['GET'], endpoint='pm_people')
    @wrap
    def pm_people(db, uid, pid):
        ctx(db, pid, 'owner')
        rows = db.execute(
            f"""SELECT u.id, {_NAME_SQL} AS name FROM users u
                 WHERE {addable_sql()} ORDER BY LOWER({_NAME_SQL}), u.id""", (pid,)).fetchall()
        return jsonify({'people': [{'id': r['id'], 'name': r['name']} for r in rows]})

    @app.route('/api/projects/<int:pid>/members', methods=['POST'], endpoint='pm_member_add')
    @wrap
    def pm_member_add(db, uid, pid):
        c = ctx(db, pid, 'owner', lock=True)
        body = _body()
        target = to_int(body.get('user_id'), 'Person')
        role = one_of(body.get('role'), ('editor', 'viewer'), 'Role')
        u = db.execute(f'SELECT u.id, {_NAME_SQL} AS name FROM users u WHERE u.id = %s AND {addable_sql()}',
                       (target, pid)).fetchone()
        if not u:
            # One message for "no such user", "no access" and "already on
            # it": the owner learns nothing about accounts they can't add.
            raise Refuse(400, "That person can't be added.")
        n = db.execute('SELECT COUNT(*) AS n FROM pm_members WHERE project_id = %s', (pid,)).fetchone()['n']
        if n >= LIMITS['members']:
            raise Refuse(400, f"A project can have at most {LIMITS['members']} people.")
        db.execute('INSERT INTO pm_members (project_id, user_id, role, added_by) VALUES (%s, %s, %s, %s)',
                   (pid, target, role, uid))
        rev = touch(db, pid)
        activity(db, c, 'added', 'member', target, f"added {u['name']} as {role}")
        notify(db, pid, [target], 'invite', _link(pid), f'm:{target}', actor_id=uid)
        db.commit()
        syslog('PM_MEMBER_ADD', pid, user_id=target)
        return jsonify({'success': True, 'rev': rev, 'members': _members_out(db, pid)})

    @app.route('/api/projects/<int:pid>/members/<int:target>', methods=['PUT'], endpoint='pm_member_role')
    @wrap
    def pm_member_role(db, uid, pid, target):
        c = ctx(db, pid, 'owner', lock=True)
        role = one_of(_body().get('role'), ('editor', 'viewer'), 'Role')
        m = member_row(db, pid, target)
        if m['role'] == 'owner':
            raise Refuse(400, 'Transfer ownership instead.')
        if m['role'] != role:
            db.execute('UPDATE pm_members SET role = %s WHERE project_id = %s AND user_id = %s',
                       (role, pid, target))
            rev = touch(db, pid)
            activity(db, c, 'role', 'member', target, f"changed {m['name']} to {role}")
            db.commit()
            syslog('PM_MEMBER_ROLE', pid, user_id=target)
        else:
            rev = c.project['rev']
        return jsonify({'success': True, 'rev': rev, 'members': _members_out(db, pid)})

    @app.route('/api/projects/<int:pid>/members/<int:target>', methods=['DELETE'],
               endpoint='pm_member_remove')
    @wrap
    def pm_member_remove(db, uid, pid, target):
        """The owner removes someone, or a member leaves (target = self).
        Their tasks keep assignee_id (the page shows "(not on project)");
        their notifications about this project are scrubbed, read or not."""
        c = ctx(db, pid, 'view', lock=True)
        leaving = target == uid
        if c.readonly:
            raise Refuse(403, _VIEW_ONLY)
        if not leaving and c.role != 'owner':
            raise Refuse(403, _OWNER_ONLY)
        m = member_row(db, pid, target)
        if m['role'] == 'owner':
            raise Refuse(400, 'Transfer ownership before leaving.')
        db.execute('DELETE FROM pm_members WHERE project_id = %s AND user_id = %s', (pid, target))
        _scrub(db, pid=pid, user_id=target)
        rev = touch(db, pid)
        activity(db, c, 'left' if leaving else 'removed', 'member', target,
                 'left the project' if leaving else f"removed {m['name']}")
        db.commit()
        syslog('PM_MEMBER_LEAVE' if leaving else 'PM_MEMBER_REMOVE', pid, user_id=target)
        if leaving:                 # no longer entitled to the member list
            return jsonify({'success': True})
        return jsonify({'success': True, 'rev': rev, 'members': _members_out(db, pid)})

    @app.route('/api/projects/<int:pid>/members/<int:target>/owner', methods=['POST'],
               endpoint='pm_member_owner')
    @wrap
    def pm_member_owner(db, uid, pid, target):
        """Transfer ownership. Under the project lock (ctx re-reads my role
        after waiting, so a second transfer racing this one finds I am no
        longer the owner); the old owner is demoted FIRST — the one-owner
        unique index would refuse two owners even for a moment."""
        c = ctx(db, pid, 'owner', lock=True)
        m = member_row(db, pid, target)
        if target == uid or m['role'] == 'owner' or not m['active'] or m['ro']:
            raise Refuse(400, "That person can't be made the owner.")
        db.execute("UPDATE pm_members SET role = 'editor' WHERE project_id = %s AND user_id = %s",
                   (pid, uid))
        db.execute("UPDATE pm_members SET role = 'owner' WHERE project_id = %s AND user_id = %s",
                   (pid, target))
        rev = touch(db, pid)
        activity(db, c, 'owner', 'member', target, f"made {m['name']} the owner")
        notify(db, pid, [target], 'owner', _link(pid), 'o', actor_id=uid)
        db.commit()
        syslog('PM_OWNER_TRANSFER', pid, user_id=target)
        return jsonify({'success': True, 'rev': rev, 'members': _members_out(db, pid)})


def _routes_tasks(app):

    def write_response(db, c, before, saved, rev, **extra):
        tasks, new = _after_task_write(db, c, before, saved)
        return dict({'success': True, 'rev': rev, 'tasks': tasks, 'new_conflicts': new}, **extra)

    @app.route('/api/projects/<int:pid>/tasks', methods=['POST'], endpoint='pm_task_create')
    @wrap
    def pm_task_create(db, uid, pid):
        c = ctx(db, pid, 'edit', lock=True)
        body = _body()
        f = _task_fields(db, c, body)
        dep_ids = _dep_ids(db, pid, body.get('depends_on'))
        if _live_task_count(db, pid) >= LIMITS['tasks']:
            raise Refuse(400, f"This project has reached its limit of {LIMITS['tasks']} tasks.")
        before = _conflicts(*_sched(db, pid))
        status = f.get('status', 'todo')
        order = db.execute('SELECT COALESCE(MAX(sort_order), 0) + 10 AS n FROM pm_tasks WHERE project_id = %s',
                           (pid,)).fetchone()['n']
        tid = db.execute(
            """INSERT INTO pm_tasks (project_id, title, description, status, priority, assignee_id,
                      group_name, start_date, due_date, is_milestone, sort_order, contact_id,
                      completed_at, completed_by, created_by, updated_by, updated_name)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                       CASE WHEN %s THEN CURRENT_TIMESTAMP END, %s, %s, %s, %s) RETURNING id""",
            (pid, f['title'], f.get('description', ''), status, f.get('priority', 1),
             f.get('assignee_id'), f.get('group_name', ''), f.get('start_date'), f.get('due_date'),
             f.get('is_milestone', 0), order, f.get('contact_id'),
             status == 'done', uid if status == 'done' else None, uid, uid, c.name)).fetchone()['id']
        _set_deps(db, pid, tid, dep_ids)
        rev = touch(db, pid)
        activity(db, c, 'created', 'task', tid, f"created task {_quote(f['title'])}")
        _notify_assignee(db, c, tid, f['title'], f.get('assignee_id'))
        resp = write_response(db, c, before, [tid], rev, id=tid)
        db.commit()
        syslog('PM_TASK_CREATE', pid, task_id=tid)
        return jsonify(resp)

    @app.route('/api/projects/<int:pid>/tasks/<int:tid>', methods=['PUT'], endpoint='pm_task_update')
    @wrap
    def pm_task_update(db, uid, pid, tid):
        """PARTIAL: only keys present are written. Editors — or a view-only
        member changing ONLY the status of a task assigned to them (they do
        the work, they may tick it off)."""
        c = ctx(db, pid, 'view', lock=True)
        body = _body()
        cur = live_task(db, pid, tid, code=404)
        if not c.can_edit:
            own_status = (set(body) == {'status'} and cur['assignee_id'] == uid
                          and not c.readonly and not c.archived)
            if not own_status:
                raise Refuse(403, _ARCHIVED if c.archived else _VIEW_ONLY)
        f = _task_fields(db, c, body, cur)
        tasks, deps = _sched(db, pid)
        dep_ids = None
        if 'depends_on' in body:
            dep_ids = _dep_ids(db, pid, body['depends_on'], tid)
            # Edges to soft-deleted tasks stay, so they stay in the check.
            kept = {d for d in deps.get(tid, ()) if d in tasks and tasks[d]['gone']}
            if _would_loop(deps, tid, set(dep_ids) | kept):
                raise Refuse(400, 'That would make a loop.')
            live_now = sorted(d for d in deps.get(tid, ()) if d in tasks and not tasks[d]['gone'])
            if live_now == dep_ids:
                dep_ids = None
        changed = {k: v for k, v in f.items() if not _same(cur[k], v)}
        if not changed and dep_ids is None:     # an identical save is not a write
            return jsonify({'success': True, 'rev': c.project['rev'], 'new_conflicts': [],
                            'tasks': _tasks_out(db, pid, [tid], (tasks, deps))})
        before = _conflicts(tasks, deps)
        sets, args = ['updated_at = CURRENT_TIMESTAMP', 'updated_by = %s', 'updated_name = %s'], [uid, c.name]
        for k, v in changed.items():            # keys come from _task_fields' fixed list
            sets.append(f'{k} = %s')
            args.append(v)
        if 'status' in changed:
            if changed['status'] == 'done':
                sets += ['completed_at = CURRENT_TIMESTAMP', 'completed_by = %s']
                args.append(uid)
            else:
                sets += ['completed_at = NULL', 'completed_by = NULL']
        db.execute(f"UPDATE pm_tasks SET {', '.join(sets)} WHERE id = %s AND project_id = %s",
                   (*args, tid, pid))
        if dep_ids is not None:
            _set_deps(db, pid, tid, dep_ids)
        rev = touch(db, pid)
        title = changed.get('title', cur['title'])
        keys = set(changed) | ({'depends_on'} if dep_ids is not None else set())
        if keys == {'status'}:
            detail = f"set {_quote(title)} to {_TASK_STATUS_LABEL[changed['status']]}"
        elif keys == {'title'}:
            detail = f"renamed {_quote(cur['title'])} to {_quote(title)}"
        elif keys <= {'start_date', 'due_date'}:
            detail = f'changed the dates of {_quote(title)}'
        elif keys == {'assignee_id'}:
            who = user_name(db, changed['assignee_id'])
            detail = f'assigned {_quote(title)} to {who}' if who else f'unassigned {_quote(title)}'
        elif keys == {'depends_on'}:
            detail = f'changed what {_quote(title)} depends on'
        else:
            detail = f'updated {_quote(title)}'
        activity(db, c, 'updated', 'task', tid, detail)
        if 'assignee_id' in changed:
            _notify_assignee(db, c, tid, title, changed['assignee_id'])
        resp = write_response(db, c, before, [tid], rev)
        db.commit()
        syslog('PM_TASK_UPDATE', pid, task_id=tid)
        return jsonify(resp)

    @app.route('/api/projects/<int:pid>/tasks/<int:tid>', methods=['DELETE'], endpoint='pm_task_delete')
    @wrap
    def pm_task_delete(db, uid, pid, tid):
        c = ctx(db, pid, 'edit', lock=True)
        cur = live_task(db, pid, tid, code=404)
        before = _conflicts(*_sched(db, pid))
        db.execute('UPDATE pm_tasks SET deleted_at = CURRENT_TIMESTAMP, deleted_by = %s, deleted_name = %s '
                   'WHERE id = %s AND project_id = %s', (uid, c.name, tid, pid))
        rev = touch(db, pid)
        activity(db, c, 'deleted', 'task', tid, f"deleted task {_quote(cur['title'])}")
        resp = write_response(db, c, before, [], rev, deleted=tid)
        db.commit()
        syslog('PM_TASK_DELETE', pid, task_id=tid)
        return jsonify(resp)

    @app.route('/api/projects/<int:pid>/tasks/<int:tid>/restore', methods=['POST'], endpoint='pm_task_restore')
    @wrap
    def pm_task_restore(db, uid, pid, tid):
        c = ctx(db, pid, 'edit', lock=True)
        cur = db.execute('SELECT id, title FROM pm_tasks WHERE id = %s AND project_id = %s '
                         'AND deleted_at IS NOT NULL', (_url_id(tid), pid)).fetchone()
        if not cur:
            raise Refuse(404, _NOT_FOUND)
        if _live_task_count(db, pid) >= LIMITS['tasks']:
            raise Refuse(400, f"This project has reached its limit of {LIMITS['tasks']} tasks.")
        before = _conflicts(*_sched(db, pid))
        db.execute("UPDATE pm_tasks SET deleted_at = NULL, deleted_by = NULL, deleted_name = '' "
                   'WHERE id = %s AND project_id = %s', (tid, pid))
        rev = touch(db, pid)
        activity(db, c, 'restored', 'task', tid, f"restored task {_quote(cur['title'])}")
        resp = write_response(db, c, before, [tid], rev)
        db.commit()
        syslog('PM_TASK_RESTORE', pid, task_id=tid)
        return jsonify(resp)

    @app.route('/api/projects/<int:pid>/tasks/deleted', methods=['GET'], endpoint='pm_tasks_deleted')
    @wrap
    def pm_tasks_deleted(db, uid, pid):
        ctx(db, pid, 'edit')
        rows = db.execute(
            """SELECT id, title, deleted_at::timestamptz AS at, deleted_name FROM pm_tasks
                WHERE project_id = %s AND deleted_at IS NOT NULL
                ORDER BY deleted_at DESC, id DESC LIMIT 50""", (pid,)).fetchall()
        return jsonify({'tasks': [{'id': r['id'], 'title': r['title'], 'deleted_at': ts(r['at']),
                                   'deleted_by': r['deleted_name'] or ''} for r in rows]})

    @app.route('/api/projects/<int:pid>/tasks/reorder', methods=['POST'], endpoint='pm_tasks_reorder')
    @wrap
    def pm_tasks_reorder(db, uid, pid):
        """`ids` (live tasks of this project, no duplicates) keep the SET of
        sort_order values they already hold, handed out in the order given —
        so reordering one group or one board column from a stale tab can
        never undo what someone did to the rest. When those values tie the
        whole project is first renumbered 10, 20, 30… in its current order."""
        c = ctx(db, pid, 'edit', lock=True)
        raw = _body().get('ids')
        if not isinstance(raw, list) or not raw or len(raw) > LIMITS['tasks']:
            raise Refuse(400, 'Bad request.')
        ids = [to_int(v, 'Task') for v in raw]
        if len(set(ids)) != len(ids):
            raise Refuse(400, 'Bad request.')
        live = db.execute('SELECT id, sort_order FROM pm_tasks WHERE project_id = %s AND deleted_at IS NULL '
                          'ORDER BY sort_order, id', (pid,)).fetchall()
        cur = {r['id']: r['sort_order'] or 0 for r in live}
        if any(i not in cur for i in ids):
            raise Refuse(400, 'That task is not on this project.')
        new = dict(cur)
        if len({cur[i] for i in ids}) != len(ids):
            new = {r['id']: (n + 1) * 10 for n, r in enumerate(live)}
        for i, slot in zip(ids, sorted(new[i] for i in ids)):
            new[i] = slot
        moved = sorted(i for i in new if new[i] != cur[i])
        if not moved:
            return jsonify({'success': True, 'rev': c.project['rev'], 'tasks': []})
        # sort_order only: never updated_at / updated_by (a reorder is not an
        # edit of the task — open dialogs must not say "updated by …").
        db.execute(
            """UPDATE pm_tasks t SET sort_order = v.so
                 FROM unnest(%s::int[], %s::int[]) AS v(id, so)
                WHERE t.id = v.id AND t.project_id = %s""",
            (moved, [new[i] for i in moved], pid))
        rev = touch(db, pid)
        quiet = db.execute(
            "SELECT 1 FROM pm_activity WHERE project_id = %s AND user_id = %s AND verb = 'reordered' "
            "AND created_at > NOW() - INTERVAL '10 minutes' LIMIT 1", (pid, uid)).fetchone()
        if not quiet:               # dragging cards is many saves: one history line
            activity(db, c, 'reordered', 'project', pid, 'reordered tasks')
        db.commit()
        if not quiet:
            syslog('PM_TASK_REORDER', pid, count=len(moved))
        return jsonify({'success': True, 'rev': rev, 'tasks': _tasks_out(db, pid, moved)})

    @app.route('/api/projects/<int:pid>/tasks/<int:tid>/shift-preview', methods=['POST'],
               endpoint='pm_task_shift_preview')
    @wrap
    def pm_task_shift_preview(db, uid, pid, tid):
        """What `shift` would do — a read (POST only so it is never
        prefetched or cached). Nothing moves without the explicit shift."""
        ctx(db, pid, 'edit')
        live_task(db, pid, tid, code=404)
        moves = _shift_plan(*_sched(db, pid), tid)
        return jsonify({'moves': moves, 'token': _shift_token(moves)})

    @app.route('/api/projects/<int:pid>/tasks/<int:tid>/shift', methods=['POST'], endpoint='pm_task_shift')
    @wrap
    def pm_task_shift(db, uid, pid, tid):
        """Apply the previewed moves — only if a plan recomputed under the
        lock is identical to the one the person looked at (the token)."""
        c = ctx(db, pid, 'edit', lock=True)
        token = _body().get('token')
        live_task(db, pid, tid, code=404)
        tasks, deps = _sched(db, pid)
        moves = _shift_plan(tasks, deps, tid)
        if not isinstance(token, str) or token != _shift_token(moves):
            raise Refuse(409, 'Dates changed since the preview — review again.')
        if not moves:
            return jsonify({'success': True, 'rev': c.project['rev'], 'tasks': [],
                            'new_conflicts': [], 'moved': 0})
        before = _conflicts(tasks, deps)
        for m in moves:
            db.execute(
                'UPDATE pm_tasks SET start_date = %s, due_date = %s, updated_at = CURRENT_TIMESTAMP, '
                'updated_by = %s, updated_name = %s WHERE id = %s AND project_id = %s',
                (m['new_start'], m['new_due'], uid, c.name, m['id'], pid))
        rev = touch(db, pid)
        n = len(moves)
        activity(db, c, 'shifted', 'task', tid, f"moved {n} task{'' if n == 1 else 's'} later")
        for m in moves:
            who = tasks[m['id']]['assignee_id']
            if who:
                notify(db, pid, [who], 'shift', _link(pid, 'tasks', task=m['id']),
                       f"t:{m['id']}:shift", actor_id=uid, subject=m['title'])
        resp = write_response(db, c, before, [m['id'] for m in moves], rev, moved=n)
        db.commit()
        syslog('PM_TASK_SHIFT', pid, task_id=tid, count=n)
        return jsonify(resp)

    @app.route('/api/projects/<int:pid>/tasks/<int:tid>/to-show', methods=['POST'], endpoint='pm_task_to_show')
    @wrap
    def pm_task_to_show(db, uid, pid, tid):
        c = ctx(db, pid, 'edit', lock=True)
        cur = live_task(db, pid, tid, code=404)
        if cur['show_id']:
            raise Refuse(409, 'This task already has a show.')
        fields = _new_show_fields(_body())
        before = _conflicts(*_sched(db, pid))
        sid, name = _create_show(db, c, fields)
        db.execute('UPDATE pm_tasks SET show_id = %s, updated_at = CURRENT_TIMESTAMP, updated_by = %s, '
                   'updated_name = %s WHERE id = %s AND project_id = %s', (sid, uid, c.name, tid, pid))
        rev = touch(db, pid)
        activity(db, c, 'created', 'show', sid, f"made the show {_quote(name)} from {_quote(cur['title'])}")
        resp = write_response(db, c, before, [tid], rev, show_id=sid)
        db.commit()
        _show_created_log(sid, name)
        syslog('PM_TASK_TO_SHOW', pid, task_id=tid, show_id=sid)
        return jsonify(resp)


def _routes_comments(app):

    _COLS = (f"c.id, c.task_id, c.user_id, c.body, c.created_at::timestamptz AS at, "
             f"c.edited_at::timestamptz AS edited_at, COALESCE({_NAME_SQL}, NULLIF(c.author_name, ''), "
             f"'Former user') AS author")

    def out(r, c, owner):
        mine = r['user_id'] is not None and r['user_id'] == c['uid']
        alive = not c['readonly'] and not c['archived']
        return {'id': r['id'], 'task_id': r['task_id'], 'user_id': r['user_id'],
                'author': r['author'], 'body': r['body'], 'at': ts(r['at']),
                'edited_at': ts(r['edited_at']), 'mine': mine,
                'can_edit': mine and c['can_comment'],
                'can_delete': alive and (mine or owner)}

    def one(db, c, cid):
        r = db.execute(
            f'SELECT {_COLS} FROM pm_comments c LEFT JOIN users u ON u.id = c.user_id '
            'WHERE c.id = %s AND c.project_id = %s AND c.deleted_at IS NULL', (cid, c['pid'])).fetchone()
        return out(r, c, c['role'] == 'owner')

    def comment_row(db, pid, cid):
        """A live comment of this project (a task comment only while its
        task is live) — or 404."""
        r = db.execute(
            """SELECT c.*, t.title AS task_title FROM pm_comments c
                 LEFT JOIN pm_tasks t ON t.id = c.task_id AND t.project_id = c.project_id
                WHERE c.id = %s AND c.project_id = %s AND c.deleted_at IS NULL
                  AND (c.task_id IS NULL OR t.deleted_at IS NULL)""", (_url_id(cid), pid)).fetchone()
        if not r:
            raise Refuse(404, _NOT_FOUND)
        return r

    def link_for(pid, tid, cid):
        return (_link(pid, 'tasks', task=tid, comment=cid) if tid
                else _link(pid, 'discussion', comment=cid))

    @app.route('/api/projects/<int:pid>/comments', methods=['GET'], endpoint='pm_comments')
    @wrap
    def pm_comments(db, uid, pid):
        c = ctx(db, pid)
        tid = request.args.get('task_id')
        before = request.args.get('before')
        sql = (f'SELECT {_COLS} FROM pm_comments c LEFT JOIN users u ON u.id = c.user_id '
               'WHERE c.project_id = %s AND c.deleted_at IS NULL')
        args = [pid]
        if tid not in (None, ''):
            t = live_task(db, pid, to_int(tid, 'Task'), code=404)
            sql += ' AND c.task_id = %s'
            args.append(t['id'])
        else:
            sql += ' AND c.task_id IS NULL'
        if before not in (None, ''):
            sql += ' AND c.id < %s'
            args.append(to_int(before, 'Before'))
        rows = db.execute(sql + ' ORDER BY c.id DESC LIMIT 201', args).fetchall()
        owner = c.role == 'owner'
        return jsonify({'comments': [out(r, c, owner) for r in reversed(rows[:200])],
                        'has_more': len(rows) > 200})

    @app.route('/api/projects/<int:pid>/comments', methods=['POST'], endpoint='pm_comment_create')
    @wrap
    def pm_comment_create(db, uid, pid):
        c = ctx(db, pid, 'comment', lock=True)
        body = _body()
        text = clean_text(body.get('body'), CAPS['comment'], 'Comment')
        if not text:
            raise Refuse(400, 'Write a comment first.')
        task = live_task(db, pid, body.get('task_id'))
        tid = task['id'] if task else None
        cid = db.execute(
            'INSERT INTO pm_comments (project_id, task_id, user_id, author_name, body) '
            'VALUES (%s, %s, %s, %s, %s) RETURNING id', (pid, tid, uid, c.name, text)).fetchone()['id']
        rev = touch(db, pid)
        if task:
            activity(db, c, 'commented', 'task', tid, f"commented on {_quote(task['title'])}")
        else:
            activity(db, c, 'commented', 'discussion', cid, 'posted in the discussion')
        # @mentions: only members with access can be told (notify filters).
        who = set(_d['mention_user_ids'](db, text)) - {uid}
        notify(db, pid, who, 'mention', link_for(pid, tid, cid), f'c:{cid}', actor_id=uid,
               subject=task['title'] if task else '')
        resp = {'success': True, 'rev': rev, 'comment': one(db, c, cid)}
        db.commit()
        syslog('PM_COMMENT_CREATE', pid, comment_id=cid, **({'task_id': tid} if tid else {}))
        return jsonify(resp)

    @app.route('/api/projects/<int:pid>/comments/<int:cid>', methods=['PUT'], endpoint='pm_comment_update')
    @wrap
    def pm_comment_update(db, uid, pid, cid):
        c = ctx(db, pid, 'comment', lock=True)
        cur = comment_row(db, pid, cid)
        if cur['user_id'] != uid:
            raise Refuse(403, 'Only its author can edit a comment.')
        text = clean_text(_body().get('body'), CAPS['comment'], 'Comment')
        if not text:
            raise Refuse(400, 'Write a comment first.')
        if text == cur['body']:
            return jsonify({'success': True, 'rev': c.project['rev'], 'comment': one(db, c, cid)})
        db.execute('UPDATE pm_comments SET body = %s, edited_at = CURRENT_TIMESTAMP '
                   'WHERE id = %s AND project_id = %s', (text, cid, pid))
        rev = touch(db, pid)
        now = set(_d['mention_user_ids'](db, text))
        # Tell only people NEWLY mentioned; withdraw the unread, un-emailed
        # notice of anyone the edit no longer mentions.
        db.execute("DELETE FROM notifications WHERE kind = 'project' AND field_key = %s "
                   'AND read_at IS NULL AND emailed_at IS NULL AND NOT (user_id = ANY(%s::int[]))',
                   (f'pm:{pid}:c:{cid}', sorted(now)))
        fresh = now - set(_d['mention_user_ids'](db, cur['body'])) - {uid}
        notify(db, pid, fresh, 'mention', link_for(pid, cur['task_id'], cid), f'c:{cid}',
               actor_id=uid, subject=cur['task_title'] or '')
        resp = {'success': True, 'rev': rev, 'comment': one(db, c, cid)}
        db.commit()
        syslog('PM_COMMENT_EDIT', pid, comment_id=cid)
        return jsonify(resp)

    @app.route('/api/projects/<int:pid>/comments/<int:cid>', methods=['DELETE'], endpoint='pm_comment_delete')
    @wrap
    def pm_comment_delete(db, uid, pid, cid):
        """Soft delete — by its author or the project owner."""
        c = ctx(db, pid, 'view', lock=True)
        cur = comment_row(db, pid, cid)
        if c.archived:
            raise Refuse(403, _ARCHIVED)
        if c.readonly:
            raise Refuse(403, _VIEW_ONLY)
        if cur['user_id'] != uid and c.role != 'owner':
            raise Refuse(403, 'Only its author or the project owner can delete a comment.')
        db.execute('UPDATE pm_comments SET deleted_at = CURRENT_TIMESTAMP, deleted_by = %s '
                   'WHERE id = %s AND project_id = %s', (uid, cid, pid))
        db.execute("DELETE FROM notifications WHERE kind = 'project' AND field_key = %s "
                   'AND read_at IS NULL AND emailed_at IS NULL', (f'pm:{pid}:c:{cid}',))
        rev = touch(db, pid)
        if cur['task_id']:
            activity(db, c, 'uncommented', 'task', cur['task_id'],
                     f"deleted a comment on {_quote(cur['task_title'])}")
        else:
            activity(db, c, 'uncommented', 'discussion', cid, 'deleted a discussion post')
        db.commit()
        syslog('PM_COMMENT_DELETE', pid, comment_id=cid)
        return jsonify({'success': True, 'rev': rev, 'deleted': cid})

    @app.route('/api/projects/<int:pid>/activity', methods=['GET'], endpoint='pm_activity')
    @wrap
    def pm_activity(db, uid, pid):
        c = ctx(db, pid)
        entity = request.args.get('entity') or None
        if entity is not None and not _ENTITY_RE.match(entity):
            raise Refuse(400, 'Bad request.')
        if entity in BUDGET_ENTITIES and not c.can_budget:
            raise Refuse(404, _NOT_FOUND)
        eid, before = request.args.get('entity_id'), request.args.get('before')
        eid = to_int(eid, 'Entity') if eid not in (None, '') else None
        before = to_int(before, 'Before') if before not in (None, '') else None
        rows = _activity_rows(db, c, 51, entity, eid, before)
        return jsonify({'items': rows[:50], 'has_more': len(rows) > 50})


def _routes_shows(app):

    @app.route('/api/projects/<int:pid>/show-search', methods=['POST'], endpoint='pm_show_search')
    @wrap
    def pm_show_search(db, uid, pid):
        """Shows the caller may open that aren't linked yet; active first,
        then nearest to today. '' lists the nearest ones."""
        ctx(db, pid, 'edit')
        q = clean_text(_body().get('q'), CAPS['q'], 'Search', line=True)
        like = _like(q)
        rows = db.execute(
            """SELECT s.id, s.name, s.show_date, s.venue, s.show_mode, s.status,
                      COALESCE(s.is_test, 0) AS is_test
                 FROM shows s
                WHERE NOT EXISTS (SELECT 1 FROM pm_shows l
                                   WHERE l.project_id = %s AND l.show_id = s.id)
                  AND (s.name ILIKE %s OR COALESCE(s.venue, '') ILIKE %s)
                ORDER BY (COALESCE(s.status, 'active') <> 'active'), (s.show_date IS NULL),
                         ABS(s.show_date - %s), s.id DESC LIMIT 60""",
            (pid, like, like, date.today())).fetchall()
        can = _d['can_access_show']
        return jsonify({'results': [{
            'id': r['id'], 'name': r['name'], 'date': iso(r['show_date']), 'venue': r['venue'] or '',
            'show_mode': r['show_mode'] or 'show', 'status': r['status'] or 'active',
            'is_test': bool(r['is_test'])} for r in rows if can(uid, r['id'])][:20]})

    @app.route('/api/projects/<int:pid>/shows', methods=['POST'], endpoint='pm_show_link')
    @wrap
    def pm_show_link(db, uid, pid):
        """Link an existing show ({show_id}) or create a real one and link it
        ({new: {...}})."""
        c = ctx(db, pid, 'edit', lock=True)
        body = _body()
        created = None
        if body.get('new') is not None:
            sid, created = _create_show(db, c, _new_show_fields(body['new']))
            detail = f'created and linked the show {_quote(created)}'
        else:
            sid = to_int(body.get('show_id'), 'Show')
            s = db.execute('SELECT id, name FROM shows WHERE id = %s', (sid,)).fetchone()
            if not s or not _d['can_access_show'](uid, sid):
                raise Refuse(400, "That show can't be linked.")
            if db.execute('SELECT 1 FROM pm_shows WHERE project_id = %s AND show_id = %s',
                          (pid, sid)).fetchone():
                raise Refuse(409, 'That show is already linked.')
            db.execute('INSERT INTO pm_shows (project_id, show_id, linked_by) VALUES (%s, %s, %s)',
                       (pid, sid, uid))
            detail = f"linked the show {_quote(s['name'])}"
        rev = touch(db, pid)
        activity(db, c, 'linked', 'show', sid, detail)
        db.commit()
        if created is not None:
            _show_created_log(sid, created)
        syslog('PM_SHOW_LINK', pid, show_id=sid)
        return jsonify({'success': True, 'rev': rev, 'show_id': sid})

    @app.route('/api/projects/<int:pid>/shows/<int:show_id>', methods=['DELETE'], endpoint='pm_show_unlink')
    @wrap
    def pm_show_unlink(db, uid, pid, show_id):
        """Unlink only: the link row goes and this project's tasks stop
        pointing at the show. The show itself is never touched."""
        c = ctx(db, pid, 'edit', lock=True)
        gone = db.execute('DELETE FROM pm_shows WHERE project_id = %s AND show_id = %s RETURNING id',
                          (pid, _url_id(show_id))).fetchone()
        if not gone:
            raise Refuse(404, _NOT_FOUND)
        tids = [r['id'] for r in db.execute(
            'UPDATE pm_tasks SET show_id = NULL, updated_at = CURRENT_TIMESTAMP, updated_by = %s, '
            'updated_name = %s WHERE project_id = %s AND show_id = %s RETURNING id',
            (uid, c.name, pid, show_id)).fetchall()]
        rev = touch(db, pid)
        s = db.execute('SELECT name FROM shows WHERE id = %s', (show_id,)).fetchone()
        activity(db, c, 'unlinked', 'show', show_id,
                 f"unlinked the show {_quote(s['name'])}" if s else 'unlinked a show')
        db.commit()
        syslog('PM_SHOW_UNLINK', pid, show_id=show_id)
        return jsonify({'success': True, 'rev': rev,
                        'tasks': _tasks_out(db, pid, [t for t in tids])})


_CONTACT_FIELDS = (('name', 'title', 'Name', True), ('company', 'short', 'Company', True),
                   ('role', 'short', 'Role', True), ('email', 'email', 'Email', True),
                   ('phone', 'short', 'Phone', True), ('notes', 'long', 'Notes', False))


def _contacts_out(db, pid, cid=None):
    sql = ('SELECT id, name, company, role, email, phone, notes FROM pm_contacts '
           'WHERE project_id = %s AND deleted_at IS NULL')
    args = [pid]
    if cid is not None:
        sql += ' AND id = %s'
        args.append(cid)
    return [{'id': r['id'], 'name': r['name'], 'company': r['company'] or '', 'role': r['role'] or '',
             'email': r['email'] or '', 'phone': r['phone'] or '', 'notes': r['notes'] or ''}
            for r in db.execute(sql + ' ORDER BY LOWER(name), id', args).fetchall()]


def _routes_contacts(app):

    def fields(body, cur=None):
        out = {}
        for k, cap, label, line in _CONTACT_FIELDS:
            if k in body:
                out[k] = clean_text(body[k], CAPS[cap], label, line=line)
        if ('name' in out or cur is None) and not out.get('name'):
            raise Refuse(400, 'Give the contact a name.')
        return out

    @app.route('/api/projects/<int:pid>/contacts', methods=['GET'], endpoint='pm_contacts')
    @wrap
    def pm_contacts(db, uid, pid):
        ctx(db, pid)
        return jsonify({'contacts': _contacts_out(db, pid)})

    @app.route('/api/projects/<int:pid>/contacts', methods=['POST'], endpoint='pm_contact_create')
    @wrap
    def pm_contact_create(db, uid, pid):
        c = ctx(db, pid, 'edit', lock=True)
        body = _body()
        f = fields(body)
        src = body.get('source_contact_id')
        if src is not None:
            # Remembered as a plain number (which entry of the app's contact
            # list this was copied from) — it must at least exist.
            src = to_int(src, 'Contact')
            if not db.execute('SELECT 1 FROM contacts WHERE id = %s', (src,)).fetchone():
                raise Refuse(400, 'Contact is not valid.')
        n = db.execute('SELECT COUNT(*) AS n FROM pm_contacts WHERE project_id = %s AND deleted_at IS NULL',
                       (pid,)).fetchone()['n']
        if n >= LIMITS['contacts']:
            raise Refuse(400, f"This project has reached its limit of {LIMITS['contacts']} contacts.")
        cid = db.execute(
            'INSERT INTO pm_contacts (project_id, name, company, role, email, phone, notes, '
            'source_contact_id, created_by) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
            (pid, f['name'], f.get('company', ''), f.get('role', ''), f.get('email', ''),
             f.get('phone', ''), f.get('notes', ''), src, uid)).fetchone()['id']
        rev = touch(db, pid)
        activity(db, c, 'created', 'contact', cid, f"added the contact {_quote(f['name'])}")
        db.commit()
        syslog('PM_CONTACT_CREATE', pid, contact_id=cid)
        return jsonify({'success': True, 'rev': rev, 'contact': _contacts_out(db, pid, cid)[0]})

    @app.route('/api/projects/<int:pid>/contacts/<int:cid>', methods=['PUT'], endpoint='pm_contact_update')
    @wrap
    def pm_contact_update(db, uid, pid, cid):
        c = ctx(db, pid, 'edit', lock=True)
        cur = live_contact(db, pid, cid, code=404)
        f = {k: v for k, v in fields(_body(), cur).items() if (cur[k] or '') != v}
        if not f:
            return jsonify({'success': True, 'rev': c.project['rev'],
                            'contact': _contacts_out(db, pid, cid)[0]})
        sets = ', '.join(f'{k} = %s' for k in f)        # keys from _CONTACT_FIELDS
        db.execute(f'UPDATE pm_contacts SET {sets}, updated_at = CURRENT_TIMESTAMP '
                   'WHERE id = %s AND project_id = %s', (*f.values(), cid, pid))
        rev = touch(db, pid)
        activity(db, c, 'updated', 'contact', cid,
                 f"updated the contact {_quote(f.get('name', cur['name']))}")
        db.commit()
        syslog('PM_CONTACT_UPDATE', pid, contact_id=cid)
        return jsonify({'success': True, 'rev': rev, 'contact': _contacts_out(db, pid, cid)[0]})

    @app.route('/api/projects/<int:pid>/contacts/<int:cid>', methods=['DELETE'], endpoint='pm_contact_delete')
    @wrap
    def pm_contact_delete(db, uid, pid, cid):
        """Soft delete; this project's tasks and budget lines stop pointing
        at it (a deleted contact must not linger as a dangling reference)."""
        c = ctx(db, pid, 'edit', lock=True)
        cur = live_contact(db, pid, cid, code=404)
        db.execute('UPDATE pm_contacts SET deleted_at = CURRENT_TIMESTAMP WHERE id = %s AND project_id = %s',
                   (cid, pid))
        tids = [r['id'] for r in db.execute(
            'UPDATE pm_tasks SET contact_id = NULL, updated_at = CURRENT_TIMESTAMP, updated_by = %s, '
            'updated_name = %s WHERE project_id = %s AND contact_id = %s RETURNING id',
            (uid, c.name, pid, cid)).fetchall()]
        db.execute('UPDATE pm_budget_lines SET contact_id = NULL WHERE project_id = %s AND contact_id = %s',
                   (pid, cid))
        rev = touch(db, pid)
        activity(db, c, 'deleted', 'contact', cid, f"deleted the contact {_quote(cur['name'])}")
        db.commit()
        syslog('PM_CONTACT_DELETE', pid, contact_id=cid)
        return jsonify({'success': True, 'rev': rev, 'deleted': cid, 'tasks': _tasks_out(db, pid, tids)})

    @app.route('/api/projects/<int:pid>/contact-search', methods=['POST'], endpoint='pm_contact_search')
    @wrap
    def pm_contact_search(db, uid, pid):
        """The app's own contact list (visible to every signed-in user), to
        copy an entry into the project."""
        ctx(db, pid, 'edit')
        q = clean_text(_body().get('q'), CAPS['q'], 'Search', line=True)
        like = _like(q)
        rows = db.execute(
            """SELECT id, name, title, department, email, phone FROM contacts
                WHERE name ILIKE %s OR COALESCE(title, '') ILIKE %s OR COALESCE(department, '') ILIKE %s
                   OR COALESCE(email, '') ILIKE %s
                ORDER BY LOWER(name), id LIMIT 20""", (like, like, like, like)).fetchall()
        return jsonify({'results': [{
            'id': r['id'], 'name': r['name'], 'title': r['title'] or '',
            'department': r['department'] or '', 'email': r['email'] or '',
            'phone': r['phone'] or ''} for r in rows]})


def _routes_search(app):

    @app.route('/api/projects/<int:pid>/search', methods=['POST'], endpoint='pm_search')
    @wrap
    def pm_search(db, uid, pid):
        """Search inside ONE project the caller is a member of: LIVE rows
        only, budget rows only for someone who can see the budget, files
        only those the caller may see. The pattern is escaped and BOUND."""
        c = ctx(db, pid)
        q = clean_text(_body().get('q'), CAPS['q'], 'Search', line=True)
        if len(q) < 2:
            raise Refuse(400, 'Type at least 2 characters.')
        like, out = _like(q), []
        span = 120 + len(q)

        def add(kind, rid, title, text, tab, **params):
            out.append({'type': kind, 'id': rid, 'title': title or '',
                        'snippet': _snippet(text, q) if text else '', 'tab': tab, 'params': params})

        for r in db.execute(
                """SELECT id, title, description FROM pm_tasks
                    WHERE project_id = %s AND deleted_at IS NULL
                      AND (title ILIKE %s OR description ILIKE %s)
                    ORDER BY (title ILIKE %s) DESC, sort_order, id LIMIT 15""",
                (pid, like, like, like)).fetchall():
            add('task', r['id'], r['title'], r['description'], 'tasks', task=r['id'])
        for r in db.execute(
                """SELECT c.id, c.task_id, c.body, t.title FROM pm_comments c
                     LEFT JOIN pm_tasks t ON t.id = c.task_id AND t.project_id = c.project_id
                    WHERE c.project_id = %s AND c.deleted_at IS NULL AND c.body ILIKE %s
                      AND (c.task_id IS NULL OR t.deleted_at IS NULL)
                    ORDER BY c.id DESC LIMIT 12""", (pid, like)).fetchall():
            if r['task_id']:
                add('comment', r['id'], r['title'], r['body'], 'tasks', task=r['task_id'], comment=r['id'])
            else:
                add('comment', r['id'], 'Discussion', r['body'], 'discussion', comment=r['id'])
        for r in db.execute(
                """SELECT id, name, company, role, email, notes FROM pm_contacts
                    WHERE project_id = %s AND deleted_at IS NULL
                      AND (name ILIKE %s OR company ILIKE %s OR role ILIKE %s OR email ILIKE %s
                           OR notes ILIKE %s)
                    ORDER BY LOWER(name), id LIMIT 8""", (pid, like, like, like, like, like)).fetchall():
            text = next((r[k] for k in ('notes', 'company', 'role', 'email')
                         if r[k] and q.lower() in r[k].lower()), '')
            add('contact', r['id'], r['name'], text, 'contacts', contact=r['id'])
        # Long texts (a note's body, a file's extracted text) are cut around
        # the hit by PostgreSQL — never shipped whole just to make a snippet.
        for r in db.execute(
                """SELECT id, title,
                          substr(body_text, GREATEST(strpos(lower(body_text), lower(%s)) - 60, 1), %s) AS hit
                     FROM pm_notes
                    WHERE project_id = %s AND deleted_at IS NULL
                      AND (title ILIKE %s OR body_text ILIKE %s)
                    ORDER BY updated_at DESC, id LIMIT 8""", (q, span, pid, like, like)).fetchall():
            add('note', r['id'], r['title'], r['hit'], 'notes', note=r['id'])
        for r in db.execute(
                f"""SELECT f.id, f.title, v.filename,
                           substr(v.extracted_text,
                                  GREATEST(strpos(lower(v.extracted_text), lower(%s)) - 60, 1), %s) AS hit,
                           (v.extracted_text ILIKE %s) AS in_text
                      FROM pm_files f
                      LEFT JOIN pm_file_versions v ON v.id = f.current_version_id
                           AND v.file_id = f.id AND v.project_id = f.project_id
                           AND v.preview_of IS NULL AND v.deleted_at IS NULL
                     WHERE f.project_id = %s AND f.deleted_at IS NULL AND {can_see_file_sql(c)}
                       AND (f.title ILIKE %s OR v.filename ILIKE %s OR v.extracted_text ILIKE %s)
                     ORDER BY f.id DESC LIMIT 10""", (q, span, like, pid, like, like, like)).fetchall():
            add('file', r['id'], r['title'] or r['filename'],
                r['hit'] if r['in_text'] else (r['filename'] or ''), 'files', file=r['id'])
        if c.can_budget:
            for r in db.execute(
                    """SELECT id, category, description, notes FROM pm_budget_lines
                        WHERE project_id = %s AND deleted_at IS NULL
                          AND (category ILIKE %s OR description ILIKE %s OR notes ILIKE %s)
                        ORDER BY sort_order, id LIMIT 8""", (pid, like, like, like)).fetchall():
                text = r['notes'] if r['notes'] and q.lower() in r['notes'].lower() else (r['category'] or '')
                add('budget_line', r['id'], r['description'] or r['category'], text, 'budget', line=r['id'])
            for r in db.execute(
                    """SELECT s.id, s.line_id, s.label, s.body FROM pm_budget_sources s
                         JOIN pm_budget_lines l ON l.id = s.line_id AND l.project_id = s.project_id
                        WHERE s.project_id = %s AND l.deleted_at IS NULL
                          AND (s.label ILIKE %s OR s.body ILIKE %s)
                        ORDER BY s.id DESC LIMIT 6""", (pid, like, like)).fetchall():
                add('budget_source', r['id'], r['label'] or 'Source', r['body'], 'budget', line=r['line_id'])
        return jsonify({'results': out[:60]})


def _routes_admin(app):
    """The admin page's API. Works with the module off. An admin here manages
    WHO may use the module — never what is in a project: nothing below reads
    a project's name or content (orphans are listed by id)."""
    log_audit = lambda *a, **k: _d['log_audit'](*a, **k)

    @app.route('/api/projects/admin/access', methods=['GET'], endpoint='pm_admin_access')
    @wrap_admin
    def pm_admin_access(db, uid):
        users = db.execute(
            f"""SELECT u.id, {_NAME_SQL} AS name, u.username, u.role,
                       EXISTS (SELECT 1 FROM pm_access a WHERE a.user_id = u.id) AS has_access
                  FROM users u WHERE {_USABLE_USER_SQL}
                 ORDER BY LOWER({_NAME_SQL}), u.id""").fetchall()
        orphans = db.execute(
            'SELECT p.id FROM pm_projects p WHERE NOT EXISTS '
            '(SELECT 1 FROM pm_members m WHERE m.project_id = p.id) ORDER BY p.id').fetchall()
        try:
            import project_docs
            msg = bool(project_docs.has_msg_support())
        except Exception:
            msg = False
        return jsonify({
            'enabled': _module_on(db),
            'users': [{'id': r['id'], 'name': r['name'], 'username': r['username'],
                       'role': r['role'], 'has_access': bool(r['has_access'])} for r in users],
            'orphans': [{'id': r['id']} for r in orphans],
            'msg_support': msg,
            # Office previews are made by LibreOffice (the injected converter
            # shells out to `soffice`).
            'office_preview': shutil.which('soffice') is not None})

    @app.route('/api/projects/admin/access', methods=['POST'], endpoint='pm_admin_access_set')
    @wrap_admin
    def pm_admin_access_set(db, uid):
        body = _body()
        target = to_int(body.get('user_id'), 'User')
        if not isinstance(body.get('grant'), bool):
            raise Refuse(400, 'Bad request.')
        if body['grant']:
            if not db.execute(f'SELECT 1 FROM users u WHERE u.id = %s AND {_USABLE_USER_SQL}',
                              (target,)).fetchone():
                raise Refuse(400, "That account can't be given access.")
            db.execute('INSERT INTO pm_access (user_id, granted_by) VALUES (%s, %s) '
                       'ON CONFLICT (user_id) DO NOTHING', (target, uid))
            action = 'PM_ACCESS_GRANT'
        else:
            if not db.execute('SELECT 1 FROM users WHERE id = %s', (target,)).fetchone():
                raise Refuse(400, 'User is not valid.')
            db.execute('DELETE FROM pm_access WHERE user_id = %s', (target,))
            # Their member rows stay (re-granting restores them) but every
            # notification they hold goes, read or not: a bell they can
            # still open must not keep project and task titles.
            _scrub(db, user_id=target)
            action = 'PM_ACCESS_REVOKE'
        log_audit(db, action, 'user', target)
        db.commit()
        syslog(action, user_id=target)
        return jsonify({'success': True, 'user_id': target, 'has_access': body['grant']})

    @app.route('/api/projects/admin/module', methods=['POST'], endpoint='pm_admin_module')
    @wrap_admin
    def pm_admin_module(db, uid):
        """The module switch (it is not listed in Settings → Modules for
        admins without access, so it lives here). Same row, audit entry and
        syslog line as POST /settings/modules."""
        v = _body().get('enabled')
        if not isinstance(v, bool):
            raise Refuse(400, 'Bad request.')
        enabled = '1' if v else '0'
        db.execute('INSERT INTO app_settings (key, value) VALUES (%s, %s) '
                   'ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value', (_SETTING, enabled))
        log_audit(db, 'SETTINGS_CHANGE', 'setting', None, detail=f'{_SETTING}={enabled}')
        db.commit()
        try:
            _d['syslog_logger'].info(f'MODULE_TOGGLE key={MODULE_KEY} enabled={enabled} by={_username()}')
        except Exception:
            pass
        return jsonify({'success': True, 'enabled': v})

    @app.route('/api/projects/admin/orphans/<int:pid>/delete', methods=['POST'],
               endpoint='pm_admin_orphan_delete')
    @wrap_admin
    def pm_admin_orphan_delete(db, uid, pid):
        """Delete a project NOBODY is on any more (every member's account was
        deleted) — the only thing an admin can do to a project, and only to
        one with no members: a project with even one member answers 404."""
        if _body().get('confirm') != 'DELETE':
            raise Refuse(400, 'Type DELETE to confirm.')
        p = db.execute('SELECT id FROM pm_projects WHERE id = %s FOR UPDATE', (_url_id(pid),)).fetchone()
        if not p or db.execute('SELECT 1 FROM pm_members WHERE project_id = %s LIMIT 1', (pid,)).fetchone():
            raise Refuse(404, _NOT_FOUND)
        keys = _delete_project(db, pid)
        log_audit(db, 'PM_ORPHAN_DELETE', 'project', pid)
        db.commit()
        _s3_cleanup(keys, pid)
        syslog('PM_ORPHAN_DELETE', pid)
        return jsonify({'success': True})


# ─── Registration ─────────────────────────────────────────────────────────────

def register(app, **deps):
    """Wire the module in (called once from app.py). deps: get_db,
    get_current_user, log_audit, syslog_logger, module_enabled,
    can_access_show, ts_out, as_date, notify_users, mention_user_ids,
    get_distinct_venues, get_upload_max, served_type,
    safe_content_disposition, cols_except, office_to_pdf, pdf_font_config,
    get_logo, file_store, s3_storage, db_unavailable, app_version,
    create_show, can_create_show, show_bars, show_figures.
    Builds `core` (the namespace projects_docs_module registers through),
    installs the ONE gate and the routes."""
    global _core
    _d.clear()
    _d.update(deps, app=app)
    core = types.SimpleNamespace(
        wrap=wrap, wrap_admin=wrap_admin, ctx=ctx, Refuse=Refuse, touch=touch,
        activity=activity, notify=notify, syslog=syslog, member_ids=member_ids,
        user_name=user_name, d=_d, clean_text=clean_text, parse_date=parse_date,
        money=money, iso=iso, ts=ts, valid_color=valid_color, live_task=live_task,
        live_contact=live_contact, can_see_file_sql=can_see_file_sql,
        # Beyond the spec's list — the same helpers this file uses:
        body=_body, to_int=to_int, to_flag=to_flag, one_of=one_of, url_id=_url_id,
        like=_like, snippet=_snippet, link=_link, withdraw=_withdraw, scrub=_scrub,
        owner_id=_owner_id, log_failure=_log_failure, s3_cleanup=_s3_cleanup,
        quote=_quote, limits=LIMITS, caps=CAPS, BUDGET_ENTITIES=BUDGET_ENTITIES,
        access_sql=_access_sql, NAME_SQL=_NAME_SQL)
    _core = core

    @app.before_request
    def _pm_gate():
        """THE gate. Any path equal to or under the module's three trees —
        routed or not, any method — is a plain 404 for anyone who may not
        use it. before_request runs before Flask would answer 405 or an
        automatic OPTIONS, so those can't reveal that a route exists.
        (Registered after app.py's own before_request functions on purpose:
        the CSRF check, the document-viewer gate etc. answer first, exactly
        as they do for a url that does not exist.)"""
        # '//projects' would be redirected to '/projects' by the router
        # (merge_slashes) — a redirect a missing url never gets.
        path = re.sub(r'/{2,}', '/', request.path or '')
        if not _is_under(path, _GATED):
            return None
        if not session.get('user_id'):
            abort(404)
        if _is_under(path, _ADMIN):
            if session.get('user_role') != 'admin':
                abort(404)
            return None
        if not has_access():
            abort(404)
        return None

    @app.context_processor
    def _inject_projects():
        def projects_enabled():
            # Display only (base.html renders on error pages during an
            # outage): an unreachable database hides the link. Routes use
            # has_access(), which raises → 503.
            try:
                return has_access()
            except _d['db_unavailable']:
                return False
        return {'projects_enabled': projects_enabled}

    _routes_pages(app)
    _routes_admin(app)          # before '/api/projects/<int:pid>…' for readability only
    _routes_projects(app)
    _routes_members(app)
    _routes_tasks(app)
    _routes_comments(app)
    _routes_shows(app)
    _routes_contacts(app)
    _routes_search(app)

    # Budgets, files and notes. Optional while the build is in progress.
    try:
        import projects_docs_module
    except ImportError:
        projects_docs_module = None
    if projects_docs_module is not None:
        projects_docs_module.register(app, core)
        _d['docs'] = projects_docs_module
    return core
