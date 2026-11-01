# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
#Tags + Search — SANDBOXED module (3.35.0).

TAGS. Anyone can write a #tag in any free-text box of a show (Advance text /
notes fields, schedule rows + VENUE & TECH text, Post Show notes, comments,
labor notes / day notes / line notes, Assets line notes (never hidden,
admin-only lines), external rentals, piano tuning notes (only while the
piano_tuning module is on), file descriptions). Nothing is stored at write time:
the tag index is a QUERY over the live text (`_sources_sql`, one UNION ALL
builder over every source), so the ~25 write paths, the module code, merges
and snapshot restores can never leave a stale index behind.

  Grammar — keep IDENTICAL in Python (TAG_RE), PostgreSQL (TAG_PG_PATTERN,
  always bound as a parameter) and JS (static/js/tags.js, captured-prefix
  form because Safari < 16.4 has no regex lookbehind):
      '#' + a letter + [A-Za-z0-9_]* + any number of '-[A-Za-z0-9_]+' groups,
      preceded by the start of the text or a character NOT in [A-Za-z0-9_&/#]
      (so 'x#a', 'Truck #2', 'http://x/#frag', '&#39;' are not tags),
      normalized to lowercase, at most TAG_MAX_LEN characters (longer = not a
      tag). ASCII only, so every engine agrees.

SEARCH. /search + /api/search: free text over show name / venue / Arts
Group / PM names (optionally inside every tag source as well), PM (show PM
OR any day's cover PM, via the injected _pm_contacts_many — never another PM
lookup), Edited by (audit_log minus the noisy FORM_SAVE, plus
form_history.saved_by), tags (AND), venue, date overlap, status, test shows.

SANDBOX RULES (mirrors security_module / spot_module):
  * Never imports app.py; app.py's only knowledge of it is ONE
    `register(app, **deps)` call plus the base.html nav link / m-header
    button / tags.js include.
  * Read-only: no table of its own, no writes, every route a side-effect-free
    GET. All routes are login_required; document viewers never reach them
    (_viewer_gate — none of these endpoints is whitelisted). Show access goes
    through the injected get_accessible_shows / can_access_show.

Routes:
  GET /tags                    every tag with its counts (filter + sort)
  GET /tags/<tag>              one tag: occurrences grouped by show, deep links
  GET /search                  the Search page
  GET /api/tags?q=             prefix autocomplete (top 20 by count)
  GET /api/shows/<id>/tags     one show's tags + counts (show header strip)
  GET /api/search              search results (JSON)
  GET /api/search/editors      users who have edited shows (Edited-by picker)

Deep links: /shows/<id>?tab=<tab>&focus=<kind>:<ref>, handled by tags.js on
the show page. Kinds (FOCUS_KINDS): adv:<field_key>, sched:<row_id>,
schedmeta:<key>, postnotes:<key>, comment:<id>, labornotes,
laborday:<YYYY-MM-DD>, laborreq:<labor_request id>, pslabor:<post_show_labor
id>, asset:<show_assets id>, rental:<external rental id>, piano:<tuning id>,
file:<attachment id>.
"""

import re
import threading
import time
from datetime import date
from urllib.parse import quote

from flask import (abort, has_request_context, jsonify, redirect,
                   render_template, request, session, url_for)
from markupsafe import Markup, escape

# Filled by register().
_d = {}

# ─── Tag grammar ──────────────────────────────────────────────────────────────

TAG_MAX_LEN = 50
_TAG_CORE = r'#([A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*)'
TAG_RE = re.compile(r'(?<![A-Za-z0-9_&/#])' + _TAG_CORE)
# PostgreSQL ARE: the prefix is consumed instead of looked-behind; with the
# 'g' flag that never steals a character from the previous tag (the tag body
# can't end in the delimiter). Bind it as a parameter — never inline it.
TAG_PG_PATTERN = r'(?:^|[^A-Za-z0-9_&/#])' + _TAG_CORE
_TAG_NAME_RE = re.compile(r'^[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*$')

MAX_Q_LEN = 200              # search text cap
MAX_TAGS = 5                 # tags per search (AND)
MAX_RESULTS = 500            # hard cap on a search page
DEFAULT_RESULTS = 200
TAG_PAGE_CAP = 1000          # occurrences listed on one tag page (newest shows first)
SHOW_STRIP_MAX = 40          # tags /api/shows/<id>/tags returns (the header strip; +N for the rest)
CONTENT_HITS_PER_SHOW = 3    # snippets per show in a content search
_CACHE_TTL = 30.0            # all-tags list, per process
_CACHE_MAX_KEYS = 8

_EN_DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_EN_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug',
              'Sep', 'Oct', 'Nov', 'Dec')


def normalize_tag(raw):
    """'#Load-In' / 'load-in' → 'load-in'; anything that isn't a valid tag
    name → None."""
    s = str(raw or '').strip()
    if s.startswith('#'):
        s = s[1:]
    s = s.lower()
    if not s or len(s) > TAG_MAX_LEN or not _TAG_NAME_RE.match(s):
        return None
    return s


def extract_tags(text):
    """Distinct normalized tags in a text, in order of first appearance."""
    out, seen = [], set()
    for m in TAG_RE.finditer(str(text or '')):
        t = m.group(1).lower()
        if len(t) <= TAG_MAX_LEN and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _tag_spans(text):
    """[(start, end, tag)] of every tag in text ('#' included in the span)."""
    out = []
    for m in TAG_RE.finditer(text):
        t = m.group(1).lower()
        if len(t) <= TAG_MAX_LEN:
            out.append((m.start(), m.end(), t))
    return out


def tag_url(tag):
    return '/tags/' + quote(tag, safe='')


def tagify(text):
    """Jinja filter: escaped text with each #tag a link to its tag page.
    Plain text for document viewers (they can't open /tags)."""
    s = '' if text is None else str(text)
    if not has_request_context() or session.get('is_document_viewer'):
        return escape(s)
    parts, pos = [], 0
    for start, end, t in _tag_spans(s):
        parts.append(escape(s[pos:start]))
        parts.append(Markup('<a class="tag-link" href="{}">{}</a>').format(
            tag_url(t), s[start:end]))
        pos = end
    parts.append(escape(s[pos:]))
    return Markup('').join(parts)


# ─── The text sources (ONE builder — keep the inventory here) ─────────────────
# Each branch: (kind, FROM ... [WHERE ...] sql, extra where or '', show col,
# ref expr, label expr, body expr, date expr (ISO text), time expr).
# Labels are finished in Python (_occ_label). manager_notes / the piano
# cancel note are manager-only and are NEVER a source. Piano-owned schedule
# rows and piano external-rental lines carry derived text — excluded.
# Hidden (admin-only) asset lines are never a source either: the Assets tab,
# invoices and reports hide them from non-admins, and the tag counts are
# cached per access set, not per role. The 'piano' branch is dropped while
# the piano_tuning module is off (_sources_sql).

_POSTNOTE_LABELS = {
    'show_notes_tour': 'Show Notes (Tour/Client)',
    'house_notes': 'House Notes',
    'equipment_issues': 'Equipment / Maintenance Issues',
    'miscellaneous': 'Miscellaneous',
}
_POSTNOTE_KEYS_SQL = ', '.join("'%s'" % k for k in _POSTNOTE_LABELS)  # code constants

_ISO = "to_char({}, 'YYYY-MM-DD')"

_BRANCHES = (
    ('adv', 'advance_data ad JOIN form_fields ff ON ff.field_key = ad.field_key',
     "(ff.field_type IN ('text', 'textarea') OR ff.is_notes_field = 1)",
     'ad.show_id', 'ad.field_key', 'ff.label', 'ad.field_value', 'NULL', 'NULL'),
    ('sched', 'schedule_rows sr LEFT JOIN show_performances sp ON sp.id = sr.perf_id',
     'sr.piano_tuning_id IS NULL',
     'sr.show_id', 'sr.id', "''",
     "concat_ws(chr(10), NULLIF(sr.description, ''), NULLIF(sr.notes, ''))",
     "COALESCE(NULLIF(sr.day_date, ''), " + _ISO.format('sp.perf_date') + ')',
     'sr.start_time'),
    ('schedmeta', 'schedule_meta sm JOIN schedule_meta_fields smf ON smf.field_key = sm.field_key',
     "COALESCE(smf.advance_field_key, '') = ''",
     'sm.show_id', 'sm.field_key', 'smf.label', 'sm.field_value', 'NULL', 'NULL'),
    ('labornotes', 'shows sh', '',
     'sh.id', "''", "''", 'sh.labor_notes', 'NULL', 'NULL'),
    ('laborday', 'show_labor_days ld', '',
     'ld.show_id', _ISO.format('ld.work_date'), "''", 'ld.day_notes',
     _ISO.format('ld.work_date'), 'NULL'),
    ('laborreq', 'labor_requests lr LEFT JOIN job_positions jp ON jp.id = lr.position_id', '',
     'lr.show_id', 'lr.id', 'jp.name', 'lr.notes', _ISO.format('lr.work_date'), 'lr.in_time'),
    ('asset', 'show_assets sa LEFT JOIN asset_types aty ON aty.id = sa.asset_type_id',
     'COALESCE(sa.is_hidden, 0) = 0',
     'sa.show_id', 'sa.id', 'aty.name', 'sa.notes', 'NULL', 'NULL'),
    ('rental', 'show_external_rentals er', 'er.piano_tuning_id IS NULL',
     'er.show_id', 'er.id', "''", 'er.description', 'NULL', 'NULL'),
    ('piano', 'piano_tunings pt', 'pt.show_id IS NOT NULL',
     'pt.show_id', 'pt.id', 'pt.piano', 'pt.notes',
     _ISO.format('COALESCE(pt.tuning_date, pt.requested_date)'), 'NULL'),
    ('postnotes', 'post_show_notes pn', 'pn.field_key IN (' + _POSTNOTE_KEYS_SQL + ')',
     'pn.show_id', 'pn.field_key', "''", 'pn.field_value', 'NULL', 'NULL'),
    ('pslabor', 'post_show_labor pl LEFT JOIN job_positions pjp ON pjp.id = pl.position_id', '',
     'pl.show_id', 'pl.id', 'pjp.name', 'pl.notes', _ISO.format('pl.work_date'), 'pl.in_time'),
    ('comment', 'show_comments sc LEFT JOIN users cu ON cu.id = sc.user_id',
     'sc.deleted_at IS NULL',
     'sc.show_id', 'sc.id',
     "COALESCE(NULLIF(cu.display_name, ''), cu.username, NULLIF(sc.author_name, ''), 'Former user')",
     'sc.body', _ISO.format('sc.created_at'), 'NULL'),
    ('file', 'show_attachments fa', 'fa.deleted_at IS NULL',
     'fa.show_id', 'fa.id', 'fa.filename', 'fa.description', 'NULL', 'NULL'),
)

# kind → (show-page tab, ordering on the tag page)
FOCUS_KINDS = {
    'adv': ('advance', 0), 'piano': ('advance', 1),
    'sched': ('schedule', 2), 'schedmeta': ('schedule', 3),
    'labornotes': ('staffing', 4), 'laborday': ('staffing', 5), 'laborreq': ('staffing', 6),
    'asset': ('assets', 7), 'rental': ('assets', 8),
    'postnotes': ('postnotes', 9), 'pslabor': ('postnotes', 10),
    'comment': ('comments', 11), 'file': ('export', 12),
}


def _piano_on():
    """The piano_tuning module's switch (app.py's module_enabled, injected —
    this module never imports app.py). Off = its notes are no source."""
    return bool(_d['module_enabled']('piano_tuning'))


def _sources_sql(cond, cond_params=(), show_ids=None, piano=None):
    """(sql, params) for the UNION ALL of every text source, as rows of
    (show_id, kind, ref, label, body, at_date, at_time). `cond` is a body
    predicate with '{body}' standing for the branch's text expression (e.g.
    "strpos({body}, '#') > 0") and `cond_params` its parameters, applied in
    EVERY branch so each table is filtered at the source. show_ids: None = all
    shows, else only those ids. piano: include the piano-tuning branch
    (None = read the module switch now)."""
    if piano is None:
        piano = _piano_on()
    parts, params = [], []
    for (kind, frm, where, show, ref, label, body, at_date, at_time) in _BRANCHES:
        if kind == 'piano' and not piano:
            continue
        conds = [where] if where else []
        if show_ids is not None:
            conds.append(f'{show} = ANY(%s)')
            params.append(list(show_ids))
        conds.append(cond.replace('{body}', body))
        params.extend(cond_params)
        parts.append(
            f"SELECT {show} AS show_id, CAST('{kind}' AS TEXT) AS kind, "
            f"CAST({ref} AS TEXT) AS ref, CAST({label} AS TEXT) AS label, "
            f"CAST({body} AS TEXT) AS body, CAST({at_date} AS TEXT) AS at_date, "
            f"CAST({at_time} AS TEXT) AS at_time "
            f"FROM {frm} WHERE {' AND '.join(conds)}")
    return '\nUNION ALL\n'.join(parts), params


def _like_escape(s):
    return s.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_')


# ─── Access ───────────────────────────────────────────────────────────────────

def _accessible_ids():
    """None = every show; otherwise the list of show ids this user may see."""
    acc = _d['get_accessible_shows'](session['user_id'])
    return None if acc is None else sorted({int(i) for i in acc})


# ─── Queries ──────────────────────────────────────────────────────────────────

_cache = {}
_cache_lock = threading.Lock()


def all_tag_counts(db, show_ids=None):
    """[{tag, n (occurrences = text boxes holding it), shows}] for every tag,
    most used first. Cached per process for _CACHE_TTL seconds per access set
    (and piano-module state, which decides whether tuning notes count)."""
    piano = _piano_on()
    key = (piano, 'all' if show_ids is None else tuple(show_ids))
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    if show_ids is not None and not show_ids:
        out = []
    else:
        src, params = _sources_sql("strpos({body}, '#') > 0", (), show_ids, piano=piano)
        rows = _d['get_db_rows'](db, f"""
            SELECT tag, COUNT(*) AS n, COUNT(DISTINCT show_id) AS shows FROM (
              SELECT DISTINCT s.show_id, s.kind, s.ref, lower(m[1]) AS tag
              FROM ({src}) s CROSS JOIN LATERAL regexp_matches(s.body, %s, 'g') AS m
            ) t
            WHERE length(tag) <= %s
            GROUP BY tag ORDER BY COUNT(*) DESC, tag
        """, params + [TAG_PG_PATTERN, TAG_MAX_LEN])
        out = [{'tag': r['tag'], 'n': int(r['n']), 'shows': int(r['shows'])} for r in rows]
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_KEYS:
            _cache.clear()
        _cache[key] = (now, out)
    return out


def show_tag_counts(db, show_id, limit=SHOW_STRIP_MAX):
    """([{tag, n}] — the `limit` most used —, number of distinct tags) for
    one show (live, not cached)."""
    src, params = _sources_sql("strpos({body}, '#') > 0", (), [int(show_id)])
    rows = _d['get_db_rows'](db, f"""
        SELECT tag, COUNT(*) AS n, COUNT(*) OVER () AS total FROM (
          SELECT DISTINCT s.kind, s.ref, lower(m[1]) AS tag
          FROM ({src}) s CROSS JOIN LATERAL regexp_matches(s.body, %s, 'g') AS m
        ) t WHERE length(tag) <= %s GROUP BY tag ORDER BY COUNT(*) DESC, tag
        LIMIT %s
    """, params + [TAG_PG_PATTERN, TAG_MAX_LEN, limit])
    return ([{'tag': r['tag'], 'n': int(r['n'])} for r in rows],
            int(rows[0]['total']) if rows else 0)


def tag_occurrences(db, tag, show_ids=None, limit=TAG_PAGE_CAP):
    """Source rows holding #tag (each text box once), newest show first (the
    tag page's order, so the cap drops the OLDEST), at most `limit`. Every
    row carries `total` = all text boxes holding it, before the cap."""
    if show_ids is not None and not show_ids:
        return []
    src, params = _sources_sql('strpos(lower({body}), %s) > 0', ('#' + tag,), show_ids)
    return _d['get_db_rows'](db, f"""
        SELECT s.*, COUNT(*) OVER () AS total FROM ({src}) s
        JOIN shows sh ON sh.id = s.show_id
        LEFT JOIN LATERAL (SELECT MIN(perf_date) AS first_perf, MAX(perf_date) AS last_perf
                           FROM show_performances WHERE show_id = sh.id) p ON TRUE
        WHERE EXISTS (SELECT 1 FROM regexp_matches(s.body, %s, 'g') AS m
                      WHERE lower(m[1]) = %s)
        ORDER BY GREATEST(sh.load_out_date, sh.show_date, p.last_perf) DESC NULLS LAST,
                 LEAST(sh.load_in_date, sh.show_date, p.first_perf) DESC NULLS LAST,
                 s.show_id DESC, s.kind, s.ref
        LIMIT %s
    """, params + [TAG_PG_PATTERN, tag, limit])


def shows_with_tags(db, tags, show_ids=None):
    """{show_id: [tags found]} for shows holding EVERY tag in `tags`."""
    if not tags or (show_ids is not None and not show_ids):
        return {}
    src, params = _sources_sql("strpos({body}, '#') > 0", (), show_ids)
    rows = _d['get_db_rows'](db, f"""
        SELECT s.show_id, array_agg(DISTINCT lower(m[1])) AS tags
        FROM ({src}) s CROSS JOIN LATERAL regexp_matches(s.body, %s, 'g') AS m
        WHERE lower(m[1]) = ANY(%s)
        GROUP BY s.show_id
        HAVING COUNT(DISTINCT lower(m[1])) = %s
    """, params + [TAG_PG_PATTERN, list(tags), len(tags)])
    return {r['show_id']: sorted(r['tags']) for r in rows}


def content_hits(db, q, show_ids):
    """{show_id: {'n': hits, 'rows': [first CONTENT_HITS_PER_SHOW source rows]}}
    for a case-insensitive phrase anywhere in the tag sources."""
    if not q or (show_ids is not None and not show_ids):
        return {}
    like = '%' + _like_escape(q) + '%'
    src, params = _sources_sql('{body} ILIKE %s', (like,), show_ids)
    rows = _d['get_db_rows'](db, f"""
        SELECT * FROM (
          SELECT s.*, row_number() OVER (PARTITION BY s.show_id ORDER BY s.kind, s.ref) AS rn,
                 COUNT(*) OVER (PARTITION BY s.show_id) AS hits
          FROM ({src}) s
        ) x WHERE rn <= %s ORDER BY show_id, rn
    """, params + [CONTENT_HITS_PER_SHOW])
    out = {}
    for r in rows:
        e = out.setdefault(r['show_id'], {'n': int(r['hits']), 'rows': []})
        e['rows'].append(r)
    return out


_SHOW_COLS = """
    s.id, s.name, s.venue, s.status, s.is_test, s.show_mode, s.prism_status,
    s.show_date,
    LEAST(s.load_in_date, s.show_date, p.first_perf) AS span_start,
    GREATEST(s.load_out_date, s.show_date, p.last_perf) AS span_end,
    COALESCE(NULLIF(NULLIF(TRIM(adc.field_value), ''), '-'),
             NULLIF(TRIM(s.performance_company), '')) AS company
"""
_SHOW_FROM = """
    FROM shows s
    LEFT JOIN LATERAL (SELECT MIN(perf_date) AS first_perf, MAX(perf_date) AS last_perf
                       FROM show_performances WHERE show_id = s.id) p ON TRUE
    LEFT JOIN advance_data adc ON adc.show_id = s.id AND adc.field_key = 'performance_company'
"""


def _show_rows(db, where_sql, params):
    return _d['get_db_rows'](db, f'SELECT {_SHOW_COLS} {_SHOW_FROM} WHERE {where_sql}', params)


# ─── Presentation helpers ─────────────────────────────────────────────────────

def _d_short(dv):
    return f'{_EN_DAYS[dv.weekday()]} {dv.month}/{dv.day}'


def _span_label(a, b):
    a, b = _d['as_date'](a), _d['as_date'](b)
    if not a and not b:
        return 'No dates'
    a = a or b
    b = b or a
    if a == b:
        return f'{_EN_DAYS[a.weekday()]}, {_EN_MONTHS[a.month - 1]} {a.day}, {a.year}'
    if a.year == b.year:
        return f'{_EN_MONTHS[a.month - 1]} {a.day} – {_EN_MONTHS[b.month - 1]} {b.day}, {b.year}'
    return (f'{_EN_MONTHS[a.month - 1]} {a.day}, {a.year} – '
            f'{_EN_MONTHS[b.month - 1]} {b.day}, {b.year}')


def _occ_label(r):
    """Human source label: 'Advance › Special Notes', 'Schedule › Mon 11/2 09:00'…"""
    kind, label = r['kind'], (r['label'] or '').strip()
    dv = _d['as_date'](r['at_date']) if r['at_date'] else None
    day = _d_short(dv) if dv else ''
    tm = (r['at_time'] or '').strip()
    if kind == 'adv':
        return f'Advance › {label or r["ref"]}'
    if kind == 'sched':
        return 'Schedule › ' + (' '.join(x for x in (day, tm) if x) or 'Timeline row')
    if kind == 'schedmeta':
        return f'Schedule › {label or r["ref"]}'
    if kind == 'labornotes':
        return 'Labor › Show labor notes'
    if kind == 'laborday':
        return f'Labor › Day notes {day}'.rstrip()
    if kind == 'laborreq':
        return 'Labor › Line notes' + ''.join(f' · {x}' for x in (label, day) if x)
    if kind == 'asset':
        return 'Assets › ' + (f'{label} notes' if label else 'Line notes')
    if kind == 'rental':
        return 'Assets › External rental'
    if kind == 'piano':
        return 'Piano Tuning › ' + (f'{label} notes' if label else 'Notes')
    if kind == 'postnotes':
        return f'Post Show › {_POSTNOTE_LABELS.get(r["ref"], r["ref"])}'
    if kind == 'pslabor':
        return 'Post Show › Labor line' + ''.join(f' · {x}' for x in (label, day) if x)
    if kind == 'comment':
        return f'Comment by {label or "someone"}' + (f' · {day}' if day else '')
    if kind == 'file':
        return f'Files › {label or "File"}'
    return label or kind


def occ_link(show_id, kind, ref):
    tab = FOCUS_KINDS.get(kind, ('advance', 0))[0]
    focus = kind + (':' + str(ref) if str(ref or '') else '')
    return f'/shows/{int(show_id)}?tab={tab}&focus={quote(focus, safe=":")}'


def _snippet(body, hits, radius=80, max_len=260):
    """Snippet around the first hit as segments [[kind, text, tag]]: kind
    'text', 'tag' (a #tag → link) or 'hit' (the match; tag set when the
    match is exactly a #tag). Newlines become ' · '."""
    body = body or ''
    if hits:
        s0, e0 = hits[0]
        start = max(0, s0 - radius)
        end = min(len(body), max(e0 + radius, start + max_len))
        if end - start > max_len:
            end = max(e0, start + max_len)
    else:
        start, end = 0, min(len(body), max_len)
    # snap to word edges so the window doesn't cut a word in half
    if start > 0:
        sp = body.rfind(' ', max(0, start - 15), start)
        start = sp + 1 if sp >= 0 else start
    if end < len(body):
        sp = body.find(' ', end, min(len(body), end + 15))
        end = sp if sp >= 0 else end
    # Keep only the hits / tags that can touch the window (a hit inside a
    # #tag may sit up to a tag's length outside it), so the matching below is
    # O(window) however many tags the body holds — over the whole body it was
    # quadratic, and one huge tagged note stalled every view of its tag page.
    m = TAG_MAX_LEN + 1
    hits = [(a, b) for a, b in hits if a < end + m and b > start - m]
    tags = {(a, b): t for a, b, t in _tag_spans(body) if a < end + m and b > start - m}
    # A text hit inside a #tag marks (and links) the whole tag.
    hits = [next(((ta, tb) for (ta, tb) in tags if ta <= a and b <= tb), (a, b))
            for a, b in hits]
    marks = []
    for a, b in hits:
        if a < end and b > start:
            marks.append((max(a, start), min(b, end), 'hit', tags.get((a, b), '')))
    for (a, b), t in tags.items():
        if a >= start and b <= end and not any(a < mb and b > ma for ma, mb, _, _ in marks):
            marks.append((a, b, 'tag', t))
    marks.sort()
    segs, pos = [], start
    if start > 0:
        segs.append(['text', '…', ''])
    for a, b, k, t in marks:
        if a < pos:
            continue
        if a > pos:
            segs.append(['text', body[pos:a], ''])
        segs.append([k, body[a:b], t])
        pos = b
    if pos < end:
        segs.append(['text', body[pos:end], ''])
    if end < len(body):
        segs.append(['text', '…', ''])
    for s in segs:
        s[1] = re.sub(r'\s*\n\s*', ' · ', s[1])
    return segs


def _show_card(r):
    sd = _d['as_date'](r['span_start'])
    ed = _d['as_date'](r['span_end'])
    return {
        'id': r['id'], 'name': r['name'], 'venue': (r['venue'] or '').strip(),
        'status': r['status'], 'is_test': bool(r['is_test']),
        'is_event': (r['show_mode'] or '') == 'event',
        'prism_status': r['prism_status'] or '',
        'company': r['company'] or '',
        'start': sd.isoformat() if sd else None,
        'end': ed.isoformat() if ed else None,
        'dates_label': _span_label(sd, ed),
        'url': f'/shows/{int(r["id"])}?tab=advance',
    }


# ─── Views ────────────────────────────────────────────────────────────────────

def _tags_index_view():
    db = _d['get_db']()
    try:
        tags = all_tag_counts(db, _accessible_ids())
    finally:
        db.close()
    return render_template('tags.html', tags=tags, user=_d['get_current_user']())


def _tag_view(tag):
    norm = normalize_tag(tag)
    if not norm:
        abort(404)
    if norm != tag:
        return redirect(url_for('tag_page', tag=norm))
    acc = _accessible_ids()
    db = _d['get_db']()
    try:
        occ = tag_occurrences(db, norm, acc)
        total_all = int(occ[0]['total']) if occ else 0
        ids = sorted({r['show_id'] for r in occ})
        shows = {r['id']: r for r in _show_rows(db, 's.id = ANY(%s)', [ids])} if ids else {}
    finally:
        db.close()
    groups = {}
    for r in occ:
        sh = shows.get(r['show_id'])
        if not sh:
            continue
        body = r['body'] or ''
        hits = [(a, b) for a, b, t in _tag_spans(body) if t == norm]
        groups.setdefault(r['show_id'], []).append({
            'kind': r['kind'], 'label': _occ_label(r),
            'link': occ_link(r['show_id'], r['kind'], r['ref']),
            'snippet': _snippet(body, hits),
            '_k': (FOCUS_KINDS.get(r['kind'], ('', 99))[1], r['at_date'] or '',
                   r['at_time'] or '', r['ref'] or ''),
        })
    out = []
    for sid, items in groups.items():
        items.sort(key=lambda o: o['_k'])
        card = _show_card(shows[sid])
        card['occurrences'] = items
        out.append(card)
    out.sort(key=lambda c: (c['end'] or '', c['start'] or '', c['id']), reverse=True)
    total = sum(len(c['occurrences']) for c in out)
    return render_template('tag.html', tag=norm, groups=out, total=total,
                           total_all=total_all, capped=total_all > len(occ),
                           user=_d['get_current_user']())


def _api_tags_view():
    raw = (request.args.get('q') or '').strip()[:TAG_MAX_LEN + 1]
    prefix = raw[1:] if raw.startswith('#') else raw
    prefix = prefix.lower()
    db = _d['get_db']()
    try:
        tags = all_tag_counts(db, _accessible_ids())
    finally:
        db.close()
    if prefix:
        tags = [t for t in tags if t['tag'].startswith(prefix)]
    return jsonify({'success': True, 'tags': tags[:20]})


def _api_show_tags_view(show_id):
    if not _d['can_access_show'](session['user_id'], show_id):
        return jsonify({'success': False, 'error': 'Access denied.'}), 403
    db = _d['get_db']()
    try:
        if not db.execute('SELECT 1 FROM shows WHERE id=%s', (show_id,)).fetchone():
            return jsonify({'success': False, 'error': 'Show not found.'}), 404
        tags, total = show_tag_counts(db, show_id)
    finally:
        db.close()
    return jsonify({'success': True, 'tags': tags, 'total': total})


def _search_page_view():
    db = _d['get_db']()
    try:
        venues = _d['get_distinct_venues'](db)
    finally:
        db.close()
    return render_template('search.html', venues=venues, user=_d['get_current_user']())


def _api_editors_view():
    """Non-locked, approved users who have edited at least one show — the
    Edited-by picker (NOT the admin-only /api/users)."""
    db = _d['get_db']()
    try:
        rows = _d['get_db_rows'](db, """
            SELECT u.id, u.username, COALESCE(NULLIF(TRIM(u.display_name), ''), u.username) AS name
            FROM users u
            WHERE COALESCE(u.is_locked, 0) = 0 AND COALESCE(u.pending_approval, 0) = 0
              AND (u.id IN (SELECT DISTINCT saved_by FROM form_history WHERE saved_by IS NOT NULL)
                   OR EXISTS (SELECT 1 FROM audit_log a WHERE a.user_id = u.id
                              AND a.show_id IS NOT NULL AND a.action <> 'FORM_SAVE'))
            ORDER BY lower(COALESCE(NULLIF(TRIM(u.display_name), ''), u.username)), u.id
        """, [])
    finally:
        db.close()
    seen = {}
    for r in rows:
        seen[r['name'].lower()] = seen.get(r['name'].lower(), 0) + 1
    users = [{'id': r['id'],
              'name': r['name'] if seen[r['name'].lower()] == 1 else f"{r['name']} ({r['username']})"}
             for r in rows]
    return jsonify({'success': True, 'users': users, 'me': session['user_id']})


def _parse_date(s):
    s = (s or '').strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _api_search_view():
    a = request.args
    q = ' '.join((a.get('q') or '').split())[:MAX_Q_LEN]
    content = a.get('content') in ('1', 'true', 'on')
    pm = ' '.join((a.get('pm') or '').split())[:200]
    editor_raw = (a.get('editor') or '').strip()
    venue = (a.get('venue') or '').strip()[:200]
    d_from, d_to = _parse_date(a.get('from')), _parse_date(a.get('to'))
    status = a.get('status') if a.get('status') in ('active', 'archived', 'all') else 'all'
    include_test = a.get('test') in ('1', 'true', 'on')
    sort = a.get('sort') if a.get('sort') in ('date', 'date_asc', 'name', 'edit') else ''
    try:
        limit = max(1, min(MAX_RESULTS, int(a.get('limit') or DEFAULT_RESULTS)))
    except ValueError:
        limit = DEFAULT_RESULTS
    raw_tags = []
    for v in a.getlist('tag'):
        raw_tags.extend(str(v).replace(',', ' ').split())
    tags, bad_tags = [], []
    for t in raw_tags[:MAX_TAGS * 2]:
        n = normalize_tag(t)
        if n and n not in tags:
            tags.append(n)
        elif not n:
            bad_tags.append(t[:60])
    tags = tags[:MAX_TAGS]
    editor = None
    if editor_raw == 'me':
        editor = session['user_id']
    elif editor_raw:
        try:
            editor = int(editor_raw)
        except ValueError:
            editor = 0
        if not 0 < editor < 2 ** 31:
            return jsonify({'success': False, 'error': 'Invalid editor.'}), 400

    if bad_tags and not tags:
        # Only non-tags were asked for: say so rather than listing everything.
        return jsonify({'success': True, 'results': [], 'total': 0, 'shown': 0,
                        'truncated': False, 'bad_tags': bad_tags})
    if not (q or pm or editor or tags or venue or d_from or d_to):
        return jsonify({'success': True, 'idle': True, 'results': [], 'total': 0,
                        'shown': 0, 'truncated': False})
    if d_from and d_to and d_from > d_to:
        d_from, d_to = d_to, d_from

    acc = _accessible_ids()
    where, params = ['TRUE'], []
    if acc is not None:
        where.append('s.id = ANY(%s)')
        params.append(acc)
    if status != 'all':
        where.append('s.status = %s')
        params.append(status)
    if not include_test:
        where.append('COALESCE(s.is_test, 0) = 0')
    if venue:
        where.append('lower(TRIM(s.venue)) = lower(%s)')
        params.append(venue)
    if d_to:
        where.append('LEAST(s.load_in_date, s.show_date, p.first_perf) <= %s')
        params.append(d_to)
    if d_from:
        where.append('GREATEST(s.load_out_date, s.show_date, p.last_perf) >= %s')
        params.append(d_from)

    name_key = _d['name_key']
    db = _d['get_db']()
    try:
        cands = {r['id']: r for r in _show_rows(db, ' AND '.join(where), params)}
        matched = {sid: [] for sid in cands}
        edits = {}

        if editor and cands:
            ur = db.execute("SELECT COALESCE(NULLIF(TRIM(display_name), ''), username) AS name, "
                            "COALESCE(is_locked, 0) = 0 AND COALESCE(pending_approval, 0) = 0 AS listed "
                            "FROM users WHERE id = %s", (editor,)).fetchone()
            if not ur or not (ur['listed'] or editor == session['user_id']):
                # Same rule as the Edited-by picker (_api_editors_view): a
                # locked, pending or unknown account's activity isn't searchable.
                cands = {}

        if editor and cands:
            erows = _d['get_db_rows'](db, """
                SELECT show_id, MAX(ts)::timestamptz AS last_edit, COUNT(*) AS n FROM (
                  SELECT show_id, "timestamp" AS ts FROM audit_log
                   WHERE user_id = %s AND show_id IS NOT NULL AND action <> 'FORM_SAVE'
                  UNION ALL
                  SELECT show_id, saved_at FROM form_history WHERE saved_by = %s
                ) x WHERE show_id = ANY(%s) GROUP BY show_id
            """, [editor, editor, sorted(cands)])
            ename = ur['name']
            edits = {r['show_id']: r for r in erows}
            for sid in list(cands):
                if sid not in edits:
                    cands.pop(sid)
                else:
                    e = edits[sid]
                    matched[sid].append({'type': 'editor', 'name': ename, 'n': int(e['n']),
                                         'at': _d['ts_out'](e['last_edit'])})

        if tags and cands:
            found = shows_with_tags(db, tags, sorted(cands))
            for sid in list(cands):
                if sid not in found:
                    cands.pop(sid)
                else:
                    matched[sid].extend({'type': 'tag', 'tag': t} for t in found[sid])

        pm_data = {}
        if (pm or q) and cands:
            pm_data = _d['pm_contacts_many'](db, sorted(cands))

        if pm and cands:
            if pm.lower() == 'me':
                want = {name_key(n) for n in _d['my_pm_names'](db, session['user_id'])}
            else:
                want = {name_key(pm)}
            want.discard('')
            for sid in list(cands):
                info = pm_data.get(sid) or {}
                chips = []
                sp = info.get('show_pm')
                if sp and name_key(sp['name']) in want:
                    chips.append({'type': 'pm', 'name': sp['name'], 'cover_days': []})
                cover = {}
                for iso, card in sorted((info.get('days') or {}).items()):
                    if card and card.get('is_cover') and name_key(card['name']) in want:
                        cover.setdefault(card['name'], []).append(iso)
                for nm, days in cover.items():
                    chips.append({'type': 'pm', 'name': nm, 'cover_days': [
                        _d_short(_d['as_date'](x)) for x in days]})
                if chips:
                    matched[sid].extend(chips)
                else:
                    cands.pop(sid)

        if q and cands:
            terms = q.lower().split()
            hits = content_hits(db, q, sorted(cands)) if content else {}
            for sid in list(cands):
                r = cands[sid]
                info = pm_data.get(sid) or {}
                pm_names = []
                if info.get('show_pm'):
                    pm_names.append(info['show_pm']['name'])
                for card in (info.get('days') or {}).values():
                    if card and card.get('is_cover') and card['name'] not in pm_names:
                        pm_names.append(card['name'])
                fields = [r['name'] or '', r['venue'] or '', r['company'] or ''] + pm_names
                hay = ' '.join(fields).lower()
                meta_ok = all(t in hay for t in terms)
                h = hits.get(sid)
                if not meta_ok and not h:
                    cands.pop(sid)
                    continue
                shown = ((r['name'] or '') + ' ' + (r['venue'] or '')).lower()
                if meta_ok and not all(t in shown for t in terms):
                    # Say WHY when it isn't the visible name/venue.
                    if r['company'] and any(t in r['company'].lower() for t in terms):
                        matched[sid].append({'type': 'meta', 'text': f"Arts Group: {r['company']}"})
                    for nm in pm_names:
                        if any(t in nm.lower() for t in terms) and not any(
                                m.get('type') == 'pm' and m.get('name') == nm for m in matched[sid]):
                            matched[sid].append({'type': 'meta', 'text': f'PM: {nm}'})
                if h:
                    ql = q.lower()
                    for row in h['rows']:
                        body = row['body'] or ''
                        bl, spans, i = body.lower(), [], 0
                        while len(spans) < 5:
                            j = bl.find(ql, i)
                            if j < 0:
                                break
                            spans.append((j, j + len(ql)))
                            i = j + len(ql)
                        matched[sid].append({
                            'type': 'text', 'label': _occ_label(row),
                            'link': occ_link(sid, row['kind'], row['ref']),
                            'snippet': _snippet(body, spans, radius=60, max_len=200)})
                    if h['n'] > len(h['rows']):
                        matched[sid].append({'type': 'more', 'n': h['n'] - len(h['rows'])})

        results = []
        for sid, r in cands.items():
            card = _show_card(r)
            info = pm_data.get(sid)
            card['_sp'] = info['show_pm']['name'] if info and info.get('show_pm') else None
            card['matched'] = matched[sid]
            e = edits.get(sid)
            card['last_edit'] = _d['ts_out'](e['last_edit']) if e else None
            results.append(card)
        if not sort:
            sort = 'edit' if editor else 'date'
        if sort == 'name':
            results.sort(key=lambda c: (c['name'] or '').lower())
        elif sort == 'edit' and editor:
            results.sort(key=lambda c: c['last_edit'] or '', reverse=True)
        elif sort == 'date_asc':
            results.sort(key=lambda c: (c['start'] is None, c['start'] or '', c['id']))
        else:
            results.sort(key=lambda c: (c['start'] is not None, c['start'] or '', c['id']),
                         reverse=True)
        total = len(results)
        results = results[:limit]
        # The PM for the cards (when the filters didn't already load it).
        need = [c['id'] for c in results if c['_sp'] is None and c['id'] not in pm_data]
        if need:
            extra = _d['pm_contacts_many'](db, need)
            for c in results:
                if c['id'] in extra and extra[c['id']].get('show_pm'):
                    c['_sp'] = extra[c['id']]['show_pm']['name']
    finally:
        db.close()
    for c in results:
        c['pm'] = c.pop('_sp') or ''
    return jsonify({'success': True, 'results': results, 'total': total,
                    'shown': len(results), 'truncated': total > len(results),
                    'sort': sort, 'tags': tags, 'bad_tags': bad_tags})


# ─── Registration ─────────────────────────────────────────────────────────────

def register(app, **deps):
    """
    Wire the module into the Flask app. `deps` must provide:
      get_db, get_current_user, login_required, can_access_show,
      get_accessible_shows, my_pm_names, pm_contacts_many, name_key, as_date,
      ts_out, get_distinct_venues, module_enabled (app.py's switch for the
      optional modules — piano tuning notes are a source only while it's on).
    Called once from app.py; everything else in this file is self-contained.
    """
    _d.update(deps, app=app)
    _d['get_db_rows'] = lambda db, sql, params: db.execute(sql, params).fetchall()
    login = deps['login_required']
    app.add_template_filter(tagify, 'tagify')

    app.add_url_rule('/tags', 'tags_index', login(_tags_index_view))
    app.add_url_rule('/tags/<tag>', 'tag_page', login(_tag_view))
    app.add_url_rule('/search', 'search_page', login(_search_page_view))
    app.add_url_rule('/api/tags', 'api_tags', login(_api_tags_view))
    app.add_url_rule('/api/shows/<int:show_id>/tags', 'api_show_tags',
                     login(_api_show_tags_view))
    app.add_url_rule('/api/search', 'api_search', login(_api_search_view))
    app.add_url_rule('/api/search/editors', 'api_search_editors',
                     login(_api_editors_view))
