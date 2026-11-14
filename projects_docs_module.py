# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""Projects (3.49.0) — the document half: budgets with sources, files with
versions, notes with history, and the executive-summary PDF.

SANDBOXED like its sibling: never imports app.py. projects_module.register()
imports this file and calls `register(app, core)`; everything from app.py
arrives through `core.d` (the deps dict) and every route uses the sibling's
conventions — `@core.wrap` views taking (db, uid, pid, …), `core.ctx()` as
the one door to a project (lock=True on EVERY write), then
writes → `core.touch` → `core.activity` → commit → `core.syslog`.

The rules this file protects (they are the point of the feature)
- PRIVATE. Nothing a person typed or uploaded reaches a log line, the audit
  log or a URL: `core.syslog` writes verbs + integer ids, failures are logged
  by exception TYPE only (`core.log_failure`), generated filenames carry ids
  (`project_summary_<pid>_<date>.pdf`, `project_note_<nid>.pdf`), S3 keys are
  ids. There is NO `log_audit` call in this file.
- EVERY child row is fetched with its project id (`WHERE id = %s AND
  project_id = %s`; versions also `AND file_id = %s`). An id from someone
  else's project is "Not found" (404 for an id in the URL, 400 for one in the
  request body) — the same answer as an id that never existed.
- BUDGET VISIBILITY. `ctx(…, 'budget')` answers 404 for a member who may not
  see the budget (they must not learn it has content), and for that member a
  `budget_only` file DOES NOT EXIST: every file read path goes through
  `_file()`, which applies `core.can_see_file_sql(c)`. Budget history lives
  in pm_activity under the entities `budget_line` / `budget_source` ONLY —
  the sibling's readers hide those two — and its detail never holds an
  amount. Activity about a budget-only file uses `budget_source` too.
- FILES follow CLAUDE.md "File redundancy": the bytes live on
  pm_file_versions (file_store kind `project_files`), every read goes through
  `file_store.read_bytes`, `content_sha256` is the hash of the UNCOMPRESSED
  bytes, deleting is ARCHIVE (gzip the database copy, keep the S3 object) and
  only the owner's purge of an already archived file destroys bytes — S3
  deletes happen AFTER the commit that removed the rows.
- NO TRANSACTION AND NO PROJECT LOCK ACROSS S3, LibreOffice or a parser. An
  upload is three steps: (1) one short transaction stores the row with its
  bytes; (2) with nothing open, the S3 upload; (3) one short conditional
  UPDATE records the key. Text extraction runs afterwards on a worker thread
  with its own connections and ends in a conditional UPDATE.
- A cached PDF preview of an office file is a pm_file_versions row with
  `preview_of` set (version = −source version). It is never a version: every
  list / count / next-version / download query here says
  `preview_of IS NULL`.
- NOTES are HTML typed by a person: sanitized by project_docs on the way IN
  and again whenever they are served or rendered. A save is atomic on the
  note's own `rev` (409 with the current text on a mismatch) and the text it
  replaces is kept as a version first.
- GETs never lock, never bump a rev and never write — with one sanctioned
  exception: preview.pdf stores the converted PDF so LibreOffice runs once.
"""

import gzip
import json
import queue
import re
import threading
import unicodedata
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from flask import jsonify, make_response, render_template, request
from markupsafe import Markup
from werkzeug.utils import secure_filename

import project_docs

core = None             # the namespace projects_module.register() built (set by register)

_NOT_FOUND = 'Not found.'
_TOO_LONG = 'This note is too long.'
_NO_PREVIEW = 'No preview available.'
_NO_SUCH_FILE = 'That file is not on this project.'

_MONEY = (('estimate', 'Estimate'), ('committed', 'Committed'), ('actual', 'Actual'))
# (column, cap key in core.caps, label, single line)
_LINE_TEXT = (('category', 'short', 'Category', True),
              ('description', 'long', 'Description', True),
              ('notes', 'long', 'Notes', False))

_TEXT_PREVIEW = 20000           # characters of a file's text sent with its detail
_MAX_VERSIONS = 200             # real versions kept for one file
_NOTE_VERSIONS = 50             # earlier texts kept for one note
_NOTE_VERSION_GAP = '10 minutes'   # one person's typing cuts a version this often
_HISTORY_MAX = 200
_PREVIEW_CACHE_MAX = 64 * 1024 * 1024
_SIZE_SLACK = 1024 * 1024       # multipart framing around the file itself
# What LibreOffice is asked to convert. Only the EXTENSION is ever handed to
# the converter ('file.docx') — never the person's filename.
_OFFICE_EXTS = frozenset(('doc', 'docx', 'odt', 'rtf', 'ppt', 'pptx', 'odp', 'xls', 'xlsx', 'ods'))
# Stored types a browser may show in place. Everything else is a download.
_INLINE_KINDS = frozenset(('pdf', 'image', 'text'))

_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
_TASK_STATUS = (('todo', 'To do'), ('doing', 'In progress'), ('blocked', 'Blocked'), ('done', 'Done'))
_PROJECT_STATUS = {'planning': 'Planning', 'active': 'Active', 'on_hold': 'On hold',
                   'done': 'Done', 'archived': 'Archived'}
_ROLE = {'owner': 'Owner', 'editor': 'Editor', 'viewer': 'Viewer'}
_PDF_LIST_MAX = 40              # rows per table in the summary PDF


# ─── Small helpers ────────────────────────────────────────────────────────────

def _refuse(code, message, **extra):
    return core.Refuse(code, message, **extra)


def _num(v):
    """A stored DOUBLE PRECISION for JSON (None stays None)."""
    return None if v is None else round(float(v), 2)


def _money_text(v):
    """An amount as budget-history text. (pm_budget_history is read only
    through ctx 'budget'; pm_activity never gets an amount.)"""
    return None if v is None else f'{float(v):.2f}'


def _over(estimate, actual):
    return estimate is not None and actual is not None and float(actual) > float(estimate)


def _ext(filename):
    """The lower-case extension, when it is a short plain one."""
    m = re.search(r'\.([A-Za-z0-9]{1,10})$', filename or '')
    return m.group(1).lower() if m else ''


def _clean_filename(raw):
    """The name of an uploaded file as it is STORED (and shown, by
    textContent): the last path segment only, no control / format characters
    (that includes the bidi overrides that make `exe.fdp` read as `pdf.exe`)
    or lone surrogates, whitespace folded, ≤ 200 characters with the
    extension kept. It is never used as a path, an S3 key or — raw — a
    header: downloads go through _header_name()."""
    s = unicodedata.normalize('NFC', str(raw or ''))
    s = s.replace('\\', '/').rsplit('/', 1)[-1]
    s = ''.join(ch if unicodedata.category(ch)[0] != 'C' else ' ' for ch in s)
    s = ' '.join(s.split()).strip(' .')
    if len(s) > 200:
        ext = _ext(s)
        s = (s[:199 - len(ext)].rstrip(' .') + '.' + ext) if ext else s[:200]
    return s or 'file'


def _header_name(filename, vid):
    """An ASCII name for Content-Disposition. secure_filename() drops every
    non-ASCII character, which can leave nothing — or just the extension —
    of a name in another script: then the download is `file_<id>.<ext>`."""
    safe = secure_filename(filename or '')[:150]
    ext = _ext(filename)
    if not safe or (ext and not safe.lower().endswith('.' + ext)):
        safe = f'file_{int(vid)}' + (f'.{ext}' if ext else '')
    return safe


def _fmt_date(v):
    """'Dec 1, 2026' from a date / ISO string (English month names — never
    %b, which follows the server's locale)."""
    if not v:
        return ''
    if isinstance(v, str):
        try:
            v = date.fromisoformat(v[:10])
        except ValueError:
            return ''
    return f'{_MONTHS[v.month - 1]} {v.day}, {v.year}'


def _fmt_money(v):
    if v is None:
        return '—'
    v = round(float(v), 2)
    return ('−' if v < 0 else '') + f'${abs(v):,.2f}'


def _pct(part, whole):
    """A bar width: an integer 0–100, whatever the numbers are (negative,
    zero, absurdly large, not a number at all)."""
    try:
        part, whole = float(part or 0), float(whole or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    if part != part or whole != whole or part <= 0:     # NaN, or nothing to show
        return 0
    if whole <= 0 or part >= whole:
        return 100
    return int(round(100.0 * part / whole))


def _doc_response(data, ctype, disposition, sandbox=True):
    """Every document this module serves: never cached anywhere, never
    sniffed, and — unless it is a PDF / image shown in place — in a CSP
    sandbox with nothing allowed, so an uploaded file can not act as a page
    of this site whatever it contains."""
    resp = make_response(data)
    resp.headers['Content-Type'] = ctype
    resp.headers['Content-Disposition'] = disposition
    resp.headers['Cache-Control'] = 'private, no-store'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    if sandbox:
        resp.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return resp


def _file_entity(budget_only):
    """pm_activity entity for a file write: a budget-only file's history is
    budget history (hidden from members who may not see the budget)."""
    return 'budget_source' if budget_only else 'file'


def _file_activity(db, c, verb, f_id, budget_only, detail):
    # A budget-only file's rows carry no entity_id: `budget_source` ids are
    # source ids, and a file id there would point at the wrong thing.
    core.activity(db, c, verb, _file_entity(budget_only), None if budget_only else f_id, detail)


# ─── Budget ───────────────────────────────────────────────────────────────────

def _live_line(db, pid, lid, code=404):
    """The live budget line `lid` OF THIS PROJECT (404 for a URL id, 400 for
    one named in a request body)."""
    if code == 400:
        lid = core.to_int(lid, 'Budget line')
    else:
        core.url_id(lid)
    r = db.execute('SELECT * FROM pm_budget_lines WHERE id = %s AND project_id = %s '
                   'AND deleted_at IS NULL', (lid, pid)).fetchone()
    if not r:
        raise _refuse(code, _NOT_FOUND if code == 404 else 'That budget line is not on this project.')
    return r


def _sources(db, pid, line_id=None, source_id=None):
    """SOURCE objects (plus line_id) of this project's LIVE lines. A file
    source is pinned to the version that was current when it was attached:
    `newer_version` says the file has moved on, `file_missing` that the file
    was archived or purged (the line keeps the record that there was one)."""
    sql = """SELECT s.id, s.line_id, s.kind, s.label, s.body, s.url, s.file_id, s.version_id,
                    s.creator_name, s.created_at::timestamptz AS at_tz,
                    f.id AS f_id, f.title AS f_title, f.deleted_at AS f_deleted,
                    f.current_version_id AS f_current,
                    v.id AS v_id, v.kind AS v_kind, v.filename AS v_filename
               FROM pm_budget_sources s
               JOIN pm_budget_lines l ON l.id = s.line_id AND l.project_id = s.project_id
               LEFT JOIN pm_files f ON f.id = s.file_id AND f.project_id = s.project_id
               LEFT JOIN pm_file_versions v ON v.id = s.version_id AND v.file_id = s.file_id
                    AND v.project_id = s.project_id AND v.preview_of IS NULL
              WHERE s.project_id = %s AND l.deleted_at IS NULL"""
    args = [pid]
    if line_id is not None:
        sql += ' AND s.line_id = %s'
        args.append(line_id)
    if source_id is not None:
        sql += ' AND s.id = %s'
        args.append(source_id)
    out = []
    for r in db.execute(sql + ' ORDER BY s.id', args).fetchall():
        is_file = r['kind'] == 'file'
        missing = is_file and (r['f_id'] is None or r['f_deleted'] is not None or r['v_id'] is None)
        out.append({
            'id': r['id'], 'line_id': r['line_id'], 'kind': r['kind'],
            'label': r['label'] or '', 'body': r['body'] or '', 'url': r['url'] or '',
            'file_id': r['f_id'] if is_file else None,
            'version_id': r['version_id'] if is_file else None,
            'file_title': (r['f_title'] or r['v_filename'] or '') if is_file else '',
            'file_kind': (r['v_kind'] or '') if is_file else '',
            'file_missing': bool(missing),
            'newer_version': bool(is_file and not missing and r['f_current'] != r['version_id']),
            'creator': r['creator_name'] or '', 'at': core.ts(r['at_tz'])})
    return out


def _budget(db, pid, lid=None):
    """{lines, totals, by_category} for the project's live lines (or the one
    line `lid`). A line's task_id reads null while that task is deleted — the
    column keeps it, so restoring the task brings the link back."""
    sql = """SELECT l.id, l.category, l.description, l.estimate, l.committed, l.actual,
                    k.id AS contact_id, t.id AS task_id, l.notes, l.sort_order,
                    l.updated_at::timestamptz AS updated_tz
               FROM pm_budget_lines l
               LEFT JOIN pm_tasks t ON t.id = l.task_id AND t.project_id = l.project_id
                    AND t.deleted_at IS NULL
               LEFT JOIN pm_contacts k ON k.id = l.contact_id AND k.project_id = l.project_id
                    AND k.deleted_at IS NULL
              WHERE l.project_id = %s AND l.deleted_at IS NULL"""
    args = [pid]
    if lid is not None:
        sql += ' AND l.id = %s'
        args.append(lid)
    by_line = {}
    for s in _sources(db, pid, line_id=lid):
        by_line.setdefault(s['line_id'], []).append(s)
    lines = []
    for r in db.execute(sql + ' ORDER BY l.sort_order, l.id', args).fetchall():
        lines.append({
            'id': r['id'], 'category': r['category'] or '', 'description': r['description'] or '',
            'estimate': _num(r['estimate']), 'committed': _num(r['committed']),
            'actual': _num(r['actual']), 'contact_id': r['contact_id'], 'task_id': r['task_id'],
            'notes': r['notes'] or '', 'sort_order': r['sort_order'] or 0,
            'over': _over(r['estimate'], r['actual']),
            'sources': by_line.get(r['id'], []), 'updated_at': core.ts(r['updated_tz'])})

    def total(rows):
        # Sums are always numbers (an empty column is 0.00); only a single
        # line's own amount can be null ("not entered").
        return {k: round(sum(x[k] for x in rows if x[k] is not None), 2) for k, _ in _MONEY}

    cats = {}
    for ln in lines:
        cats.setdefault(ln['category'], []).append(ln)
    return {'lines': lines, 'totals': total(lines),
            'by_category': [dict(total(rows), category=cat) for cat, rows in cats.items()]}


def _line_fields(db, pid, body, cur=None):
    """Validated columns present in a create / partial-update body."""
    out = {}
    for k, cap, label, line in _LINE_TEXT:
        if k in body:
            out[k] = core.clean_text(body[k], core.caps[cap], label, line=line)
    if (cur is None or 'description' in out) and not out.get('description'):
        raise _refuse(400, 'Give the line a description.')
    for k, label in _MONEY:
        if k in body:
            try:
                out[k] = core.money(body[k], label)
            except OverflowError:   # a JSON integer too big to be a float at all
                raise _refuse(400, f'{label} is not a valid amount.')
    # Ids in a body are checked against THIS project (400), never trusted.
    if 'contact_id' in body:
        r = core.live_contact(db, pid, body['contact_id'])
        out['contact_id'] = r['id'] if r else None
    if 'task_id' in body:
        r = core.live_task(db, pid, body['task_id'])
        out['task_id'] = r['id'] if r else None
    return out


def _line_changed(cur, k, v):
    old = cur[k]
    if k in ('estimate', 'committed', 'actual'):
        return _num(old) != v
    if k in ('contact_id', 'task_id'):
        return old != v
    return (old or '') != v


def _budget_alert(db, c, lid, was_over, now_over):
    """The `budget` notification: to the OWNER only, when a line's actual
    goes above its estimate and was not above before. It names no line and no
    amount (title = the fixed text, body = the project) — the link opens the
    line. Back under the estimate (or deleted) → an unread notice is
    withdrawn."""
    if now_over and not was_over:
        owner = core.owner_id(db, c.pid)
        if owner:
            core.notify(db, c.pid, [owner], 'budget', core.link(c.pid, 'budget', line=lid),
                        f'b:{int(lid)}', actor_id=c.uid)
    elif was_over and not now_over:
        core.withdraw(db, c.pid, f'b:{int(lid)}')


def _clean_url(v):
    """A link source: http / https with a host, nothing else. `javascript:`,
    `data:`, a scheme hidden behind whitespace or a control character, and
    scheme-less text are all refused — the client writes this into an href."""
    bad = _refuse(400, 'Links must start with http:// or https://.')
    s = core.clean_text(v, core.caps['url'], 'Link', required=True)
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f or ch in '<>"\'\\`' for ch in s):
        raise bad
    try:
        parts = urlsplit(s)
        host = parts.hostname
    except ValueError:
        raise bad
    if parts.scheme.lower() not in ('http', 'https') or not host or not s.lower().startswith(
            (parts.scheme.lower() + '://')):
        raise bad
    return s


def _routes_budget(app):

    @app.route('/api/projects/<int:pid>/budget', methods=['GET'], endpoint='pm_budget')
    @core.wrap
    def pm_budget(db, uid, pid):
        c = core.ctx(db, pid, 'budget')
        return jsonify(dict(_budget(db, pid), can_edit=bool(c.can_edit)))

    @app.route('/api/projects/<int:pid>/budget/lines', methods=['POST'], endpoint='pm_budget_line_create')
    @core.wrap
    def pm_budget_line_create(db, uid, pid):
        c = core.ctx(db, pid, 'budget_edit', lock=True)
        f = _line_fields(db, pid, core.body())
        n = db.execute('SELECT COUNT(*) AS n, COALESCE(MAX(sort_order), 0) AS top FROM pm_budget_lines '
                       'WHERE project_id = %s AND deleted_at IS NULL', (pid,)).fetchone()
        if n['n'] >= core.limits['budget_lines']:
            raise _refuse(400, f"This project has reached its limit of "
                               f"{core.limits['budget_lines']} budget lines.")
        lid = db.execute(
            'INSERT INTO pm_budget_lines (project_id, category, description, estimate, committed, '
            'actual, contact_id, task_id, notes, sort_order, created_by) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id',
            (pid, f.get('category', ''), f['description'], f.get('estimate'), f.get('committed'),
             f.get('actual'), f.get('contact_id'), f.get('task_id'), f.get('notes', ''),
             n['top'] + 10, uid)).fetchone()['id']
        rev = core.touch(db, pid)
        core.activity(db, c, 'created', 'budget_line', lid,
                      f"added the budget line {core.quote(f['description'])}")
        _budget_alert(db, c, lid, False, _over(f.get('estimate'), f.get('actual')))
        db.commit()
        core.syslog('PM_BUDGET_LINE_CREATE', pid, line_id=lid)
        return jsonify({'success': True, 'rev': rev, 'line': _budget(db, pid, lid)['lines'][0]})

    @app.route('/api/projects/<int:pid>/budget/lines/<int:lid>', methods=['PUT'],
               endpoint='pm_budget_line_update')
    @core.wrap
    def pm_budget_line_update(db, uid, pid, lid):
        """Partial update. Every changed amount / text is one pm_budget_history
        row (old → new, who, and the optional "why" the person gave)."""
        c = core.ctx(db, pid, 'budget_edit', lock=True)
        cur = _live_line(db, pid, lid)
        body = core.body()
        why = core.clean_text(body.get('note'), core.caps['comment'], 'Reason')
        f = {k: v for k, v in _line_fields(db, pid, body, cur).items() if _line_changed(cur, k, v)}
        if not f:                   # an identical save changes (and logs) nothing
            return jsonify({'success': True, 'rev': c.project['rev'],
                            'line': _budget(db, pid, lid)['lines'][0]})
        sets = ', '.join(f'{k} = %s' for k in f)        # keys from the fixed lists above
        db.execute(f'UPDATE pm_budget_lines SET {sets}, updated_at = CURRENT_TIMESTAMP '
                   'WHERE id = %s AND project_id = %s', (*f.values(), lid, pid))
        money_keys = {k for k, _ in _MONEY}
        for k, v in f.items():
            if k in money_keys:
                old, new = _money_text(cur[k]), _money_text(v)
            elif k in ('contact_id', 'task_id'):
                continue            # links are not amounts or text: the activity row covers them
            else:
                old, new = cur[k] or '', v
            db.execute(
                'INSERT INTO pm_budget_history (line_id, project_id, user_id, actor_name, field, '
                'old_value, new_value, note) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                (lid, pid, uid, c.name, k, old, new, why))
        rev = core.touch(db, pid)
        core.activity(db, c, 'updated', 'budget_line', lid,
                      f"updated the budget line {core.quote(f.get('description', cur['description']))}")
        _budget_alert(db, c, lid, _over(cur['estimate'], cur['actual']),
                      _over(f.get('estimate', _num(cur['estimate'])), f.get('actual', _num(cur['actual']))))
        db.commit()
        core.syslog('PM_BUDGET_LINE_UPDATE', pid, line_id=lid)
        return jsonify({'success': True, 'rev': rev, 'line': _budget(db, pid, lid)['lines'][0]})

    @app.route('/api/projects/<int:pid>/budget/lines/<int:lid>', methods=['DELETE'],
               endpoint='pm_budget_line_delete')
    @core.wrap
    def pm_budget_line_delete(db, uid, pid, lid):
        """Soft delete. Its sources and history stay with the row; a file that
        was uploaded as a source stays in Files (budget-only as before)."""
        c = core.ctx(db, pid, 'budget_edit', lock=True)
        cur = _live_line(db, pid, lid)
        db.execute('UPDATE pm_budget_lines SET deleted_at = CURRENT_TIMESTAMP '
                   'WHERE id = %s AND project_id = %s', (lid, pid))
        _budget_alert(db, c, lid, True, False)
        rev = core.touch(db, pid)
        core.activity(db, c, 'deleted', 'budget_line', lid,
                      f"deleted the budget line {core.quote(cur['description'])}")
        db.commit()
        core.syslog('PM_BUDGET_LINE_DELETE', pid, line_id=lid)
        return jsonify({'success': True, 'rev': rev, 'deleted': lid})

    @app.route('/api/projects/<int:pid>/budget/lines/<int:lid>/history', methods=['GET'],
               endpoint='pm_budget_line_history')
    @core.wrap
    def pm_budget_line_history(db, uid, pid, lid):
        core.ctx(db, pid, 'budget')
        _live_line(db, pid, lid)
        rows = db.execute(
            'SELECT id, field, old_value, new_value, note, actor_name, created_at::timestamptz AS at_tz '
            'FROM pm_budget_history WHERE line_id = %s AND project_id = %s ORDER BY id DESC LIMIT %s',
            (lid, pid, _HISTORY_MAX)).fetchall()
        return jsonify({'history': [{
            'id': r['id'], 'field': r['field'], 'old_value': r['old_value'],
            'new_value': r['new_value'], 'note': r['note'] or '',
            'actor': r['actor_name'] or 'Former user', 'at': core.ts(r['at_tz'])} for r in rows]})

    @app.route('/api/projects/<int:pid>/budget/lines/<int:lid>/sources', methods=['POST'],
               endpoint='pm_budget_source_add')
    @core.wrap
    def pm_budget_source_add(db, uid, pid, lid):
        """Where a number came from: one of this project's files (pinned to
        the version that is current NOW), a note, or a web link."""
        c = core.ctx(db, pid, 'budget_edit', lock=True)
        line = _live_line(db, pid, lid)
        body = core.body()
        kind = core.one_of(body.get('kind'), ('file', 'note', 'link'), 'Source type')
        label = core.clean_text(body.get('label'), core.caps['short'], 'Label', line=True)
        text, url, fid, vid = '', '', None, None
        if kind == 'file':
            fid = core.to_int(body.get('file_id'), 'File')
            f = db.execute(
                f"""SELECT f.id, f.current_version_id FROM pm_files f
                     WHERE f.id = %s AND f.project_id = %s AND f.deleted_at IS NULL
                       AND {core.can_see_file_sql(c)}""", (fid, pid)).fetchone()
            if not f or not f['current_version_id']:
                raise _refuse(400, _NO_SUCH_FILE)
            vid = f['current_version_id']
            if db.execute('SELECT 1 FROM pm_budget_sources WHERE line_id = %s AND project_id = %s '
                          'AND file_id = %s AND version_id = %s', (lid, pid, fid, vid)).fetchone():
                raise _refuse(400, 'That file is already a source of this line.')
        elif kind == 'note':
            text = core.clean_text(body.get('body'), core.caps['comment'], 'Note')
            if not text and not label:
                raise _refuse(400, 'Write the note.')
        else:
            url = _clean_url(body.get('url'))
        _source_room(db, pid, lid)
        sid = db.execute(
            'INSERT INTO pm_budget_sources (line_id, project_id, kind, file_id, version_id, label, '
            'body, url, created_by, creator_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) '
            'RETURNING id', (lid, pid, kind, fid, vid, label, text, url, uid, c.name)).fetchone()['id']
        rev = core.touch(db, pid)
        core.activity(db, c, 'added', 'budget_source', sid,
                      f"added a source to the budget line {core.quote(line['description'])}")
        db.commit()
        core.syslog('PM_BUDGET_SOURCE_ADD', pid, line_id=lid, source_id=sid)
        return jsonify({'success': True, 'rev': rev, 'source': _sources(db, pid, source_id=sid)[0]})

    @app.route('/api/projects/<int:pid>/budget/sources/<int:sid>', methods=['DELETE'],
               endpoint='pm_budget_source_delete')
    @core.wrap
    def pm_budget_source_delete(db, uid, pid, sid):
        """Removes the link only — a file that was a source stays in Files."""
        c = core.ctx(db, pid, 'budget_edit', lock=True)
        core.url_id(sid)
        s = db.execute(
            """SELECT s.id, s.line_id, l.description FROM pm_budget_sources s
                 JOIN pm_budget_lines l ON l.id = s.line_id AND l.project_id = s.project_id
                WHERE s.id = %s AND s.project_id = %s AND l.deleted_at IS NULL""", (sid, pid)).fetchone()
        if not s:
            raise _refuse(404, _NOT_FOUND)
        db.execute('DELETE FROM pm_budget_sources WHERE id = %s AND project_id = %s', (sid, pid))
        rev = core.touch(db, pid)
        core.activity(db, c, 'removed', 'budget_source', sid,
                      f"removed a source from the budget line {core.quote(s['description'])}")
        db.commit()
        core.syslog('PM_BUDGET_SOURCE_REMOVE', pid, line_id=s['line_id'], source_id=sid)
        return jsonify({'success': True, 'rev': rev, 'deleted': sid, 'line_id': s['line_id']})

    @app.route('/api/projects/<int:pid>/budget/shows', methods=['GET'], endpoint='pm_budget_shows')
    @core.wrap
    def pm_budget_shows(db, uid, pid):
        """What the linked shows cost — loaded on demand, never stored. Every
        number comes from app.py's own report helpers (injected), for the
        CALLER: a figure their Reports permissions don't cover is null, and a
        show they can't open is not listed."""
        core.ctx(db, pid, 'budget')
        return jsonify(_show_figures(db, uid, pid))


def _source_room(db, pid, lid):
    n = db.execute('SELECT COUNT(*) AS n FROM pm_budget_sources WHERE line_id = %s AND project_id = %s',
                   (lid, pid)).fetchone()['n']
    if n >= core.limits['sources_per_line']:
        raise _refuse(400, f"A budget line can have at most {core.limits['sources_per_line']} sources.")


def _linked_show_ids(db, uid, pid):
    """Linked shows the CALLER can open (the state bundle applies the same
    filter): a show they have no access to is not named anywhere here."""
    ids = [r['show_id'] for r in db.execute(
        'SELECT show_id FROM pm_shows WHERE project_id = %s ORDER BY id', (pid,)).fetchall()]
    return [s for s in ids if core.d['can_access_show'](uid, s)]


def _show_figures(db, uid, pid):
    figs = core.d['show_figures'](db, _linked_show_ids(db, uid, pid))
    return {'can': figs['can'], 'shows': figs['shows'], 'totals': figs['totals']}


# ─── Files: rows ──────────────────────────────────────────────────────────────

def _vcols(db, alias='v'):
    """A version row for file_store.read_bytes: every column except the three
    big ones, plus the `in_db` flag (the bytes are fetched only if the
    database copy is the one served)."""
    return (core.d['cols_except'](db, 'pm_file_versions',
                                  ('file_data', 'extracted_text', 'email_json'), alias)
            + ', ' + core.d['file_store'].in_db_col('project_files', alias))


def _file(db, c, fid, state='live', lock=False, code=404):
    """THE way to a pm_files row. For a member who may not see the budget a
    budget-only file does not exist (same 404 as a wrong id). state:
    'live' — not archived; 'archived' — archived only; 'any' — live, or
    archived for someone who can edit (archived files are an editor's view:
    restore / purge). code=400 for an id named in a request body."""
    if code == 400:
        fid = core.to_int(fid, 'File')
        miss = _refuse(400, _NO_SUCH_FILE)
    else:
        core.url_id(fid)
        miss = _refuse(404, _NOT_FOUND)
    f = db.execute(
        f"""SELECT f.* FROM pm_files f
             WHERE f.id = %s AND f.project_id = %s AND {core.can_see_file_sql(c)}
             {'FOR UPDATE' if lock else ''}""", (fid, c.pid)).fetchone()
    if not f:
        raise miss
    gone = f['deleted_at'] is not None
    if (state == 'live' and gone) or (state == 'archived' and not gone) or (
            state == 'any' and gone and not c.can_edit):
        raise miss
    return f


def _version(db, f, vid, extra=''):
    """A REAL version `vid` of file `f` (never a cached preview row)."""
    core.url_id(vid)
    v = db.execute(
        f"""SELECT {_vcols(db)}{extra} FROM pm_file_versions v
             WHERE v.id = %s AND v.file_id = %s AND v.project_id = %s AND v.preview_of IS NULL""",
        (vid, f['id'], f['project_id'])).fetchone()
    if not v:
        raise _refuse(404, _NOT_FOUND)
    return v


def _read(db, v):
    """The bytes of a version row, through file_store (S3 or the database,
    whichever the admin prefers, falling back to the other). The read-only
    transaction is ended first: nothing stays open across an S3 call."""
    db.rollback()
    fs = core.d['file_store']
    try:
        return fs.read_bytes('project_files', v)
    except fs.FileMissing:
        raise _refuse(404, _NOT_FOUND)
    except fs.FileUnavailable:
        raise _refuse(503, 'The file store is not reachable right now. Try again shortly.')


def _files_out(db, c, archived=False, fid=None):
    """The Files list (or the one file `fid`, whatever its state)."""
    where = ['f.project_id = %s', core.can_see_file_sql(c)]
    args = [c.pid]
    if fid is not None:
        where.append('f.id = %s')
        args.append(fid)
    else:
        where.append('f.deleted_at IS NOT NULL' if archived else 'f.deleted_at IS NULL')
    rows = db.execute(
        f"""SELECT f.id, f.title, f.folder, f.budget_only, (f.deleted_at IS NOT NULL) AS archived,
                   (SELECT COUNT(*) FROM pm_file_versions x
                     WHERE x.file_id = f.id AND x.project_id = f.project_id
                       AND x.preview_of IS NULL) AS versions,
                   v.id AS v_id, v.version, v.filename, v.mime_type, v.kind, v.file_size,
                   v.uploader_name, v.created_at::timestamptz AS v_at, v.extract_status,
                   (COALESCE(octet_length(v.extracted_text), 0) > 0) AS has_text,
                   (v.email_json IS NOT NULL) AS is_email
              FROM pm_files f
              LEFT JOIN pm_file_versions v ON v.id = f.current_version_id AND v.file_id = f.id
                   AND v.project_id = f.project_id AND v.preview_of IS NULL
             WHERE {' AND '.join(where)}
             ORDER BY LOWER(f.folder), LOWER(f.title), f.id""", args).fetchall()
    used = {}
    if c.can_budget and rows:
        for r in db.execute(
                """SELECT DISTINCT s.file_id, s.line_id FROM pm_budget_sources s
                     JOIN pm_budget_lines l ON l.id = s.line_id AND l.project_id = s.project_id
                    WHERE s.project_id = %s AND s.kind = 'file' AND s.file_id IS NOT NULL
                      AND l.deleted_at IS NULL ORDER BY s.line_id""", (c.pid,)).fetchall():
            used.setdefault(r['file_id'], []).append(r['line_id'])
    out = []
    for r in rows:
        item = {
            'id': r['id'], 'title': r['title'] or r['filename'] or '', 'folder': r['folder'] or '',
            'budget_only': bool(r['budget_only']), 'archived': bool(r['archived']),
            'versions': r['versions'],
            'current': None if r['v_id'] is None else {
                'id': r['v_id'], 'version': r['version'], 'filename': r['filename'],
                'mime': r['mime_type'] or 'application/octet-stream', 'kind': r['kind'],
                'size': r['file_size'] or 0, 'uploaded_by': r['uploader_name'] or '',
                'at': core.ts(r['v_at']), 'extract_status': r['extract_status'] or '',
                'has_text': bool(r['has_text']), 'is_email': bool(r['is_email'])}}
        if c.can_budget:            # which lines cite it is budget information
            item['used_by_lines'] = used.get(r['id'], [])
        out.append(item)
    return out


# ─── Files: storing ───────────────────────────────────────────────────────────

def _s3_key(pid, fid, version, vid):
    """Ids only — the same key file_store's migration tool derives
    (KINDS['project_files']['s3_key_for'])."""
    return f'project-files/{int(pid)}/{int(fid)}/v{int(version)}-{int(vid)}'


def _to_s3(db, pid, fid, vid, version, data, mime):
    """Steps 2 and 3 of an upload. Called with NO transaction open: put the
    object, then record its key in one short conditional UPDATE (the database
    copy is cleared unless dual-write keeps it). Nothing here can fail the
    request — the row committed in step 1 already holds the bytes, so an S3
    problem only means the file stays in the database."""
    s3 = core.d['s3_storage']
    try:
        if not s3.is_configured():
            return False
    except Exception:
        return False
    key = _s3_key(pid, fid, version, vid)
    try:
        s3.upload_file(key, data, mime or 'application/octet-stream')
    except Exception as e:
        _warn(f'S3_UPLOAD_FAILED table=pm_file_versions id={int(vid)} type={type(e).__name__}')
        return False
    try:
        keep = bool(core.d['file_store'].keep_db_copy())
        got = db.execute(
            'UPDATE pm_file_versions SET s3_key = %s, '
            'file_data = CASE WHEN %s THEN file_data ELSE NULL END '
            'WHERE id = %s AND file_id = %s AND project_id = %s AND s3_key IS NULL RETURNING id',
            (key, keep, vid, fid, pid)).fetchone()
        still = got or db.execute(
            'SELECT s3_key FROM pm_file_versions WHERE id = %s AND file_id = %s AND project_id = %s',
            (vid, fid, pid)).fetchone()
        db.commit()
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        core.log_failure('s3_record', e, version_id=int(vid))
        still = None
    if not still:
        # The row went away while we uploaded (the file was purged, the
        # project deleted) or could not be updated: nothing points at the
        # object, so it must not be left behind. (A row whose key is already
        # set — the migration tool copied it to this same key — keeps it.)
        core.s3_cleanup([key], pid)
        return False
    return bool(got)


def _warn(line):
    try:
        core.d['syslog_logger'].warning(line)
    except Exception:
        pass


def _store(db, pid, data, filename, claimed_mime, *, title='', folder='', file_id=None,
           line_id=None, from_file=None, verb='uploaded'):
    """Step 1 of an upload — ONE short transaction under the project lock:
    the pm_files row (or the lock on an existing one), the version row WITH
    its bytes, current_version_id, the budget source when `line_id` is given,
    rev + activity, COMMIT. Returns what steps 2–3 and the response need.

    `line_id` makes this a budget write (ctx 'budget_edit' — checked before
    anything is stored) and the file budget-only. `from_file` = the file an
    e-mail attachment is being saved out of: the new file inherits its
    budget_only flag and folder, and it must still be visible to the caller
    now that we hold the lock."""
    fs = core.d['file_store']
    c = core.ctx(db, pid, 'budget_edit' if line_id is not None else 'edit', lock=True)
    line = _live_line(db, pid, line_id, code=400) if line_id is not None else None
    if line is not None:
        _source_room(db, pid, line['id'])
    budget_only = 1 if line is not None else 0
    if from_file is not None:
        src = _file(db, c, from_file)
        budget_only = budget_only or int(src['budget_only'] or 0)
        folder = folder or (src['folder'] or '')
    info = project_docs.sniff(data, filename, claimed_mime)
    if file_id is not None:
        # A new version of an existing file: lock the file row too, so the
        # version number below can never be handed out twice.
        f = _file(db, c, file_id, lock=True, code=400)
        fid = f['id']
        n = db.execute('SELECT COUNT(*) AS n FROM pm_file_versions WHERE file_id = %s '
                       'AND project_id = %s AND preview_of IS NULL', (fid, pid)).fetchone()['n']
        if n >= _MAX_VERSIONS:
            raise _refuse(400, f'A file can have at most {_MAX_VERSIONS} versions.')
        if budget_only and not f['budget_only']:
            _make_budget_only(db, pid, fid)
        budget_only = budget_only or int(f['budget_only'] or 0)
        shown = f['title'] or filename
    else:
        n = db.execute('SELECT COUNT(*) AS n FROM pm_files WHERE project_id = %s AND deleted_at IS NULL',
                       (pid,)).fetchone()['n']
        if n >= core.limits['files']:
            raise _refuse(400, f"This project has reached its limit of {core.limits['files']} files.")
        shown = title or filename
        fid = db.execute(
            'INSERT INTO pm_files (project_id, title, folder, budget_only, created_by) '
            'VALUES (%s, %s, %s, %s, %s) RETURNING id',
            (pid, shown, folder, budget_only, c.uid)).fetchone()['id']
    # The next version number counts REAL versions only (a cached preview is
    # a negative-numbered row of the same file).
    v = db.execute(
        """INSERT INTO pm_file_versions (file_id, project_id, version, filename, mime_type, kind,
                                         file_size, file_data, content_sha256, uploaded_by, uploader_name)
           SELECT %s, %s, COALESCE(MAX(version) FILTER (WHERE preview_of IS NULL), 0) + 1,
                  %s, %s, %s, %s, %s, %s, %s, %s
             FROM pm_file_versions WHERE file_id = %s AND project_id = %s
        RETURNING id, version""",
        (fid, pid, filename, info['mime'], info['kind'], len(data), data, fs.sha256_hex(data),
         c.uid, c.name, fid, pid)).fetchone()
    db.execute('UPDATE pm_files SET current_version_id = %s WHERE id = %s AND project_id = %s',
               (v['id'], fid, pid))
    sid = None
    if line is not None:
        sid = db.execute(
            'INSERT INTO pm_budget_sources (line_id, project_id, kind, file_id, version_id, '
            "created_by, creator_name) VALUES (%s, %s, 'file', %s, %s, %s, %s) RETURNING id",
            (line['id'], pid, fid, v['id'], c.uid, c.name)).fetchone()['id']
    rev = core.touch(db, pid)
    detail = (f'{verb} version {v["version"]} of the file {core.quote(shown)}' if file_id is not None
              else f'{verb} the file {core.quote(shown)}')
    _file_activity(db, c, 'uploaded', fid, budget_only, detail)
    db.commit()
    return {'c': c, 'fid': fid, 'vid': v['id'], 'version': v['version'], 'rev': rev,
            'mime': info['mime'], 'source_id': sid}


def _make_budget_only(db, pid, fid):
    """A file becomes budget-only: its EARLIER history lines (which name it)
    become budget history too, or a member who may no longer see the file
    would still read its title in the activity list."""
    db.execute('UPDATE pm_files SET budget_only = 1 WHERE id = %s AND project_id = %s', (fid, pid))
    db.execute("UPDATE pm_activity SET entity = 'budget_source', entity_id = NULL "
               "WHERE project_id = %s AND entity = 'file' AND entity_id = %s", (pid, fid))


def _finish_store(db, s, data):
    """Steps 2–3 (S3) and the hand-off to the extraction worker, then the
    JSON every upload-like route answers with."""
    _to_s3(db, s['c'].pid, s['fid'], s['vid'], s['version'], data, s['mime'])
    files = _files_out(db, s['c'], fid=s['fid'])
    if not files:
        # Stored — and then the project was deleted while we talked to S3
        # (no lock is held there, on purpose). It is simply gone.
        raise _refuse(404, _NOT_FOUND)
    _enqueue(s['c'].pid, s['fid'], s['vid'])
    out = {'success': True, 'rev': s['rev'], 'file': files[0]}
    if s['source_id']:
        out['source'] = _sources(db, s['c'].pid, source_id=s['source_id'])[0]
    return out


def _upload_max():
    try:
        return int(core.d['get_upload_max']())
    except Exception:
        return 20 * 1024 * 1024


def _too_large(mx):
    return _refuse(413, f'File too large (max {max(1, mx // (1024 * 1024))} MB).')


# ─── Files: text extraction (worker thread) ───────────────────────────────────
# One daemon thread per process works through a queue of (project, file,
# version) IDS — never bytes — so a burst of uploads costs one parser at a
# time and no memory while waiting. A process that dies loses its queue; the
# hourly retry_extractions() picks those rows up.

_queue = queue.Queue()
_pending = set()                # version ids queued or running in THIS process
_worker = None
_worker_lock = threading.Lock()


def _enqueue(pid, fid, vid):
    global _worker
    with _worker_lock:
        if vid in _pending:
            return False
        _pending.add(vid)
        _queue.put((int(pid), int(fid), int(vid)))
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name='pm-extract', daemon=True)
            _worker.start()
    return True


def _work():
    while True:
        pid, fid, vid = _queue.get()
        try:
            _extract_one(pid, fid, vid)
        except Exception as e:      # a database outage included: the row stays '' and is retried
            core.log_failure('extract', e, version_id=vid)
        finally:
            _pending.discard(vid)
            _queue.task_done()


def _extract_one(pid, fid, vid):
    """Read one version's text (and, for an e-mail, its header block) and
    store it. Three separate moments, no connection held in between: read
    the row; read + parse the bytes; then — under the project lock, the same
    order every route takes its locks in — the conditional UPDATE
    (`extract_status = ''`, so a second worker or a re-run changes nothing)
    and, when that changed a row, the project rev so open pages refetch.
    Returns True when it stored a result."""
    get_db = core.d['get_db']
    db = get_db()
    try:
        v = db.execute(
            f"""SELECT {_vcols(db)} FROM pm_file_versions v
                 WHERE v.id = %s AND v.file_id = %s AND v.project_id = %s
                   AND v.preview_of IS NULL AND v.extract_status = ''""", (vid, fid, pid)).fetchone()
    finally:
        db.close()
    if not v:
        return False
    text, info, status = '', None, 'error'
    try:
        data = core.d['file_store'].read_bytes('project_files', v)
        # project_docs never raises and never logs: '' / None mean "nothing".
        if v['kind'] == 'email':
            info = project_docs.parse_email(data, v['filename'])
        text = project_docs.extract_text(data, v['filename'], v['mime_type'] or '')
        status = 'done' if (text or info) else 'none'
        del data
    except Exception as e:
        core.log_failure('extract_read', e, version_id=vid)
        text, info, status = '', None, 'error'
    email_json = json.dumps(info, ensure_ascii=False) if info else None
    db = get_db()
    try:
        if not db.execute('SELECT id FROM pm_projects WHERE id = %s FOR UPDATE', (pid,)).fetchone():
            return False
        got = db.execute(
            "UPDATE pm_file_versions SET extracted_text = %s, email_json = %s, extract_status = %s "
            "WHERE id = %s AND file_id = %s AND project_id = %s AND extract_status = '' RETURNING id",
            (text or None, email_json, status, vid, fid, pid)).fetchone()
        if got:
            # rev only (no activity, updated_at untouched): reading a file is
            # not something a person did to the project.
            db.execute('UPDATE pm_projects SET rev = rev + 1 WHERE id = %s', (pid,))
        db.commit()
        return bool(got)
    finally:
        db.close()


def retry_extractions():
    """Called by projects_module.run_hourly() (leader-gated job, no request,
    no session). Re-runs up to 20 versions still waiting for their text 15
    minutes after upload — their worker's process restarted, or the database
    was away when it finished. A database outage propagates (the job skips
    the run); anything else about one file ends as status 'error' on that
    row, so nothing is retried forever. Returns how many rows got a result."""
    if core is None:
        return 0
    db = core.d['get_db']()
    try:
        rows = db.execute(
            "SELECT id, file_id, project_id FROM pm_file_versions "
            "WHERE extract_status = '' AND preview_of IS NULL "
            "AND created_at < NOW() - INTERVAL '15 minutes' ORDER BY id LIMIT 20").fetchall()
    finally:
        db.close()
    done = 0
    for r in rows:
        with _worker_lock:
            if r['id'] in _pending:     # this process is on it right now
                continue
        if _extract_one(r['project_id'], r['file_id'], r['id']):
            done += 1
    if done:
        core.syslog('PM_EXTRACT_RETRY', count=done)
    return done


# ─── Files: routes ────────────────────────────────────────────────────────────

def _routes_files(app):

    @app.route('/api/projects/<int:pid>/files', methods=['GET'], endpoint='pm_files')
    @core.wrap
    def pm_files(db, uid, pid):
        archived = request.args.get('archived') == '1'
        # Archived files are an editor's view (restore / purge live there).
        c = core.ctx(db, pid, 'edit' if archived else 'view')
        return jsonify({'files': _files_out(db, c, archived=archived)})

    @app.route('/api/projects/<int:pid>/files', methods=['POST'], endpoint='pm_file_upload')
    @core.wrap
    def pm_file_upload(db, uid, pid):
        """multipart: file (required), title?, folder?, file_id? (= a new
        version of that file), line_id? (= also a source of that budget line,
        and the file becomes budget-only)."""
        # Membership, then size, BEFORE the body is parsed: reading
        # request.form makes werkzeug spool the whole upload to a temp file.
        core.ctx(db, pid)
        db.rollback()
        mx = _upload_max()
        if (request.content_length or 0) > mx + _SIZE_SLACK:
            raise _too_large(mx)
        # Rights next (no lock, nothing stored): a member who may not edit —
        # or may not see the budget, when a line is named — is refused before
        # a byte of the file is kept.
        raw_line = (request.form.get('line_id') or '').strip() or None
        raw_file = (request.form.get('file_id') or '').strip() or None
        core.ctx(db, pid, 'budget_edit' if raw_line is not None else 'edit')
        db.rollback()
        up = request.files.get('file')
        if up is None:
            raise _refuse(400, 'Choose a file to upload.')
        data = up.read(mx + 1)
        if len(data) > mx:
            raise _too_large(mx)
        if not data:
            raise _refuse(400, 'That file is empty.')
        filename = _clean_filename(up.filename)
        title = core.clean_text(request.form.get('title'), core.caps['title'], 'Title', line=True)
        folder = core.clean_text(request.form.get('folder'), core.caps['short'], 'Folder', line=True)
        s = _store(db, pid, data, filename, up.mimetype or '', title=title, folder=folder,
                   file_id=core.to_int(raw_file, 'File') if raw_file is not None else None,
                   line_id=core.to_int(raw_line, 'Budget line') if raw_line is not None else None)
        core.syslog('PM_FILE_UPLOAD', pid, file_id=s['fid'], version_id=s['vid'])
        return jsonify(_finish_store(db, s, data))

    @app.route('/api/projects/<int:pid>/files/<int:fid>', methods=['GET'], endpoint='pm_file_detail')
    @core.wrap
    def pm_file_detail(db, uid, pid, fid):
        """One file: its versions and — for the chosen version (default: the
        current one) — the e-mail header block or the first part of its text."""
        c = core.ctx(db, pid)
        f = _file(db, c, fid, 'any')
        raw = request.args.get('version')
        if raw is None:
            vid = f['current_version_id']
        elif re.fullmatch(r'\d{1,10}', raw):
            vid = int(raw)
        else:
            raise _refuse(404, _NOT_FOUND)
        v = _version(db, f, vid or 0, extra=(
            f', substr(v.extracted_text, 1, {_TEXT_PREVIEW}) AS text_head, '
            f'(char_length(v.extracted_text) > {_TEXT_PREVIEW}) AS text_more, v.email_json'))
        versions = db.execute(
            """SELECT v.id, v.version, v.filename, v.mime_type, v.kind, v.file_size, v.uploader_name,
                      v.created_at::timestamptz AS at_tz
                 FROM pm_file_versions v
                WHERE v.file_id = %s AND v.project_id = %s AND v.preview_of IS NULL
                ORDER BY v.version DESC""", (fid, pid)).fetchall()
        email, text, more = None, v['text_head'] or '', bool(v['text_more'])
        if v['email_json']:
            try:
                e = json.loads(v['email_json'])
            except ValueError:
                e = None
            if isinstance(e, dict):
                email = {k: str(e.get(k) or '') for k in
                         ('from', 'from_name', 'from_email', 'to', 'cc', 'date', 'subject')}
                # The attachment's type is what the SENDER claimed: not sent.
                email['attachments'] = [
                    {'index': a.get('index'), 'name': str(a.get('name') or ''), 'size': a.get('size') or 0}
                    for a in (e.get('attachments') or []) if isinstance(a, dict)]
                # For an e-mail the text IS the message body (the headers are
                # in `email`; extracted_text repeats them for search).
                body_text = str(e.get('body_text') or '')
                text, more = body_text[:_TEXT_PREVIEW], len(body_text) > _TEXT_PREVIEW
        out = {
            'file': {'id': f['id'], 'title': f['title'] or v['filename'], 'folder': f['folder'] or '',
                     'budget_only': bool(f['budget_only']), 'archived': f['deleted_at'] is not None,
                     'current_version_id': f['current_version_id']},
            'version_id': v['id'],
            'versions': [{'id': r['id'], 'version': r['version'], 'filename': r['filename'],
                          'mime': r['mime_type'] or 'application/octet-stream', 'kind': r['kind'],
                          'size': r['file_size'] or 0, 'uploaded_by': r['uploader_name'] or '',
                          'at': core.ts(r['at_tz'])} for r in versions],
            'email': email, 'text': text, 'text_more': more,
            'extract_status': v['extract_status'] or ''}
        if c.can_budget:
            out['file']['used_by_lines'] = [r['line_id'] for r in db.execute(
                """SELECT DISTINCT s.line_id FROM pm_budget_sources s
                     JOIN pm_budget_lines l ON l.id = s.line_id AND l.project_id = s.project_id
                    WHERE s.project_id = %s AND s.kind = 'file' AND s.file_id = %s
                      AND l.deleted_at IS NULL ORDER BY s.line_id""", (pid, fid)).fetchall()]
        return jsonify(out)

    @app.route('/api/projects/<int:pid>/files/<int:fid>', methods=['PUT'], endpoint='pm_file_update')
    @core.wrap
    def pm_file_update(db, uid, pid, fid):
        """Rename / move / (with budget rights) change who can see the file."""
        c = core.ctx(db, pid, 'edit', lock=True)
        f = _file(db, c, fid)
        body, new = core.body(), {}
        if 'title' in body:
            new['title'] = core.clean_text(body['title'], core.caps['title'], 'Title',
                                           required=True, line=True)
        if 'folder' in body:
            new['folder'] = core.clean_text(body['folder'], core.caps['short'], 'Folder', line=True)
        if 'budget_only' in body:
            new['budget_only'] = core.to_flag(body['budget_only'], 'Budget only')
        new = {k: v for k, v in new.items()
               if (int(f[k] or 0) if k == 'budget_only' else (f[k] or '')) != v}
        if 'budget_only' in new and not c.can_budget:
            raise _refuse(403, 'Only people who can see the budget can change that.')
        if not new:
            return jsonify({'success': True, 'rev': c.project['rev'],
                            'file': _files_out(db, c, fid=fid)[0]})
        if new.get('budget_only') == 1:
            _make_budget_only(db, pid, fid)
        sets = ', '.join(f'{k} = %s' for k in new)          # keys from the three above
        db.execute(f'UPDATE pm_files SET {sets} WHERE id = %s AND project_id = %s',
                   (*new.values(), fid, pid))
        name = core.quote(new.get('title', f['title']))
        bits = []
        if 'title' in new:
            bits.append(f"renamed the file {core.quote(f['title'])} to {name}")
        if 'folder' in new:
            bits.append(f'moved the file {name}')
        if 'budget_only' in new:
            bits.append(f'made the file {name} ' + ('budget-only' if new['budget_only']
                                                     else 'visible to everyone on the project'))
        rev = core.touch(db, pid)
        _file_activity(db, c, 'updated', fid, new.get('budget_only', f['budget_only']), ', '.join(bits))
        db.commit()
        core.syslog('PM_FILE_UPDATE', pid, file_id=fid)
        return jsonify({'success': True, 'rev': rev, 'file': _files_out(db, c, fid=fid)[0]})

    @app.route('/api/projects/<int:pid>/files/<int:fid>', methods=['DELETE'], endpoint='pm_file_archive')
    @core.wrap
    def pm_file_archive(db, uid, pid, fid):
        """Deleting a file ARCHIVES it: the file and every version row
        (cached previews too) are stamped, each database copy is gzipped in
        place, and the S3 objects are KEPT. Only purge destroys bytes."""
        c = core.ctx(db, pid, 'edit', lock=True)
        f = _file(db, c, fid)
        for r in db.execute(
                'SELECT id FROM pm_file_versions WHERE file_id = %s AND project_id = %s '
                'AND file_data IS NOT NULL AND COALESCE(is_compressed, 0) = 0', (fid, pid)).fetchall():
            # One blob in memory at a time.
            raw = bytes(db.execute(
                'SELECT file_data FROM pm_file_versions WHERE id = %s AND file_id = %s '
                'AND project_id = %s', (r['id'], fid, pid)).fetchone()['file_data'])
            packed = gzip.compress(raw)
            if len(packed) < len(raw):
                db.execute('UPDATE pm_file_versions SET file_data = %s, is_compressed = 1 '
                           'WHERE id = %s AND file_id = %s AND project_id = %s',
                           (packed, r['id'], fid, pid))
        db.execute('UPDATE pm_file_versions SET deleted_at = CURRENT_TIMESTAMP '
                   'WHERE file_id = %s AND project_id = %s', (fid, pid))
        db.execute('UPDATE pm_files SET deleted_at = CURRENT_TIMESTAMP, deleted_by = %s '
                   'WHERE id = %s AND project_id = %s', (uid, fid, pid))
        rev = core.touch(db, pid)
        _file_activity(db, c, 'archived', fid, f['budget_only'],
                       f"archived the file {core.quote(f['title'])}")
        db.commit()
        core.syslog('PM_FILE_ARCHIVE', pid, file_id=fid)
        return jsonify({'success': True, 'rev': rev, 'archived': fid})

    @app.route('/api/projects/<int:pid>/files/<int:fid>/restore', methods=['POST'],
               endpoint='pm_file_restore')
    @core.wrap
    def pm_file_restore(db, uid, pid, fid):
        c = core.ctx(db, pid, 'edit', lock=True)
        f = _file(db, c, fid, 'archived')
        n = db.execute('SELECT COUNT(*) AS n FROM pm_files WHERE project_id = %s AND deleted_at IS NULL',
                       (pid,)).fetchone()['n']
        if n >= core.limits['files']:
            raise _refuse(400, f"This project has reached its limit of {core.limits['files']} files.")
        for r in db.execute(
                'SELECT id FROM pm_file_versions WHERE file_id = %s AND project_id = %s '
                'AND file_data IS NOT NULL AND COALESCE(is_compressed, 0) = 1', (fid, pid)).fetchall():
            packed = bytes(db.execute(
                'SELECT file_data FROM pm_file_versions WHERE id = %s AND file_id = %s '
                'AND project_id = %s', (r['id'], fid, pid)).fetchone()['file_data'])
            db.execute('UPDATE pm_file_versions SET file_data = %s, is_compressed = 0 '
                       'WHERE id = %s AND file_id = %s AND project_id = %s',
                       (gzip.decompress(packed), r['id'], fid, pid))
        # A row with no database copy has nothing compressed either way.
        db.execute('UPDATE pm_file_versions SET deleted_at = NULL, is_compressed = 0 '
                   'WHERE file_id = %s AND project_id = %s', (fid, pid))
        db.execute('UPDATE pm_files SET deleted_at = NULL, deleted_by = NULL '
                   'WHERE id = %s AND project_id = %s', (fid, pid))
        rev = core.touch(db, pid)
        _file_activity(db, c, 'restored', fid, f['budget_only'],
                       f"restored the file {core.quote(f['title'])}")
        db.commit()
        core.syslog('PM_FILE_RESTORE', pid, file_id=fid)
        return jsonify({'success': True, 'rev': rev, 'file': _files_out(db, c, fid=fid)[0]})

    @app.route('/api/projects/<int:pid>/files/<int:fid>/purge', methods=['POST'], endpoint='pm_file_purge')
    @core.wrap
    def pm_file_purge(db, uid, pid, fid):
        """The ONLY route that destroys a file's bytes: the owner, and only
        for a file that is already archived — so it can never hit a live
        one. The S3 objects go AFTER the commit that removed the rows (a
        rolled-back purge must not have lost them)."""
        c = core.ctx(db, pid, 'owner', lock=True)
        if c.archived:
            raise _refuse(403, 'This project is archived.')
        core.url_id(fid)
        f = db.execute('SELECT * FROM pm_files WHERE id = %s AND project_id = %s FOR UPDATE',
                       (fid, pid)).fetchone()
        if not f:
            raise _refuse(404, _NOT_FOUND)
        if f['deleted_at'] is None:
            raise _refuse(400, 'Archive the file first.')
        keys = [r['s3_key'] for r in db.execute(
            "SELECT s3_key FROM pm_file_versions WHERE file_id = %s AND project_id = %s "
            "AND s3_key IS NOT NULL AND s3_key <> ''", (fid, pid)).fetchall()]
        # Version rows cascade; a budget source that cited the file keeps its
        # row with file_id NULL (shown as "file missing").
        db.execute('DELETE FROM pm_files WHERE id = %s AND project_id = %s', (fid, pid))
        rev = core.touch(db, pid)
        _file_activity(db, c, 'purged', fid, f['budget_only'],
                       f"permanently deleted the file {core.quote(f['title'])}")
        db.commit()
        core.s3_cleanup(keys, pid)
        core.syslog('PM_FILE_PURGE', pid, file_id=fid)
        return jsonify({'success': True, 'rev': rev, 'purged': fid})

    @app.route('/api/projects/<int:pid>/files/<int:fid>/v/<int:vid>/reextract', methods=['POST'],
               endpoint='pm_file_reextract')
    @core.wrap
    def pm_file_reextract(db, uid, pid, fid, vid):
        """"Try again" for a file whose text could not be read: re-queued when
        its status is 'error', or still '' (waiting) more than 2 minutes
        after upload. Anything else answers queued: false — nothing to do."""
        c = core.ctx(db, pid, 'edit', lock=True)
        f = _file(db, c, fid)
        v = _version(db, f, vid, extra=", (v.created_at < NOW() - INTERVAL '2 minutes') AS stale")
        status, rev, queued = v['extract_status'] or '', c.project['rev'], False
        if status == 'error':
            db.execute("UPDATE pm_file_versions SET extract_status = '' WHERE id = %s AND file_id = %s "
                       "AND project_id = %s AND extract_status = 'error'", (vid, fid, pid))
            rev, status = core.touch(db, pid), ''
            db.commit()
            queued = True
        elif status == '' and v['stale']:
            db.rollback()
            queued = True
        if queued:
            _enqueue(pid, fid, vid)         # a no-op when this process is already on it
            core.syslog('PM_FILE_REEXTRACT', pid, file_id=fid, version_id=vid)
        return jsonify({'success': True, 'rev': rev, 'queued': queued, 'extract_status': status})

    @app.route('/api/projects/<int:pid>/files/<int:fid>/v/<int:vid>/attachments/<int:n>/save',
               methods=['POST'], endpoint='pm_file_attachment_save')
    @core.wrap
    def pm_file_attachment_save(db, uid, pid, fid, vid, n):
        """Save attachment `n` of a stored e-mail as a NEW project file (same
        folder, and budget-only when the e-mail is). The attachment's bytes
        are treated like any upload: its type is sniffed here — the type in
        the message is whatever the sender claimed."""
        c = core.ctx(db, pid, 'edit')
        f = _file(db, c, fid)
        v = _version(db, f, vid)
        if v['kind'] != 'email':
            raise _refuse(404, _NOT_FOUND)
        got = project_docs.email_attachment(_read(db, v), v['filename'], n)
        if not got:
            raise _refuse(404, _NOT_FOUND)
        name, _claimed, data = got
        mx = _upload_max()
        if len(data) > mx:
            raise _too_large(mx)
        name = _clean_filename(name)
        s = _store(db, pid, data, name, '', title=name[:core.caps['title']], from_file=fid, verb='saved')
        core.syslog('PM_FILE_ATTACHMENT_SAVE', pid, file_id=s['fid'], version_id=s['vid'], from_file_id=fid)
        return jsonify(_finish_store(db, s, data))

    @app.route('/projects/<int:pid>/files/<int:fid>/v/<int:vid>/download', methods=['GET'],
               endpoint='pm_file_download')
    @core.wrap
    def pm_file_download(db, uid, pid, fid, vid):
        """A version's bytes. A download unless `?inline=1` AND the stored
        type (sniffed from the bytes at upload, never the browser's claim) is
        one that is safe to show in place — a PDF, an image or plain text.
        html / svg / anything unknown was stored as application/octet-stream
        and can only ever be downloaded."""
        c = core.ctx(db, pid)
        f = _file(db, c, fid, 'any')
        v = _version(db, f, vid)
        data = _read(db, v)
        ctype, inline_ok = core.d['served_type'](v['mime_type'])
        inline = bool(request.args.get('inline') == '1' and inline_ok and v['kind'] in _INLINE_KINDS)
        return _doc_response(
            data, ctype,
            core.d['safe_content_disposition'](_header_name(v['filename'], v['id']), inline),
            sandbox=not (inline and v['kind'] in ('pdf', 'image')))

    @app.route('/projects/<int:pid>/files/<int:fid>/v/<int:vid>/preview.pdf', methods=['GET'],
               endpoint='pm_file_preview')
    @core.wrap
    def pm_file_preview(db, uid, pid, fid, vid):
        """A PDF to look at: a PDF version as it is; an office document
        converted by LibreOffice once and kept as a preview row (the one GET
        here that stores something — see the module docstring)."""
        c = core.ctx(db, pid)
        f = _file(db, c, fid, 'any')
        v = _version(db, f, vid)
        if v['kind'] == 'pdf':
            pdf = _read(db, v)
        elif v['kind'] == 'office' and _ext(v['filename']) in _OFFICE_EXTS:
            pdf = _office_preview(db, pid, fid, v)
        else:
            raise _refuse(404, _NO_PREVIEW)
        return _doc_response(
            pdf, 'application/pdf',
            core.d['safe_content_disposition'](f'preview_{int(vid)}.pdf', True), sandbox=False)


def _office_preview(db, pid, fid, v):
    cached = db.execute(
        f"""SELECT {_vcols(db)} FROM pm_file_versions v
             WHERE v.preview_of = %s AND v.file_id = %s AND v.project_id = %s""",
        (v['id'], fid, pid)).fetchone()
    if cached:
        return _read(db, cached)
    src = _read(db, v)                       # ends the read transaction first
    try:
        # Only the extension reaches the converter (its temp file, its
        # process list) — never the person's filename.
        pdf = core.d['office_to_pdf'](src, 'file.' + _ext(v['filename']))
    except Exception as e:
        core.log_failure('office_preview', e, version_id=v['id'])
        pdf = None
    if not pdf or bytes(pdf[:5]) != b'%PDF-':
        raise _refuse(404, _NO_PREVIEW)
    pdf = bytes(pdf)
    if len(pdf) <= _PREVIEW_CACHE_MAX:
        try:
            _cache_preview(db, pid, fid, v['id'], pdf)
        except core.d['db_unavailable']:
            raise
        except Exception as e:              # the person still gets their preview
            try:
                db.rollback()
            except Exception:
                pass
            core.log_failure('preview_cache', e, version_id=v['id'])
    return pdf


def _cache_preview(db, pid, fid, vid, pdf):
    """Keep a converted PDF as the preview row of version `vid` — the same
    three steps as an upload. INSERT … SELECT from the source row, so a
    version that was purged meanwhile inserts nothing; two people previewing
    at once: the unique index on preview_of lets one row in (ON CONFLICT DO
    NOTHING) and the other request simply serves the PDF it made. The row
    copies the source's deleted_at, so a preview made of an archived file is
    archived with it."""
    fs = core.d['file_store']
    got = db.execute(
        """INSERT INTO pm_file_versions (file_id, project_id, version, filename, mime_type, kind,
                                         file_size, file_data, content_sha256, extract_status,
                                         preview_of, deleted_at)
           SELECT v.file_id, v.project_id, -v.version, 'preview.pdf', 'application/pdf', 'pdf',
                  %s, %s, %s, 'none', v.id, v.deleted_at
             FROM pm_file_versions v
            WHERE v.id = %s AND v.file_id = %s AND v.project_id = %s AND v.preview_of IS NULL
        ON CONFLICT DO NOTHING RETURNING id, version""",
        (len(pdf), pdf, fs.sha256_hex(pdf), vid, fid, pid)).fetchone()
    db.commit()
    if got:
        _to_s3(db, pid, fid, got['id'], got['version'], pdf, 'application/pdf')


# ─── Notes ────────────────────────────────────────────────────────────────────

def _note(db, pid, nid):
    """The live note `nid` OF THIS PROJECT, or 404."""
    core.url_id(nid)
    n = db.execute(
        'SELECT n.*, n.updated_at::timestamptz AS updated_tz FROM pm_notes n '
        'WHERE n.id = %s AND n.project_id = %s AND n.deleted_at IS NULL', (nid, pid)).fetchone()
    if not n:
        raise _refuse(404, _NOT_FOUND)
    return n


def _safe_html(html):
    """Note HTML on its way OUT (JSON, PDF, Word): sanitized again, whatever
    the row holds. If it can not be sanitized it is sent as plain text."""
    try:
        return project_docs.sanitize_note_html(html or '')
    except Exception:
        return str(Markup.escape(project_docs.html_to_text(html or '')))


def _note_html_in(v):
    """body_html from a request → the sanitized HTML that is stored. Over the
    limit (before OR after sanitizing) is a 413 — never a truncation."""
    if not isinstance(v, str):
        raise _refuse(400, 'Note must be text.')
    if len(v) > project_docs.NOTE_HTML_MAX:
        raise _refuse(413, _TOO_LONG)
    try:
        out = project_docs.sanitize_note_html(v)
    except ValueError:
        raise _refuse(413, _TOO_LONG)
    try:
        out.encode('utf-8')
    except UnicodeEncodeError:      # a lone surrogate: PostgreSQL can't store it
        raise _refuse(400, 'Note has unreadable characters.')
    return out


def _note_head(n, **over):
    return {'id': n['id'], 'title': over.get('title', n['title']), 'rev': over.get('rev', n['rev']),
            'updated_name': over.get('updated_name', n['updated_name'] or ''),
            'updated_at': core.ts(over.get('updated_tz', n['updated_tz']))}


def _note_full(n):
    return dict(_note_head(n), body_html=_safe_html(n['body_html']))


def _keep_version(db, n, uid, force=False):
    """Keep the note's CURRENT text — the text a save is about to replace —
    as a version, credited to whoever wrote it (the note's updated_by). Cut
    when the saver is someone else, when the newest version is more than 10
    minutes old, or when there is none (or always, for a restore): one
    person's typing leaves a version every ten minutes, not one per autosave.
    The newest 50 are kept. Returns True when a row was written."""
    if not force:
        last = db.execute(
            f"SELECT (created_at > NOW() - INTERVAL '{_NOTE_VERSION_GAP}') AS fresh "
            "FROM pm_note_versions WHERE note_id = %s AND project_id = %s ORDER BY id DESC LIMIT 1",
            (n['id'], n['project_id'])).fetchone()
        if last and last['fresh'] and n['updated_by'] == uid:
            return False
    db.execute(
        'INSERT INTO pm_note_versions (note_id, project_id, title, body_html, saved_by, saver_name) '
        'VALUES (%s, %s, %s, %s, %s, %s)',
        (n['id'], n['project_id'], n['title'], n['body_html'] or '', n['updated_by'],
         n['updated_name'] or ''))
    db.execute(
        'DELETE FROM pm_note_versions WHERE note_id = %s AND project_id = %s AND id NOT IN '
        '(SELECT id FROM pm_note_versions WHERE note_id = %s AND project_id = %s '
        'ORDER BY id DESC LIMIT %s)',
        (n['id'], n['project_id'], n['id'], n['project_id'], _NOTE_VERSIONS))
    return True


def _write_note(db, c, n, title, html):
    """The save itself: atomic on the note's own rev (`AND rev = %s`) — of
    two saves made from the same text exactly one matches."""
    return db.execute(
        'UPDATE pm_notes SET title = %s, body_html = %s, body_text = %s, rev = rev + 1, '
        'updated_by = %s, updated_name = %s, updated_at = CURRENT_TIMESTAMP '
        'WHERE id = %s AND project_id = %s AND deleted_at IS NULL AND rev = %s '
        'RETURNING rev, updated_at::timestamptz AS updated_tz',
        (title, html, project_docs.html_to_text(html), c.uid, c.name, n['id'], c.pid, n['rev'])).fetchone()


def _conflict(n):
    return _refuse(409, 'Someone else changed this note.', current={
        'rev': n['rev'], 'title': n['title'], 'body_html': _safe_html(n['body_html']),
        'updated_name': n['updated_name'] or '', 'updated_at': core.ts(n['updated_tz'])})


def _routes_notes(app):

    @app.route('/api/projects/<int:pid>/notes', methods=['GET'], endpoint='pm_notes')
    @core.wrap
    def pm_notes(db, uid, pid):
        core.ctx(db, pid)
        rows = db.execute(
            'SELECT n.id, n.title, n.rev, n.updated_name, n.updated_at::timestamptz AS updated_tz '
            'FROM pm_notes n WHERE n.project_id = %s AND n.deleted_at IS NULL '
            'ORDER BY n.updated_at DESC, n.id DESC', (pid,)).fetchall()
        return jsonify({'notes': [_note_head(r) for r in rows]})

    @app.route('/api/projects/<int:pid>/notes', methods=['POST'], endpoint='pm_note_create')
    @core.wrap
    def pm_note_create(db, uid, pid):
        c = core.ctx(db, pid, 'edit', lock=True)
        title = core.clean_text(core.body().get('title'), core.caps['title'], 'Title', line=True) or 'Untitled'
        n = db.execute('SELECT COUNT(*) AS n FROM pm_notes WHERE project_id = %s AND deleted_at IS NULL',
                       (pid,)).fetchone()['n']
        if n >= core.limits['notes']:
            raise _refuse(400, f"This project has reached its limit of {core.limits['notes']} notes.")
        nid = db.execute(
            'INSERT INTO pm_notes (project_id, title, updated_by, updated_name, created_by) '
            'VALUES (%s, %s, %s, %s, %s) RETURNING id', (pid, title, uid, c.name, uid)).fetchone()['id']
        rev = core.touch(db, pid)
        core.activity(db, c, 'created', 'note', nid, f'created the note {core.quote(title)}')
        db.commit()
        core.syslog('PM_NOTE_CREATE', pid, note_id=nid)
        return jsonify({'success': True, 'rev': rev, 'note': _note_full(_note(db, pid, nid))})

    @app.route('/api/projects/<int:pid>/notes/<int:nid>', methods=['GET'], endpoint='pm_note')
    @core.wrap
    def pm_note(db, uid, pid, nid):
        core.ctx(db, pid)
        return jsonify({'note': _note_full(_note(db, pid, nid))})

    @app.route('/api/projects/<int:pid>/notes/<int:nid>', methods=['PUT'], endpoint='pm_note_update')
    @core.wrap
    def pm_note_update(db, uid, pid, nid):
        """{title?, body_html?, base_rev} — the autosave. base_rev is the rev
        the editor loaded; anything else in the row means someone saved in
        between → 409 with their text (the client offers "load theirs" /
        "keep mine as a copy"; it never overwrites silently).

        Typing does NOT bump the project rev (every open page would refetch
        every few seconds) and writes history / syslog only when it cuts a
        version; a rename does bump it — the notes list shows titles."""
        c = core.ctx(db, pid, 'edit', lock=True)
        n = _note(db, pid, nid)
        body = core.body()
        base = core.to_int(body.get('base_rev'), 'base_rev', lo=0)
        title, html = n['title'], n['body_html'] or ''
        if 'title' in body:
            title = core.clean_text(body['title'], core.caps['title'], 'Title', line=True) or 'Untitled'
        if 'body_html' in body:
            html = _note_html_in(body['body_html'])
        if base != n['rev']:
            raise _conflict(n)
        if title == n['title'] and html == (n['body_html'] or ''):
            return jsonify({'success': True, 'note': _note_head(n)})    # nothing to save
        cut = _keep_version(db, n, uid)
        row = _write_note(db, c, n, title, html)
        if not row:
            # Unreachable while every note write holds the project lock —
            # but the conditional UPDATE is the rule, so honour its answer.
            db.rollback()
            raise _conflict(_note(db, pid, nid))
        renamed = title != n['title']
        out = {'success': True}
        if renamed:
            out['rev'] = core.touch(db, pid)
            core.activity(db, c, 'updated', 'note', nid,
                          f"renamed the note {core.quote(n['title'])} to {core.quote(title)}")
        elif cut:
            core.activity(db, c, 'updated', 'note', nid, f'edited the note {core.quote(title)}')
        db.commit()
        if renamed or cut:
            core.syslog('PM_NOTE_SAVE', pid, note_id=nid)
        out['note'] = _note_head(n, title=title, rev=row['rev'], updated_name=c.name,
                                 updated_tz=row['updated_tz'])
        return jsonify(out)

    @app.route('/api/projects/<int:pid>/notes/<int:nid>', methods=['DELETE'], endpoint='pm_note_delete')
    @core.wrap
    def pm_note_delete(db, uid, pid, nid):
        c = core.ctx(db, pid, 'edit', lock=True)
        n = _note(db, pid, nid)
        db.execute('UPDATE pm_notes SET deleted_at = CURRENT_TIMESTAMP WHERE id = %s AND project_id = %s',
                   (nid, pid))
        rev = core.touch(db, pid)
        core.activity(db, c, 'deleted', 'note', nid, f"deleted the note {core.quote(n['title'])}")
        db.commit()
        core.syslog('PM_NOTE_DELETE', pid, note_id=nid)
        return jsonify({'success': True, 'rev': rev, 'deleted': nid})

    @app.route('/api/projects/<int:pid>/notes/<int:nid>/versions', methods=['GET'],
               endpoint='pm_note_versions')
    @core.wrap
    def pm_note_versions(db, uid, pid, nid):
        core.ctx(db, pid)
        _note(db, pid, nid)
        rows = db.execute(
            'SELECT id, title, saver_name, created_at::timestamptz AS at_tz FROM pm_note_versions '
            'WHERE note_id = %s AND project_id = %s ORDER BY id DESC LIMIT %s',
            (nid, pid, _NOTE_VERSIONS)).fetchall()
        return jsonify({'versions': [{'id': r['id'], 'title': r['title'] or '',
                                      'saver_name': r['saver_name'] or '', 'at': core.ts(r['at_tz'])}
                                     for r in rows]})

    def version_row(db, pid, nid, vid):
        core.url_id(vid)
        v = db.execute(
            'SELECT id, title, body_html, saver_name, created_at::timestamptz AS at_tz '
            'FROM pm_note_versions WHERE id = %s AND note_id = %s AND project_id = %s',
            (vid, nid, pid)).fetchone()
        if not v:
            raise _refuse(404, _NOT_FOUND)
        return v

    @app.route('/api/projects/<int:pid>/notes/<int:nid>/versions/<int:vid>', methods=['GET'],
               endpoint='pm_note_version')
    @core.wrap
    def pm_note_version(db, uid, pid, nid, vid):
        core.ctx(db, pid)
        _note(db, pid, nid)
        v = version_row(db, pid, nid, vid)
        return jsonify({'version': {'id': v['id'], 'title': v['title'] or '',
                                    'body_html': _safe_html(v['body_html']),
                                    'saver_name': v['saver_name'] or '', 'at': core.ts(v['at_tz'])}})

    @app.route('/api/projects/<int:pid>/notes/<int:nid>/versions/<int:vid>/restore', methods=['POST'],
               endpoint='pm_note_version_restore')
    @core.wrap
    def pm_note_version_restore(db, uid, pid, nid, vid):
        """Put an earlier text back. The text it replaces is kept as a version
        first (always), so a restore can itself be undone."""
        c = core.ctx(db, pid, 'edit', lock=True)
        n = _note(db, pid, nid)
        v = version_row(db, pid, nid, vid)
        title = v['title'] or 'Untitled'
        try:
            html = project_docs.sanitize_note_html(v['body_html'] or '')
        except ValueError:
            raise _refuse(413, _TOO_LONG)
        if title == n['title'] and html == (n['body_html'] or ''):
            return jsonify({'success': True, 'rev': c.project['rev'], 'note': _note_full(n)})
        _keep_version(db, n, uid, force=True)
        if not _write_note(db, c, n, title, html):
            db.rollback()
            raise _conflict(_note(db, pid, nid))
        rev = core.touch(db, pid)
        core.activity(db, c, 'restored', 'note', nid,
                      f'restored an earlier version of the note {core.quote(title)}')
        db.commit()
        core.syslog('PM_NOTE_RESTORE', pid, note_id=nid)
        return jsonify({'success': True, 'rev': rev, 'note': _note_full(_note(db, pid, nid))})

    @app.route('/projects/<int:pid>/notes/<int:nid>/note.pdf', methods=['GET'], endpoint='pm_note_pdf')
    @core.wrap
    def pm_note_pdf(db, uid, pid, nid):
        c = core.ctx(db, pid)
        n = _note(db, pid, nid)
        db.rollback()
        html = render_template(
            'pdf/project_note_pdf.html', title=n['title'], project=c.project['name'],
            # Sanitized AGAIN here: this is the one place the stored HTML is
            # rendered as markup on the server.
            body=Markup(_safe_html(n['body_html'])),
            updated_by=n['updated_name'] or '', updated=_fmt_stamp(n['updated_at']),
            generated=_fmt_stamp(datetime.now()), generated_by=c.name, logo=_logo())
        pdf = _render_pdf(html)
        core.syslog('PM_NOTE_PDF', pid, note_id=nid)
        return _doc_response(
            pdf, 'application/pdf',
            core.d['safe_content_disposition'](f'project_note_{int(nid)}.pdf', True), sandbox=False)

    @app.route('/projects/<int:pid>/notes/<int:nid>/download/docx', methods=['GET'], endpoint='pm_note_docx')
    @core.wrap
    def pm_note_docx(db, uid, pid, nid):
        core.ctx(db, pid)
        n = _note(db, pid, nid)
        db.rollback()
        data = project_docs.html_to_docx(n['title'], _safe_html(n['body_html']))
        if not data:                # python-docx missing: say so, never an empty file
            raise _refuse(503, 'Word export is not available on this server.')
        core.syslog('PM_NOTE_DOCX', pid, note_id=nid)
        return _doc_response(
            data, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            core.d['safe_content_disposition'](f'project_note_{int(nid)}.docx', False))


# ─── PDFs ─────────────────────────────────────────────────────────────────────

def _fmt_stamp(v):
    """'Dec 1, 2026 14:05' — a naive server-clock datetime, as printed on
    paper (no browser involved, so no timezone conversion)."""
    if not v:
        return ''
    return f'{_fmt_date(v.date())} {v.hour:02d}:{v.minute:02d}'


def _logo():
    """The app logo as a data: URL, or '' — anything else is not rendered
    (the PDF must never fetch a resource)."""
    try:
        logo = core.d['get_logo']() or ''
    except core.d['db_unavailable']:
        raise
    except Exception:
        return ''
    return logo if isinstance(logo, str) and logo.startswith('data:image/') else ''


def _data_only_fetcher():
    """WeasyPrint's URL fetcher for these PDFs: inline data: URLs (the logo)
    and NOTHING else — a note or a project text can never make the server
    request an address. Newer WeasyPrint takes a URLFetcher object (and
    refuses a plain function); older ones take a function."""
    try:
        from weasyprint.urls import URLFetcher
    except ImportError:
        from weasyprint import default_url_fetcher

        def data_only(url, *args, **kwargs):
            if str(url)[:5].lower() == 'data:':
                return default_url_fetcher(url, *args, **kwargs)
            raise ValueError('external resources are not loaded')
        return data_only
    return URLFetcher(allowed_protocols=('data',), allow_redirects=False)


def _render_pdf(html):
    """HTML → PDF bytes with the shared per-thread font configuration
    (CLAUDE.md "PDF rendering": without it every render leaks native memory)."""
    from weasyprint import HTML as WP_HTML
    return WP_HTML(string=html, url_fetcher=_data_only_fetcher()).write_pdf(
        font_config=core.d['pdf_font_config']())


def _capped(rows):
    """(rows shown, how many more)."""
    return rows[:_PDF_LIST_MAX], max(0, len(rows) - _PDF_LIST_MAX)


def _summary(db, c, uid):
    """Everything the executive summary prints, as plain display-ready values
    (the template does no arithmetic and autoescapes every string). The
    Budget block — and the linked shows' figures in it — exist only when the
    person generating the PDF can see the budget; each show figure only where
    their own Reports permissions cover it (same gates as …/budget/shows)."""
    import projects_module as pm        # the sibling is loaded: it called register()
    p, pid, today = c.project, c.pid, date.today()
    tasks = pm._tasks_out(db, pid)
    members = pm._members_out(db, pid)
    names = {m['id']: m['name'] for m in members}

    def who(t):
        a = t['assignee_id']
        if not a:
            return ''
        return names.get(a) or (core.user_name(db, a) + ' (not on project)')

    def d(v):
        return date.fromisoformat(v) if v else None

    total = len(tasks)
    done = sum(1 for t in tasks if t['status'] == 'done')
    statuses = [{'label': label, 'key': key, 'count': n, 'pct': _pct(n, total)}
                for key, label in _TASK_STATUS
                for n in [sum(1 for t in tasks if t['status'] == key)]]
    open_tasks = [t for t in tasks if t['status'] != 'done']
    overdue = sorted((t for t in open_tasks if d(t['due_date']) and d(t['due_date']) < today),
                     key=lambda t: (t['due_date'], t['id']))
    horizon = today + timedelta(days=30)
    coming = sorted((t for t in open_tasks if d(t['due_date']) and today <= d(t['due_date']) <= horizon),
                    key=lambda t: (t['due_date'], t['id']))
    label_of = dict(_TASK_STATUS)

    def task_row(t):
        return {'title': t['title'], 'assignee': who(t), 'due': _fmt_date(t['due_date']),
                'days': (today - d(t['due_date'])).days if d(t['due_date']) else 0}

    miles = []
    for t in tasks:
        if not t['is_milestone']:
            continue
        when = d(t['due_date'] or t['start_date'])
        miles.append({'when': when, 'date': _fmt_date(when) or 'No date', 'title': t['title'],
                      'status': label_of.get(t['status'], t['status']), 'done': t['status'] == 'done',
                      'late': bool(when and when < today and t['status'] != 'done')})
    miles.sort(key=lambda m: (m['when'] is None, m['when'] or today, m['title'].lower()))

    shows = []
    show_ids = _linked_show_ids(db, uid, pid)
    for s in (core.d['show_bars'](db, show_ids) if show_ids else []):
        a, b = _fmt_date(s.get('start')), _fmt_date(s.get('end'))
        shows.append({'name': s['name'], 'venue': s.get('venue') or '',
                      'kind': 'Event' if s.get('show_mode') == 'event' else 'Show',
                      'dates': (a if a == b or not b else f'{a} – {b}') or 'No date yet',
                      'past': bool(s.get('is_past'))})

    budget = None
    if c.can_budget:
        b = _budget(db, pid)
        if b['lines']:
            def brow(name, t):
                var = round(t['estimate'] - t['actual'], 2)
                return {'name': name, 'estimate': _fmt_money(t['estimate']),
                        'committed': _fmt_money(t['committed']), 'actual': _fmt_money(t['actual']),
                        'variance': (_fmt_money(abs(var)) + (' over' if var < 0 else ' under')) if var else '—',
                        'over': var < 0,
                        'committed_pct': _pct(t['committed'], t['estimate']),
                        'actual_pct': _pct(t['actual'], t['estimate'])}
            budget = {'total': brow('Total', b['totals']),
                      'categories': [brow(x['category'] or 'Uncategorized', x) for x in b['by_category']],
                      'lines': len(b['lines']), 'over_lines': sum(1 for x in b['lines'] if x['over']),
                      'figures': None}
        else:
            budget = {'total': None, 'categories': [], 'lines': 0, 'over_lines': 0, 'figures': None}
        if show_ids:
            figs = core.d['show_figures'](db, show_ids)
            can = figs.get('can') or {}
            cols = [(k, label) for k, label, gate in (
                ('assets', 'Assets', 'assets'), ('external', 'External rentals', 'assets'),
                ('piano', 'Piano', 'piano'), ('labor_estimate', 'Labor estimate', 'labor'),
                ('labor_actual', 'Labor actual', 'labor')) if can.get(gate)]
            if cols and figs.get('shows'):
                budget['figures'] = {
                    'cols': [label for _k, label in cols],
                    'rows': [{'name': s['name'], 'settled': bool(s.get('settled')),
                              'cells': [_fmt_money(s.get(k)) for k, _l in cols]}
                             for s in figs['shows']],
                    'totals': [_fmt_money((figs.get('totals') or {}).get(k)) for k, _l in cols]}
        if not budget['lines'] and not budget['figures']:
            budget = None           # nothing to print: the section is omitted

    contacts = [{'name': k['name'], 'company': k['company'], 'role': k['role']}
                for k in pm._contacts_out(db, pid)]
    owner = next((m['name'] for m in members if m['role'] == 'owner'), '')
    over_rows, over_more = _capped([task_row(t) for t in overdue])
    next_rows, next_more = _capped([task_row(t) for t in coming])
    mile_rows, mile_more = _capped(miles)
    contact_rows, contact_more = _capped(contacts)
    start, end = _fmt_date(p['start_date']), _fmt_date(p['end_date'])
    return {
        'name': p['name'], 'status': _PROJECT_STATUS.get(p['status'], p['status']),
        'dates': f'{start} – {end}' if start and end else (start and f'From {start}') or (end and f'Until {end}') or '',
        'description': p['description'] or '', 'owner': owner,
        'summary_text': p['summary_text'] or '', 'risks_text': p['risks_text'] or '',
        'progress': {'total': total, 'done': done, 'pct': _pct(done, total), 'statuses': statuses,
                     'overdue': len(overdue), 'conflicts': sum(1 for t in tasks if t['conflict'])},
        'milestones': mile_rows, 'milestones_more': mile_more,
        'overdue': over_rows, 'overdue_more': over_more,
        'coming': next_rows, 'coming_more': next_more,
        'shows': shows, 'budget': budget,
        'team': [{'name': m['name'], 'role': _ROLE.get(m['role'], m['role'])} for m in members],
        'contacts': contact_rows, 'contacts_more': contact_more,
    }


def _routes_summary(app):

    @app.route('/projects/<int:pid>/summary.pdf', methods=['GET'], endpoint='pm_summary_pdf')
    @core.wrap
    def pm_summary_pdf(db, uid, pid):
        c = core.ctx(db, pid)
        s = _summary(db, c, uid)
        logo = _logo()
        db.rollback()               # nothing open while WeasyPrint lays the pages out
        html = render_template('pdf/project_summary_pdf.html', s=s, logo=logo,
                               generated=_fmt_stamp(datetime.now()), generated_by=c.name)
        pdf = _render_pdf(html)
        core.syslog('PM_SUMMARY_PDF', pid)
        return _doc_response(
            pdf, 'application/pdf',
            core.d['safe_content_disposition'](
                f'project_summary_{int(pid)}_{date.today().isoformat()}.pdf', True), sandbox=False)


# ─── Registration ─────────────────────────────────────────────────────────────

def register(app, core_ns):
    """Called once by projects_module.register() with its `core` namespace.
    Installs the budget, file, note and summary routes (all under the URL
    trees that module's gate already owns)."""
    global core
    core = core_ns
    _routes_budget(app)
    _routes_files(app)
    _routes_notes(app)
    _routes_summary(app)
    return core
