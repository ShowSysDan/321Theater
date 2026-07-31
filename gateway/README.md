# 3·2·1→Theater Public Gateway — Theory of Operation & Install Runbook

This directory is the **companion gateway** that puts 3·2·1→Theater on the
internet safely. It runs on the VPS (**cyclorama**, `129.121.114.249`), serves
`https://dpc.321.theater`, and requires every visitor to pass an **email
one-time-code check** before a single byte of app traffic flows to the
internal server over the WireGuard tunnel. The app's own username/password
login remains untouched as the second factor.

Nothing in this directory runs on the internal app server. The app server's
half of the feature (the `/internal/gateway/otp/*` API) ships inside `app.py`
and is dormant until `GATEWAY_SHARED_SECRET` is set in its `.env`.

---

## 0. Handoff guide — read this if you inherited this system

Written for a successor who is NOT a Linux person. Everything deeper is in
the sections below, but these ten minutes of reading keep the lights on.

**What this is, in one paragraph.** Staff reach the theater app from home at
`https://dpc.321.theater`. That address points at a small rented server (a
"VPS", named **cyclorama**, IP `129.121.114.249`) whose only job is to be
the front door: it makes visitors prove they own a staff email (one-time
code) before passing them through a private tunnel to the real app server
inside the building (`10.201.2.101`). The app itself, and all its data,
never leave the building.

**The three machines:**
1. **cyclorama (the VPS)** — rented from a hosting provider (whoever pays
   that bill has console access if SSH is ever lost). Runs two services:
   `caddy` (the web server) and `321gateway` (the code-check app).
2. **The app server** (`10.201.2.101` inside the building) — runs the
   `321theater` service. Nothing about it changed for this feature except
   three lines in its `.env` file.
3. **The WireGuard/tunnel server** — the building side of the private
   tunnel, with a firewall rule that only lets the VPS reach the app's one
   port.

**Where the secrets live** (never in git):
- VPS: `/etc/321gateway/gateway.env` — two random keys.
- App server: `/opt/321theater/.env` — one of those keys must match
  (`GATE_SHARED_SECRET` there = `GATEWAY_SHARED_SECRET` here).
- DNS for `321.theater` — whoever manages the domain registrar account.

**To rebuild the VPS from absolute zero** (new VPS, old one lost):
1. Get a Debian VPS, point the DNS `A` record for `dpc.321.theater` at its
   IP, and set up the WireGuard tunnel to the building (copy the config
   from the old VPS or the tunnel server).
2. Log in as root and run:
   ```bash
   apt install -y git
   git clone --depth 1 --filter=blob:none --sparse \
       -b main <this repo's URL> /opt/321gateway-src
   cd /opt/321gateway-src && git sparse-checkout set gateway
   bash gateway/install.sh
   ```
3. The script will stop and tell you to paste one value: copy
   `GATEWAY_SHARED_SECRET=...` from the app server's `.env` into
   `/etc/321gateway/gateway.env` as `GATE_SHARED_SECRET=...`, then run
   `systemctl restart 321gateway`. Done.

**To update after a code change:** `cd /opt/321gateway-src && git pull &&
bash gateway/install.sh`

**Emergency OFF switch** (take the site off the internet immediately,
building access unaffected): `ssh` to cyclorama, `systemctl stop caddy`.
Turn it back on with `systemctl start caddy`.

**Log everyone out of the public site immediately** (lost/stolen laptop):
edit `/etc/321gateway/gateway.env`, replace the `GATE_SECRET_KEY` value
with the output of
`python3 -c 'import secrets; print(secrets.token_hex(32))'`, then
`systemctl restart 321gateway`. Every browser has to redo the email code.

**When something's broken,** start with the table in §7 — the two most
common answers are "the tunnel is down" and "the shared secret doesn't
match after someone edited an env file."

---

## 1. Theory of operation

### 1.1 The moving parts

```
                        ┌────────────────────────── VPS: cyclorama ───────────────────────────┐
 Public                 │                                                                      │
 browser ── HTTPS ────▶ │  Caddy :443  (TLS via Let's Encrypt, HSTS)                           │
 dpc.321.theater        │   ├── /__gate/*  ─────────────▶  gateway_app.py (127.0.0.1:8100)     │
                        │   ├── /internal/*, /register,                                        │
                        │   │   /confirm-email/*  ──▶  404 (blocked at the edge)               │
                        │   └── everything else:                                               │
                        │        forward_auth → GET /__gate/check                              │
                        │          ├─ 200  → reverse_proxy 10.201.2.101:5400 ──── wg0 ─────────┼──▶ internal
                        │          └─ 302  → /__gate/login                        10.201.4.9   │    app server
                        └──────────────────────────────────────────────────────────────────────┘    :5400

 LAN users ── HTTP ──▶ 10.201.2.101:5400 directly, exactly as before (nothing changes for them)
```

- **Caddy** owns TLS and routing. On *every* request outside `/__gate/*` it
  makes a subrequest to the gateway (`forward_auth`): HTTP 200 means "let it
  through", anything else is returned to the browser (a redirect to the login
  form, or 401 for AJAX).
- **The gateway Flask app** (`gateway_app.py`) renders two small forms and
  answers `/__gate/check`. It is **stateless**: its only "session" is a signed
  cookie (`itsdangerous`, 12-hour max age). It holds **no database, no SMTP
  credentials, and no user data** — just two secrets from the environment.
- **The main app** does everything sensitive over the tunnel: it checks
  whether an email belongs to an active account, generates and emails the
  6-digit code, stores only a **hash** of it, and enforces the real rate
  limits in PostgreSQL. The gateway can't even tell whether an email exists —
  the API answers `{"status":"ok"}` either way.

### 1.2 A visitor's first request, step by step

1. Browser hits `https://dpc.321.theater/shows/42`. Caddy asks the gateway:
   no gate cookie → **302 to `/__gate/login?next=/shows/42`**.
2. Visitor enters their email. The gateway POSTs it (plus the client IP) to
   `POST /internal/gateway/otp/request` on the app server, authenticated with
   the `X-Gateway-Secret` header. The app server:
   - fails closed if it detects it's on a stale SQLite fallback instead of
     PostgreSQL;
   - enforces DB-backed rate limits (3 codes/email, 10/IP per 15 min);
   - looks up `users` for a row with that email (case-insensitive, non-blank,
     not locked) — *any* active account qualifies, employee or not;
   - on a hit: generates a 6-digit code with `secrets`, stores
     `generate_password_hash(code)` with a 10-minute expiry, invalidates any
     older outstanding codes for that email, and emails the code
     (asynchronously — send failures land in the Settings → Email Send Errors
     panel);
   - on a miss: burns equivalent CPU hashing a dummy value so timing doesn't
     leak, and sends nothing;
   - **always** answers `{"status":"ok"}`.
3. The gateway sets a 10-minute signed "pending" cookie carrying the email
   and shows the code form. The page says *"If that email has an account, a
   code is on its way"* — same message for everyone.
4. Visitor types the code. The gateway calls
   `POST /internal/gateway/otp/verify`. The app server **increments the
   attempt counter before comparing** (`UPDATE … WHERE attempts < 5 AND
   used=0` — race-safe across its 16 worker threads), then checks the hash.
   Five wrong guesses burn the code even if the sixth guess is right.
5. On `{"valid": true}` the gateway mints the **gate cookie**
   (`__Host-321gate`: signed, `Secure`, `HttpOnly`, `SameSite=Lax`, 12 h) and
   redirects to the original `next` path.
6. Caddy's next `forward_auth` check returns 200 and the request proxies to
   `10.201.2.101:5400` over the tunnel — where the visitor now sees the app's
   normal login page and signs in with username/password as always.

Subsequent requests skip straight to step 6 until the cookie expires
(~12 hours), after which the visitor repeats the email-code dance.

### 1.3 Security model — what each side holds and what a compromise costs

| Component | Holds | If compromised |
|---|---|---|
| Caddy / VPS | TLS key for dpc.321.theater, the two gateway secrets | Attacker can mint gate cookies → reaches the app's **login page**, still needs a password. Can observe traffic passing through the proxy (inherent to any public reverse proxy — mitigate with VPS hygiene, §5). Cannot reach anything internal except `10.201.2.101:5400` (firewall rule on the WG server). |
| gateway_app.py | `GATE_SECRET_KEY` (cookie signing), `GATE_SHARED_SECRET` (API auth) | Same as above — no DB, no SMTP, no stored state. |
| Main app | Everything, as today | Unchanged from the current LAN deployment. |

Enumeration resistance, in one place, because it's easy to regress:

- `/otp/request` returns the same body for hit, miss, and throttled.
- Timing is equalized with a dummy hash on the miss path.
- Rate limiting counts **all** requests in the window (hits and misses), so
  the throttle itself can't be used as an oracle.
- Syslog lines log a truncated SHA-256 of the email (`email_key=…`), never
  the address, so journald doesn't become the oracle either.
- The user-facing pages never distinguish "wrong code" from "expired" from
  "no such account".

### 1.4 Failure modes (know these before you're paged)

| Failure | Visible effect |
|---|---|
| Gateway service down | **Public outage** — `forward_auth` runs per request, so Caddy 502s everything. LAN unaffected. `systemctl restart 321gateway`. |
| Tunnel down | Public users who passed the gate get Caddy 502s; the gate's email step also fails quietly (no codes sent). LAN unaffected. |
| PostgreSQL down | App-side OTP endpoints **fail closed** (generic response, ERROR in the app journal, no code sent). Nobody new passes the gate; existing cookies still pass `forward_auth` but the app itself will be struggling anyway. |
| SMTP broken | Codes are generated but never arrive. Check Settings → Email Send Errors (`gateway_otp` rows) on the app. |
| Wrong/missing shared secret | The internal API answers 404; gate emails silently never send. Compare `GATE_SHARED_SECRET` (VPS) with `GATEWAY_SHARED_SECRET` (app `.env`). |

### 1.5 What the gate deliberately does NOT do

- It does **not** log anyone into the app — the app's session and the gate
  cookie are independent, both ~12 h on separate clocks. `/__gate/signout`
  clears only the gate; the app's own logout clears only the app.
- It does not gate the LAN. Internal users on `10.201.2.101:5400` never see
  any of this.
- `X-Gate-Email` (forwarded to the app for log correlation) is
  **informational only** — nothing in the app trusts it for auth, and nothing
  ever should.

---

## 2. Prerequisites

- VPS: cyclorama (`129.121.114.249`) — Debian, with **firewalld** as the
  firewall (managed via Cockpit; all rules below are plain `firewall-cmd`,
  so they show up in Cockpit's Networking → Firewall page too).
- WireGuard tunnel up: VPS `wg0` = `10.201.4.9`, and `10.201.2.101:5400`
  reachable from the VPS through it.
- **Firewall rule on the WireGuard server (internal side): traffic from
  `10.201.4.9` may reach ONLY `10.201.2.101:5400`.** This rule is what keeps
  a compromised VPS from being a bridge into the network — do not skip it.
- DNS: `dpc.321.theater` → `129.121.114.249` (A record live before Caddy
  first starts, or Let's Encrypt issuance fails).
- The app server runs a build of 321Theater that includes the
  `/internal/gateway/otp/*` endpoints and the `gateway_otp_codes` table
  (run `python3 init_db.py --migrate` / restart after updating).

## 3. Install — internal app server (5 minutes)

> **Which machine:** §3 runs on the **321T server inside the building**
> (`10.201.2.101`) — the only step that touches it. §4 runs on the **VPS**
> (cyclorama). Do §3 first: the shared secret is generated here, and the
> §4 installer stops and asks for it.

```bash
# 1. Generate the shared secret (KEEP THIS — the VPS needs the same value)
python3 -c 'import secrets; print(secrets.token_hex(32))'

# 2. Add to /opt/321theater/.env  (install.sh stubs these, commented out):
GATEWAY_SHARED_SECRET=<value from step 1>
GATEWAY_PEER_IPS=10.201.4.9
TRUSTED_PROXY_IPS=10.201.4.9

# 3. Apply schema + restart
cd /opt/321theater && python3 init_db.py --migrate   # creates gateway_otp_codes
sudo systemctl restart 321theater
```

**Verify the source IP assumption** (the values above assume the inter-VLAN
router does not NAT). From the VPS: `curl -s http://10.201.2.101:5400/login
>/dev/null`, then on the app server check the gunicorn access log
(`journalctl -u 321theater -n 5`) — the connecting IP shown is the value that
belongs in `GATEWAY_PEER_IPS` and `TRUSTED_PROXY_IPS`. If it isn't
`10.201.4.9`, substitute what you saw.

Sanity checks from the VPS:

```bash
# Wrong secret → 404 (endpoint plays dead)
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  http://10.201.2.101:5400/internal/gateway/otp/request \
  -H 'Content-Type: application/json' -H 'X-Gateway-Secret: wrong' \
  -d '{"email":"x@y.z"}'          # → 404

# Right secret → {"status":"ok"} (and a real account email gets a code)
curl -s -X POST http://10.201.2.101:5400/internal/gateway/otp/request \
  -H 'Content-Type: application/json' -H "X-Gateway-Secret: $SECRET" \
  -d '{"email":"you@drphillipscenter.org","client_ip":"test"}'
```

## 4. Install — VPS (cyclorama)

### The fast path: one script does all of it

After getting the files onto the box (sparse checkout, §4.1):

```bash
sudo bash /opt/321gateway-src/gateway/install.sh
```

The script installs the packages, creates the `gateway` user, syncs the app
to `/opt/321gateway`, builds the venv, writes `/etc/321gateway/gateway.env`
with a fresh auto-generated `GATE_SECRET_KEY`, installs + enables the
systemd unit, installs the Caddyfile (backing up any existing one; it never
touches a Caddyfile that already serves the site), opens http/https in
firewalld, and sets up the fail2ban jail. It is **idempotent** — re-running
it is the update procedure — and it never overwrites an existing
`gateway.env`. The one thing it cannot do for you: paste the
`GATE_SHARED_SECRET` value from the app server into `gateway.env` (it stops
and tells you exactly that if it's missing).

### Manual steps (what the script does, for reference)

```bash
# ── 0. Basics ────────────────────────────────────────────────────────────────
apt update && apt install -y caddy python3-venv fail2ban
# (firewalld is already installed/managed via Cockpit on this box)

# ── 1. Dedicated user + app directory ────────────────────────────────────────
useradd --system --no-create-home --shell /usr/sbin/nologin gateway
mkdir -p /opt/321gateway
# Get THIS directory's contents (gateway_app.py, templates/, static/,
# requirements.txt) into /opt/321gateway — see §4.1 for the recommended
# sparse-checkout setup (git pull that fetches ONLY gateway/).
cd /opt/321gateway
python3 -m venv venv
venv/bin/pip install -r requirements.txt
chown -R gateway:gateway /opt/321gateway

# ── 2. Secrets ───────────────────────────────────────────────────────────────
mkdir -p /etc/321gateway
cp gateway.env.example /etc/321gateway/gateway.env
python3 -c 'import secrets; print(secrets.token_hex(32))'   # → GATE_SECRET_KEY
# GATE_SHARED_SECRET = the value already in the app server's .env
$EDITOR /etc/321gateway/gateway.env
chown root:gateway /etc/321gateway/gateway.env
chmod 640 /etc/321gateway/gateway.env

# ── 3. Service ───────────────────────────────────────────────────────────────
cp 321gateway.service /etc/systemd/system/       # 1 worker × 16 threads
systemctl daemon-reload
systemctl enable --now 321gateway
curl -s http://127.0.0.1:8100/__gate/healthz     # → {"ok": true}

# ── 4. Caddy ─────────────────────────────────────────────────────────────────
cp Caddyfile.example /etc/caddy/Caddyfile
systemctl reload caddy
journalctl -u caddy -f    # watch the Let's Encrypt issuance succeed

# ── 5. Firewall (firewalld — already running, managed via Cockpit) ──────────
# No WireGuard rule needed: this VPS is an outbound-connecting peer, it
# doesn't listen for WG. Only the web ports are opened.
firewall-cmd --permanent --add-service=http     # 80  (Let's Encrypt + redirect)
firewall-cmd --permanent --add-service=https    # 443
# ssh is allowed in the default public zone already; brute-force protection
# comes from the fail2ban sshd jail rather than a port rule.
firewall-cmd --reload
firewall-cmd --list-all                          # confirm: http https ssh only
```

### 4.1 Getting the files there — and updating later (sparse checkout)

You don't need (or want) the whole app source on an internet-facing box.
A sparse, blobless clone materializes **only** `gateway/` and stays
pullable:

```bash
git clone --depth 1 --filter=blob:none --sparse \
    -b main git@github.com:ShowSysDan/321Theater.git /opt/321gateway-src
cd /opt/321gateway-src
git sparse-checkout set gateway     # working tree now contains gateway/ only
```

Deploy (first time and every update):

```bash
cd /opt/321gateway-src && git pull
sudo bash gateway/install.sh     # idempotent: re-sync, re-pip, restart
```

`git pull` in a sparse+blobless clone only downloads blobs for paths in the
sparse set — a change to `app.py` costs you a few bytes of history metadata,
not the file. Notes:

- If the repo is private, give the VPS a **read-only deploy key** (GitHub →
  repo Settings → Deploy keys), never your personal credentials.
- `/opt/321gateway` (what systemd runs) is deliberately separate from the
  checkout, so a broken pull can't take the service down until you rsync.
- The alternative with zero credentials on the VPS: skip git entirely and
  `rsync gateway/ cyclorama:/opt/321gateway/` from a trusted machine.

### 4.2 Optional: fail2ban jail for code-guessing

Both the gateway and the app log a `GATE_OTP_FAIL ip=<ip>` /
`GATEWAY_OTP_FAIL` line per failed attempt. On the VPS:

```ini
# /etc/fail2ban/filter.d/321gateway.conf
[Definition]
failregex = GATE_OTP_FAIL ip=<HOST>
journalmatch = _SYSTEMD_UNIT=321gateway.service

# /etc/fail2ban/jail.d/321gateway.conf
[321gateway]
enabled  = true
backend  = systemd
filter   = 321gateway
maxretry = 10
findtime = 600
bantime  = 3600
```

Since this box uses firewalld, make fail2ban issue its bans through it
(instead of raw iptables, which firewalld can clobber on reload) — in
`/etc/fail2ban/jail.local`:

```ini
[DEFAULT]
banaction = firewallcmd-rich-rules
```

Then `systemctl restart fail2ban` and `fail2ban-client status 321gateway`.
Active bans appear as rich rules in `firewall-cmd --list-rich-rules` (and in
Cockpit).

(The app-side attempt cap — 5 guesses per code, 3 codes per email per
15 min — is the real defense; this jail just cuts the noise.)

## 5. VPS hygiene (the residual risk lives here)

TLS terminates on this box, so a *fully* compromised VPS could observe
traffic in flight. Keep the target small:

- Automatic security updates: `apt install unattended-upgrades` and enable it.
- SSH: keys only (`PasswordAuthentication no`), consider moving off port 22.
- Nothing else runs on this box. Don't add services to cyclorama.
- Rotate both gateway secrets if you ever suspect exposure — takes one edit
  on each side plus `systemctl restart 321gateway` / `321theater`. Rotating
  `GATE_SECRET_KEY` instantly invalidates every outstanding gate cookie
  (this is also the "revoke all public sessions NOW" lever).

## 6. Acceptance test (run once after install)

| # | Test | Expect |
|---|---|---|
| 1 | `https://dpc.321.theater/` in a fresh browser | Redirects to the gate's email form |
| 2 | Enter an email with an account | "Check your email" page; code arrives |
| 3 | Enter an email WITHOUT an account | Identical page, no email sent |
| 4 | Wrong code ×5, then the correct code | All six attempts fail (code burned) |
| 5 | Fresh code, correct entry | Lands on the app's normal login page |
| 6 | Log into the app, open a show, submit a form | Works (proves Host/CSRF passthrough) |
| 7 | Download a labor PDF | Works (proves proxy streaming) |
| 8 | `https://dpc.321.theater/register` | 404 |
| 9 | `https://dpc.321.theater/internal/gateway/otp/request` | 404 |
| 10 | From the LAN: `http://10.201.2.101:5400/login` | Normal login, no gate anywhere |
| 11 | On the app server: audit log for the test login | Shows your real public IP, not 10.201.4.9 |
| 12 | Browser devtools → app session cookie via the gateway | Has the `Secure` flag |
| 13 | `curl https://dpc.321.theater/robots.txt` | `Disallow: /` for `*` and the AI crawlers, served without hitting the gate |
| 14 | `curl -sI https://dpc.321.theater/__gate/login \| grep -i x-robots` | `noindex, nofollow, noarchive, nosnippet, noai, noimageai` |

## 7. Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Everything 502s publicly | Gateway service down (`systemctl status 321gateway`) or tunnel down (`ping 10.201.2.101` from the VPS). |
| Codes never arrive, page looks normal | Secret mismatch (VPS ↔ app `.env`), app not restarted after adding it, or SMTP failure — check Settings → Email Send Errors for `gateway_otp` rows and `journalctl -u 321theater` for `GATEWAY_OTP` / fail-closed ERROR lines. |
| Form POSTs inside the app 403 | Something is rewriting the Host header — the Caddyfile must not set `header_up Host`. |
| Audit logs show 10.201.4.9 for everyone | `TRUSTED_PROXY_IPS` unset/wrong on the app server, or the router NATs (re-run the source-IP check in §3). |
| `__Host-321gate` cookie rejected by the browser | The site must be reached over HTTPS with no Domain attribute — check you're not testing via plain HTTP or an IP address. |
| Let's Encrypt issuance fails | DNS not propagated yet, or port 80 blocked. `journalctl -u caddy`. |

## 8. Multiple 321T servers (primary/secondary redundancy)

The app supports running several installations against the same PostgreSQL
database — its cluster heartbeat elects one server as primary (Settings →
System shows the LEADER badge). The gateway plugs into that same election
instead of inventing its own:

- Every 321T server (v2.27.0+) answers `GET /internal/cluster/primary` —
  HTTP 200 while it holds the primary role, 503 otherwise (including when
  it can't reach PostgreSQL). Single-server installs always answer 200, so
  nothing changes for them.
- **The server list lives in ONE place:** `GATE_APP_INTERNAL_URLS` in
  `/etc/321gateway/gateway.env` — comma-separated, preference order:

  ```
  GATE_APP_INTERNAL_URLS=http://10.201.2.101:5400,http://10.201.2.102:5400
  ```

- After editing the list, run `sudo bash install.sh --rewrite-caddy`. That
  regenerates the Caddyfile's proxy block with all upstreams,
  `lb_policy first`, and a 10-second health check against the primary
  probe — so browsing traffic follows the election and fails over
  automatically when the primary dies (its heartbeats go stale and the
  next server wins the election within ~30s).
- The gateway's own OTP calls do the same thing in-process: poll the
  probe (cached 10s), send to the primary, retry the others on error.

Also required per added server: it must reach the same PostgreSQL, its
`.env` needs the same `GATEWAY_SHARED_SECRET` + `TRUSTED_PROXY_IPS`, and
the WireGuard-server firewall rule must allow the VPS to reach its
`:5400` too. Cluster heartbeat must stay enabled (it is by default) —
with it disabled, every server claims primary.
