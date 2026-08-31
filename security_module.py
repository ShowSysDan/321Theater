# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
Security Sign-In Sheets — SANDBOXED module (per-show paperwork).

Generates the sign-in sheet that sits at the security desk on show day: a
venue-color-themed PDF headed with the show name/date, then one row per
expected person with a blank signature/time column, plus a few blank walk-up
rows at the end.

PMs typically receive the personnel list pasted into an email, so entry is
built around bulk import: a paste box (one name per line, or a single
comma-separated line) and a CSV upload, both parsed by ONE server-side parser
(`_parse_names_payload`) so the two paths can never disagree. Individual rows
can still be edited/added/removed by hand before saving.

SANDBOX RULES (mirrors prism_module / snapshot_module):
  * This module owns exactly one table — `security_signin_names` — and never
    writes any other app table (audit_log via the injected log_audit only).
  * app.py's only knowledge of it is one `register(app, **deps)` call, the
    module-enabled flag it reads through the injected `module_enabled`, and
    the show-page export card. Delete this file + that card and the app runs
    unchanged.
  * The whole feature is toggleable in Settings → System → Modules
    (app_settings key `module_security_signin_enabled`). When disabled every
    route here 404s and the show-page card disappears.

Routes (all show-scoped, all require show access; module must be enabled):
  GET  /shows/<id>/security            page (name editor + export)
  GET  /shows/<id>/security/names      JSON list of saved names
  POST /shows/<id>/security/names      replace the saved list (editors only)
  POST /shows/<id>/security/parse      parse pasted text / uploaded CSV into
                                       names — read-only helper, saves nothing
  GET  /shows/<id>/security/sheet.pdf  the sign-in sheet PDF

Syslog events: SECURITY_SIGNIN_SAVE, SECURITY_SIGNIN_EXPORT.
"""

import csv
import io
import re
from datetime import date
from functools import wraps

from flask import abort, jsonify, make_response, render_template, request, session
from werkzeug.utils import secure_filename

# Filled by register() — mirrors prism_module's dependency-dict pattern.
_d = {}

MODULE_KEY = 'security_signin'

MAX_NAMES = 500          # hard cap on rows per show (also enforced client-side)
MAX_NAME_LEN = 200       # single name length cap
MAX_PARSE_BYTES = 512 * 1024  # pasted text / uploaded CSV size cap
BLANK_WALKUP_ROWS = 8    # extra empty rows printed after the named rows

# Header cells that mean "this CSV column holds the name" (case-insensitive).
_NAME_HEADER_RE = re.compile(r'^\s*(full\s*)?names?\s*$', re.I)
_FIRST_HEADER_RE = re.compile(r'^\s*first(\s*name)?\s*$', re.I)
_LAST_HEADER_RE = re.compile(r'^\s*last(\s*name)?\s*$', re.I)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _enabled():
    return _d['module_enabled'](MODULE_KEY)


def _module_required(f):
    """404 every route in this module while it's switched off in Settings."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _enabled():
            abort(404)
        return f(*args, **kwargs)
    return decorated


def _require_show(show_id):
    """Access check + fetch. Returns the show row or aborts (403/404).
    Caller owns no connection yet — this opens and closes its own."""
    if not _d['can_access_show'](session['user_id'], show_id):
        abort(403)
    db = _d['get_db']()
    try:
        show = db.execute('SELECT * FROM shows WHERE id=?', (show_id,)).fetchone()
    finally:
        db.close()
    if not show:
        abort(404)
    return show


def _is_editor():
    """Anyone with show access may edit the list EXCEPT read-only and
    restricted users — same policy as the advance form."""
    return not (session.get('is_readonly') or session.get('is_restricted'))


def _clean_name(raw):
    """Collapse internal whitespace, strip, cap length. '' means drop."""
    name = ' '.join(str(raw or '').split())
    return name[:MAX_NAME_LEN]


def _fetch_names(db, show_id):
    return [r['name'] for r in db.execute(
        'SELECT name FROM security_signin_names WHERE show_id=? '
        'ORDER BY sort_order, id', (show_id,)).fetchall()]


# ─── Name parsing (paste box + CSV upload share this) ────────────────────────

def _parse_names_text(text):
    """Pasted plain text → names.
    Multiple lines → one name per line (kept as-is, so 'Last, First' lines
    survive). A single line → treat as a comma/semicolon-separated email list
    ('Ann Lee, Bo Ray, Cy Cruz'). Semicolons split even multi-line input —
    Outlook address lines use them and never appear inside a single name."""
    if ';' in text:
        parts = []
        for line in text.splitlines():
            parts.extend(line.split(';'))
        return [_clean_name(p) for p in parts if _clean_name(p)]
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1:
        single = lines[0] if lines else ''
        parts = single.split(',') if single.count(',') >= 2 else [single]
        return [_clean_name(p) for p in parts if _clean_name(p)]
    return [_clean_name(ln) for ln in lines if _clean_name(ln)]


def _parse_names_csv(text):
    """CSV file content → names.
    Header row containing a name-ish column ('name', or 'first'+'last') picks
    the column(s); with no header, two-column rows are joined 'first last',
    otherwise the first non-empty cell wins."""
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        return []

    header = rows[0]
    name_col = first_col = last_col = None
    for i, cell in enumerate(header):
        if name_col is None and _NAME_HEADER_RE.match(cell):
            name_col = i
        if first_col is None and _FIRST_HEADER_RE.match(cell):
            first_col = i
        if last_col is None and _LAST_HEADER_RE.match(cell):
            last_col = i
    has_header = name_col is not None or (first_col is not None and last_col is not None)

    names = []
    for row in (rows[1:] if has_header else rows):
        if name_col is not None:
            cell = row[name_col] if name_col < len(row) else ''
            name = _clean_name(cell)
        elif first_col is not None and last_col is not None:
            first = row[first_col] if first_col < len(row) else ''
            last = row[last_col] if last_col < len(row) else ''
            name = _clean_name(f'{first} {last}')
        elif len([c for c in row if c.strip()]) == 2 and len(row) == 2:
            name = _clean_name(f'{row[0]} {row[1]}')
        else:
            name = next((_clean_name(c) for c in row if c.strip()), '')
        if name:
            names.append(name)
    return names


def _parse_names_payload():
    """The shared parser behind POST .../security/parse: multipart `file`
    (CSV) or JSON {'text': ...} (paste box). Returns (names, error)."""
    if 'file' in request.files:
        f = request.files['file']
        raw = f.read(MAX_PARSE_BYTES + 1)
        if len(raw) > MAX_PARSE_BYTES:
            return None, 'File too large (512 KB max).'
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                text = raw.decode('latin-1')
            except Exception:
                return None, 'Could not read that file as text/CSV.'
        return _parse_names_csv(text), None

    data = request.get_json(silent=True) or {}
    text = str(data.get('text') or '')
    if len(text) > MAX_PARSE_BYTES:
        return None, 'Pasted text too large (512 KB max).'
    return _parse_names_text(text), None


# ─── Views ────────────────────────────────────────────────────────────────────

def _page_view(show_id):
    show = _require_show(show_id)
    db = _d['get_db']()
    try:
        names = _fetch_names(db, show_id)
    finally:
        db.close()
    return render_template(
        'security_signin.html',
        show=dict(show),
        names=names,
        can_edit=_is_editor(),
        max_names=MAX_NAMES,
        user=_d['get_current_user'](),
    )


def _names_get_view(show_id):
    _require_show(show_id)
    db = _d['get_db']()
    try:
        names = _fetch_names(db, show_id)
    finally:
        db.close()
    return jsonify({'success': True, 'names': names})


def _names_save_view(show_id):
    show = _require_show(show_id)
    if not _is_editor():
        abort(403)
    data = request.get_json(silent=True) or {}
    raw = data.get('names')
    if not isinstance(raw, list):
        return jsonify({'success': False, 'error': 'names must be a list.'}), 400
    names = [n for n in (_clean_name(x) for x in raw) if n]
    if len(names) > MAX_NAMES:
        return jsonify({'success': False,
                        'error': f'Too many names (max {MAX_NAMES}).'}), 400

    db = _d['get_db']()
    try:
        before = _fetch_names(db, show_id)
        db.execute('DELETE FROM security_signin_names WHERE show_id=?', (show_id,))
        for i, name in enumerate(names):
            db.execute(
                'INSERT INTO security_signin_names (show_id, name, sort_order, created_by) '
                'VALUES (?,?,?,?)',
                (show_id, name, i, session.get('user_id')))
        if names != before:
            _d['log_audit'](db, 'SECURITY_SIGNIN_SAVE', 'security_signin', show_id,
                            show_id=show_id,
                            before={'names': before}, after={'names': names},
                            detail=f'{len(before)} → {len(names)} names')
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        db.close()
    _d['syslog_logger'].info(
        f"SECURITY_SIGNIN_SAVE show_id={show_id} count={len(names)} "
        f"by={session.get('username')}")
    return jsonify({'success': True, 'names': names})


def _parse_view(show_id):
    _require_show(show_id)
    if not _is_editor():
        abort(403)
    names, err = _parse_names_payload()
    if err:
        return jsonify({'success': False, 'error': err}), 400
    return jsonify({'success': True, 'names': names[:MAX_NAMES]})


def _pdf_view(show_id):
    show = _require_show(show_id)
    db = _d['get_db']()
    try:
        names = _fetch_names(db, show_id)
        pdf_colors = _d['get_venue_pdf_colors'](db, show['venue'])
        logo_data = _d['get_logo_for_venue'](db, show['venue'])
    finally:
        db.close()

    html_str = render_template(
        'pdf/security_signin_pdf.html',
        show=dict(show),
        names=names,
        blank_rows=BLANK_WALKUP_ROWS,
        pdf_colors=pdf_colors,
        logo_data=logo_data,
        generated_date=date.today().isoformat(),
    )
    try:
        from weasyprint import HTML as WP_HTML
        pdf_bytes = WP_HTML(string=html_str, base_url=request.host_url).write_pdf()
    except Exception as e:
        _d['app'].logger.error(
            f'WeasyPrint security-signin error show_id={show_id}: {e}')
        return f'PDF generation failed: {e}', 500

    _d['syslog_logger'].info(
        f"SECURITY_SIGNIN_EXPORT show_id={show_id} count={len(names)} "
        f"by={session.get('username')}")
    safe_name = secure_filename(show['name'] or f'show_{show_id}')
    resp = make_response(pdf_bytes)
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = \
        _d['safe_content_disposition'](f'{safe_name}_security_signin.pdf')
    return resp


# ─── Registration ─────────────────────────────────────────────────────────────

def register(app, **deps):
    """
    Wire the module into the Flask app. `deps` must provide:
      get_db, get_current_user, login_required, can_access_show,
      module_enabled, log_audit, syslog_logger, get_venue_pdf_colors,
      get_logo_for_venue, safe_content_disposition.
    Called once from app.py; everything else in this file is self-contained.
    """
    _d.update(deps, app=app)
    login = deps['login_required']

    def _guard(view):
        return login(_module_required(view))

    app.add_url_rule('/shows/<int:show_id>/security',
                     'security_signin_page', _guard(_page_view))
    app.add_url_rule('/shows/<int:show_id>/security/names',
                     'security_signin_names', _guard(_names_get_view))
    app.add_url_rule('/shows/<int:show_id>/security/names',
                     'security_signin_save', _guard(_names_save_view),
                     methods=['POST'])
    app.add_url_rule('/shows/<int:show_id>/security/parse',
                     'security_signin_parse', _guard(_parse_view),
                     methods=['POST'])
    app.add_url_rule('/shows/<int:show_id>/security/sheet.pdf',
                     'security_signin_pdf', _guard(_pdf_view))
