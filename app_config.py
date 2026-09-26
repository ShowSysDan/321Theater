# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""
Per-install server configuration: the app's `.env` file (3.4.0).

Everything that belongs to THIS MACHINE rather than to the (possibly shared)
database lives in one file, `.env` in the app directory (override the path
with THEATER_ENV_FILE): the PostgreSQL connection (PG_*), S3 storage (S3_*),
SECRET_KEY, the gateway/proxy settings, and TEST_MODE. See .env.example.

Lookup order for every key (an empty value counts as missing):
  1. the process environment — systemd's `EnvironmentFile=` loads .env there
  2. the .env file itself, read directly — so CLI runs (init_db.py,
     install.sh, `python3 app.py`) see the same values as the service
  3. for the database / S3 keys only: the legacy db_config.ini
     ([postgresql] / [seaweedfs]), key by key. Deprecated but still honoured
     so a running install keeps working while its values are ported; every
     key it supplies is logged once per process (CONFIG_LEGACY).

`python3 app_config.py` prints where each setting currently comes from
(secrets masked); `python3 app_config.py --export` prints the .env lines for
the values still coming only from db_config.ini, ready to append.

This module must stay dependency-free (db_adapter and s3_storage import it).
"""
import configparser
import logging
import os
import sys
import threading

_log = logging.getLogger('showadvance')

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.environ.get('THEATER_ENV_FILE') or os.path.join(APP_DIR, '.env')

_TRUE = ('1', 'true', 'yes', 'on')

# Source labels returned by lookup() / shown by the CLI.
SRC_ENVIRON = 'environment'
SRC_ENV_FILE = '.env'
SRC_INI = 'db_config.ini'
SRC_DEFAULT = 'default'


# ─── .env file ────────────────────────────────────────────────────────────────
# Parsed on first use and re-parsed only when the file's mtime/size changes,
# so calling get() on a hot path costs one os.stat().
_env_cache = {'key': None, 'values': {}}
_env_lock = threading.Lock()


def _parse_env_text(text):
    """KEY=value lines, parsed the way systemd's EnvironmentFile= parses them
    (so the service and the CLI tools always see the same value): blank lines
    and lines starting with # or ; are skipped, an optional `export ` prefix
    is allowed. A value wholly in single quotes is literal; in double quotes,
    backslash escapes only " \\ ` $; unquoted, a backslash escapes the next
    character (and is dropped). No inline comments and no $VAR expansion, so
    a password containing # or $ works unquoted."""
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in '#;':
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, sep, val = line.partition('=')
        key = key.strip()
        if not sep or not key:
            continue
        out[key] = _unquote(val.strip())
    return out


def _unquote(val):
    if len(val) >= 2 and val[0] == val[-1] == "'":
        return val[1:-1]
    if len(val) >= 2 and val[0] == val[-1] == '"':
        body, res, i = val[1:-1], [], 0
        while i < len(body):
            if body[i] == '\\' and i + 1 < len(body) and body[i + 1] in '"\\`$':
                res.append(body[i + 1])
                i += 2
            else:
                res.append(body[i])
                i += 1
        return ''.join(res)
    res, i = [], 0
    while i < len(val):
        if val[i] == '\\' and i + 1 < len(val):
            res.append(val[i + 1])
            i += 2
        else:
            res.append(val[i])
            i += 1
    return ''.join(res)


def env_quote(val):
    """A value written so _parse_env_text / systemd read it back unchanged."""
    if val and not any(c in val for c in '\\\'"') and val == val.strip():
        return val
    if "'" not in val:
        return f"'{val}'"
    return '"' + val.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _env_file_values():
    try:
        st = os.stat(ENV_PATH)
    except OSError:
        return {}
    cache_key = (st.st_mtime_ns, st.st_size)
    with _env_lock:
        if _env_cache['key'] == cache_key:
            return _env_cache['values']
        try:
            with open(ENV_PATH, encoding='utf-8') as f:
                values = _parse_env_text(f.read())
        except Exception as e:
            _log.error(f'CONFIG_ERROR could not read {ENV_PATH}: {e}')
            values = {}
        _env_cache['key'] = cache_key
        _env_cache['values'] = values
        return values


def lookup(key):
    """(value, source) for a .env key; (None, None) when unset or empty."""
    val = os.environ.get(key)
    if val:
        return val, SRC_ENVIRON
    val = _env_file_values().get(key)
    if val:
        return val, SRC_ENV_FILE
    return None, None


def get(key, default=''):
    """A .env value (process environment first, then the file itself)."""
    val, _ = lookup(key)
    return default if val is None else val


def get_bool(key, default=False):
    val, _ = lookup(key)
    if val is None:
        return default
    return val.strip().lower() in _TRUE


# ─── Legacy db_config.ini fallback ────────────────────────────────────────────
def read_ini_section(path, section):
    """The given ini section as a plain dict, or None when the file or the
    section doesn't exist. Raises on a malformed file."""
    if not path or not os.path.exists(path):
        return None
    cp = configparser.ConfigParser()
    cp.read(path, encoding='utf-8')
    return dict(cp[section]) if section in cp else None


def layered(spec, ini_path, ini_section):
    """Resolve a group of settings: .env first, then db_config.ini key by key.

    spec: iterable of (out_key, env_key, ini_keys, default); ini_keys is one
    ini key or a tuple of them (first non-empty wins — for renamed keys).
    Returns (values, sources, present). `values` has every out_key (falling
    back to the default); `sources` maps out_key → SRC_*; `present` is True
    when ANY key came from the environment/.env or the ini section exists at
    all (i.e. the group is configured somewhere)."""
    try:
        ini = read_ini_section(ini_path, ini_section)
    except Exception as e:
        _log.error(f'CONFIG_ERROR {ini_path} could not be parsed: {e}')
        ini = None
    values, sources, from_env, from_ini = {}, {}, False, []
    for out_key, env_key, ini_keys, default in spec:
        val, src = lookup(env_key)
        if val is not None:
            from_env = True
        else:
            if isinstance(ini_keys, str):
                ini_keys = (ini_keys,)
            for k in ini_keys:
                v = ((ini or {}).get(k) or '').strip()
                if v:
                    val, src = v, SRC_INI
                    from_ini.append(env_key)
                    break
            else:
                val, src = default, SRC_DEFAULT
        values[out_key] = val
        sources[out_key] = src
    if from_ini:
        warn_legacy(ini_section, from_ini)
    return values, sources, (from_env or ini is not None)


_legacy_warned = set()


def warn_legacy(section, env_keys):
    """Log once per process which values still come from db_config.ini."""
    sig = (section, tuple(env_keys))
    if sig in _legacy_warned:
        return
    _legacy_warned.add(sig)
    _log.warning(
        f'CONFIG_LEGACY db_config.ini [{section}] still supplies '
        f'{", ".join(env_keys)} — move them to {ENV_PATH} '
        f'(python3 app_config.py --export prints the lines). '
        f'db_config.ini keeps working until then.')


# ─── Test mode ────────────────────────────────────────────────────────────────
# TEST_MODE=1 marks a test/staging install. Read ONCE at import (it's a fact
# about the machine; changing it needs a restart, like every .env value under
# systemd). It lives in .env and never in app_settings on purpose: a test
# server often points at a copy of — or the same — database as production,
# and a DB flag would travel with the data. What it switches off is enforced
# in app.py (search TEST_MODE): cluster membership + leadership, email to
# anyone but admins, and every leader-only background job; syslog lines and
# email subjects are tagged [TEST INSTANCE].
TEST_MODE = get_bool('TEST_MODE')


def test_email_allowlist():
    """Extra addresses (lower-case) that may receive mail in TEST_MODE on top
    of the app's admin accounts (app.py adds those), from
    TEST_MODE_EMAIL_ALLOWLIST (comma/space separated). Exact addresses only —
    no domains or wildcards, so a copied contact list can never match."""
    raw = get('TEST_MODE_EMAIL_ALLOWLIST', '')
    return {a.strip().lower() for a in raw.replace(';', ',').replace(' ', ',').split(',')
            if '@' in a}


# ─── CLI: show where settings come from / export legacy values ────────────────
_SECRET_HINTS = ('PASSWORD', 'SECRET', 'KEY')


def _mask(env_key, val):
    if not val or not any(h in env_key for h in _SECRET_HINTS):
        return val
    return '********'


def _main(argv):
    # The report below shows every db_config.ini source itself; keep the
    # one-time CONFIG_LEGACY warning off the terminal.
    _log.addHandler(logging.NullHandler())
    _log.propagate = False
    import db_adapter
    import s3_storage
    groups = [
        ('PostgreSQL', db_adapter.CONFIG_PATH, 'postgresql', db_adapter.PG_SETTINGS_SPEC),
        ('S3 storage', s3_storage._CONFIG_PATH, 'seaweedfs', s3_storage.S3_SETTINGS_SPEC),
    ]
    if '--export' in argv:
        lines = []
        for title, path, section, spec in groups:
            values, sources, _ = layered(spec, path, section)
            todo = [(env_key, values[out_key]) for out_key, env_key, _ini, _default in spec
                    if sources[out_key] == SRC_INI]
            if todo:
                lines.append(f'# {title} (ported from {os.path.basename(path)} [{section}])')
                lines.extend(f'{k}={env_quote(v)}' for k, v in todo)
        if lines:
            print('\n'.join(lines))
        else:
            print('# Nothing to port: no value is coming from db_config.ini.', file=sys.stderr)
        return 0
    print(f'.env file:     {ENV_PATH} ({"found" if os.path.exists(ENV_PATH) else "MISSING"})')
    print(f'db_config.ini: {db_adapter.CONFIG_PATH} '
          f'({"found — legacy fallback" if os.path.exists(db_adapter.CONFIG_PATH) else "not present"})')
    print(f'TEST_MODE:     {"ON" if TEST_MODE else "off"}')
    for title, path, section, spec in groups:
        values, sources, present = layered(spec, path, section)
        print(f'\n{title}{"" if present else "  (not configured)"}')
        for out_key, env_key, _ini_key, _default in spec:
            shown = _mask(env_key, values[out_key]) or (
                '(built-in default)' if sources[out_key] == SRC_DEFAULT else '(empty)')
            print(f'  {env_key:<18} {shown:<32} ← {sources[out_key]}')
    print('\nOther')
    for key in ('SECRET_KEY', 'TEST_MODE_EMAIL_ALLOWLIST', 'SESSION_COOKIE_SECURE',
                'TRUSTED_PROXY_IPS', 'GATEWAY_SHARED_SECRET', 'GATEWAY_PEER_IPS'):
        val, src = lookup(key)
        if key == 'SECRET_KEY' or val is not None:
            shown = _mask(key, val) if val is not None else '(unset — sessions reset on restart)'
            print(f'  {key:<26} {shown:<32} ← {src or "-"}')
    return 0


if __name__ == '__main__':
    sys.exit(_main(sys.argv[1:]))
