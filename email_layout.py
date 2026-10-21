"""Branded layout for every system email (3.22.0).

One builder, `build()`, returns (text, html) for an email so the plain-text
part and the HTML part always say the same thing. The HTML is styled like
the app (sidebar blue header band, orange accents, white card on the page
grey) but built the way Outlook needs it: nested tables, inline styles only
(clients drop <style>), widths in attributes, a bulletproof table-cell
button, system fonts (no webfonts: Outlook ignores them). No images, so
nothing is blocked; the wordmark is text.

Dependency-free on purpose: app.py passes every value in.

Content model (all optional except title):
  eyebrow  small uppercase label above the title ("ASSET APPROVAL")
  title    headline
  intro    one paragraph under the title (plain text)
  facts    [(label, value)] — a two-column key/value block (Show, Date…)
  blocks   list of dicts, rendered in order:
             {'heading': str, 'items': [str], 'tone': 'added'|'removed'|
              'changed'|'info'}             → a bulleted, colour-flagged list
             {'heading': str, 'table': {'columns': [str], 'rows': [[str |
              (text, url)]],
              'align': [...'l'|'r'], 'total': [str] }} → a data table
             {'heading': str, 'text': str}  → a paragraph (keeps newlines)
             {'code': str}                   → a big centred code (OTP)
             {'html': str, 'text': str}      → trusted pre-built HTML + its
                                               plain-text twin (maintenance)
  button   (label, url) — the main call to action
  links    [(label, url)] — smaller links under the button
  note     small grey text under everything (e.g. "PDF attached")
  footer   footer line (sender / base URL)
  preheader hidden inbox-preview text
"""
import html as _html
import os

# The sidebar's stagelight, as a PNG (Outlook can't show SVG), embedded in
# the message as a cid: inline image — inline parts display without the
# "download pictures" prompt that blocks linked images. app.py's
# _build_mime_message attaches it whenever the HTML references LOGO_CID.
LOGO_CID = '321t-logo'
_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'static', 'img', 'email-logo.png')
_logo_cache = []


def logo_png():
    """The logo's bytes, or None if the file is missing (then
    strip_logo() takes the <img> out so nothing shows broken)."""
    if not _logo_cache:
        try:
            with open(_LOGO_PATH, 'rb') as f:
                _logo_cache.append(f.read())
        except OSError:
            _logo_cache.append(None)
    return _logo_cache[0]


def strip_logo(html):
    import re
    return re.sub(r'<td[^>]*data-logo[^>]*>.*?</td>', '', html, flags=re.S)

BLUE = '#1c5494'
BLUE_DARK = '#163f6e'
ORANGE = '#F57F20'
ORANGE_TEXT = '#B85A12'
INK = '#1d2026'
TEXT = '#374151'
DIM = '#6b7280'
LINE = '#e3e6ea'
PAGE = '#eef0f3'
SOFT = '#f6f7f9'
FONT = "'Segoe UI', -apple-system, Roboto, Helvetica, Arial, sans-serif"
MONO = "Consolas, 'Courier New', monospace"

TONES = {
    'added':   ('#15803D', '#ecfdf3', '+'),
    'removed': ('#B91C1C', '#fef2f2', '−'),
    'changed': ('#B45309', '#fffbeb', '→'),
    'info':    (BLUE, '#eff5fc', '•'),
}


def _e(v):
    return _html.escape('' if v is None else str(v))


def _p(text, size=14, color=TEXT, margin='0 0 14px'):
    body = '<br>'.join(_e(line) for line in str(text).split('\n'))
    return (f'<p style="margin:{margin};font-family:{FONT};font-size:{size}px;'
            f'line-height:1.5;color:{color}">{body}</p>')


def _heading(text):
    return (f'<p style="margin:20px 0 8px;font-family:{FONT};font-size:12px;font-weight:bold;'
            f'letter-spacing:1px;text-transform:uppercase;color:{DIM}">{_e(text)}</p>')


def _facts_html(facts):
    rows = ''.join(
        f'<tr><td valign="top" style="padding:6px 12px 6px 0;font-family:{FONT};font-size:13px;'
        f'color:{DIM};white-space:nowrap;width:1%">{_e(k)}</td>'
        f'<td valign="top" style="padding:6px 0;font-family:{FONT};font-size:14px;color:{INK};'
        f'font-weight:600">{_e(v)}</td></tr>'
        for k, v in facts if v not in (None, ''))
    if not rows:
        return ''
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="border-collapse:collapse;background:{SOFT};border:1px solid {LINE};'
            f'margin:0 0 6px"><tr><td style="padding:10px 16px">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
            f'{rows}</table></td></tr></table>')


def _items_html(items, tone):
    color, bg, mark = TONES.get(tone or 'info', TONES['info'])
    rows = ''.join(
        f'<tr><td width="4" bgcolor="{color}" style="background:{color};width:4px;font-size:0;'
        f'line-height:0">&nbsp;</td>'
        f'<td bgcolor="{bg}" style="background:{bg};padding:8px 12px;font-family:{FONT};'
        f'font-size:14px;line-height:1.4;color:{INK}">'
        f'<span style="color:{color};font-weight:bold">{mark}</span>&nbsp; {_e(it)}</td></tr>'
        f'<tr><td colspan="2" style="height:4px;font-size:0;line-height:0">&nbsp;</td></tr>'
        for it in items)
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="border-collapse:collapse">{rows}</table>')


def _table_html(t):
    cols = t.get('columns') or []
    align = t.get('align') or ['l'] * len(cols)
    al = lambda i: 'right' if i < len(align) and align[i] == 'r' else 'left'
    head = ''.join(
        f'<th align="{al(i)}" style="padding:8px 10px;font-family:{FONT};font-size:11px;'
        f'font-weight:bold;letter-spacing:.5px;text-transform:uppercase;color:#ffffff;'
        f'background:{BLUE};text-align:{al(i)}">{_e(c)}</th>'
        for i, c in enumerate(cols))
    body = ''
    for n, row in enumerate(t.get('rows') or []):
        bg = '#ffffff' if n % 2 == 0 else SOFT
        body += '<tr>' + ''.join(
            f'<td align="{al(i)}" bgcolor="{bg}" style="padding:7px 10px;font-family:{FONT};'
            f'font-size:13px;color:{INK};border-bottom:1px solid {LINE};text-align:{al(i)};'
            f'vertical-align:top">{_cell(v)}</td>'
            for i, v in enumerate(row)) + '</tr>'
    if t.get('total'):
        body += '<tr>' + ''.join(
            f'<td align="{al(i)}" style="padding:8px 10px;font-family:{FONT};font-size:13px;'
            f'font-weight:bold;color:{INK};border-top:2px solid {BLUE};text-align:{al(i)}">'
            f'{_e(v)}</td>' for i, v in enumerate(t['total'])) + '</tr>'
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="border-collapse:collapse;border:1px solid {LINE}">'
            f'<tr>{head}</tr>{body}</table>')


def _cell(v):
    """A table cell: plain value, or (text, url) for a link."""
    if isinstance(v, tuple):
        text, url = v
        if url:
            return (f'<a href="{_e(url)}" style="color:{ORANGE_TEXT};font-weight:600;'
                    f'text-decoration:underline">{_e(text)}</a>')
        return _e(text)
    return _e(v)


def _cell_text(v):
    if isinstance(v, tuple):
        return f'{v[0]} ({v[1]})' if v[1] else str(v[0])
    return '' if v is None else str(v)


def _button_html(label, url):
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:22px 0 6px"><tr><td bgcolor="{ORANGE}" style="background:{ORANGE};'
            f'border-radius:6px;padding:11px 22px">'
            f'<a href="{_e(url)}" style="font-family:{FONT};font-size:14px;font-weight:bold;'
            f'color:#ffffff;text-decoration:none;display:inline-block">{_e(label)}</a>'
            f'</td></tr></table>')


def _code_html(code):
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            f'style="margin:6px 0 16px"><tr><td align="center" bgcolor="{SOFT}" '
            f'style="background:{SOFT};border:1px solid {LINE};border-radius:8px;padding:18px;'
            f'font-family:{MONO};font-size:32px;font-weight:bold;letter-spacing:8px;color:{INK}">'
            f'{_e(code)}</td></tr></table>')


def _text_table(t):
    cols = t.get('columns') or []
    rows = [[_cell_text(v) for v in r] for r in (t.get('rows') or [])]
    if t.get('total'):
        rows.append([_cell_text(v) for v in t['total']])
    out = []
    for r in rows:
        out.append('  ' + ' | '.join(f'{c}: {v}' for c, v in zip(cols, r) if v not in ('', None)))
    return '\n'.join(out)


def build(title, *, eyebrow='', intro='', facts=(), blocks=(), button=None, links=(),
          note='', footer='', preheader=''):
    """(text, html) for one email. See the module docstring."""
    # ── plain text ──
    t = []
    if eyebrow:
        t.append(eyebrow.upper())
    t.append(title)
    t.append('')
    if intro:
        t += [intro, '']
    for k, v in facts:
        if v not in (None, ''):
            t.append(f'{k}: {v}')
    if any(v not in (None, '') for _, v in facts):
        t.append('')
    for b in blocks:
        if b.get('heading'):
            t.append(b['heading'].upper())
        if 'items' in b:
            mark = TONES.get(b.get('tone') or 'info', TONES['info'])[2]
            mark = {'−': '-', '→': '~', '•': '*'}.get(mark, mark)
            t += [f'  {mark} {it}' for it in b['items']]
        elif 'table' in b:
            t.append(_text_table(b['table']))
        elif 'text' in b and 'html' not in b:
            t.append(b['text'])
        elif 'code' in b:
            t += ['', f"    {b['code']}"]
        elif 'html' in b:
            t.append(b.get('text') or '')
        t.append('')
    if button:
        t += [f'{button[0]}: {button[1]}', '']
    for label, url in links:
        t.append(f'{label}: {url}')
    if links:
        t.append('')
    if note:
        t += [note, '']
    t += ['--', footer or '3·2·1→Theater · Dr. Phillips Center for the Performing Arts']
    text = '\n'.join(t)

    # ── html ──
    parts = []
    if intro:
        parts.append(_p(intro, size=15, color=TEXT))
    if facts:
        parts.append(_facts_html(facts))
    for b in blocks:
        if b.get('heading'):
            parts.append(_heading(b['heading']))
        if 'items' in b:
            parts.append(_items_html(b['items'], b.get('tone')))
        elif 'table' in b:
            parts.append(_table_html(b['table']))
        elif 'code' in b:
            parts.append(_code_html(b['code']))
        elif 'html' in b:
            parts.append(f'<div style="font-family:{FONT};font-size:14px;line-height:1.5;'
                         f'color:{TEXT}">{b["html"]}</div>')
        elif 'text' in b:
            parts.append(_p(b['text']))
    if button:
        parts.append(_button_html(*button))
    if links:
        parts.append('<p style="margin:10px 0 0;font-family:%s;font-size:13px;color:%s">%s</p>' % (
            FONT, DIM, ' &nbsp;·&nbsp; '.join(
                f'<a href="{_e(u)}" style="color:{ORANGE_TEXT};text-decoration:underline">{_e(l)}</a>'
                for l, u in links)))
    if note:
        parts.append(_p(note, size=12, color=DIM, margin='18px 0 0'))
    # The label sits in the card above the title, in the app's orange text
    # colour — one brand look for every email, no per-type colour bars.
    eyebrow_html = (
        f'<p style="margin:0 0 6px;font-family:{FONT};font-size:11px;font-weight:bold;'
        f'letter-spacing:1.5px;text-transform:uppercase;color:{ORANGE_TEXT}">{_e(eyebrow)}</p>'
    ) if eyebrow else ''
    pre = (f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all;font-size:1px;'
           f'color:{PAGE};line-height:1px">{_e(preheader or intro or title)}</div>')
    html = (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light only"><meta name="supported-color-schemes" content="light">'
        f'<title>{_e(title)}</title></head>'
        f'<body style="margin:0;padding:0;background:{PAGE}" bgcolor="{PAGE}">{pre}'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'bgcolor="{PAGE}" style="background:{PAGE}"><tr><td align="center" style="padding:24px 12px">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:100%;max-width:600px;border-collapse:collapse">'
        # header band — the sidebar's blue, stagelight + wordmark
        f'<tr><td bgcolor="{BLUE}" style="background:{BLUE};padding:16px 28px">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td data-logo valign="middle" style="padding:0 12px 0 0">'
        f'<img src="cid:{LOGO_CID}" width="28" height="34" alt="" '
        f'style="display:block;border:0;outline:none;width:28px;height:34px"></td>'
        f'<td valign="middle">'
        f'<span style="font-family:{FONT};font-size:20px;font-weight:bold;color:#ffffff;'
        f'letter-spacing:.5px">3·2·1<span style="color:{ORANGE}">→</span>THEATER</span><br>'
        f'<span style="font-family:{FONT};font-size:11px;color:#c9d6e8;letter-spacing:1px;'
        f'text-transform:uppercase">Advance to stage in 3-2-1</span></td></tr></table></td></tr>'
        f'<tr><td bgcolor="{ORANGE}" style="background:{ORANGE};height:3px;font-size:0;'
        f'line-height:0">&nbsp;</td></tr>'
        # card
        f'<tr><td bgcolor="#ffffff" style="background:#ffffff;padding:24px 28px 28px;'
        f'border:1px solid {LINE};border-top:none">'
        f'{eyebrow_html}'
        f'<h1 style="margin:0 0 14px;font-family:{FONT};font-size:21px;line-height:1.3;'
        f'font-weight:bold;color:{INK}">{_e(title)}</h1>'
        + ''.join(parts) +
        '</td></tr>'
        f'<tr><td style="padding:14px 28px;font-family:{FONT};font-size:11px;line-height:1.5;'
        f'color:{DIM};text-align:center">'
        + _e(footer or '3·2·1→Theater · Dr. Phillips Center for the Performing Arts') +
        '</td></tr></table></td></tr></table></body></html>')
    return text, html


def from_text(body_text, title=''):
    """Wrap an existing plain-text body (an admin's custom template, or any
    caller that only sends text) in the branded layout. Returns HTML."""
    return build(title or '3·2·1→Theater', blocks=[{'text': body_text}])[1]
