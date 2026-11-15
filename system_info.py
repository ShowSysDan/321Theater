# 3·2·1→Theater
# © 2026 Dr. Phillips Center for the Performing Arts; portions © 2026 Thauma Systems, LLC.
# MIT Licensed — see LICENSE for details.
"""Facts about THIS server for Settings → About 321Theater (3.50.0).

Dependency-free and never imports app.py (app.py calls it and caches the
answers). Every function NEVER raises: anything it cannot find out comes back
as None / '' / an empty list, and the page says "unknown".

Every external command runs with a fixed argv (no shell), a short timeout,
LANG=C (parsable English output) and the environment the caller passes —
app.py passes app_config.child_env(), so no secret reaches a child. Nothing
here needs root: dpkg-query, apt list, apt-config, systemctl show / is-enabled
and /proc are all readable by the service account. Debian and its
derivatives only (dpkg / apt); on anything else the package sections are
simply empty.
"""

import os
import platform
import re
import shutil
import socket
import subprocess
import time

_TIMEOUT = 15


def _run(argv, env=None, timeout=_TIMEOUT):
    """stdout of a command (text), or None when it can't be run / fails."""
    try:
        e = dict(env if env is not None else os.environ)
        e['LANG'] = e['LC_ALL'] = 'C'
        r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout, env=e)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _read(path, limit=200_000):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read(limit)
    except Exception:
        return None


# ─── Release ──────────────────────────────────────────────────────────────────

def release_info(app_dir, env=None):
    """{commit, commit_date, branch} of the checkout in app_dir ('' when it
    is not a git checkout or git is missing)."""
    out = {'commit': '', 'commit_date': '', 'branch': ''}
    if not shutil.which('git'):
        return out
    log = _run(['git', '-C', app_dir, 'log', '-1', '--format=%h%x09%cI'], env, 5)
    if log and '\t' in log:
        out['commit'], out['commit_date'] = (x.strip() for x in log.strip().split('\t', 1))
    br = _run(['git', '-C', app_dir, 'rev-parse', '--abbrev-ref', 'HEAD'], env, 5)
    out['branch'] = (br or '').strip()
    return out


# ─── Server hardware / OS ─────────────────────────────────────────────────────

def _os_release():
    txt = _read('/etc/os-release') or ''
    vals = dict(re.findall(r'^([A-Z_]+)="?(.*?)"?$', txt, re.M))
    return vals.get('PRETTY_NAME') or platform.platform()


def _cpu_model():
    txt = _read('/proc/cpuinfo') or ''
    for key in ('model name', 'Hardware', 'Processor', 'cpu model'):
        m = re.search(r'^' + key + r'\s*:\s*(.+)$', txt, re.M)
        if m:
            return ' '.join(m.group(1).split())
    return platform.processor() or ''


def _meminfo():
    txt = _read('/proc/meminfo') or ''
    vals = {k: int(v) * 1024 for k, v in re.findall(r'^(\w+):\s+(\d+) kB', txt, re.M)}
    return {'total': vals.get('MemTotal'), 'available': vals.get('MemAvailable'),
            'swap_total': vals.get('SwapTotal'), 'swap_free': vals.get('SwapFree')}


def _disk(label, path):
    try:
        u = shutil.disk_usage(path)
        return {'label': label, 'path': path, 'total': u.total, 'used': u.used, 'free': u.free}
    except Exception:
        return None


def server_info(disk_paths=()):
    """Hostname, OS, kernel, CPU, memory, load, uptime and the disks under
    `disk_paths` [(label, path)] (duplicates of the same filesystem kept
    once, under the first label)."""
    try:
        load = os.getloadavg()
    except Exception:
        load = None
    up = _read('/proc/uptime')
    try:
        uptime_s = int(float(up.split()[0])) if up else None
    except Exception:
        uptime_s = None
    disks, by_dev = [], {}
    for label, path in disk_paths:
        try:
            dev = os.stat(path).st_dev
        except Exception:
            continue
        if dev in by_dev:               # same filesystem: one entry, every label on it
            by_dev[dev]['label'] += ', ' + label
            continue
        d = _disk(label, path)
        if d:
            by_dev[dev] = d
            disks.append(d)
    return {
        'hostname': socket.gethostname(), 'os': _os_release(),
        'kernel': platform.release(), 'arch': platform.machine(),
        'cpu_model': _cpu_model(), 'cpu_count': os.cpu_count(),
        'memory': _meminfo(), 'load': list(load) if load else None,
        'uptime_s': uptime_s, 'disks': disks,
    }


# ─── This service ─────────────────────────────────────────────────────────────

def _proc_ppid(pid):
    st = _read(f'/proc/{pid}/stat', 4096)
    if not st:
        return None
    try:                                    # the command may contain spaces / ')'
        return int(st.rsplit(')', 1)[1].split()[1])
    except Exception:
        return None


def _rss(pid):
    txt = _read(f'/proc/{pid}/status', 8192) or ''
    m = re.search(r'^VmRSS:\s+(\d+) kB', txt, re.M)
    return int(m.group(1)) * 1024 if m else None


def worker_info():
    """This process, its parent (the gunicorn master) and the parent's other
    children (the sibling workers) with their memory."""
    me, parent = os.getpid(), os.getppid()
    try:
        my_exe = os.readlink('/proc/self/exe')
    except Exception:
        my_exe = None
    workers = []
    try:
        for name in os.listdir('/proc'):
            if not name.isdigit() or _proc_ppid(int(name)) != parent:
                continue
            # Siblings running the same program only (gunicorn's master has
            # only workers as children; a shell or a helper would not count).
            try:
                if my_exe and os.readlink(f'/proc/{name}/exe') != my_exe:
                    continue
            except Exception:
                continue
            workers.append({'pid': int(name), 'rss': _rss(int(name)), 'me': int(name) == me})
    except Exception:
        pass
    workers.sort(key=lambda w: w['pid'])
    try:
        import pwd
        user = pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        user = str(os.geteuid())
    return {'pid': me, 'master_pid': parent, 'user': user, 'workers': workers}


def service_status(name, env=None):
    """{active, since, enabled} of a systemd unit (None values when unknown)."""
    out = {'active': None, 'since': None, 'enabled': None}
    if not name or not shutil.which('systemctl'):
        return out
    txt = _run(['systemctl', 'show', name, '--property=ActiveState,ActiveEnterTimestamp,UnitFileState'],
               env, 5) or ''
    vals = dict(l.split('=', 1) for l in txt.splitlines() if '=' in l)
    out['active'] = vals.get('ActiveState') or None
    out['since'] = vals.get('ActiveEnterTimestamp') or None
    out['enabled'] = vals.get('UnitFileState') or None
    return out


# ─── Python packages (the venv) ───────────────────────────────────────────────

_REQ_RE = re.compile(r'^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*?)\s*(?:#.*)?$')


def _vtuple(v):
    """A comparable version, for the plain `>=` specs requirements.txt uses
    when `packaging` isn't importable."""
    parts = []
    for p in re.split(r'[.+-]', str(v)):
        m = re.match(r'\d+', p)
        parts.append(int(m.group(0)) if m else 0)
    return tuple(parts)


def _satisfies(installed, spec):
    if not spec:
        return True
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version
        return Version(installed) in SpecifierSet(spec)
    except Exception:
        pass
    ok = True
    for clause in spec.split(','):
        m = re.match(r'\s*(>=|==|>|<=|<|~=|!=)\s*([^\s]+)', clause)
        if not m:
            continue
        op, want = m.groups()
        a, b = _vtuple(installed), _vtuple(want)
        ok = ok and {'>=': a >= b, '==': a == b, '>': a > b, '<=': a <= b, '<': a < b,
                     '~=': a >= b, '!=': a != b}[op]
    return ok


def python_packages(requirements_path, purposes=None):
    """One row per line of requirements.txt: {name, spec, installed,
    status: ok | missing | old, purpose}. Read from the running
    interpreter's metadata — i.e. this venv."""
    from importlib import metadata
    rows = []
    txt = _read(requirements_path) or ''
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', '-')):
            continue
        m = _REQ_RE.match(line)
        if not m:
            continue
        name, _extras, spec = m.groups()
        try:
            installed = metadata.version(name)
        except Exception:
            installed = None
        status = 'missing' if installed is None else ('ok' if _satisfies(installed, spec) else 'old')
        rows.append({'name': name, 'spec': spec, 'installed': installed or '', 'status': status,
                     'purpose': (purposes or {}).get(name.lower(), '')})
    return rows


# ─── System programs + Debian packages ────────────────────────────────────────

def dpkg_versions(packages, env=None):
    """{package: installed version} for the installed ones."""
    if not packages or not shutil.which('dpkg-query'):
        return {}
    txt = _run(['dpkg-query', '-W', '-f=${Package}\t${Status}\t${Version}\n', *packages], env, 10)
    if txt is None:
        # dpkg-query exits 1 when ANY name is unknown — ask one at a time.
        out = {}
        for p in packages:
            one = _run(['dpkg-query', '-W', '-f=${Package}\t${Status}\t${Version}\n', p], env, 5)
            if one:
                out.update(dpkg_versions_parse(one))
        return out
    return dpkg_versions_parse(txt)


def dpkg_versions_parse(txt):
    out = {}
    for line in txt.splitlines():
        parts = line.split('\t')
        if len(parts) == 3 and parts[1].endswith(' installed'):
            out[parts[0]] = parts[2]
    return out


def dpkg_matching(pattern, env=None):
    """{package: version} of the installed packages matching a dpkg-query
    glob (e.g. 'postgresql-client-[0-9]*')."""
    if not shutil.which('dpkg-query'):
        return {}
    txt = _run(['dpkg-query', '-W', '-f=${Package}\t${Status}\t${Version}\n', pattern], env, 10)
    return dpkg_versions_parse(txt) if txt else {}


def program_version(argv, env=None, pattern=r'(\d+(?:\.\d+)+)'):
    """First version-looking string the program prints, or ''."""
    if not shutil.which(argv[0]):
        return ''
    txt = _run(argv, env, 10) or ''
    m = re.search(pattern, txt)
    return m.group(1) if m else ''


def apt_upgradable(env=None):
    """What the server's own package lists say can be upgraded:
    {'packages': {name: {'candidate', 'installed', 'suite', 'security'}},
     'total', 'security', 'lists_age_s'} — or None without apt. `apt list`
    reads the lists the system's daily apt timer downloads; it changes
    nothing and needs no root. lists_age_s tells how fresh they are."""
    if not shutil.which('apt'):
        return None
    txt = _run(['apt', 'list', '--upgradable'], env, 60)
    if txt is None:
        return None
    pkgs = {}
    for line in txt.splitlines():
        m = re.match(r'^([^/\s]+)/(\S+)\s+(\S+)\s+\S+(?:\s+\[upgradable from:\s*([^\]]+)\])?', line)
        if not m:
            continue
        name, suite, cand, inst = m.groups()
        pkgs[name] = {'candidate': cand, 'installed': (inst or '').strip(), 'suite': suite,
                      'security': 'security' in suite}
    age = None
    for stamp in ('/var/lib/apt/periodic/update-success-stamp', '/var/lib/apt/lists/partial',
                  '/var/cache/apt/pkgcache.bin'):
        try:
            age = max(0, int(time.time() - os.stat(stamp).st_mtime))
            break
        except Exception:
            continue
    return {'packages': pkgs, 'total': len(pkgs),
            'security': sum(1 for p in pkgs.values() if p['security']), 'lists_age_s': age}


def auto_updates(env=None):
    """Debian's automatic updates: is `unattended-upgrades` installed, is it
    switched on (APT::Periodic::Unattended-Upgrade), are the apt timers
    enabled, and when did it last run."""
    out = {'installed': False, 'enabled': False, 'update_lists': False,
           'timers': {}, 'last_run_s': None, 'lists_stamp_s': None}
    if not shutil.which('apt-config'):
        return out
    out['installed'] = bool(dpkg_versions(['unattended-upgrades'], env))
    cfg = _run(['apt-config', 'dump', 'APT::Periodic'], env, 5) or ''
    vals = dict(re.findall(r'^(APT::Periodic::[\w-]+)\s+"([^"]*)";', cfg, re.M))

    def on(key):
        v = vals.get('APT::Periodic::' + key, '0').strip()
        return v not in ('', '0') and not v.lower().startswith(('false', 'no'))
    out['enabled'] = out['installed'] and on('Unattended-Upgrade')
    out['update_lists'] = on('Update-Package-Lists')
    if shutil.which('systemctl'):
        for t in ('apt-daily.timer', 'apt-daily-upgrade.timer'):
            out['timers'][t] = (_run(['systemctl', 'is-enabled', t], env, 5) or '').strip() or 'unknown'
    now = time.time()
    for key, path in (('last_run_s', '/var/lib/apt/periodic/unattended-upgrades-stamp'),
                      ('lists_stamp_s', '/var/lib/apt/periodic/update-success-stamp')):
        try:
            out[key] = max(0, int(now - os.stat(path).st_mtime))
        except Exception:
            pass
    return out
