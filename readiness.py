"""
3·2·1→Theater — show readiness score (3.54.0, admin-only beta).

"This show is NN% ready for showtime!" — one number per show built from five
components (advance, schedule, labor requests, crew / scheduler, gear) minus
penalties for specific gaps (a day the scheduler half-did, an off-date
schedule row, …).

This module is PURE: no database, no Flask, never imports app.py, never
raises on odd facts. app.py gathers the facts for many shows in a fixed
number of batched queries (`_readiness_facts_many`) and calls `score()` per
show; the Settings panel's Preview calls `score()` again with unsaved knobs.
Keeping the math here makes it testable with hand-made fact dicts:
`python3 readiness.py` runs the self-test.

Facts contract (one dict per show; dates are ISO 'YYYY-MM-DD' strings):
  span          sorted list of engagement dates (app.py: load-in → load-out
                ∪ performance dates, falling back to show_date)
  perf_dates    dated performance dates
  undated_perfs number of performances without a date
  load_in, load_out   ISO or None
  dark          dates flagged dark on the Schedule tab (raw flags)
  adv           {field_key: (credit 0..1, decoded answer text)}; credit is
                1 answered / 0.5 pdf_form draft; missing key = unanswered
  adv_updated   last advance edit (ISO with offset) or None
  sched_days    {iso date: content rows} grouped exactly as the editor groups
                them (piano rows and blank rows already left out)
  labor         [{date: iso|None, sched: bool, crew: bool, archived: bool,
                  named: bool, at: iso|None}] — training shifts left out
  gear          {lines: int, approved: bool, snapshot: bool, approved_at}
and, shared by every show, `fields`: [{key, type, notes: bool}] in form order.
"""
import math
from datetime import date, timedelta

# ── Knobs ────────────────────────────────────────────────────────────────
# Stored in app_settings as readiness_<name>; a missing key = the default.
# (min, max) ranges are enforced by clean_knobs(); numbers outside clamp.
PENALTY_CODES = (
    # code, default points, label for the settings panel
    ('NO_LABOR_SOON', 5, 'No labor requested inside the labor window'),
    ('LABOR_EXPECTED_NONE', 8, "Advance says labor 'Yes' but none requested"),
    ('PARTIAL_DAY', 5, 'Day partly scheduled (per day)'),
    ('UNTOUCHED_DAY', 5, 'Day left unscheduled while others are done (per day)'),
    ('DARK_PERF_DAY', 2, 'Performance day flagged dark'),
    ('DARK_LOAD_DAY', 2, 'Load-in / load-out day flagged dark'),
    ('UNDATED_PERF', 2, 'Performance with no date'),
    ('OFF_DATE_ROWS', 3, 'Schedule rows outside the show dates'),
    ('LABOR_ON_DARK', 3, 'Labor on a dark day'),
    ('UNDATED_LINES', 3, 'Labor lines with no date'),
    ('ARCHIVED_TECH', 2, 'Shift assigned to an archived technician'),
    ('GEAR_CHANGED', 3, 'Gear changed since approval'),
    ('GEAR_UNAPPROVED', 3, 'Gear never approved (within 6 weeks)'),
)

WEIGHT_KEYS = ('advance', 'schedule', 'labor', 'crew', 'gear')

# The component each penalty belongs to: a component turned off (weight 0)
# takes its penalties with it (the issue still shows, worth 0 points).
# Codes not listed (UNDATED_PERF) are about the show's dates and always count.
PENALTY_OWNER = {
    'NO_LABOR_SOON': 'labor', 'LABOR_EXPECTED_NONE': 'labor',
    'LABOR_ON_DARK': 'labor', 'UNDATED_LINES': 'labor',
    'PARTIAL_DAY': 'crew', 'UNTOUCHED_DAY': 'crew', 'ARCHIVED_TECH': 'crew',
    'DARK_PERF_DAY': 'schedule', 'DARK_LOAD_DAY': 'schedule',
    'OFF_DATE_ROWS': 'schedule',
    'GEAR_CHANGED': 'gear', 'GEAR_UNAPPROVED': 'gear',
}
# Scheduler-gap penalties: never allowed to drop a show below the score the
# same show gets with nothing scheduled (starting the work must not cost %).
SCHEDULER_GAP_CODES = frozenset(('PARTIAL_DAY', 'UNTOUCHED_DAY'))

DEFAULTS = {
    'w_advance': 35, 'w_schedule': 25, 'w_labor': 15, 'w_crew': 15, 'w_gear': 10,
    'advance_target': 0.30,
    'adv_required_keys': 'production_manager',
    'adv_key_penalty': 0.25,
    'sched_min_rows': 1,
    'labor_cue_keys': 'labor_load_in,labor_show_call,labor_load_out',
    'labor_day_floor': 0.5,
    'labor_grace_days': 14,
    'penalty_cap': 20,
    'window_days': 60,
}
for _code, _pts, _lbl in PENALTY_CODES:
    DEFAULTS['penalty_' + _code.lower()] = _pts

# name → (kind, min, max)
RANGES = {
    'w_advance': ('int', 0, 100), 'w_schedule': ('int', 0, 100),
    'w_labor': ('int', 0, 100), 'w_crew': ('int', 0, 100), 'w_gear': ('int', 0, 100),
    'advance_target': ('float', 0.05, 1.0),
    'adv_required_keys': ('keys', 0, 10),
    'adv_key_penalty': ('float', 0.0, 1.0),
    'sched_min_rows': ('int', 1, 10),
    'labor_cue_keys': ('keys', 0, 10),
    'labor_day_floor': ('float', 0.0, 1.0),
    'labor_grace_days': ('int', 0, 120),
    'penalty_cap': ('int', 0, 50),
    'window_days': ('int', 1, 365),
}
for _code, _pts, _lbl in PENALTY_CODES:
    RANGES['penalty_' + _code.lower()] = ('int', 0, 25)

# Field types / keys that never count toward the advance percentage.
ADV_SKIP_TYPES = frozenset(('notes', 'piano_tuning', 'checkbox'))
# Free-text types: a '-' / 'N/A' there is not an answer (a pick list or a
# contact may still say N/A on purpose).
ADV_FREE_TEXT_TYPES = frozenset(('text', 'textarea', 'number', 'date', 'time'))
# Ticked checkboxes add to the answered count, but at most this share of the
# counted fields (so ticks alone can't reach the target).
ADV_CHECKBOX_MAX_SHARE = 0.10
ADV_SKIP_KEYS = frozenset(('load_in_date', 'load_in_time', 'load_out_date',
                           'load_out_time', 'show_name', 'show_date',
                           'show_time', 'venue'))

BANDS = ((90, 'Ready for showtime'), (67, 'Almost there'),
         (34, 'In progress'), (0, 'Getting started'))
BAND_NO_WEIGHTS = 'Nothing to score'

# Days out → % a show "should" be at by then. Display only (pace chip).
PACE_CURVE = ((90, 20), (60, 40), (30, 65), (14, 85), (7, 95), (0, 100))

GEAR_WINDOW_DAYS = 42
GEAR_CHANGED_CREDIT = 0.5
GEAR_UNAPPROVED_CREDIT = 0.3
TECH_UNCONFIRMED_CREDIT = 0.5

_DOW = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


class KnobError(ValueError):
    """A knob value that can't be used (message is user-facing)."""


def _key_list(v, limit):
    if isinstance(v, (list, tuple)):
        parts = [str(x) for x in v]
    else:
        parts = str(v or '').replace('\n', ',').split(',')
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(p) > 80 or not all(c.isalnum() or c in '_-' for c in p):
            raise KnobError(f'"{p[:40]}" is not a field key.')
        if p not in out:
            out.append(p)
    if len(out) > limit:
        raise KnobError(f'At most {limit} field keys.')
    return ','.join(out)


def clean_knobs(raw, strict=False):
    """Knob dict → validated, typed dict with every key present.

    strict=False (reading stored settings): a bad value falls back to the
    default, an out-of-range number clamps. strict=True (a Save / Preview
    from the panel): raise KnobError naming the first bad value, so the
    admin sees what was wrong instead of a silent change."""
    raw = raw or {}
    out = {}
    for name, default in DEFAULTS.items():
        kind, lo, hi = RANGES[name]
        v = raw.get(name, default)
        if v is None or (isinstance(v, str) and not v.strip() and kind != 'keys'):
            v = default
        try:
            if kind == 'keys':
                out[name] = _key_list(v, hi)
                continue
            num = float(v)
            if math.isnan(num) or math.isinf(num):
                raise ValueError
            if kind == 'int':
                if strict and num != int(num):
                    raise KnobError(f'{name}: use a whole number.')
                num = int(round(num))
            if num < lo or num > hi:
                if strict:
                    raise KnobError(f'{name}: must be between {lo} and {hi}.')
                num = min(hi, max(lo, num))
            out[name] = num
        except KnobError:
            if strict:
                raise
            out[name] = default
        except (TypeError, ValueError):
            if strict:
                raise KnobError(f'{name}: not a number.')
            out[name] = default
    if sum(out['w_' + k] for k in WEIGHT_KEYS) <= 0:
        if strict:
            raise KnobError('At least one component weight must be above 0.')
        for k in WEIGHT_KEYS:
            out['w_' + k] = DEFAULTS['w_' + k]
    return out


# ── Helpers ──────────────────────────────────────────────────────────────

def _d(iso):
    try:
        return date.fromisoformat(str(iso)[:10])
    except (TypeError, ValueError):
        return None


def _label(iso):
    d = _d(iso)
    return f'{_DOW[d.weekday()]} {d.month}/{d.day}' if d else str(iso)


def _plural(n, word, plural=None):
    return f'{n} {word if n == 1 else (plural or word + "s")}'


def band_for(score):
    if score is None:
        return 'No dates yet'
    for lo, label in BANDS:
        if score >= lo:
            return label
    return BANDS[-1][1]


def pace_target(days_out):
    """Interpolated pace-curve target for `days_out` (display only)."""
    if days_out is None:
        return None
    pts = PACE_CURVE
    if days_out >= pts[0][0]:
        return pts[0][1]
    for (d1, t1), (d2, t2) in zip(pts, pts[1:]):
        if d2 <= days_out <= d1:
            if d1 == d2:
                return t2
            return round(t2 + (t1 - t2) * (days_out - d2) / (d1 - d2))
    return pts[-1][1]


# ── The scorer ───────────────────────────────────────────────────────────

def score(facts, knobs, today, fields):
    """Readiness of one show. Never raises: an unexpected error returns a
    result with score None and the error type in `error`."""
    try:
        facts, k, fields = facts or {}, clean_knobs(knobs), fields or []
        res = _score(facts, k, today, fields)
        if any(i['code'] in SCHEDULER_GAP_CODES and i['points'] for i in res['issues']):
            # The same show with no scheduler work done (nothing scheduled,
            # no tech picked): the gap penalties may not take it below that.
            bare = dict(facts, labor=[{'date': ln.get('date')} for ln in (facts.get('labor') or [])])
            floor = _score(bare, k, today, fields)['score']
            _cap_scheduler_gaps(res, k, floor, today)
        return res
    except Exception as e:  # pragma: no cover - defensive
        return {'id': (facts or {}).get('id'), 'score': None, 'band': band_for(None),
                'error': type(e).__name__, 'components': {}, 'issues': [],
                'days': [], 'headline': 'Readiness could not be worked out.'}


def _pace(final, first, last, today, days_out):
    if final is None or not first:
        return None
    if first <= today <= (last or first):
        return {'label': 'In progress', 'target': None}
    if today < first:
        t = pace_target(days_out)
        lbl = 'On track' if final >= t else ('Behind' if final >= t - 15 else 'At risk')
        return {'label': lbl, 'target': t}
    return None


_SEV_RANK = {'error': 0, 'warn': 1, 'info': 2}


def _cap_scheduler_gaps(res, k, floor, today):
    """Trim the scheduler-gap issues' points so the final score is not
    below `floor` (the nothing-scheduled score) — never below 0, never more
    than each issue's own points. Updates score / band / headline / penalty."""
    if res.get('score') is None or floor is None or res['score'] >= floor:
        return
    gaps = [i for i in res['issues'] if i['code'] in SCHEDULER_GAP_CODES and i['points']]
    other = sum(i['points'] for i in res['issues'] if i not in gaps)
    base100 = int(math.floor(res['base'] + 1e-9))
    room = base100 - floor          # most penalty points the show may lose
    allowed = max(0, room - other) if k['penalty_cap'] > room else sum(i['points'] for i in gaps)
    for i in gaps:                   # later days give up their points first
        keep = min(i['points'], allowed)
        if keep != i['points']:
            i['points_full'] = i['points']
            i['points'] = keep
        allowed -= keep
    raw = sum(i['points'] for i in res['issues'])
    res['penalty_raw'] = raw
    res['penalty'] = min(k['penalty_cap'], raw)
    res['score'] = max(0, min(100, base100 - res['penalty']))
    res['band'] = band_for(res['score'])
    res['headline'] = f'This show is {res["score"]}% ready for showtime!'
    res['scheduler_floor'] = floor
    first, last = _d(res.get('first_day')), _d(res.get('last_day'))
    res['pace'] = _pace(res['score'], first, last, today, res.get('days_out'))
    res['issues'].sort(key=lambda i: (_SEV_RANK.get(i['sev'], 3), -i['points']))


def _score(f, k, today, fields):
    issues = []
    pts = {code: k['penalty_' + code.lower()] for code, _p, _l in PENALTY_CODES}
    for code, owner in PENALTY_OWNER.items():
        if k['w_' + owner] <= 0:
            pts[code] = 0

    def issue(sev, code, text, tab, points=None):
        issues.append({'sev': sev, 'code': code, 'text': text, 'tab': tab,
                       'points': pts.get(code, 0) if points is None else points})

    # ── 2.1 Engagement days ────────────────────────────────────────────
    span = sorted({s for s in (f.get('span') or []) if _d(s)})
    perf = {p for p in (f.get('perf_dates') or []) if _d(p)}
    dark_flags = {x for x in (f.get('dark') or []) if _d(x)}
    # Load-in / load-out are work days by definition: like a performance
    # day, a dark flag on one is a contradiction, never an exclusion.
    loads = {x for x in (f.get('load_in'), f.get('load_out')) if x and _d(x)}
    dark = (dark_flags & set(span)) - perf - loads
    for x in sorted(dark_flags & perf & set(span)):
        issue('warn', 'DARK_PERF_DAY',
              f'{_label(x)} is marked dark but has a performance — still counted.',
              'schedule')
    for x in sorted((dark_flags & loads & set(span)) - perf):
        issue('warn', 'DARK_LOAD_DAY',
              f'{_label(x)} is marked dark but is the '
              f'{"load-in" if x == f.get("load_in") else "load-out"} — still counted.',
              'schedule')
    E = [x for x in span if x not in dark]
    if not E and span:
        E, dark = list(span), set()
    Eset = set(E)
    if f.get('undated_perfs'):
        n = int(f['undated_perfs'])
        issue('warn', 'UNDATED_PERF', f'{_plural(n, "performance")} with no date.', 'advance')
    load_in = f.get('load_in') if f.get('load_in') in Eset else None
    load_out = f.get('load_out') if f.get('load_out') in Eset else None
    perf_days = perf & Eset
    first = _d(E[0]) if E else None
    last = _d(E[-1]) if E else None
    days_out = (first - today).days if first else None

    def role(x):
        r = []
        if x == load_in:
            r.append('load-in')
        if x in perf_days:
            r.append('performance')
        if x == load_out:
            r.append('load-out')
        return r or (['dark'] if x in dark else ['work day'])

    comps = {}

    def comp(key, applies, value, reason='', detail=None, lines=None):
        w = k['w_' + key]
        if w <= 0:
            applies, reason = False, 'Turned off (weight 0)'
        comps[key] = {'applies': bool(applies), 'score': (round(value, 4) if applies and value is not None else None),
                      'pct': (int(math.floor(100 * value + 1e-9)) if applies and value is not None else None),
                      'weight': w, 'reason': reason, 'detail': detail or {}, 'lines': lines or []}

    # ── 2.2 Advance ─────────────────────────────────────────────────────
    adv = f.get('adv') or {}
    known = {fd['key'] for fd in fields}
    counted = [fd for fd in fields
               if fd.get('type') not in ADV_SKIP_TYPES and not fd.get('notes')
               and fd['key'] not in ADV_SKIP_KEYS]
    ans = 0.0
    for fd in counted:
        c = adv.get(fd['key'])
        if not c:
            continue
        if fd.get('type', 'text') in ADV_FREE_TEXT_TYPES and \
                str(c[1]).strip().upper() in ('N/A', '-', 'NA'):
            continue                  # '-' typed into a text box is not an answer
        ans += float(c[0])
    cb_ticked = sum(1 for fd in fields if fd.get('type') == 'checkbox'
                    and adv.get(fd['key']) and adv[fd['key']][0] > 0)
    cb = min(cb_ticked, ADV_CHECKBOX_MAX_SHARE * len(counted))
    req_keys = [x for x in k['adv_required_keys'].split(',') if x]
    missing_req, unknown_req = [], [x for x in req_keys if x not in known]
    pm = ''
    pmv = adv.get('production_manager')
    if pmv and pmv[0] > 0 and pmv[1] not in ('N/A', '-'):
        pm = pmv[1]
    if counted:
        fill = min(1.0, (ans + cb) / max(1, len(counted)))
        A = min(1.0, fill / k['advance_target'])
        for x in req_keys:
            if x in unknown_req:
                continue
            v = adv.get(x)
            if not v or v[0] <= 0 or v[1] in ('N/A', '-'):
                missing_req.append(x)
                A *= (1 - k['adv_key_penalty'])
        label = {fd['key']: fd.get('label') or fd['key'] for fd in fields}
        for x in missing_req:
            issue('warn', 'ADV_KEY_MISSING', f'Advance: "{label.get(x, x)}" is blank.',
                  'advance', points=0)
        answered_n = ans + cb
        lines = [f'{answered_n:g} of {len(counted)} questions answered '
                 f'({int(math.floor(100 * fill + 1e-9))}% — {int(round(k["advance_target"] * 100))}% counts as full)',
                 f'Production manager: {pm or "not set"}']
        comp('advance', True, A, detail={
            'answered': answered_n, 'counted': len(counted), 'fill': round(fill, 4),
            'target': k['advance_target'], 'pm': pm, 'missing_required': missing_req,
            'last_edit': f.get('adv_updated')}, lines=lines)
    else:
        comp('advance', False, None, 'No advance fields configured')

    # ── 2.3 Schedule ────────────────────────────────────────────────────
    sd = f.get('sched_days') or {}
    day_rows = []
    for x in E:
        n = int(sd.get(x) or 0)
        day_rows.append((x, n, min(1.0, n / k['sched_min_rows'])))
    off = sorted(x for x, n in sd.items() if n and x not in set(span) and x not in dark)
    if off:
        nrows = sum(int(sd[x]) for x in off)
        issue('warn', 'OFF_DATE_ROWS',
              f'{_plural(nrows, "schedule row")} on dates outside the show '
              f'({", ".join(_label(x) for x in off[:4])}{"…" if len(off) > 4 else ""}).',
              'schedule')
    if E:
        S = sum(c for _x, _n, c in day_rows) / len(E)
        filled = sum(1 for _x, n, _c in day_rows if n > 0)
        lines = [f'{filled} of {_plural(len(E), "day")} have a schedule']
        if dark:
            lines.append(f'{_plural(len(dark), "dark day")} not expected')
        comp('schedule', True, S, detail={'days': len(E), 'filled': filled,
                                         'min_rows': k['sched_min_rows']}, lines=lines)
    else:
        comp('schedule', False, None, 'No show dates')

    # ── 2.4 Labor requests ──────────────────────────────────────────────
    lab = f.get('labor') or []
    cue_keys = [x for x in k['labor_cue_keys'].split(',') if x]
    # The cue list is positional: 1st = load-in, 2nd = show call, 3rd =
    # load-out (any further key only decides whether labor applies).
    cue_role = dict(zip(('load_in', 'show_call', 'load_out'), cue_keys))
    cues = {x: (adv.get(x) or (0, ''))[1] for x in cue_keys}
    cue_yes = [x for x in cue_keys if cues.get(x) == 'Yes']
    all_no = bool(cue_keys) and all(x in known for x in cue_keys) and \
        all(cues.get(x) == 'No' for x in cue_keys)
    unknown_cues = [x for x in cue_keys if x not in known]
    dated = [ln for ln in lab if _d(ln.get('date'))]
    undated = len(lab) - len(dated)
    ldays = {ln['date'][:10] for ln in dated}
    req_by_day, sched_by_day = {}, {}
    for ln in dated:
        x = ln['date'][:10]
        req_by_day[x] = req_by_day.get(x, 0) + 1
        if ln.get('sched'):
            sched_by_day[x] = sched_by_day.get(x, 0) + 1
    grace = k['labor_grace_days']
    if lab:
        X = set(E)
        def said_no(role):
            return role in cue_role and cues.get(cue_role[role]) == 'No'
        if said_no('load_in') and load_in:
            X.discard(load_in)
        if said_no('load_out') and load_out:
            X.discard(load_out)
        if said_no('show_call'):
            X -= {x for x in perf_days if x not in (load_in, load_out)}
        cov = (len(ldays & X) / len(X)) if X else 1.0
        L = k['labor_day_floor'] + (1 - k['labor_day_floor']) * cov
        lines = [f'{len(lab)} shifts requested on {_plural(len(ldays & X), "day")} '
                 f'of {len(X)} expected']
        missing = sorted(X - ldays)
        if missing and len(missing) <= 6:
            lines.append('No labor yet: ' + ', '.join(_label(x) for x in missing))
        elif missing:
            lines.append(f'No labor yet on {len(missing)} days')
        comp('labor', True, L, detail={'lines': len(lab), 'expected_days': len(X),
                                      'covered_days': len(ldays & X), 'coverage': round(cov, 4),
                                      'missing_days': missing}, lines=lines)
        off_l = sorted(x for x in ldays if x not in set(span))
        if off_l:
            issue('info', 'LABOR_OFF_SPAN',
                  f'Labor on {", ".join(_label(x) for x in off_l[:4])} — outside the show dates.',
                  'staffing', points=0)
        if all_no:
            issue('info', 'LINES_BUT_ADVANCE_NO',
                  "Labor is requested but the advance answers 'No' to every labor question.",
                  'advance', points=0)
        on_dark = sorted(ldays & dark)
        if on_dark:
            issue('warn', 'LABOR_ON_DARK',
                  f'Labor on dark day{"s" if len(on_dark) > 1 else ""} '
                  f'{", ".join(_label(x) for x in on_dark[:4])}.', 'staffing')
        if undated:
            issue('warn', 'UNDATED_LINES', f'{_plural(undated, "labor line")} with no date.',
                  'staffing')
    elif cue_yes:
        comp('labor', True, 0.0, lines=['No shifts requested yet'])
        names = ', '.join(x.replace('labor_', '').replace('_', ' ') for x in cue_yes)
        issue('error', 'LABOR_EXPECTED_NONE',
              f"Advance says labor is needed ({names}) but no labor is requested.", 'staffing')
    elif all_no:
        comp('labor', False, None, 'Advance says no labor')
    elif days_out is not None and days_out <= grace:
        comp('labor', True, 0.0, lines=['No shifts requested yet'])
        issue('error', 'NO_LABOR_SOON',
              f'No labor requested and the show is {_plural(max(days_out, 0), "day")} out.',
              'staffing')
    else:
        comp('labor', False, None,
             f'No labor requested yet (counts from {grace} days out)'
             if days_out is not None else 'No labor requested yet')
        comps['labor']['amber'] = days_out is not None and comps['labor']['weight'] > 0

    # ── 2.5 Crew / scheduler ────────────────────────────────────────────
    sched_n = sum(1 for ln in lab if ln.get('sched'))
    picked = sum(1 for ln in lab if not ln.get('sched') and ln.get('crew'))
    stamps = [ln['at'] for ln in lab if ln.get('at')]
    last_at = max(stamps) if stamps else None
    responded = sched_n > 0 or bool(stamps)
    if not lab:
        status = 'No shifts to schedule'
    elif sched_n == len(lab):
        status = f'All {len(lab)} shifts scheduled ✓'
    elif responded:
        status = f'Responded · {sched_n} of {len(lab)} shifts scheduled'
    else:
        status = 'Awaiting scheduler'
    scheduler = {'status': status, 'responded': responded, 'scheduled': sched_n,
                 'lines': len(lab), 'tech_unconfirmed': picked, 'last_update': last_at}
    if lab:
        C = (sched_n + TECH_UNCONFIRMED_CREDIT * picked) / len(lab)
        lines = [status]
        if picked:
            lines.append(f'{_plural(picked, "shift")} with a tech picked but not marked scheduled (half credit)')
        no_name = sum(1 for ln in lab if ln.get('sched') and not ln.get('crew') and not ln.get('named'))
        if no_name:
            issue('info', 'SCHED_NO_TECH',
                  f'{_plural(no_name, "shift")} marked scheduled with no technician named.',
                  'staffing', points=0)
        if picked:
            issue('info', 'TECH_UNCONFIRMED',
                  f'{_plural(picked, "shift")} with a tech picked but not marked scheduled.',
                  'staffing', points=0)
        for x in sorted(req_by_day):
            r, s = req_by_day[x], sched_by_day.get(x, 0)
            if 0 < s < r:
                issue('error', 'PARTIAL_DAY',
                      f'{_label(x)}: {r - s} of {r} shifts left unscheduled.', 'staffing')
            elif s == 0 and sched_n > 0:
                issue('error', 'UNTOUCHED_DAY',
                      f'{_label(x)}: none of {_plural(r, "shift")} scheduled yet '
                      f'(other days are).', 'staffing')
        arch = sum(1 for ln in lab if ln.get('archived'))
        if arch:
            issue('warn', 'ARCHIVED_TECH',
                  f'{_plural(arch, "shift")} assigned to an archived technician.', 'staffing')
        comp('crew', True, C, detail=dict(scheduler), lines=lines)
    else:
        comp('crew', False, None, 'No shifts to schedule')

    # ── 2.6 Gear ────────────────────────────────────────────────────────
    g = f.get('gear') or {}
    gl = int(g.get('lines') or 0)
    if gl <= 0:
        comp('gear', False, None, 'No gear booked')
    elif g.get('approved'):
        comp('gear', True, 1.0, detail=dict(g), lines=[f'{_plural(gl, "line")} · approved'])
    elif g.get('snapshot'):
        comp('gear', True, GEAR_CHANGED_CREDIT, detail=dict(g),
             lines=[f'{_plural(gl, "line")} · changed since approval'])
        issue('warn', 'GEAR_CHANGED', 'Gear changed since it was approved — needs re-approval.',
              'assets')
    else:
        comp('gear', True, GEAR_UNAPPROVED_CREDIT, detail=dict(g),
             lines=[f'{_plural(gl, "line")} · not approved yet'])
        if days_out is not None and days_out <= GEAR_WINDOW_DAYS:
            issue('warn', 'GEAR_UNAPPROVED', 'Gear has not been approved yet.', 'assets')

    # ── 2.7 Total + penalties ───────────────────────────────────────────
    if not E:
        issue('error', 'NO_DATES', 'The show has no dates yet.', 'advance', points=0)
    num = sum(c['weight'] * c['score'] for c in comps.values() if c['applies'])
    den = sum(c['weight'] for c in comps.values() if c['applies'])
    penalty = min(k['penalty_cap'], sum(i['points'] for i in issues))
    if not E or den <= 0:
        final, base = None, (num / den if den else None)
        if E:
            issue('info', 'NO_WEIGHTS',
                  'Nothing to score: every part that applies to this show has weight 0.',
                  'advance', points=0)
    else:
        base = num / den
        final = max(0, min(100, int(math.floor(100 * base + 1e-9)) - penalty))
    for c in comps.values():
        c['share'] = round(100 * c['weight'] / den, 1) if c['applies'] and den else 0

    # ── 2.8 Pace chip (display only) ────────────────────────────────────
    pace = _pace(final, first, last, today, days_out)

    days = []
    for x, n, c in day_rows:
        days.append({'date': x, 'label': _label(x), 'roles': role(x), 'dark': False,
                     'sched_rows': n, 'sched_credit': round(c, 4),
                     'labor_req': req_by_day.get(x, 0), 'labor_sched': sched_by_day.get(x, 0)})
    for x in sorted(dark):
        days.append({'date': x, 'label': _label(x), 'roles': ['dark'], 'dark': True,
                     'sched_rows': int(sd.get(x) or 0), 'sched_credit': None,
                     'labor_req': req_by_day.get(x, 0), 'labor_sched': sched_by_day.get(x, 0)})
    days.sort(key=lambda d: d['date'])

    issues.sort(key=lambda i: (_SEV_RANK.get(i['sev'], 3), -i['points']))
    cfg = []
    if unknown_req:
        cfg.append('Required advance key(s) not on the form: ' + ', '.join(unknown_req))
    if unknown_cues:
        cfg.append('Labor question key(s) not on the form: ' + ', '.join(unknown_cues))
    return {
        'id': f.get('id'), 'score': final,
        'band': band_for(final) if final is not None or not E else BAND_NO_WEIGHTS,
        'headline': (f'This show is {final}% ready for showtime!' if final is not None
                     else 'Add show dates to get a readiness score.' if not E
                     else 'Nothing to score with the current weights.'),
        'base': round(100 * base, 2) if base is not None else None,
        'penalty': penalty, 'penalty_raw': sum(i['points'] for i in issues),
        'first_day': E[0] if E else None, 'last_day': E[-1] if E else None,
        'days_out': days_out, 'pace': pace, 'components': comps,
        'issues': issues, 'days': days, 'scheduler': scheduler,
        'config_warnings': cfg,
    }


# ── Self-test: python3 readiness.py ──────────────────────────────────────

def _selftest():
    T = date(2026, 12, 1)
    fields = [{'key': 'production_manager', 'type': 'contact_dropdown'},
              {'key': 'labor_load_in', 'type': 'yes_no'},
              {'key': 'labor_show_call', 'type': 'yes_no'},
              {'key': 'labor_load_out', 'type': 'yes_no'},
              {'key': 'show_name', 'type': 'text'},
              {'key': 'notes1', 'type': 'notes'},
              {'key': 'cb', 'type': 'checkbox'}] + \
             [{'key': f'q{i}', 'type': 'text'} for i in range(16)]
    # counted = pm + 3 cues + 16 q = 20
    run = ['2026-12-10', '2026-12-11', '2026-12-12', '2026-12-13']

    def base(**kw):
        f = {'id': 1, 'span': run, 'perf_dates': ['2026-12-11', '2026-12-12'],
             'load_in': run[0], 'load_out': run[-1], 'dark': [], 'adv': {},
             'sched_days': {}, 'labor': [], 'gear': {'lines': 0}}
        f.update(kw)
        return f
    K = dict(DEFAULTS)
    full_adv = {'production_manager': (1, 'Pat'), 'labor_load_in': (1, 'No'),
                'labor_show_call': (1, 'No'), 'labor_load_out': (1, 'No')}
    full_adv.update({f'q{i}': (1, 'x') for i in range(16)})

    # Worked check: advance full, schedule 3/4, all cues No, no gear → 89.
    r = score(base(adv=full_adv, sched_days={run[0]: 2, run[1]: 1, run[2]: 4}), K, T, fields)
    assert r['score'] == 89, r['score']
    assert r['band'] == 'Almost there'
    assert not r['components']['labor']['applies'] and r['components']['labor']['reason'] == 'Advance says no labor'
    assert not r['components']['gear']['applies']

    # Advance 30% filled → 100% advance component.
    adv30 = {'production_manager': (1, 'Pat')}
    adv30.update({f'q{i}': (1, 'x') for i in range(5)})          # 6 / 20 = 30%
    r = score(base(adv=adv30), K, T, fields)
    assert r['components']['advance']['pct'] == 100, r['components']['advance']
    adv15 = {'production_manager': (1, 'Pat'), 'q0': (1, 'x'), 'q1': (1, 'x')}   # 15%
    r = score(base(adv=adv15), K, T, fields)
    assert r['components']['advance']['pct'] == 50, r['components']['advance']
    # PM blank → × 0.75 and an ADV_KEY_MISSING issue.
    adv_nopm = {f'q{i}': (1, 'x') for i in range(6)}
    r = score(base(adv=adv_nopm), K, T, fields)
    assert r['components']['advance']['pct'] == 75, r['components']['advance']
    assert any(i['code'] == 'ADV_KEY_MISSING' for i in r['issues'])

    # No labor + far out → N/A (amber); within grace → 0 + NO_LABOR_SOON.
    far = base(span=['2027-03-01'], perf_dates=['2027-03-01'], load_in=None, load_out=None)
    r = score(far, K, T, fields)
    assert not r['components']['labor']['applies'] and r['components']['labor'].get('amber')
    assert not any(i['code'] == 'NO_LABOR_SOON' for i in r['issues'])
    r = score(base(), K, T, fields)        # 9 days out
    assert r['components']['labor']['applies'] and r['components']['labor']['pct'] == 0
    assert any(i['code'] == 'NO_LABOR_SOON' for i in r['issues'])
    # Advance cue 'Yes' with no lines → LABOR_EXPECTED_NONE even when far.
    r = score(dict(far, adv={'labor_load_in': (1, 'Yes')}), K, T, fields)
    assert any(i['code'] == 'LABOR_EXPECTED_NONE' for i in r['issues'])

    # Labor on 2 of 4 days, everything scheduled: L = 0.5 + 0.5·0.5 = 0.75.
    lab = [{'date': run[0], 'sched': True, 'crew': True}, {'date': run[1], 'sched': True, 'crew': True}]
    r = score(base(labor=lab), K, T, fields)
    assert r['components']['labor']['pct'] == 75, r['components']['labor']
    assert r['components']['crew']['pct'] == 100
    assert r['scheduler']['status'].startswith('All 2')

    # Partly scheduled day → PARTIAL_DAY; untouched day while others done.
    lab = [{'date': run[0], 'sched': True}, {'date': run[0], 'sched': False},
           {'date': run[1], 'sched': False}, {'date': run[1], 'sched': False, 'crew': True}]
    r = score(base(labor=lab), K, T, fields)
    codes = [i['code'] for i in r['issues']]
    assert codes.count('PARTIAL_DAY') == 1 and codes.count('UNTOUCHED_DAY') == 1, codes
    assert r['components']['crew']['score'] == (1 + 0.5) / 4
    before = r['score']
    r2 = score(base(labor=lab), dict(K, penalty_partial_day=0, penalty_untouched_day=0), T, fields)
    assert r2['score'] > before, (before, r2['score'])
    # …but never below the same show with nothing scheduled.
    bare = score(base(labor=[{'date': ln['date']} for ln in lab]), K, T, fields)
    assert r['score'] >= bare['score'], (r['score'], bare['score'])

    # Scheduling in day order never scores below doing nothing (4 days ×
    # 2 lines, full advance + schedule).
    sched4 = {x: 2 for x in run}
    adv_yes = dict(full_adv, labor_load_in=(1, 'Yes'), labor_show_call=(1, 'Yes'),
                   labor_load_out=(1, 'Yes'))
    for n_done in range(0, 9):
        lines8 = [{'date': run[i // 2], 'sched': i < n_done, 'crew': i < n_done} for i in range(8)]
        r = score(base(adv=adv_yes, sched_days=sched4, labor=lines8), K, T, fields)
        if n_done == 0:
            nothing = r['score']
        assert r['score'] >= nothing, (n_done, r['score'], nothing)
    assert r['score'] == 100, r['score']
    # No scheduling at all → no PARTIAL/UNTOUCHED, "Awaiting scheduler".
    r = score(base(labor=[{'date': run[0]}, {'date': run[1]}]), K, T, fields)
    assert r['scheduler']['status'] == 'Awaiting scheduler'
    assert not any(i['code'] in ('PARTIAL_DAY', 'UNTOUCHED_DAY') for i in r['issues'])

    # No gear → N/A; unapproved / changed / approved → 0.3 / 0.5 / 1.
    r = score(base(), K, T, fields)
    assert not r['components']['gear']['applies'] and r['components']['gear']['reason'] == 'No gear booked'
    for g, want in (({'lines': 2}, 30), ({'lines': 2, 'snapshot': True}, 50),
                    ({'lines': 2, 'approved': True}, 100)):
        assert score(base(gear=g), K, T, fields)['components']['gear']['pct'] == want

    # Dark: a plain work day leaves E; a perf day stays and raises
    # DARK_PERF_DAY; load-in / load-out stay and raise DARK_LOAD_DAY.
    run5 = run + ['2026-12-14']
    r = score(base(span=run5, load_out=run5[-1], dark=[run[3], run[1]],
                   sched_days={run[2]: 1}), K, T, fields)
    assert r['components']['schedule']['detail']['days'] == 4
    assert any(i['code'] == 'DARK_PERF_DAY' for i in r['issues'])
    r = score(base(dark=[run[0], run[3]]), K, T, fields)
    assert r['components']['schedule']['detail']['days'] == 4
    assert sum(1 for i in r['issues'] if i['code'] == 'DARK_LOAD_DAY') == 2
    assert r['first_day'] == run[0]
    # Gaming check: dark-flagging the load days can't lift the score.
    lab2 = [{'date': x, 'sched': True, 'crew': True} for x in run[1:3]]
    plain = score(base(adv=adv_yes, sched_days={run[1]: 1, run[2]: 1}, labor=lab2), K, T, fields)
    gamed = score(base(adv=adv_yes, sched_days={run[1]: 1, run[2]: 1}, labor=lab2,
                       dark=[run[0], run[3]]), K, T, fields)
    assert gamed['score'] < plain['score'], (plain['score'], gamed['score'])
    # Every day dark → falls back to the full span.
    r = score(base(dark=run, perf_dates=[], load_in=None, load_out=None), K, T, fields)
    assert r['components']['schedule']['detail']['days'] == 4

    # Off-date rows; no dates → score None.
    r = score(base(sched_days={'2026-11-01': 2}), K, T, fields)
    assert any(i['code'] == 'OFF_DATE_ROWS' for i in r['issues'])
    r = score(base(span=[], perf_dates=[], load_in=None, load_out=None), K, T, fields)
    assert r['score'] is None and any(i['code'] == 'NO_DATES' for i in r['issues'])

    # Weight 0 disables; penalty cap; knob validation.
    r = score(base(adv=full_adv), dict(K, w_schedule=0), T, fields)
    assert not r['components']['schedule']['applies'] and r['score'] == 100
    # …and takes the component's penalties with it.
    lab = [{'date': run[i // 2], 'sched': i == 0} for i in range(8)]
    r = score(base(adv=full_adv, sched_days=sched4, labor=lab), dict(K, w_crew=0), T, fields)
    assert r['penalty'] == 0 and r['score'] == 100, (r['penalty'], r['score'])
    assert any(i['code'] == 'PARTIAL_DAY' and i['points'] == 0 for i in r['issues'])
    r = score(base(adv=full_adv, sched_days=sched4, gear={'lines': 1}), dict(K, w_gear=0), T, fields)
    assert not any(i['points'] for i in r['issues'] if i['code'].startswith('GEAR')), r['issues']
    r = score(base(adv=dict(full_adv, labor_load_in=(1, '')), sched_days=sched4),
              dict(K, w_labor=0), T, fields)
    assert any(i['code'] == 'NO_LABOR_SOON' for i in r['issues']) and r['penalty'] == 0
    # Dates but nothing weighted applies → its own headline, not "add dates".
    r = score(base(), dict(K, w_advance=0, w_schedule=0, w_labor=0, w_crew=0, w_gear=10), T, fields)
    assert r['score'] is None and r['band'] == BAND_NO_WEIGHTS
    assert r['headline'].startswith('Nothing to score'), r['headline']

    # Cue keys are positional (load-in, show call, load-out), whatever the names.
    flds = fields + [{'key': 'li2', 'type': 'yes_no'}]
    lab = [{'date': run[1], 'sched': True}, {'date': run[2], 'sched': True}]
    r = score(base(adv={'li2': (1, 'No')}, labor=lab), dict(K, labor_cue_keys='li2'), T, flds)
    assert r['components']['labor']['detail']['expected_days'] == 3, r['components']['labor']['detail']

    # Advance gaming: checkbox ticks are capped; '-' in a text box isn't an answer.
    cbf = [{'key': 'production_manager', 'type': 'contact_dropdown'}] + \
          [{'key': f't{i}', 'type': 'text'} for i in range(19)] + \
          [{'key': f'c{i}', 'type': 'checkbox'} for i in range(6)]
    r = score(base(adv=dict({f'c{i}': (1, 'Checked') for i in range(6)},
                            production_manager=(1, 'Pat'))), K, T, cbf)
    assert r['components']['advance']['pct'] < 100, r['components']['advance']
    r = score(base(adv=dict({f't{i}': (1, 'N/A') for i in range(6)},
                            production_manager=(1, 'Pat'))), K, T, cbf)
    assert r['components']['advance']['detail']['answered'] == 1, r['components']['advance']['detail']
    # Detail line percentage has no float drift (29 of 100 → 29%).
    f100 = [{'key': f'z{i}', 'type': 'text'} for i in range(100)]
    r = score(base(adv={f'z{i}': (1, 'x') for i in range(29)}), dict(K, adv_required_keys=''), T, f100)
    assert '(29%' in r['components']['advance']['lines'][0], r['components']['advance']['lines']
    try:
        clean_knobs(dict(K, w_advance=0, w_schedule=0, w_labor=0, w_crew=0, w_gear=0), strict=True)
        raise AssertionError('zero weights accepted')
    except KnobError:
        pass
    try:
        clean_knobs({'advance_target': 2}, strict=True)
        raise AssertionError('out of range accepted')
    except KnobError:
        pass
    assert clean_knobs({'advance_target': 2})['advance_target'] == 1.0
    assert clean_knobs({'adv_required_keys': ' a , b,a'})['adv_required_keys'] == 'a,b'
    assert pace_target(100) == 20 and pace_target(0) == 100 and pace_target(45) == 52
    print('readiness self-test OK')


if __name__ == '__main__':
    _selftest()
