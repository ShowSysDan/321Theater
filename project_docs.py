# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
Document helpers — PURE Python (3.49.0).

Text extraction, e-mail parsing, file-type sniffing, note-HTML sanitizing and
note export for the files and notes that people upload or type. Everything in
here is handed bytes or text that a user supplied, so every byte is treated
as hostile.

SANDBOX RULES — keep every one:
  * No Flask, no database, no network, no subprocess, nothing written to
    disk. Never imports app.py (or any other app module). The callers own
    storage, access checks and logging.
  * Nothing here logs. A file's text, name or sender must never reach a log
    line, so failures are reported by the RETURN VALUE only ('' / None / b'').
    The third-party parsers' own loggers are turned down for the same reason
    (_quiet_parsers).
  * Every optional library (pdfplumber, PyMuPDF, python-docx, striprtf,
    openpyxl, xlrd, extract_msg) is imported lazily through _lib(): a
    missing — or half-installed — library degrades that one format to
    '' / None. It can never break `import project_docs` or raise at call
    time. (A deploy by `git pull` + restart does not install new
    requirements; only install.sh does.)
  * Every parser call sits in try/except and every loop over file content is
    bounded (pages, sheets, rows, cells, MIME parts, attachments, output
    characters, a time budget). Zip-based formats are refused before a
    library opens them when their DECLARED unpacked size is absurd.
  * Every string that leaves this module has NUL bytes, other control
    characters and lone surrogates removed (_clean): PostgreSQL TEXT cannot
    store \\x00 and psycopg2 cannot encode a surrogate.

Public API:
  extract_text(data, filename, mime='', max_chars=300_000) -> str
  parse_email(data, filename) -> dict | None
  email_attachment(data, filename, index) -> (name, mime, bytes) | None
  has_msg_support() -> bool
  sniff(data, filename, claimed_mime='') -> {'kind', 'mime'}
  sanitize_note_html(html) -> str        (ValueError over NOTE_HTML_MAX chars)
  html_to_text(html) -> str
  html_to_docx(title, html) -> bytes     (b'' when python-docx is missing)

`python3 project_docs.py` runs a fast self-test and prints OK.
"""

import codecs
import datetime
import html as _html
import importlib
import io
import logging
import re
import threading
import time
import warnings
import zipfile
import zlib
from html.parser import HTMLParser

__all__ = [
    'extract_text', 'parse_email', 'email_attachment', 'has_msg_support',
    'sniff', 'sanitize_note_html', 'html_to_text', 'html_to_docx',
    'NOTE_HTML_MAX',
]


# ── Limits ───────────────────────────────────────────────────────────────────
# A note body may not be longer than this, in OR out (see sanitize_note_html).
NOTE_HTML_MAX = 400_000

_MAX_PDF_PAGES = 300          # pages read from one PDF
_MAX_SHEETS = 50              # sheets read from one workbook
_MAX_ROWS = 20_000            # rows read from one sheet
_MAX_COLS = 200               # cells read from one row
_MAX_CELLS = 400_000          # cells read from one workbook
_MAX_PARTS = 1000             # MIME parts visited in one e-mail
_MAX_MIME_DEPTH = 30          # multipart nesting followed
_MAX_ATTACH = 100             # attachments listed for one e-mail
_MAX_RECIPIENTS = 200         # addresses shown for To / Cc
_EMAIL_BODY_MAX = 300_000     # characters of body_text kept
_HTML_TEXT_IN_MAX = 2_000_000  # characters of HTML that html_to_text reads
_RTF_MAX_BYTES = 30_000_000
_TIME_BUDGET_S = 45.0         # one extract_text call stops starting new pages / rows after this
# Zip-based office files (declared, i.e. central-directory, sizes — Python's
# zipfile never inflates a member past its declared size, so these bound the
# real work too).
_ZIP_MAX_MEMBERS = 10_000
_DOCX_MAX_MEMBER = 30_000_000     # python-docx loads every part into memory
_DOCX_MAX_TOTAL = 120_000_000
_XLSX_MAX_MEMBER = 64_000_000     # openpyxl read-only mode streams the sheets
_XLSX_MAX_TOTAL = 256_000_000
_DOCX_MAX_TABLE_CELLS = 20_000    # html_to_docx: beyond this a table becomes plain rows


# ── Optional libraries ───────────────────────────────────────────────────────
_LIBS = {}
_QUIET_DONE = False
# PyMuPDF does not support use from several threads at once, and extraction
# runs in background threads. This serializes OUR calls into it (it cannot
# cover the app's other PyMuPDF users).
_FITZ_LOCK = threading.Lock()


def _lib(name):
    """The optional module `name`, imported once; None when it is missing.

    Catches Exception, not just ImportError: a half-installed dependency can
    raise anything while importing, and that must not take a request down."""
    if name in _LIBS:
        return _LIBS[name]
    mod = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            mod = importlib.import_module(name)
    except Exception:
        mod = None
    _LIBS[name] = mod
    return mod


def _fitz():
    """PyMuPDF under its current name, else its old one ('fitz')."""
    return _lib('pymupdf') or _lib('fitz')


def _quiet_parsers():
    """Turn the parsers' own logging down, once.

    pdfminer and extract_msg log warnings that can quote bytes of the file
    (an unknown PDF operator, a bad property value). Private file content
    must never reach a log, so only their ERROR-and-above records survive.
    (MuPDF's stderr chatter is switched off in _pdf_fitz for the same reason.)
    These are process-wide switches: the app's other uses of these libraries
    lose their WARNING lines too, never their exceptions."""
    global _QUIET_DONE
    if _QUIET_DONE:
        return
    _QUIET_DONE = True
    for name in ('pdfminer', 'pdfplumber', 'extract_msg', 'RTFDE', 'olefile'):
        try:
            lg = logging.getLogger(name)
            if lg.level < logging.ERROR:
                lg.setLevel(logging.ERROR)
        except Exception:
            pass


# ── Small text helpers ───────────────────────────────────────────────────────
# NUL + C0 controls (not \t \n) + DEL + lone surrogates + the two non-characters.
_CTL_RE = re.compile('[\x00-\x08\x0b\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]')
_WS_RE = re.compile(r'[ \t\r\n\f\v\u00a0]+')


def _clean(s, max_chars=None):
    """A string that is safe to store and show: no NUL / control characters /
    lone surrogates, newlines normalized, cut to max_chars."""
    if s is None:
        return ''
    if isinstance(s, (bytes, bytearray)):
        s = bytes(s).decode('utf-8', 'replace')
    elif not isinstance(s, str):
        s = str(s)
    if max_chars is not None and len(s) > max_chars * 2 + 1000:
        s = s[:max_chars * 2 + 1000]       # bound the work below
    if '\r' in s or '\x0c' in s:
        s = s.replace('\r\n', '\n').replace('\r', '\n').replace('\x0c', '\n')
    s = _CTL_RE.sub('', s)
    if max_chars is not None and len(s) > max_chars:
        s = s[:max_chars]
    return s


def _one_line(s, max_chars=1000):
    """One clean line (a header value, a name): whitespace runs → one space."""
    s = _WS_RE.sub(' ', _clean(s, max_chars * 4)).strip()
    if len(s) > max_chars:
        s = s[:max_chars - 1].rstrip() + '…'
    return s


def _ext(filename):
    """Lower-case extension without the dot ('' when there is none)."""
    name = str(filename or '').replace('\\', '/').rsplit('/', 1)[-1].strip().lower()
    if '.' not in name:
        return ''
    e = name.rsplit('.', 1)[1]
    return e if re.fullmatch(r'[a-z0-9]{1,10}', e) else ''


def _decode_text(data, limit_bytes=None):
    """Bytes of a text file → str. BOM first, then strict UTF-8, then cp1252.

    Only `limit_bytes` are decoded (a cut in the middle of a UTF-8 sequence is
    tolerated, any other invalid byte is not)."""
    cut = limit_bytes is not None and len(data) > limit_bytes
    if cut:
        data = data[:limit_bytes]
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode('utf-8', 'replace')
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return data.decode('utf-32', 'replace')
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode('utf-16', 'replace')
    try:
        return codecs.getincrementaldecoder('utf-8')('strict').decode(data, not cut)
    except UnicodeDecodeError:
        return data.decode('cp1252', 'replace')


def _looks_text(data):
    """True when the first 64 KB read like text rather than a binary file."""
    sample = bytes(data[:65536])
    if not sample:
        return True
    if sample.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE,
                          codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return True
    if b'\x00' in sample:
        return False
    odd = sum(1 for b in sample if b < 32 and b not in (9, 10, 12, 13, 27))
    return odd * 100 <= len(sample)


class _Null:
    """A write-only sink (xlrd prints warnings about the file to its logfile)."""
    def write(self, *_a, **_k):
        return 0

    def flush(self):
        pass


# ═════════════════════════════════════════════════════════════════════════════
# sanitize_note_html — a SECURITY BOUNDARY
# ═════════════════════════════════════════════════════════════════════════════
# The result is stored and later inserted into members' pages as HTML, so it
# is never "cleaned": the input is parsed and a NEW document is written from
# an allow-list. Nothing of the input reaches the output except
#   * text, always escaped (& < >), and
#   * an http / https / mailto link target, re-validated and re-quoted.
# Every tag in the output is one of the literal strings this code writes, so
# the output is a tiny, unambiguous subset of HTML: no raw-text elements, no
# foreign content (svg / math), no comments, no attribute but a link's three.
# That is what defeats mutation XSS — a browser has nothing to re-interpret —
# and it does not depend on html.parser agreeing with any browser's parser.
_NOTE_TAGS = frozenset((
    'p', 'br', 'h2', 'h3', 'h4', 'strong', 'b', 'em', 'i', 'u', 's',
    'ul', 'ol', 'li', 'blockquote', 'code', 'pre', 'hr', 'a',
    'table', 'thead', 'tbody', 'tr', 'th', 'td'))
_NOTE_VOID = frozenset(('br', 'hr'))
_NOTE_INLINE = frozenset(('strong', 'b', 'em', 'i', 'u', 's', 'code', 'a'))
# Elements whose CONTENT is dropped, not just the tag: script / style source,
# frame fallbacks, foreign content, widgets. (Only elements that have an end
# tag — a void one here would swallow the rest of the note.)
_DROP_CONTENT = frozenset((
    'script', 'style', 'iframe', 'object', 'applet', 'svg', 'math',
    'noscript', 'noembed', 'noframes', 'template', 'title', 'xmp',
    'plaintext', 'audio', 'video', 'canvas', 'frameset', 'select',
    'datalist'))
# Disallowed block-level wrappers. They are unwrapped like everything else,
# but text on either side of one stays on its own line (see _soft_break).
_BLOCKISH = frozenset((
    'div', 'section', 'article', 'header', 'footer', 'main', 'nav', 'aside',
    'h1', 'h5', 'h6', 'dl', 'dt', 'dd', 'figure', 'figcaption', 'address',
    'center', 'fieldset', 'form', 'details', 'summary', 'caption', 'legend',
    'body', 'html'))
_NOTE_MAX_DEPTH = 100
_HREF_MAX = 2000
# Everything a browser ignores or trims inside a URL (tab / CR / LF anywhere,
# leading C0 controls and spaces) plus invisible format characters: removed
# BEFORE the scheme is judged, so "java&#9;script:" is read as a browser
# reads it — and what is judged is exactly what is written out.
_HREF_STRIP = re.compile(
    '[\x00-\x20\x7f-\xa0\u1680\u180e\u2000-\u200f\u2028-\u202f\u205f-\u206f\u3000\ufeff]')
_HREF_OK = re.compile(r'(https?://|mailto:)(.+)', re.I | re.S)
_A_OPEN = '<a href="%s" rel="noopener noreferrer" target="_blank">'


def _safe_href(value):
    """The link target to write, or '' when it is not http / https / mailto.

    Relative URLs, protocol-relative ones, javascript: / data: / vbscript:
    and every other scheme are refused. Idempotent: a returned value passes
    unchanged."""
    if not value:
        return ''
    v = _HREF_STRIP.sub('', str(value))
    if not v or len(v) > _HREF_MAX:
        return ''
    m = _HREF_OK.match(v)
    if not m:
        return ''
    scheme, rest = m.group(1).lower(), m.group(2)
    if scheme != 'mailto:' and rest[0] in '/\\?#':
        return ''                       # "https:///x", "http://\\x": no host
    return scheme + rest


class _NoteSanitizer(HTMLParser):
    """Rebuilds note HTML from the allow-list (see the block comment above).

    Output is always balanced: an end tag closes everything opened inside
    its element, a stray end tag is ignored, and whatever is still open at
    the end is closed. A balanced, allow-listed document comes out exactly
    as it went in — that is what makes the function idempotent."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []          # open allow-listed elements, outermost first
        self.skip = None         # the _DROP_CONTENT element we are inside
        self.skip_depth = 0
        self.inline_tail = False  # the last thing written was visible text

    def _soft_break(self):
        # <div>one</div><div>two</div> must not become "onetwo": a browser's
        # contenteditable makes one <div> per line. Only when text would
        # otherwise run together, so <div><p>x</p></div> gains nothing.
        if self.inline_tail:
            self.out.append('<br>')
            self.inline_tail = False

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag == self.skip:
                self.skip_depth += 1
            return
        if tag in _DROP_CONTENT:
            self.skip, self.skip_depth = tag, 1
            return
        if tag not in _NOTE_TAGS:
            if tag in _BLOCKISH:
                self._soft_break()
            return
        if tag in _NOTE_VOID:
            self.out.append('<%s>' % tag)
            self.inline_tail = False
            return
        if len(self.stack) >= _NOTE_MAX_DEPTH:
            return                       # deeper than any real note: unwrap
        if tag == 'a':
            # The FIRST href is the one a browser uses. A link without a
            # safe target, or inside another link, is unwrapped (text kept).
            href = _safe_href(next((v for k, v in attrs if k == 'href'), ''))
            if not href or 'a' in self.stack:
                return
            self.out.append(_A_OPEN % _html.escape(href, quote=True))
        else:
            self.out.append('<%s>' % tag)      # no attribute is ever copied
            if tag not in _NOTE_INLINE:
                self.inline_tail = False
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        # "<iframe/>" does not close itself in a browser: treat a
        # self-closed raw-text element as opened, so its content is dropped.
        if tag in _DROP_CONTENT and tag not in ('svg', 'math'):
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip:
                self.skip_depth -= 1
                if self.skip_depth <= 0:
                    self.skip = None
            return
        if tag not in _NOTE_TAGS:
            if tag in _BLOCKISH:
                self._soft_break()
            return
        if tag in _NOTE_VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append('</%s>' % t)
            if t not in _NOTE_INLINE:
                self.inline_tail = False
            if t == tag:
                break

    def handle_data(self, data):
        if self.skip:
            return
        # Control characters (a form feed or CR written as &#12; / &#13; too)
        # have no place in a note and are not valid in the Word export.
        data = _CTL_RE.sub('', data.replace('\r\n', '\n').replace('\r', '\n').replace('\x0c', '\n'))
        if not data:
            return
        self.out.append(_html.escape(data, quote=False))
        if data.strip():
            self.inline_tail = True

    # Comments, doctypes, processing instructions and CDATA / marked sections
    # are dropped whole.
    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass

    def result(self):
        while self.stack:
            self.out.append('</%s>' % self.stack.pop())
        return ''.join(self.out)


def sanitize_note_html(html):
    """Note HTML → the same note written from the allow-list.

    Kept: p br h2 h3 h4 strong b em i u s ul ol li blockquote code pre hr
    table thead tbody tr th td, and a[href] for http / https / mailto (every
    link gets rel="noopener noreferrer" target="_blank"). Every other tag is
    unwrapped with its text kept; the content of script / style / iframe /
    object / svg / math … is dropped; every other attribute is dropped.

    Idempotent: sanitize(sanitize(x)) == sanitize(x).
    Raises ValueError when the note is longer than NOTE_HTML_MAX characters —
    before OR after (escaping can lengthen it, and a stored note must always
    be able to pass through here again). Never truncates."""
    if html is None:
        return ''
    if isinstance(html, (bytes, bytearray)):
        html = bytes(html).decode('utf-8', 'replace')
    elif not isinstance(html, str):
        html = str(html)
    if len(html) > NOTE_HTML_MAX:
        raise ValueError('note too long')
    p = _NoteSanitizer()
    # NUL is dropped first: browsers and parsers disagree about it inside tag
    # names, and it cannot be stored anyway.
    p.feed(html.replace('\x00', ''))
    p.close()
    out = p.result()
    if len(out) > NOTE_HTML_MAX:
        raise ValueError('note too long')
    return out


# ═════════════════════════════════════════════════════════════════════════════
# html_to_text
# ═════════════════════════════════════════════════════════════════════════════
_TEXT_BLOCK = frozenset((
    'p', 'div', 'section', 'article', 'header', 'footer', 'main', 'nav',
    'aside', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'dl', 'dt',
    'dd', 'blockquote', 'pre', 'table', 'thead', 'tbody', 'tfoot', 'tr',
    'figure', 'figcaption', 'address', 'center', 'fieldset', 'form', 'hr',
    'details', 'summary', 'caption', 'body', 'html'))
# <head> itself is NOT dropped: mail HTML often never closes it, and all it
# can hold that has text (title / style / script) is dropped by name anyway.
_TEXT_DROP = _DROP_CONTENT
# HTML tags that end an <svg> / <math> in a browser (the "breakout" list of
# the HTML parsing rules). html_to_text honours it so that a mail with an
# unclosed <svg> still yields the text its reader saw. The note sanitizer
# deliberately does NOT: there, dropping more is the safe side.
_FOREIGN_BREAKOUT = frozenset((
    'b', 'big', 'blockquote', 'body', 'br', 'center', 'code', 'dd', 'div',
    'dl', 'dt', 'em', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'li',
    'ol', 'p', 'pre', 's', 'small', 'span', 'strong', 'sub', 'sup', 'table',
    'tt', 'u', 'ul'))


class _Stop(Exception):
    """Raised inside a parser to stop reading once the output cap is hit."""


class _TextExtractor(HTMLParser):
    def __init__(self, max_chars):
        super().__init__(convert_charrefs=True)
        self.max_chars = max_chars
        self.parts = []
        self.size = 0
        self.skip = None
        self.skip_depth = 0
        self.pre = 0
        self.lists = []          # one counter per open list: None = bullets
        self.links = []          # (href, index into parts where its text began)
        self.bol = True          # at the start of a line / cell, or right after a space

    def _add(self, s):
        if not s:
            return
        self.parts.append(s)
        self.bol = s[-1] in '\n\t '      # nothing to separate from: drop a leading space
        self.size += len(s)
        if self.size > self.max_chars * 2 + 1000:
            raise _Stop()

    def handle_starttag(self, tag, attrs):
        if self.skip in ('svg', 'math') and tag in _FOREIGN_BREAKOUT:
            self.skip = None
        if self.skip:
            if tag == self.skip:
                self.skip_depth += 1
            return
        if tag in _TEXT_DROP:
            self.skip, self.skip_depth = tag, 1
            return
        if tag == 'br':
            self._add('\n')
        elif tag in ('td', 'th'):
            self._add('\t')
        elif tag in _TEXT_BLOCK:
            self._add('\n')
            if tag == 'pre':
                self.pre += 1
            elif tag == 'ul':
                self.lists.append(None)
            elif tag == 'ol':
                self.lists.append(0)
            elif tag == 'li':
                if self.lists and self.lists[-1] is not None:
                    self.lists[-1] += 1
                    self._add('%d. ' % self.lists[-1])
                else:
                    self._add('- ')
        elif tag == 'a':
            href = _safe_href(next((v for k, v in attrs if k == 'href'), ''))
            self.links.append((href, len(self.parts)))

    def handle_startendtag(self, tag, attrs):
        if tag in _TEXT_DROP and tag not in ('svg', 'math'):
            self.handle_starttag(tag, attrs)
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip:
                self.skip_depth -= 1
                if self.skip_depth <= 0:
                    self.skip = None
            return
        if tag == 'a':
            if self.links:
                href, start = self.links.pop()
                # A link's target is part of what the text says ("click
                # here"), so it is kept — as text — unless already shown.
                if href and len(href) <= 300:
                    shown = ''.join(self.parts[start:])
                    bare = href[7:] if href.startswith('mailto:') else href
                    if bare not in shown and shown.strip():
                        self._add(' (%s)' % bare)
                    elif not shown.strip():
                        self._add(bare)
        elif tag in _TEXT_BLOCK and tag != 'hr':
            self._add('\n')
            if tag == 'pre':
                self.pre = max(0, self.pre - 1)
            elif tag in ('ul', 'ol') and self.lists:
                self.lists.pop()

    def handle_data(self, data):
        if self.skip:
            return
        data = _CTL_RE.sub('', data)
        if not self.pre:
            data = _WS_RE.sub(' ', data)
            if self.bol:
                data = data.lstrip(' ')      # (a <pre> keeps its indentation)
        self._add(data)

    def handle_comment(self, data):
        pass

    def handle_decl(self, decl):
        pass

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        pass


_SP_NL = re.compile(r'[ \t]+\n')
_NL3 = re.compile(r'\n{3,}')


def html_to_text(html, max_chars=_EMAIL_BODY_MAX * 2):
    """HTML → plain text (never HTML): scripts / styles dropped, blocks and
    <br> become line breaks, list items get a marker, table cells a tab, a
    link keeps its target in brackets. '' on any failure."""
    if html is None:
        return ''
    try:
        if isinstance(html, (bytes, bytearray)):
            html = bytes(html).decode('utf-8', 'replace')
        elif not isinstance(html, str):
            html = str(html)
        if len(html) > _HTML_TEXT_IN_MAX:
            html = html[:_HTML_TEXT_IN_MAX]
        p = _TextExtractor(max_chars)
        try:
            p.feed(html.replace('\x00', ''))
            p.close()
        except _Stop:
            pass
        text = ''.join(p.parts)
        text = _SP_NL.sub('\n', text)
        text = re.sub(r'\n\t', '\n', text)          # a row's first cell
        text = _NL3.sub('\n\n', text)
        return _clean(text.strip(), max_chars)
    except Exception:
        return ''


# ═════════════════════════════════════════════════════════════════════════════
# sniff — what a file IS, from its bytes
# ═════════════════════════════════════════════════════════════════════════════
_OLE_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
_OCTET = 'application/octet-stream'
_OTHER = {'kind': 'other', 'mime': _OCTET}


def _u16(s):
    return s.encode('utf-16-le')


# An OLE2 container says nothing about what is inside; its directory does
# (stream names are stored as UTF-16). The extension picks the family and the
# stream name confirms it, so a renamed file is never trusted.
_OLE_KINDS = {
    'msg': ('email', 'application/vnd.ms-outlook', (_u16('__substg1.0_'), _u16('__properties_version1.0'))),
    'doc': ('office', 'application/msword', (_u16('WordDocument'),)),
    'xls': ('office', 'application/vnd.ms-excel', (_u16('Workbook'), _u16('Book'))),
    'ppt': ('office', 'application/vnd.ms-powerpoint', (_u16('PowerPoint Document'),)),
}
# Zip-based office files: the extension picks the family, a part name every
# file of that family has confirms it.
_ZIP_KINDS = {
    'docx': ('application/vnd.openxmlformats-officedocument.wordprocessingml.document', (b'word/',)),
    'xlsx': ('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', (b'xl/',)),
    'pptx': ('application/vnd.openxmlformats-officedocument.presentationml.presentation', (b'ppt/',)),
    'odt': ('application/vnd.oasis.opendocument.text', (b'application/vnd.oasis.opendocument.text',)),
    'ods': ('application/vnd.oasis.opendocument.spreadsheet', (b'application/vnd.oasis.opendocument.spreadsheet',)),
    'odp': ('application/vnd.oasis.opendocument.presentation', (b'application/vnd.oasis.opendocument.presentation',)),
}
_TEXT_EXTS = {
    'txt': 'text/plain', 'text': 'text/plain', 'log': 'text/plain',
    'md': 'text/plain', 'markdown': 'text/plain', 'json': 'text/plain',
    'tsv': 'text/plain', 'csv': 'text/csv',
}
# Anything a browser (or a double-click) could run or render as a page. These
# are 'other' whatever their bytes look like — they are only ever downloaded.
_SCRIPTABLE_EXTS = frozenset((
    'html', 'htm', 'xhtml', 'xht', 'shtml', 'mht', 'mhtml', 'hta', 'htc',
    'svg', 'svgz', 'xml', 'xsl', 'xslt', 'xsd', 'dtd', 'rss', 'atom', 'rdf',
    'xaml', 'xul', 'js', 'mjs', 'cjs', 'jsx', 'ts', 'vbs', 'vbe', 'jse',
    'wsf', 'wsh', 'css', 'php', 'asp', 'aspx', 'jsp', 'swf', 'url', 'lnk'))
# A claimed MIME type is only ever a HINT for the extension, when the file
# name has none; the bytes still decide.
_CLAIM_EXT = {
    'application/pdf': 'pdf', 'message/rfc822': 'eml',
    'application/vnd.ms-outlook': 'msg', 'text/plain': 'txt',
    'text/csv': 'csv', 'application/json': 'json', 'text/markdown': 'md',
    'application/rtf': 'rtf', 'text/rtf': 'rtf',
    'application/msword': 'doc', 'application/vnd.ms-excel': 'xls',
    'application/vnd.ms-powerpoint': 'ppt',
}
_CLAIM_EXT.update({v[0]: k for k, v in _ZIP_KINDS.items()})
# Text that starts like a page or a vector image is not shown as text.
_MARKUP_START = re.compile(
    rb'\s*(?:<!doctype\s+html|<html|<head|<body|<script|<iframe|<svg|<\?xml|<!doctype\s+svg)', re.I)
_HEADER_LINE = re.compile(rb'^([!-9;-~]{1,70}):(?:[ \t]|$)')
_MAIL_HEADERS = frozenset((
    b'from', b'to', b'cc', b'subject', b'date', b'received', b'message-id',
    b'mime-version', b'return-path', b'delivered-to', b'content-type',
    b'reply-to', b'sender', b'x-mailer', b'thread-topic'))


def _looks_like_email(data):
    """True when the file opens with RFC 5322 headers (at least two header
    lines, at least one of them a mail header)."""
    head = bytes(data[:16384])
    if head.startswith(codecs.BOM_UTF8):
        head = head[3:]
    head = head.lstrip(b'\r\n')
    if b'\x00' in head[:2048]:
        return False
    lines = head.replace(b'\r\n', b'\n').replace(b'\r', b'\n').split(b'\n')
    if lines and lines[0].startswith(b'From '):      # mbox separator
        lines = lines[1:]
    seen, known = 0, 0
    for ln in lines[:200]:
        if not ln.strip():
            break
        if ln[:1] in (b' ', b'\t'):                  # folded continuation
            continue
        m = _HEADER_LINE.match(ln)
        if not m:
            return False
        seen += 1
        if m.group(1).lower() in _MAIL_HEADERS:
            known += 1
    return seen >= 2 and known >= 1


def sniff(data, filename, claimed_mime=''):
    """What kind of file this is → {'kind', 'mime'}.

    kind: pdf | image | office | email | text | other. Decided from the magic
    bytes and the extension; the client's MIME type is never trusted. `mime`
    comes from a fixed list of types that are safe to store — html, svg, xml
    and everything else scriptable or unknown is 'other' /
    application/octet-stream, so it can only ever be downloaded."""
    try:
        data = bytes(data or b'')
        ext = _ext(filename)
        if not ext:
            claim = str(claimed_mime or '').split(';', 1)[0].strip().lower()
            ext = _CLAIM_EXT.get(claim, '')
        if not data or ext in _SCRIPTABLE_EXTS:
            return dict(_OTHER)
        head = data[:16]

        # Self-identifying binary formats: the magic number decides.
        if head.startswith(b'%PDF-') or (ext == 'pdf' and b'%PDF-' in data[:1024]):
            return {'kind': 'pdf', 'mime': 'application/pdf'}
        if head.startswith(b'\x89PNG\r\n\x1a\n'):
            return {'kind': 'image', 'mime': 'image/png'}
        if head.startswith(b'\xff\xd8\xff'):
            return {'kind': 'image', 'mime': 'image/jpeg'}
        if head.startswith((b'GIF87a', b'GIF89a')):
            return {'kind': 'image', 'mime': 'image/gif'}
        if head.startswith(b'RIFF') and data[8:12] == b'WEBP':
            return {'kind': 'image', 'mime': 'image/webp'}
        if (ext in ('bmp', 'dib') and head.startswith(b'BM') and len(data) >= 26
                and data[6:10] == b'\x00\x00\x00\x00'
                and int.from_bytes(data[14:18], 'little') in (12, 40, 52, 56, 64, 108, 124)):
            return {'kind': 'image', 'mime': 'image/bmp'}

        # Containers: extension + magic + a marker of that family.
        if head.startswith(_OLE_MAGIC):
            spec = _OLE_KINDS.get(ext)
            if spec and any(mark in data for mark in spec[2]):
                return {'kind': spec[0], 'mime': spec[1]}
            return dict(_OTHER)
        if head.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')):
            spec = _ZIP_KINDS.get(ext)
            if spec and any(mark in data for mark in spec[1]):
                return {'kind': 'office', 'mime': spec[0]}
            return dict(_OTHER)
        if head.startswith(b'{\\rtf'):
            if ext in ('rtf', 'doc'):
                return {'kind': 'office', 'mime': 'application/rtf'}
            return dict(_OTHER)

        # No magic number: the extension proposes, the content must agree.
        if ext == 'eml':
            if _looks_like_email(data):
                return {'kind': 'email', 'mime': 'message/rfc822'}
            return dict(_OTHER)
        if ext in _TEXT_EXTS or ext == 'rtf':
            if _looks_text(data) and not _MARKUP_START.match(data[:1024].lstrip(codecs.BOM_UTF8)):
                return {'kind': 'text', 'mime': _TEXT_EXTS.get(ext, 'text/plain')}
        return dict(_OTHER)
    except Exception:
        return dict(_OTHER)


# ═════════════════════════════════════════════════════════════════════════════
# E-mail (.eml via the standard library, .msg via optional extract_msg)
# ═════════════════════════════════════════════════════════════════════════════
_BIDI_RE = re.compile('[\u200e\u200f\u202a-\u202e\u2066-\u2069]')
_NAME_BAD = re.compile(r'[\\/:*?"<>|]+')
_MIME_OK = re.compile(r'[a-z0-9][a-z0-9.+_-]{0,60}/[a-z0-9][a-z0-9.+_-]{0,80}')
_MIME_EXT = {
    'application/pdf': '.pdf', 'image/png': '.png', 'image/jpeg': '.jpg',
    'image/gif': '.gif', 'text/plain': '.txt', 'text/csv': '.csv',
    'text/calendar': '.ics', 'application/zip': '.zip',
    'message/rfc822': '.eml', 'application/vnd.ms-outlook': '.msg',
}


def _safe_name(name, fallback):
    """An attachment's file name with nothing that could act as a path or
    disguise it: no directory part, path separators, control characters,
    bidi overrides (the "invoice[U+202E]fdp.exe" trick) or leading dots."""
    n = _BIDI_RE.sub('', _WS_RE.sub(' ', _clean(name, 4000)))
    n = n.replace('\\', '/').rsplit('/', 1)[-1]
    n = _NAME_BAD.sub('_', n).strip(' .')
    if len(n) > 200:
        stem, dot, ext = n.rpartition('.')
        n = (stem[:200 - len(ext) - 1] + dot + ext) if dot and len(ext) <= 10 else n[:200]
    return n or fallback


def _safe_mime(value):
    m = str(value or '').split(';', 1)[0].strip().lower()
    return m if _MIME_OK.fullmatch(m) else _OCTET


def _fmt_addr(name, addr):
    name, addr = _one_line(name, 200), _one_line(addr, 254)
    if name and addr and name.lower() != addr.lower():
        return '%s <%s>' % (name, addr)
    return addr or name


def _iso(dt):
    try:
        if isinstance(dt, datetime.datetime):
            return dt.replace(microsecond=0).isoformat()
        if isinstance(dt, datetime.date):
            return dt.isoformat()
    except Exception:
        pass
    return ''


def _is_ole(data):
    # olefile treats a short byte string as a FILE NAME and opens it from
    # disk; never hand it less than a whole header + one sector.
    return len(data) >= 1536 and data[:8] == _OLE_MAGIC


# ── .eml ─────────────────────────────────────────────────────────────────────
class _Eml:
    """One parsed .eml. Every accessor is defensive: with policy.default the
    standard library parses a header when it is READ, and a malformed one can
    raise there."""

    def __init__(self, data):
        from email import policy
        from email.parser import BytesParser
        # A BOM or blank lines before the first header would turn the whole
        # header block into "body".
        if data.startswith(codecs.BOM_UTF8):
            data = data[3:]
        data = data.lstrip(b'\r\n')
        self.data = data
        self.msg =BytesParser(policy=policy.default).parsebytes(data)
        self._raw = None
        self.plain = None
        self.html = None
        self.attachments = []        # [(name, mime, part)]
        self._scan()

    # headers ---------------------------------------------------------------
    def _raw_header(self, name):
        """Fallback: the header as the lenient legacy parser reads it."""
        try:
            if self._raw is None:
                from email import policy
                from email.parser import BytesParser
                self._raw = BytesParser(policy=policy.compat32).parsebytes(
                    self.data, headersonly=True)
            v = self._raw.get(name)
            if v is None:
                return ''
            from email.header import decode_header, make_header
            try:
                return str(make_header(decode_header(str(v))))
            except Exception:
                return str(v)
        except Exception:
            return ''

    def header(self, name):
        try:
            v = self.msg.get(name)
            return '' if v is None else str(v)
        except Exception:
            return self._raw_header(name)

    def addresses(self, name):
        """[(display name, address)] for an address header."""
        out = []
        try:
            for hv in (self.msg.get_all(name) or [])[:20]:
                for a in getattr(hv, 'addresses', ()) or ():
                    out.append((a.display_name or '', a.addr_spec or ''))
                    if len(out) > _MAX_RECIPIENTS:
                        return out
            if out:
                return out
        except Exception:
            out = []
        try:
            from email.utils import getaddresses
            raw = self._raw_header(name) or self.header(name)
            for nm, ad in getaddresses([raw])[:_MAX_RECIPIENTS + 1]:
                if nm or ad:
                    out.append((nm, ad))
        except Exception:
            pass
        return out

    def address_line(self, name):
        addrs = self.addresses(name)
        shown = [s for s in (_fmt_addr(n, a) for n, a in addrs[:_MAX_RECIPIENTS]) if s]
        line = ', '.join(shown)
        if len(addrs) > _MAX_RECIPIENTS:
            line += ', …'
        return _one_line(line, 8000)

    def date(self):
        try:
            hv = self.msg.get('date')
            dt = getattr(hv, 'datetime', None)
            if dt is not None:
                return _iso(dt)
        except Exception:
            pass
        try:
            from email.utils import parsedate_to_datetime
            raw = self._raw_header('date')
            return _iso(parsedate_to_datetime(raw)) if raw else ''
        except Exception:
            return ''

    # parts -----------------------------------------------------------------
    @staticmethod
    def _ctype(part):
        try:
            return str(part.get_content_type() or '').lower()
        except Exception:
            return 'text/plain'

    @staticmethod
    def _disposition(part):
        try:
            return part.get_content_disposition() or ''
        except Exception:
            return ''

    @staticmethod
    def _filename(part):
        try:
            return part.get_filename() or ''
        except Exception:
            return ''

    def _leaves(self):
        """Every part that is not a multipart container, in document order.
        An attached message (message/rfc822) is ONE leaf: its own parts are
        that message's business, not this one's. Bounded in count and depth,
        and iterative, so a hostile nesting cannot exhaust the stack."""
        stack, seen = [(self.msg, 0)], 0
        while stack:
            part, depth = stack.pop()
            seen += 1
            if seen > _MAX_PARTS:
                return
            ctype = self._ctype(part)
            if ctype.startswith('multipart/'):
                if depth >= _MAX_MIME_DEPTH:
                    continue
                try:
                    subs = part.get_payload()
                except Exception:
                    subs = None
                if isinstance(subs, list):
                    for sub in reversed(subs[:_MAX_PARTS]):
                        stack.append((sub, depth + 1))
                continue
            yield part, ctype

    def _scan(self):
        n = 0
        for part, ctype in self._leaves():
            disp = self._disposition(part)
            fname = self._filename(part)
            is_att = disp == 'attachment' or bool(fname)
            if ctype in ('text/plain', 'text/html') and not is_att:
                # The first of each is the body; later ones (list footers,
                # the text half of a nested alternative) are ignored.
                if ctype == 'text/plain' and self.plain is None:
                    self.plain = part
                elif ctype == 'text/html' and self.html is None:
                    self.html = part
                continue
            if not is_att:
                if ctype.startswith('message/') and ctype != 'message/rfc822':
                    continue             # delivery-status and friends
                if ctype.startswith('text/'):
                    continue
                try:
                    if part.get('content-id') and ctype.startswith('image/'):
                        continue         # an image embedded in the HTML body
                except Exception:
                    pass
            if len(self.attachments) >= _MAX_ATTACH:
                break
            n = len(self.attachments)
            if not fname and ctype == 'message/rfc822':
                # Named after its subject. A subject is not a path: "a / b"
                # keeps both halves instead of losing everything before the /.
                fname = re.sub(r'[\\/]+', '_', self._inner_subject(part))
                fname = (fname + '.eml') if fname else ''
            fallback = 'attachment-%d%s' % (n + 1, _MIME_EXT.get(ctype, ''))
            self.attachments.append((_safe_name(fname, fallback), _safe_mime(ctype), part))

    @staticmethod
    def _inner_subject(part):
        try:
            inner = part.get_payload()
            if isinstance(inner, list) and inner:
                return _one_line(str(inner[0].get('subject') or ''), 150)
        except Exception:
            pass
        return ''

    @staticmethod
    def part_bytes(part):
        """The decoded bytes of an attachment part (b'' when unreadable)."""
        try:
            payload = part.get_payload()
            if isinstance(payload, list):            # message/* : the inner message(s)
                chunks = []
                for inner in payload[:5]:
                    try:
                        chunks.append(inner.as_bytes())
                    except Exception:
                        from email import policy
                        try:
                            chunks.append(inner.as_bytes(policy=policy.compat32))
                        except Exception:
                            chunks.append(inner.as_string().encode('utf-8', 'replace'))
                return b''.join(chunks)
            data = part.get_payload(decode=True)
            return bytes(data) if data else b''
        except Exception:
            return b''

    @staticmethod
    def part_text(part):
        if part is None:
            return ''
        try:
            content = part.get_content()
            if isinstance(content, bytes):
                return content.decode('utf-8', 'replace')
            return str(content)
        except Exception:
            pass
        try:                                         # unknown charset, bad transfer encoding
            raw = part.get_payload(decode=True) or b''
            return _decode_text(bytes(raw))
        except Exception:
            return ''

    def body_text(self):
        text = self.part_text(self.plain)
        if text.strip():
            return _clean(text, _EMAIL_BODY_MAX)
        # An HTML-only message: its body is converted to text here and is
        # never returned (or stored) as HTML.
        return html_to_text(self.part_text(self.html), _EMAIL_BODY_MAX)


def _parse_eml(data):
    eml = _Eml(data)
    try:
        if not any(k.lower().encode('ascii', 'replace') in _MAIL_HEADERS for k in eml.msg.keys()):
            return None, None            # not an e-mail at all
    except Exception:
        return None, None
    frm = eml.addresses('from') or eml.addresses('sender')
    name, addr = (frm[0] if frm else ('', ''))
    info = {
        'from': _fmt_addr(name, addr),
        'from_name': _one_line(name, 200),
        'from_email': _one_line(addr, 254) if '@' in (addr or '') else '',
        'to': eml.address_line('to'),
        'cc': eml.address_line('cc'),
        'date': eml.date(),
        'subject': _one_line(eml.header('subject'), 1000),
        'body_text': eml.body_text(),
        'attachments': [
            {'index': i, 'name': nm, 'size': len(eml.part_bytes(part)), 'mime': mime}
            for i, (nm, mime, part) in enumerate(eml.attachments)],
    }
    return info, eml


# ── .msg ─────────────────────────────────────────────────────────────────────
def has_msg_support():
    """True when Outlook .msg files can be read (extract_msg is installed)."""
    return _lib('extract_msg') is not None


def _msg_open(data):
    em = _lib('extract_msg')
    if em is None or not _is_ole(data):
        return None
    _quiet_parsers()
    try:
        return em.openMsg(data, strict=False, delayAttachments=True)
    except Exception:
        return None


def _msg_attr(m, name):
    try:
        return getattr(m, name, None)
    except Exception:                    # a property is decoded when it is read
        return None


def _msg_stream_text(m, method, stream):
    try:
        fn = getattr(m, method, None)
        return fn(stream) if fn else None
    except Exception:
        return None


def _msg_body(m):
    """The body as text. The plain-text stream first, then the HTML stream
    (converted to text), then the RTF body through striprtf. extract_msg's
    own .body / .htmlBody fall back to de-encapsulating RTF with a grammar
    parser, which is slow on hostile input, so the streams are read directly."""
    body = _msg_stream_text(m, 'getStringStream', '__substg1.0_1000')
    if isinstance(body, bytes):
        body = _decode_text(body)
    if body and str(body).replace('\x00', '').strip():
        return _clean(body, _EMAIL_BODY_MAX)
    raw = _msg_stream_text(m, 'getStream', '__substg1.0_10130102')
    if raw:
        text = html_to_text(_decode_text(bytes(raw), _HTML_TEXT_IN_MAX * 4), _EMAIL_BODY_MAX)
        if text:
            return text
    rtf = _msg_attr(m, 'rtfBody')
    if rtf and len(rtf) <= _RTF_MAX_BYTES:
        text = _rtf_text(bytes(rtf), _EMAIL_BODY_MAX)
        if text.strip():
            return text
    return ''


def _msg_attachments(m):
    """[(name, mime, loader)] — loader() returns the bytes or b''."""
    try:
        atts = list(m.attachments)[:_MAX_ATTACH]
    except Exception:
        atts = []
    out = []
    for i, a in enumerate(atts):
        name = ''
        for attr in ('getFilename', 'longFilename', 'name', 'displayName', 'shortFilename'):
            try:
                v = getattr(a, attr, None)
                v = v() if callable(v) else v
            except Exception:
                v = None
            if v and str(v).replace('\x00', '').strip():
                name = str(v)
                break
        try:
            mime = _safe_mime(a.mimetype)
        except Exception:
            mime = _OCTET

        def load(a=a):
            try:
                d = a.data
                if isinstance(d, (bytes, bytearray)):
                    return bytes(d)
                if d is not None and hasattr(d, 'exportBytes'):   # an attached message
                    return bytes(d.exportBytes())
            except Exception:
                pass
            return b''

        embedded = False
        try:
            embedded = hasattr(a.data, 'exportBytes')
        except Exception:
            pass
        if embedded:
            mime = 'application/vnd.ms-outlook'
            if not name.lower().endswith('.msg'):
                name = (name or 'message-%d' % (i + 1)) + '.msg'
        out.append((_safe_name(name, 'attachment-%d%s' % (i + 1, _MIME_EXT.get(mime, ''))), mime, load))
    return out


def _parse_msg(m):
    from email.utils import parseaddr
    sender = _one_line(_msg_attr(m, 'sender'), 500)
    name, addr = '', ''
    if sender:
        try:
            name, addr = parseaddr(sender)
        except Exception:
            name, addr = '', ''
        if '@' not in addr:              # an Exchange address: keep what Outlook shows
            name, addr = (name or sender), ''
    date = _msg_attr(m, 'date')
    if isinstance(date, str):
        try:
            from email.utils import parsedate_to_datetime
            date = parsedate_to_datetime(date)
        except Exception:
            date = None
    info = {
        'from': _fmt_addr(name, addr),
        'from_name': _one_line(name, 200),
        'from_email': _one_line(addr, 254),
        'to': _one_line(_msg_attr(m, 'to'), 8000),
        'cc': _one_line(_msg_attr(m, 'cc'), 8000),
        'date': _iso(date),
        'subject': _one_line(_msg_attr(m, 'subject'), 1000),
        'body_text': _msg_body(m),
        'attachments': [
            {'index': i, 'name': nm, 'size': len(load()), 'mime': mime}
            for i, (nm, mime, load) in enumerate(_msg_attachments(m))],
    }
    if not (info['from'] or info['to'] or info['subject'] or info['body_text']
            or info['attachments']):
        return None
    return info


def parse_email(data, filename):
    """An e-mail file → {from, from_name, from_email, to, cc, date (ISO or
    ''), subject, body_text, attachments: [{index, name, size, mime}]}.

    .eml through the standard library; Outlook .msg through extract_msg.
    None when the file is not an e-mail, cannot be read, or is a .msg and
    extract_msg is not installed. body_text is always plain text."""
    try:
        data = bytes(data or b'')
        if not data:
            return None
        if data[:8] == _OLE_MAGIC:
            m = _msg_open(data)
            if m is None:
                return None
            try:
                return _parse_msg(m)
            finally:
                try:
                    m.close()
                except Exception:
                    pass
        if _ext(filename) != 'eml' and not _looks_like_email(data):
            return None
        return _parse_eml(data)[0]
    except Exception:
        return None


def email_attachment(data, filename, index):
    """Attachment number `index` (as listed by parse_email) of an e-mail →
    (name, mime, bytes), or None. `mime` is what the SENDER claimed: sniff()
    the bytes before storing or serving them."""
    try:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return None
        data = bytes(data or b'')
        if not data:
            return None
        if data[:8] == _OLE_MAGIC:
            m = _msg_open(data)
            if m is None:
                return None
            try:
                atts = _msg_attachments(m)
                if index >= len(atts):
                    return None
                name, mime, load = atts[index]
                blob = load()
                return (name, mime, blob) if blob else None
            finally:
                try:
                    m.close()
                except Exception:
                    pass
        if _ext(filename) != 'eml' and not _looks_like_email(data):
            return None
        info, eml = _parse_eml(data)
        if info is None or index >= len(eml.attachments):
            return None
        name, mime, part = eml.attachments[index]
        blob = eml.part_bytes(part)
        return (name, mime, blob) if blob else None
    except Exception:
        return None


def _email_text(info):
    """'From … To … Cc … Subject …' then the body (what search reads)."""
    lines = []
    for label, key in (('From', 'from'), ('To', 'to'), ('Cc', 'cc'), ('Subject', 'subject')):
        if info.get(key):
            lines.append('%s: %s' % (label, info[key]))
    return '\n'.join(lines) + '\n\n' + (info.get('body_text') or '')


# ═════════════════════════════════════════════════════════════════════════════
# extract_text
# ═════════════════════════════════════════════════════════════════════════════
def _zip_within(data, max_member, max_total):
    """True when a zip's DECLARED contents are a sane size.

    The guard against a zip bomb dressed as a .docx / .xlsx: the central
    directory is read (cheap), nothing is inflated. Python's zipfile stops
    reading a member at its declared size, so a lying header cannot get past
    this either."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            infos = z.infolist()
            if len(infos) > _ZIP_MAX_MEMBERS:
                return False
            total = 0
            for zi in infos:
                if zi.file_size > max_member:
                    return False
                total += zi.file_size
                if total > max_total:
                    return False
        return True
    except Exception:
        return False


class _Out:
    """Collects extracted lines up to a character cap and a deadline."""

    def __init__(self, max_chars):
        self.max_chars = max_chars
        self.parts = []
        self.size = 0
        self.deadline = time.monotonic() + _TIME_BUDGET_S

    def add(self, s):
        if s:
            self.parts.append(s)
            self.size += len(s) + 1

    @property
    def full(self):
        return self.size >= self.max_chars or time.monotonic() > self.deadline

    def text(self):
        return '\n'.join(self.parts)


# ── PDF ──────────────────────────────────────────────────────────────────────
# Measured (pdfplumber 0.11 / pdfminer): ONE megabyte of dense page content
# costs about 4 s and 300 MB of memory, so a 6 KB PDF whose content stream
# inflates to 4 MB takes 20 s and 1.4 GB, and a 200 KB one took 135 s.
# PyMuPDF reads the same files in under a second. So:
#   * pdfplumber reads a PDF only when a scan of the raw file proves it LIGHT
#     (_pdf_is_light): nothing in it can unpack to more than _PLUMBER_MAX_DECODED.
#   * every other PDF goes to PyMuPDF, page by page, and a page whose content
#     would unpack past _FITZ_PAGE_MAX is skipped (_fitz_page_weight).
_PLUMBER_MAX_FILE = 8_000_000
_PLUMBER_MAX_DECODED = 1_000_000
_PLUMBER_MAX_STREAMS = 5000
_FITZ_PAGE_MAX = 8_000_000
_PLUMBER_MAX_OBJECTS = 20_000
# The `stream` keyword (never the tail of `endstream`); the data begins after
# the end of that line, which is where pdfminer starts reading too.
_PDF_STREAM_RE = re.compile(rb'(?<!end)stream[^\r\n]*(?:\r\n|\n|\r)')
_PDF_NAME_ESC = re.compile(rb'#([0-9A-Fa-f]{2})')
# pdfminer reads a stream's filters from /Filter or /F.
_PDF_FILTER_RE = re.compile(rb'/(?:Filter|F)\s*(\[[^\]]{0,300}\]|/[A-Za-z0-9]+|\d+\s+\d+\s+R)')
_PDF_FILTER_LIST = re.compile(rb'(?:\s*/[A-Za-z0-9]+)*\s*')
# Filters whose unpacked size the scan below can measure (Flate) or that
# pdfminer does not unpack at all (JPEG / JPEG 2000 image data).
_PDF_FILTERS_OK = frozenset((b'FlateDecode', b'Fl', b'DCTDecode', b'DCT', b'JPXDecode'))


def _inflated_size(buf, cap):
    """How many bytes the zlib stream at the start of `buf` unpacks to —
    counted only up to cap + 1, so a bomb costs `cap` bytes of work and not
    its full size — following Flate inside Flate up to four deep.
    -1 = not zlib data at all; cap + 1 also for a stream that starts as zlib
    and then breaks (its real size cannot be known, so it counts as too big)."""
    best = -1
    for _ in range(4):
        if len(buf) < 2 or (buf[0] & 0x0F) != 8 or ((buf[0] << 8) | buf[1]) % 31:
            break
        try:
            buf = zlib.decompressobj().decompress(buf, cap + 1)
        except Exception:
            return cap + 1
        best = max(best, len(buf))
        if best > cap:
            break
    return best


def _pdf_is_light(data):
    """True when nothing in this PDF can unpack to more than pdfplumber can
    afford. Every stream object is found in the RAW bytes (a stream can never
    hide inside a compressed object) and weighed: what it inflates to, or its
    raw size when it is not Flate data. Not light: an encrypted file, a
    filter the scan cannot weigh (LZW, RunLength, ASCII85, CCITT …), a filter
    named indirectly, too many objects or streams, or more than
    _PLUMBER_MAX_DECODED in total. 'Not light' costs nothing but the choice
    of reader."""
    try:
        if len(data) > _PLUMBER_MAX_FILE:
            return False
        norm = _PDF_NAME_ESC.sub(lambda m: bytes((int(m.group(1), 16),)), data)   # /Fl#61te… = /Flate…
        if b'/Encrypt' in norm or data.count(b'endobj') > _PLUMBER_MAX_OBJECTS:
            return False
        for m in _PDF_FILTER_RE.finditer(norm):
            val = m.group(1)
            if val[:1] not in b'[/':
                return False                         # "/Filter 7 0 R": cannot see it
            if val[:1] == b'[':
                if not _PDF_FILTER_LIST.fullmatch(val[1:-1]):
                    return False
                names = re.findall(rb'/([A-Za-z0-9]+)', val)
            else:
                names = [val[1:]]
            if any(n not in _PDF_FILTERS_OK for n in names):
                return False
        view = memoryview(data)
        total = count = 0
        for m in _PDF_STREAM_RE.finditer(data):
            count += 1
            if count > _PLUMBER_MAX_STREAMS:
                return False
            start = m.end()
            end = data.find(b'endstream', start)
            if end < 0:
                end = len(data)
            size = max(end - start, _inflated_size(view[start:end], _PLUMBER_MAX_DECODED))
            total += size
            if total > _PLUMBER_MAX_DECODED:
                return False
        return True
    except Exception:
        return False


def _pdf_plumber(data, out):
    """True when pdfplumber read the file (even if it held no text)."""
    pp = _lib('pdfplumber')
    if pp is None:
        return False
    _quiet_parsers()
    try:
        with pp.open(io.BytesIO(data), pages=list(range(1, _MAX_PDF_PAGES + 1))) as pdf:
            good = bad = 0
            for i, page in enumerate(pdf.pages):
                if i >= _MAX_PDF_PAGES or out.full:
                    break
                try:
                    out.add(page.extract_text() or '')
                    good += 1
                except Exception:
                    bad += 1             # one broken page must not lose the rest
                try:
                    page.flush_cache()   # pdfplumber keeps every page's objects otherwise
                except Exception:
                    pass
            return good > 0 or bad == 0
    except Exception:
        return False


def _fitz_page_weight(doc, page, cap):
    """Bytes of page content MuPDF would have to interpret for this page (its
    content streams and the form XObjects it draws), counted up to cap + 1.
    Images are not interpreted for text and are not counted."""
    xrefs = []
    try:
        xrefs += list(page.get_contents())[:500]
        xrefs += [x[0] for x in page.get_xobjects()[:2000]]
    except Exception:
        return cap + 1
    total = 0
    for xref in xrefs:
        try:
            raw = doc.xref_stream_raw(xref) or b''
            kind, val = doc.xref_get_key(xref, 'Filter')
        except Exception:
            return cap + 1
        if kind == 'null':
            size = len(raw)
        elif str(val).replace(' ', '') in ('/FlateDecode', '/Fl', '[/FlateDecode]', '[/Fl]'):
            size = max(len(raw), _inflated_size(raw, cap))
        else:
            size = len(raw) * 512        # LZW / RunLength / chains: assume the worst ratio
        total += size
        if total > cap:
            break
    return total


def _pdf_fitz(data, out):
    fz = _fitz()
    if fz is None:
        return
    with _FITZ_LOCK:
        try:
            try:
                fz.TOOLS.mupdf_display_errors(False)     # stderr would quote the file
                fz.TOOLS.mupdf_display_warnings(False)
            except Exception:
                pass
            doc = fz.open(stream=data, filetype='pdf')
            try:
                if getattr(doc, 'needs_pass', False):
                    return
                for i in range(min(doc.page_count, _MAX_PDF_PAGES)):
                    if out.full:
                        break
                    try:
                        page = doc.load_page(i)
                        if _fitz_page_weight(doc, page, _FITZ_PAGE_MAX) > _FITZ_PAGE_MAX:
                            continue     # a content bomb: this page has no text for us
                        out.add(page.get_text() or '')
                    except Exception:
                        pass
            finally:
                doc.close()
        except Exception:
            return


def _pdf_text(data, max_chars):
    out = _Out(max_chars)
    if _pdf_is_light(data) and _pdf_plumber(data, out):
        return out.text()
    # Heavy, encrypted, unreadable by pdfplumber, or pdfplumber is missing.
    out = _Out(max_chars)
    _pdf_fitz(data, out)
    return out.text()


def _docx_text(data, max_chars):
    docx = _lib('docx')
    if docx is None or not _zip_within(data, _DOCX_MAX_MEMBER, _DOCX_MAX_TOTAL):
        return ''
    out = _Out(max_chars)
    try:
        document = docx.Document(io.BytesIO(data))

        def table_rows(table, depth=0):
            for row in table.rows:
                if out.full:
                    return
                cells, last = [], None
                for cell in row.cells:
                    if cell._tc is last:             # a merged cell repeats
                        continue
                    last = cell._tc
                    cells.append(cell.text.strip())
                line = '\t'.join(cells)
                if line.strip():
                    out.add(line)

        try:
            blocks = document.iter_inner_content()       # paragraphs + tables in order
        except Exception:
            blocks = list(document.paragraphs) + list(document.tables)
        for block in blocks:
            if out.full:
                break
            if hasattr(block, 'rows'):
                table_rows(block)
            else:
                t = block.text
                if t and t.strip():
                    out.add(t)
    except Exception:
        pass
    return out.text()


def _cell(v):
    if v is None:
        return ''
    if isinstance(v, float):
        if v != v or v in (float('inf'), float('-inf')):
            return ''
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return repr(v)
    if isinstance(v, datetime.datetime):
        if (v.hour, v.minute, v.second, v.microsecond) == (0, 0, 0, 0):
            return v.date().isoformat()
        return v.replace(microsecond=0).isoformat(sep=' ')
    return str(v)


def _xlsx_text(data, max_chars):
    ox = _lib('openpyxl')
    if ox is None or not _zip_within(data, _XLSX_MAX_MEMBER, _XLSX_MAX_TOTAL):
        return ''
    out = _Out(max_chars)
    wb = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            # read_only streams each sheet: a huge one is never held in memory
            # and reading stops at _MAX_ROWS.
            wb = ox.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        cells = 0
        for ws in wb.worksheets[:_MAX_SHEETS]:
            if out.full or cells >= _MAX_CELLS:
                break
            out.add('[%s]' % _one_line(ws.title, 120))
            for row in ws.iter_rows(min_row=1, max_row=_MAX_ROWS, max_col=_MAX_COLS,
                                    values_only=True):
                if out.full or cells >= _MAX_CELLS:
                    break
                # read-only rows come padded with None up to max_col: only
                # real cells count toward the cap.
                vals = [_cell(c) for c in row]
                cells += sum(1 for v in vals if v)
                line = '\t'.join(vals).rstrip('\t')
                if line.strip():
                    out.add(line)
    except Exception:
        pass
    finally:
        try:
            if wb is not None:
                wb.close()
        except Exception:
            pass
    return out.text()


def _xls_text(data, max_chars):
    xl = _lib('xlrd')
    if xl is None:
        return ''
    out = _Out(max_chars)
    try:
        wb = xl.open_workbook(file_contents=data, on_demand=True, logfile=_Null(), verbosity=0)
        cells = 0
        for i in range(min(wb.nsheets, _MAX_SHEETS)):
            if out.full or cells >= _MAX_CELLS:
                break
            sh = wb.sheet_by_index(i)
            out.add('[%s]' % _one_line(sh.name, 120))
            for r in range(min(sh.nrows, _MAX_ROWS)):
                if out.full or cells >= _MAX_CELLS:
                    break
                n = min(sh.row_len(r), _MAX_COLS)
                cells += n
                line = '\t'.join(_cell(sh.cell_value(r, c)) for c in range(n)).rstrip('\t')
                if line.strip():
                    out.add(line)
            try:
                wb.unload_sheet(i)
            except Exception:
                pass
    except Exception:
        pass
    return out.text()


def _rtf_text(data, max_chars):
    mod = _lib('striprtf.striprtf')
    if mod is None or len(data) > _RTF_MAX_BYTES:
        return ''
    try:
        try:
            src = data.decode('utf-8')
        except UnicodeDecodeError:
            src = data.decode('cp1252', 'replace')
        return mod.rtf_to_text(src, errors='ignore') or ''
    except Exception:
        return ''


_PLAIN_EXTS = frozenset((
    'txt', 'text', 'csv', 'tsv', 'md', 'markdown', 'json', 'log', 'yaml',
    'yml', 'ini'))


def extract_text(data, filename, mime='', max_chars=300_000):
    """The readable text of a file, for search and preview. '' when the
    format is unsupported, the library for it is missing, or the file cannot
    be read — never an exception.

    pdf (pdfplumber for small plain files, PyMuPDF for everything else — see
    the PDF block above _pdf_is_light), docx (paragraphs and tables),
    rtf, xlsx / xls ('[sheet name]' then tab-separated rows), txt / csv / md /
    json, html (as text), eml and msg ('From … To … Cc … Subject …' then the
    body). The bytes decide which reader is used; the extension only picks
    between formats that share a container."""
    try:
        data = bytes(data or b'')
        max_chars = max(0, int(max_chars))
        if not data or not max_chars:
            return ''
        ext = _ext(filename)
        mime = str(mime or '').split(';', 1)[0].strip().lower()
        head = data[:8]
        if head.startswith(b'%PDF-') or (ext == 'pdf' and b'%PDF-' in data[:1024]):
            text = _pdf_text(data, max_chars)
        elif head.startswith(b'PK'):
            if ext == 'docx' or (ext != 'xlsx' and b'word/document' in data):
                text = _docx_text(data, max_chars)
            elif ext == 'xlsx' or b'xl/workbook' in data:
                text = _xlsx_text(data, max_chars)
            else:
                text = ''
        elif head == _OLE_MAGIC:
            if ext == 'xls':
                text = _xls_text(data, max_chars)
            elif ext == 'msg' or _u16('__substg1.0_') in data:
                info = parse_email(data, filename)
                text = _email_text(info) if info else ''
            else:
                text = ''                # legacy .doc / .ppt: no reader
        elif head.startswith(b'{\\rtf'):
            text = _rtf_text(data, max_chars)
        elif ext == 'eml' or mime == 'message/rfc822':
            info = parse_email(data, filename or 'message.eml')
            text = _email_text(info) if info else ''
        elif ext in ('html', 'htm') or mime == 'text/html':
            text = html_to_text(_decode_text(data, _HTML_TEXT_IN_MAX * 4), max_chars) \
                if _looks_text(data) else ''
        elif ext in _PLAIN_EXTS or (not ext and mime.startswith('text/')):
            text = _decode_text(data, max_chars * 4 + 16) if _looks_text(data) else ''
        else:
            text = ''
        return _clean(text, max_chars).strip()
    except Exception:
        return ''


# ═════════════════════════════════════════════════════════════════════════════
# html_to_docx
# ═════════════════════════════════════════════════════════════════════════════
# XML 1.0 cannot carry these at all (python-docx raises on them).
_XML_BAD = re.compile('[^\x09\x0a\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]')
_BOLD, _ITALIC, _UNDER, _STRIKE, _CODE = 1, 2, 4, 8, 16
_FMT_OF = {'strong': _BOLD, 'b': _BOLD, 'em': _ITALIC, 'i': _ITALIC,
           'u': _UNDER, 's': _STRIKE, 'code': _CODE}
_HEADING_STYLE = {'h2': 'Heading 1', 'h3': 'Heading 2', 'h4': 'Heading 3'}


def _xml_safe(s):
    return _XML_BAD.sub('', str(s or '').replace('\r\n', '\n').replace('\r', '\n'))


class _Node:
    __slots__ = ('tag', 'href', 'kids')

    def __init__(self, tag, href=''):
        self.tag, self.href, self.kids = tag, href, []


class _TreeBuilder(HTMLParser):
    """Sanitized (so: balanced, allow-listed) note HTML → a small tree."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node('')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs).get('href') or '' if tag == 'a' else '')
        self.stack[-1].kids.append(node)
        if tag not in _NOTE_VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].kids.append(data)


def _node_text(node):
    if isinstance(node, str):
        return node
    return ''.join(_node_text(k) for k in node.kids)


class _Blk:
    """Where the next paragraph goes and how it is styled. `style` is used by
    the first paragraph made with this context, `cont` by the ones after it
    (a list item's second paragraph must not get a second bullet)."""
    __slots__ = ('container', 'style', 'cont', 'num_id', 'pre', 'depth', 'tables', 'used')

    def __init__(self, container, style=None, cont=None, num_id=None, pre=False,
                 depth=0, tables=0):
        self.container, self.style, self.cont = container, style, cont
        self.num_id, self.pre, self.depth, self.tables = num_id, pre, depth, tables
        self.used = False

    def derive(self, **kw):
        base = dict(container=self.container, style=self.style, cont=self.cont,
                    num_id=None, pre=self.pre, depth=self.depth, tables=self.tables)
        base.update(kw)
        return _Blk(**base)


class _DocxWriter:
    """Writes the note tree into a python-docx document.

    python-docx's own add_paragraph / add_table / style setter / add_num each
    search the whole document (for the section properties, the default
    style, the highest numbering id), which made a 40 000-paragraph note take
    43 s and 5 000 numbered lists 140 s. The _fast_* helpers do the same
    work in constant time through the element tree; every one falls back to
    the public API if python-docx's internals ever differ."""

    def __init__(self, doc):
        self.doc = doc
        self.cur = None              # the open paragraph: [blk, pieces, force]
        self.first = {}              # id(cell) → its built-in first paragraph
        self.cells_left = _DOCX_MAX_TABLE_CELLS
        self._style_ids = {}         # style name → id (None = not in the template)
        self._abstract = {}          # list style name → its abstract numbering id
        self._next_num = None
        self._fast = None            # (OxmlElement, qn, Paragraph, body, sectPr) or False

    # constant-time building blocks -------------------------------------------
    def _tools(self):
        if self._fast is None:
            try:
                from docx.oxml import OxmlElement
                from docx.oxml.ns import qn
                from docx.text.paragraph import Paragraph
                body = self.doc.element.body
                self._fast = (OxmlElement, qn, Paragraph, body, body.find(qn('w:sectPr')))
            except Exception:
                self._fast = False
        return self._fast

    def _new_paragraph(self, container):
        para = self.first.pop(id(container), None)
        if para is not None:
            return para
        fast = self._tools()
        if fast and container is self.doc:
            try:
                el, _qn, paragraph, body, sect = fast
                p = el('w:p')
                if sect is not None:
                    sect.addprevious(p)          # body content sits before the section properties
                else:
                    body.append(p)
                return paragraph(p, self.doc._body)
            except Exception:
                pass
        return container.add_paragraph()

    def _style_id(self, name):
        if name not in self._style_ids:
            try:
                self._style_ids[name] = self.doc.styles[name].style_id
            except Exception:
                self._style_ids[name] = None         # a template without that style
        return self._style_ids[name]

    def _set_style(self, para, name):
        sid = self._style_id(name)
        if not sid:
            return
        try:
            para._p.get_or_add_pPr().get_or_add_pStyle().val = sid
        except Exception:
            try:
                para.style = name
            except Exception:
                pass

    # paragraphs ------------------------------------------------------------
    def _open(self, blk, force=False):
        self._close()
        self.cur = [blk, [], force]

    def _close(self):
        cur, self.cur = self.cur, None
        if cur is None:
            return
        blk, pieces, force = cur
        # Merge neighbours with the same formatting, then trim the paragraph.
        merged = []
        for text, fmt in pieces:
            if merged and merged[-1][1] == fmt:
                merged[-1][0] += text
            else:
                merged.append([text, fmt])
        if merged:
            merged[0][0] = merged[0][0].lstrip('\n' if blk.pre else ' \n')
            merged[-1][0] = merged[-1][0].rstrip(' \n')
        merged = [m for m in merged if m[0]]
        if not merged and not force:
            return
        style = blk.cont if blk.used else blk.style
        first_use, blk.used = not blk.used, True
        para = self._new_paragraph(blk.container)
        if style:
            self._set_style(para, style)
        if first_use and blk.num_id is not None:
            try:
                num_pr = para._p.get_or_add_pPr().get_or_add_numPr()
                num_pr.get_or_add_ilvl().val = 0
                num_pr.get_or_add_numId().val = blk.num_id
            except Exception:
                pass
        for text, fmt in merged:
            run = para.add_run(_xml_safe(text))
            if fmt & _BOLD:
                run.bold = True
            if fmt & _ITALIC:
                run.italic = True
            if fmt & _UNDER:
                run.underline = True
            if fmt & _STRIKE:
                run.font.strike = True
            if fmt & _CODE:
                run.font.name = 'Courier New'

    def _text(self, text, fmt, blk):
        if not blk.pre:
            text = _WS_RE.sub(' ', text)
        if self.cur is None:
            if not text.strip():
                return                   # whitespace between blocks
            self.cur = [blk, [], False]
        self.cur[1].append((text, fmt))

    # numbering -------------------------------------------------------------
    def _restart_numbering(self, style_name):
        """A fresh numbering instance for one <ol>, so every list starts at
        1 (python-docx's 'List Number' style otherwise counts on through the
        whole document). None when the template does not allow it — the
        list is then still numbered, just continuing."""
        try:
            numbering = self.doc.part.numbering_part.numbering_definitions._numbering
            if style_name not in self._abstract:
                base = self.doc.styles[style_name].element.pPr.numPr.numId.val
                self._abstract[style_name] = numbering.num_having_numId(base).abstractNumId.val
            fast = self._tools()
            if not fast:
                num = numbering.add_num(self._abstract[style_name])
                num.add_lvlOverride(ilvl=0).add_startOverride(1)
                return num.numId
            el, qn = fast[0], fast[1]
            if self._next_num is None:
                self._next_num = max([n.numId for n in numbering.num_lst] or [0]) + 1
            num_id, self._next_num = self._next_num, self._next_num + 1
            # <w:num w:numId><w:abstractNumId/><w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/>
            num = el('w:num')
            num.set(qn('w:numId'), str(num_id))
            ref = el('w:abstractNumId')
            ref.set(qn('w:val'), str(self._abstract[style_name]))
            over = el('w:lvlOverride')
            over.set(qn('w:ilvl'), '0')
            start = el('w:startOverride')
            start.set(qn('w:val'), '1')
            over.append(start)
            num.append(ref)
            num.append(over)
            tail = numbering.find(qn('w:numIdMacAtCleanup'))     # must stay last
            if tail is not None:
                tail.addprevious(num)
            else:
                numbering.append(num)
            return num_id
        except Exception:
            return None

    # the walk --------------------------------------------------------------
    def walk(self, nodes, fmt, blk):
        for n in nodes:
            if isinstance(n, str):
                self._text(n, fmt, blk)
                continue
            tag = n.tag
            if tag == 'br':
                if self.cur is not None:
                    self.cur[1].append(('\n', fmt))
            elif tag in _FMT_OF:
                self.walk(n.kids, fmt | _FMT_OF[tag], blk)
            elif tag == 'a':
                # Links are written as text: the words, then the address.
                self.walk(n.kids, fmt, blk)
                shown = _node_text(n)
                href = n.href[7:] if n.href.startswith('mailto:') else n.href
                if href and href not in shown:
                    self._text((' (%s)' if shown.strip() else '%s') % href, fmt, blk)
            elif tag == 'p':
                self._open(blk, force=True)
                self.walk(n.kids, fmt, blk)
                self._close()
            elif tag in _HEADING_STYLE:
                self._open(blk.derive(style=_HEADING_STYLE[tag], cont=blk.cont))
                self.walk(n.kids, fmt, self.cur[0])
                self._close()
            elif tag == 'blockquote':
                self._close()
                self.walk(n.kids, fmt, blk.derive(style='Quote', cont='Quote'))
                self._close()
            elif tag == 'pre':
                self._open(blk.derive(pre=True))
                self.walk(n.kids, fmt | _CODE, self.cur[0])
                self._close()
            elif tag in ('ul', 'ol'):
                self._list(n, fmt, blk)
            elif tag == 'li':                        # outside any list
                self._item(n, fmt, blk, 'List Bullet', 'List Continue', None, max(1, blk.depth))
            elif tag == 'table':
                self._close()
                self._table(n, fmt, blk)
            elif tag == 'hr':
                self._close()
                self._rule(blk)
            else:                                    # stray thead / tbody / tr / td
                if tag == 'tr':
                    self._close()
                self.walk(n.kids, fmt, blk)
                if tag == 'tr':
                    self._close()
                elif tag in ('td', 'th') and self.cur is not None:
                    self.cur[1].append(('\t', fmt))

    def _list(self, node, fmt, blk):
        self._close()
        depth = blk.depth + 1
        suffix = '' if depth == 1 else ' %d' % min(depth, 3)
        style = ('List Number' if node.tag == 'ol' else 'List Bullet') + suffix
        cont = 'List Continue' + suffix
        num_id = self._restart_numbering(style) if node.tag == 'ol' else None
        for kid in node.kids:
            if isinstance(kid, _Node) and kid.tag == 'li':
                self._item(kid, fmt, blk, style, cont, num_id, depth)
            else:
                self.walk([kid], fmt, blk.derive(style=cont, cont=cont, depth=depth))
        self._close()

    def _item(self, node, fmt, blk, style, cont, num_id, depth):
        item = blk.derive(style=style, cont=cont, num_id=num_id, depth=depth)
        self._open(item)
        self.walk(node.kids, fmt, item)
        self._close()

    def _rule(self, blk):
        para = self._new_paragraph(blk.container)
        try:
            from docx.oxml import OxmlElement
            from docx.oxml.ns import qn
            borders = OxmlElement('w:pBdr')
            bottom = OxmlElement('w:bottom')
            for k, v in (('w:val', 'single'), ('w:sz', '6'), ('w:space', '1'), ('w:color', '999999')):
                bottom.set(qn(k), v)
            borders.append(bottom)
            para._p.get_or_add_pPr().append(borders)
        except Exception:
            para.add_run('────────────')

    def _new_table(self, container, nrows, ncols):
        fast = self._tools()
        table = None
        if fast and container is self.doc and fast[4] is not None:
            try:
                from docx.oxml.table import CT_Tbl
                from docx.table import Table
                tbl = CT_Tbl.new_tbl(nrows, ncols, self.doc._block_width)
                fast[4].addprevious(tbl)
                table = Table(tbl, self.doc._body)
            except Exception:
                table = None
        if table is None:
            table = container.add_table(rows=nrows, cols=ncols)
        try:
            sid = self._style_id('Table Grid')
            if sid:
                table._tbl.tblPr.style = sid
        except Exception:
            try:
                table.style = 'Table Grid'
            except Exception:
                pass
        return table

    def _table(self, node, fmt, blk):
        rows = []

        def collect(n):
            for kid in n.kids:
                if isinstance(kid, _Node):
                    if kid.tag == 'tr':
                        rows.append([c for c in kid.kids
                                     if isinstance(c, _Node) and c.tag in ('td', 'th')])
                    elif kid.tag in ('thead', 'tbody'):
                        collect(kid)
        collect(node)
        rows = [r for r in rows if r]
        cols = max((len(r) for r in rows), default=0)
        if not rows:
            self.walk(node.kids, fmt, blk)           # no real rows: keep the text
            self._close()
            return
        cells = len(rows) * cols
        if cols > 63 or cells > self.cells_left or blk.tables >= 3:
            # Too big for a Word table (or nested too deep): every row is
            # still written, as one tab-separated line. Nothing is dropped.
            for r in rows:
                line = '\t'.join(_WS_RE.sub(' ', _node_text(c)).strip() for c in r)
                self._open(blk)
                self.cur[1].append((line, fmt))      # as is: _text would fold the tabs
                self._close()
            return
        self.cells_left -= cells
        table = self._new_table(blk.container, len(rows), cols)
        for row, trow in zip(rows, table.rows):      # (indexing table.rows is a scan per row)
            tcells = trow.cells
            for c, cellnode in enumerate(row):
                cell = tcells[c]
                self.first[id(cell)] = cell.paragraphs[0]
                cblk = _Blk(cell, depth=0, tables=blk.tables + 1)
                self.walk(cellnode.kids, fmt | (_BOLD if cellnode.tag == 'th' else 0), cblk)
                self._close()
                self.first.pop(id(cell), None)


def _docx_bytes(docx, title, html, rich=True):
    doc = docx.Document()
    title = _xml_safe(_one_line(title, 200))
    try:
        doc.core_properties.title = title
        doc.core_properties.author = ''
    except Exception:
        pass
    if title:
        doc.add_heading(title, level=0)
    writer = _DocxWriter(doc)
    root = _Blk(doc)
    if rich:
        tb = _TreeBuilder()
        tb.feed(sanitize_note_html(html))            # balanced + allow-listed
        tb.close()
        writer.walk(tb.root.kids, 0, root)
        writer._close()
    else:
        for para in html_to_text(html, NOTE_HTML_MAX * 2).split('\n'):
            if para.strip():
                doc.add_paragraph(_xml_safe(para))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def html_to_docx(title, html):
    """A note as a Word file (bytes): the title, then headings, paragraphs,
    bold / italic / underline / strike, bulleted and numbered lists, quotes,
    code, tables; links are written as text with their address.

    The HTML goes through sanitize_note_html first, so only allow-listed
    structure is ever read. b'' when python-docx is not installed or the
    document cannot be built."""
    docx = _lib('docx')
    if docx is None:
        return b''
    html = '' if html is None else html
    try:
        return _docx_bytes(docx, title, html, rich=True)
    except Exception:
        pass
    try:                                 # the words still matter more than the layout
        return _docx_bytes(docx, title, html, rich=False)
    except Exception:
        return b''


# ═════════════════════════════════════════════════════════════════════════════
# Self-test: `python3 project_docs.py`
# ═════════════════════════════════════════════════════════════════════════════
def _selftest():
    """Fast asserts on in-memory fixtures. Formats whose library is not
    installed are skipped (and named), because that is a supported state."""
    skipped = []
    S = sanitize_note_html

    # -- sanitizer ----------------------------------------------------------
    assert S(None) == '' and S('') == ''
    assert S('<p>Hi <b>there</b></p>') == '<p>Hi <b>there</b></p>'
    assert S('<script>alert(1)</script>ok') == 'ok'
    assert S('<p onclick="x()" style="color:red" class="c">t</p>') == '<p>t</p>'
    assert S('<img src=x onerror=alert(1)>') == ''
    assert S('<svg><script>alert(1)</script></svg>after') == 'after'
    assert S('<a href="javascript:alert(1)">x</a>') == 'x'
    assert S('<a href="JaVa&#9;ScRiPt:alert(1)">x</a>') == 'x'
    assert S('<a href="&#106;avascript:alert(1)">x</a>') == 'x'
    assert S('<a href="data:text/html,<script>1</script>">x</a>') == 'x'
    assert S('<a href="/relative">x</a>') == 'x'
    assert S('<a href="HTTPS://Example.com/a?b=1&c=2" onclick=1>x</a>') == \
        '<a href="https://Example.com/a?b=1&amp;c=2" rel="noopener noreferrer" target="_blank">x</a>'
    assert S('<a href="mailto:a@example.com">m</a>') == \
        '<a href="mailto:a@example.com" rel="noopener noreferrer" target="_blank">m</a>'
    assert S('<b><i>x</b>y') == '<b><i>x</i></b>y'
    assert S('<p>open') == '<p>open</p>'
    assert S('</p>x<!-- c --><![CDATA[z]]>') == 'x'
    assert S('<div>one</div><div>two</div>') == 'one<br>two<br>'
    assert S('<div><p>x</p></div>') == '<p>x</p>'
    assert S('a\x00b<scr\x00ipt>1</script>') == 'ab'
    assert S('1 < 2 & 3 > 2') == '1 &lt; 2 &amp; 3 &gt; 2'
    assert S('<table><tr><td>c</td></tr></table>') == '<table><tr><td>c</td></tr></table>'
    for sample in ('<p>Hi <b>there</b></p>', '<div>one</div>two<h1>T</h1>', '<b><i>x</b>y<a href="http://a/?x=&lt;">l',
                   '&amp;lt;script&gt; &lt;b&gt;', '<ul><li>a<li>b</ul><pre>\n x\n</pre>'):
        once = S(sample)
        assert S(once) == once, sample
    try:
        S('x' * (NOTE_HTML_MAX + 1))
        raise AssertionError('expected ValueError')
    except ValueError:
        pass
    try:                                     # grows past the cap when escaped
        S('&' * NOTE_HTML_MAX)
        raise AssertionError('expected ValueError')
    except ValueError:
        pass
    assert len(S('x' * NOTE_HTML_MAX)) == NOTE_HTML_MAX

    # -- html_to_text -------------------------------------------------------
    t = html_to_text('<h2>Title</h2><p>One&nbsp;two<br>three</p><ul><li>a</li><li>b</li></ul>'
                     '<script>bad()</script><style>p{}</style><a href="https://example.com/x">link</a>')
    assert t == 'Title\n\nOne two\nthree\n\n- a\n\n- b\n\nlink (https://example.com/x)', repr(t)
    assert html_to_text(None) == '' and '<' not in html_to_text('<b>x</b><i>y</i>')
    assert html_to_text('<ol><li>a</li><li>b</li></ol>') == '1. a\n\n2. b'

    # -- sniff --------------------------------------------------------------
    assert sniff(b'%PDF-1.7\n...', 'a.txt', 'text/plain') == {'kind': 'pdf', 'mime': 'application/pdf'}
    assert sniff(b'\x89PNG\r\n\x1a\n' + b'\x00' * 20, 'a.png')['mime'] == 'image/png'
    assert sniff(b'\xff\xd8\xff\xe0' + b'\x00' * 20, 'a')['kind'] == 'image'
    assert sniff(b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>', 'a.svg', 'image/svg+xml') == _OTHER
    assert sniff(b'<html><script>1</script>', 'a.html', 'text/plain') == _OTHER
    assert sniff(b'<html><script>1</script>', 'a.txt', 'text/plain') == _OTHER
    assert sniff(b'\x89PNG\r\n\x1a\n' + b'\x00' * 20, 'a.html', 'image/png') == _OTHER
    assert sniff(b'<?xml version="1.0"?><a/>', 'a.xml', 'application/pdf') == _OTHER
    assert sniff(b'hello, world\n', 'a.txt') == {'kind': 'text', 'mime': 'text/plain'}
    assert sniff(b'a,b\n1,2\n', 'a.csv') == {'kind': 'text', 'mime': 'text/csv'}
    assert sniff(b'hello', 'noext', 'text/plain')['kind'] == 'text'
    assert sniff(b'hello', 'a.exe', 'text/plain') == _OTHER
    assert sniff(b'\x00\x01\x02binary', 'a.txt') == _OTHER
    assert sniff(b'', 'a.pdf') == _OTHER
    ole = _OLE_MAGIC + b'\x00' * 600
    assert sniff(ole + _u16('__substg1.0_0037001F'), 'm.msg') == {'kind': 'email', 'mime': 'application/vnd.ms-outlook'}
    assert sniff(ole + _u16('WordDocument'), 'd.doc') == {'kind': 'office', 'mime': 'application/msword'}
    assert sniff(ole + _u16('Workbook'), 's.xls')['mime'] == 'application/vnd.ms-excel'
    assert sniff(ole + _u16('WordDocument'), 'd.msg') == _OTHER      # renamed
    assert sniff(ole, 'd.bin') == _OTHER
    assert sniff(b'PK\x03\x04' + b'\x00' * 26 + b'word/document.xml', 'd.docx')['kind'] == 'office'
    assert sniff(b'PK\x03\x04' + b'\x00' * 26 + b'xl/workbook.xml', 'd.docx') == _OTHER
    assert sniff(b'PK\x03\x04' + b'\x00' * 26 + b'anything', 'd.zip') == _OTHER
    assert sniff(b'{\\rtf1 hi}', 'd.rtf') == {'kind': 'office', 'mime': 'application/rtf'}

    # -- e-mail (.eml, standard library only) -------------------------------
    from email.message import EmailMessage
    m = EmailMessage()
    m['From'] = 'Ana Müller <ana@example.com>'
    m['To'] = 'Bo <bo@example.com>, cy@example.com'
    m['Cc'] = 'Di <di@example.com>'
    m['Subject'] = 'Quote für die Bühne'
    m['Date'] = 'Mon, 02 Mar 2026 09:30:00 -0500'
    m.set_content('Plain body here.\n')
    m.add_alternative('<p>HTML <b>body</b></p>', subtype='html')
    m.add_attachment(b'%PDF-1.4 fake', maintype='application', subtype='pdf',
                     filename='../../etc/pass\x07wd quote.pdf')
    inner = EmailMessage()
    inner['From'] = 'x@example.com'
    inner['Subject'] = 'Inner note'
    inner.set_content('inner body')
    m.add_attachment(inner)
    raw = m.as_bytes()
    info = parse_email(raw, 'mail.eml')
    assert info and info['from_name'] == 'Ana Müller' and info['from_email'] == 'ana@example.com', info
    assert info['from'] == 'Ana Müller <ana@example.com>'
    assert info['to'] == 'Bo <bo@example.com>, cy@example.com' and info['cc'] == 'Di <di@example.com>'
    assert info['subject'] == 'Quote für die Bühne' and info['date'] == '2026-03-02T09:30:00-05:00'
    assert info['body_text'].strip() == 'Plain body here.'
    names = [a['name'] for a in info['attachments']]
    assert names == ['passwd quote.pdf', 'Inner note.eml'], names
    assert [a['index'] for a in info['attachments']] == [0, 1]
    assert info['attachments'][0]['size'] == 13 and info['attachments'][0]['mime'] == 'application/pdf'
    att = email_attachment(raw, 'mail.eml', 0)
    assert att == ('passwd quote.pdf', 'application/pdf', b'%PDF-1.4 fake'), att
    att = email_attachment(raw, 'mail.eml', 1)
    assert att and att[1] == 'message/rfc822' and b'inner body' in att[2]
    assert parse_email(att[2], att[0])['subject'] == 'Inner note'
    assert email_attachment(raw, 'mail.eml', 2) is None and email_attachment(raw, 'mail.eml', -1) is None
    assert email_attachment(raw, 'mail.eml', 'x') is None
    h = EmailMessage()
    h['From'] = 'a@example.com'
    h['Subject'] = 'html only'
    h.set_content('<div>Line one</div><div>Line <i>two</i><script>x()</script></div>', subtype='html')
    hinfo = parse_email(h.as_bytes(), 'h.eml')
    assert hinfo['body_text'] == 'Line one\n\nLine two', repr(hinfo['body_text'])
    assert parse_email(b'just some text, no headers', 'x.eml') is None
    assert parse_email(b'', 'x.eml') is None and parse_email(_OLE_MAGIC, 'x.msg') is None
    assert sniff(raw, 'mail.eml') == {'kind': 'email', 'mime': 'message/rfc822'}
    text = extract_text(raw, 'mail.eml')
    assert text.startswith('From: Ana Müller <ana@example.com>\nTo: Bo <bo@example.com>, cy@example.com\n'
                           'Cc: Di <di@example.com>\nSubject: Quote für die Bühne\n\nPlain body here.'), text
    assert isinstance(has_msg_support(), bool)

    # -- extract_text -------------------------------------------------------
    assert extract_text('héllo wörld\x0c'.encode('utf-8'), 'a.txt') == 'héllo wörld'
    assert _clean('a\x00b\x07c' + chr(0xD800) + 'd\r\ne') == 'abcd\ne'   # NUL, control, lone surrogate
    assert extract_text('with\x00nul'.encode('utf-8'), 'a.txt') == ''     # a NUL byte = not a text file
    assert extract_text(b'a,b\r\n1,2\r\n', 'a.csv') == 'a,b\n1,2'
    assert extract_text(codecs.BOM_UTF16_LE + 'ünï'.encode('utf-16-le'), 'a.txt') == 'ünï'
    assert extract_text(b'caf\xe9', 'a.txt') == 'café'                # cp1252 fallback
    assert extract_text(b'x' * 50, 'a.txt', max_chars=10) == 'x' * 10
    assert extract_text(b'\x00\x01\x02\x03', 'a.bin') == '' and extract_text(b'', 'a.txt') == ''
    assert extract_text(b'<p>Hi<script>x</script></p>', 'a.html') == 'Hi'
    assert extract_text(b'not a pdf', 'a.pdf') == '' and extract_text(b'PK\x03\x04junk', 'a.docx') == ''
    assert extract_text(_OLE_MAGIC + b'junk' * 500, 'a.xls') == ''
    if _lib('striprtf.striprtf'):
        assert extract_text(br'{\rtf1\ansi Hello \b bold\b0  world.\par Second line.}', 'a.rtf') \
            == 'Hello bold world.\nSecond line.'
    else:
        skipped.append('striprtf')
    if _lib('docx'):
        import docx as _docx
        d = _docx.Document()
        d.add_paragraph('Docx paragraph one')
        tbl = d.add_table(rows=1, cols=2)
        tbl.rows[0].cells[0].text, tbl.rows[0].cells[1].text = 'c1', 'c2'
        buf = io.BytesIO()
        d.save(buf)
        assert extract_text(buf.getvalue(), 'a.docx') == 'Docx paragraph one\nc1\tc2'
        assert sniff(buf.getvalue(), 'a.docx')['kind'] == 'office'
        # html_to_docx: must re-open, and the words must survive the trip
        out = html_to_docx('My note', '<h2>Head</h2><p>Para <b>bold</b> <i>it</i> <u>un</u></p>'
                           '<ul><li>one</li><li>two</li></ul><ol><li>first</li></ol>'
                           '<table><tr><th>H</th><td>D</td></tr></table>'
                           '<p><a href="https://example.com/">site</a></p><script>no()</script>')
        back = _docx.Document(io.BytesIO(out))
        paras = [p.text for p in back.paragraphs]
        assert paras == ['My note', 'Head', 'Para bold it un', 'one', 'two', 'first',
                         'site (https://example.com/)'], paras
        assert [p.style.name for p in back.paragraphs][:6] == \
            ['Title', 'Heading 1', 'Normal', 'List Bullet', 'List Bullet', 'List Number']
        assert [c.text for c in back.tables[0].rows[0].cells] == ['H', 'D']
        runs = {r.text: (r.bold, r.italic, r.underline) for r in back.paragraphs[2].runs}
        assert runs['bold'][0] and runs['it'][1] and runs['un'][2]
        assert html_to_docx('', '') and html_to_docx(None, None)
    else:
        skipped.append('python-docx')
        assert html_to_docx('t', '<p>x</p>') == b''
    if _lib('openpyxl'):
        import openpyxl as _ox
        wb = _ox.Workbook()
        ws = wb.active
        ws.title = 'Budget'
        ws.append(['Item', 'Cost'])
        ws.append(['Truss', 1200])
        ws.append([None, 3.5])
        buf = io.BytesIO()
        wb.save(buf)
        assert extract_text(buf.getvalue(), 'a.xlsx') == '[Budget]\nItem\tCost\nTruss\t1200\n\t3.5'
    else:
        skipped.append('openpyxl')
    fz = _fitz()
    if fz is not None and _lib('pdfplumber'):
        doc = fz.open()
        doc.new_page().insert_text((72, 72), 'Hello PDF world')
        pdf = doc.tobytes()
        doc.close()
        assert extract_text(pdf, 'a.pdf') == 'Hello PDF world'
        assert sniff(pdf, 'whatever.bin')['kind'] == 'pdf'
        out = _Out(1000)
        _pdf_fitz(pdf, out)
        assert out.text().strip() == 'Hello PDF world'
    else:
        skipped.append('pdf (PyMuPDF + pdfplumber)')
    if not has_msg_support():
        skipped.append('extract_msg')
    print('OK' + (' (skipped: %s)' % ', '.join(skipped) if skipped else ''))


if __name__ == '__main__':
    _selftest()
