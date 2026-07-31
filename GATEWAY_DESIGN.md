# Companion Gateway: Email-OTP Pre-Auth on the VPS for 321Theater

## Context

321Theater runs on the internal network (gunicorn, `0.0.0.0:5400`, no proxy awareness, no 2FA). The owner has a VPS with a WireGuard tunnel back to the internal network and wants to expose the app to the internet **behind a second layer of protection**: before anyone reaches the app's real login screen, they must enter their email at a gateway on the VPS, receive a one-time code (only if that email belongs to an active account in the app's `users` table — so non-employee account holders work automatically), and enter it. Only then does traffic proxy through the tunnel to the app, where the normal username/password login is the second factor.

**Decisions made with the user:**
- Gateway verifies emails via a **small shared-secret API added to the main app** — the VPS holds no DB or SMTP credentials.
- **Custom small Flask gateway** in this repo (`gateway/`), fronted by **Caddy with `forward_auth`** — the gateway never proxies app traffic itself.
- Trust window: **~12-hour signed cookie** per browser.
- **Everything is gated** — including `/public/*` share pages (no OTP bypass for them).
- **`/register`, `/confirm-email/*`, `/internal/*` blocked from the public side**; `/forgot-password` stays available (it has its own enumeration protection + rate limit).
- Deliverable this session: **this design/plan only — no code yet.**

## Architecture

```
Public user ──HTTPS──▶ VPS
                        ├─ Caddy (TLS via Let's Encrypt, HSTS)
                        │    ├─ /__gate/*  ─────────────▶ gateway Flask app (127.0.0.1:8100)
                        │    ├─ /internal/* /register /confirm-email/* → 404
                        │    └─ everything else:
                        │         forward_auth → gateway GET /__gate/check (cookie valid?)
                        │           ├─ 200 → reverse_proxy http://10.8.0.2:5400  (over wg0)
                        │           └─ 302 → /__gate/login
                        └─ wg0 10.8.0.1 ══ WireGuard ══ 10.8.0.2 internal host (gunicorn :5400)

LAN users ──HTTP──▶ internal host :5400 directly (completely unchanged)
```

Why Caddy `forward_auth` instead of a Flask streaming proxy: the app serves multi-MB weasyprint PDFs and file uploads/downloads; Caddy handles streaming, hop-by-hop headers, and **verbatim Host passthrough** (required — the app's CSRF check `_origin_matches()` at `app.py:351-366` compares Origin/Referer hostname to `request.host`, so a rewritten Host would 403 every POST). A gateway bug then only breaks login, not all traffic.

## 1. Main-app changes (`app.py`, `init_db.py`, `install.sh`)

### 1a. Internal OTP endpoints (near `_login_route`, `app.py:3405`)

**`POST /internal/gateway/otp/request`** — JSON `{email, client_ip}`:
- Auth: `hmac.compare_digest` of `X-Gateway-Secret` header vs `GATEWAY_SHARED_SECRET` env (from `.env`, NOT `app_settings` — avoids SQLite-fallback staleness and UI editability). **Env unset ⇒ endpoint returns 404 unconditionally** (feature off by default; LAN deployments unaffected). Optional `GATEWAY_PEER_IPS` check against the *raw socket peer* (`werkzeug.proxy_fix.orig_remote_addr`), not the forwarded address.
- PG-enforced rate limits (the Flask-Limiter `memory://` store is useless across 4 workers): count rows in `gateway_otp_codes` — max 3/email and 10/IP per 15 min. Compute the cutoff timestamp in Python and bind it (portable, avoids db_adapter quirks). Throttled ⇒ same `{"status":"ok"}` shape.
- Lookup: `SELECT id FROM users WHERE lower(email)=? AND email != '' AND is_locked=0 LIMIT 1` (email is **not unique and defaults to `''`** — the `email != ''` filter is load-bearing; `is_locked` is the only disabled flag).
- Hit: 6-digit `secrets.randbelow(10**6)` code, invalidate older unused rows for that email (mirrors `/forgot-password` at `app.py:18920`), store `generate_password_hash(code)` (never plaintext), 10-min expiry, send via `_send_simple_email_async` (`app.py:18693`) with failures also logged through `_log_email_error` (`app.py:1537`) so they appear in Settings → Email Send Errors.
- Miss: dummy `generate_password_hash()` to equalize timing (same pattern as login's dummy hash, `app.py:3468-3472`).
- **Always** return `{"status":"ok"}` — neither the user nor the gateway can distinguish hit from miss. Syslog line logs a truncated email hash, not the raw email.
- **Fail closed on the SQLite-fallback trap** (CLAUDE.md): if `db.db_type != 'postgres'`, log ERROR and return the generic response without acting.

**`POST /internal/gateway/otp/verify`** — JSON `{email, code, client_ip}`:
- Same auth. Fetch newest `used=0 AND expires_at > now` row for the email.
- **Increment-first attempt counting** (race-safe under 4×4 workers/threads): `UPDATE ... SET attempts=attempts+1 WHERE id=? AND attempts < 5 AND used=0`; rowcount 0 ⇒ burned ⇒ `{"valid": false}`.
- `check_password_hash` ⇒ on success mark `used=1`, return `{"valid": true}`. All failures return the same `{"valid": false}`.

No CSRF exemption needed (`_csrf_protect` at `app.py:318` only enforces when a session `user_id` exists; these calls carry no session cookie).

### 1b. New table `gateway_otp_codes` (`init_db.py`)

Modeled on `password_reset_tokens`; keyed by email (no FK — one email can match several user rows). **App schema, not `SHARED_TABLES`.**

```sql
CREATE TABLE IF NOT EXISTS gateway_otp_codes (
    id SERIAL PRIMARY KEY,            -- INTEGER PRIMARY KEY AUTOINCREMENT in SQLite copies
    email TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    client_ip TEXT DEFAULT '',
    expires_at TIMESTAMP NOT NULL,
    attempts INTEGER DEFAULT 0,
    used INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_gateway_otp_email ON gateway_otp_codes(email);
```

Add in **all three schema copies** (SQLite ~`init_db.py:857`, SQLite second block ~`2404`, `PG_SCHEMA` ~`3398` — no semicolons in comments), plus `TABLE_ORDER` (~`4085`, no-dependencies tier) and `serial_tables` (~`4245`). Housekeeping: `DELETE ... WHERE created_at < now - 1 day` piggybacked inside the request endpoint (no new scheduler job).

### 1c. Proxy correctness

- **Trusted-proxy middleware, env-gated — not bare ProxyFix.** If `TRUSTED_PROXY_IPS` env is set (e.g. `10.8.0.1`), wrap `app.wsgi_app` (~`app.py:111`) in a ~20-line middleware applying `X-Forwarded-For`/`X-Forwarded-Proto` **only when the socket peer is in the list**, stashing the original peer under the ProxyFix environ key. Bare ProxyFix would let LAN clients forge audit IPs (`app.py:3288/3425/3450`) and limiter keys. Unset ⇒ byte-for-byte current behavior. Side benefit: the 15/min login limiter keys on real public IPs instead of one shared tunnel IP.
- **Session cookie Secure flag per-request:** in `_DBSessionInterface.save_session` (~`app.py:250`), set `secure = request.is_secure or app.config['SESSION_COOKIE_SECURE']`. One instance serves HTTPS-public and HTTP-LAN simultaneously; the global env flag would break LAN logins. HSTS at Caddy covers the browser side.
- **Host passthrough:** no app change — Caddy passes client Host by default; the Caddyfile must not override it (documented in README).
- `install.sh` (~lines 154-175): append a commented `GATEWAY_SHARED_SECRET=` stub to the `.env` generation.

## 2. Gateway app (`gateway/` — all new files, zero imports from `app.py`)

```
gateway/
  gateway_app.py          # ~250-line Flask app
  templates/gate_email.html, gate_code.html
  static/gate.css
  requirements.txt        # flask, requests, gunicorn
  gateway.env.example     # GATE_SECRET_KEY, GATE_SHARED_SECRET, GATE_APP_INTERNAL_URL,
                          # GATE_SESSION_HOURS=12, GATE_COOKIE_NAME=__Host-321gate
  321gateway.service      # systemd unit (VPS): gunicorn -w1 --threads 4 -b 127.0.0.1:8100,
                          # EnvironmentFile=/etc/321gateway/gateway.env, User=gateway,
                          # NoNewPrivileges/ProtectSystem=strict/PrivateTmp
  Caddyfile.example
  README.md               # full VPS + WG runbook
```

Routes (all under `/__gate/` — no collision with app paths):
- `GET/POST /__gate/login` — email form; POST normalizes email, calls `/internal/gateway/otp/request`, sets a 10-min signed "pending" cookie `{email}`, redirects to `/__gate/code`. Always shows *"If that email has an account, a code has been sent."* `next` param validated (must start with `/`, not `//` — mirrors `app.py:3452`).
- `GET/POST /__gate/code` — code form (email from pending cookie, never in URL/logs); POST calls `/internal/gateway/otp/verify`; success ⇒ mint gate cookie via `itsdangerous.URLSafeTimedSerializer` — `Secure; HttpOnly; SameSite=Lax; Max-Age=43200`, `__Host-` prefixed — redirect to `next`. Failure ⇒ generic "invalid or expired".
- `GET /__gate/check` — the `forward_auth` target: valid cookie (`loads(..., max_age=12h)`) ⇒ 200 + `X-Gate-Email` header (informational only, never trusted for auth); invalid ⇒ 302 to `/__gate/login?next=<X-Forwarded-Uri>`, or **401 for AJAX** (`X-Requested-With`/JSON Accept) so in-app fetches fail cleanly.
- `GET /__gate/signout` — clears gate cookie; page links to the app's `/logout` too (the two sessions expire independently).

Stateless on the VPS (no DB, no session store). Abuse controls: cheap in-process per-IP token bucket on the two POSTs (single worker ⇒ in-memory is fine), fail2ban-friendly `GATE_OTP_FAIL ip=<ip>` journald lines, real limits enforced in PG (§1a).

### Caddyfile (per the user's "gate everything" + "block register" decisions)

```
theater.example.com {
    encode gzip
    header Strict-Transport-Security "max-age=31536000"

    handle /__gate/* { reverse_proxy 127.0.0.1:8100 }

    @blocked path /internal/* /register /confirm-email/*
    respond @blocked 404

    handle {
        forward_auth 127.0.0.1:8100 {
            uri /__gate/check
            copy_headers X-Gate-Email
        }
        reverse_proxy 10.8.0.2:5400   # Host passes through by default — do not override
    }
}
```

No `/public/*` or `/static/*` exemption — everything requires the gate cookie. (`/static` is fine gated: users only reach it after passing the gate anyway.)

## 3. Network hardening (runbook content, `gateway/README.md`)

- **Internal host firewall (currently nothing restricts :5400 LAN-wide):** allow 5400 from LAN subnet + `10.8.0.1` only; allow 51820/udp from the VPS IP; default deny. Note: `app_port` is read from the SQLite bootstrap at startup (`start.sh:12-21`) — changing it in Settings requires updating firewall rules.
- WG: VPS `10.8.0.1/24` ListenPort 51820, peer AllowedIPs `10.8.0.2/32`; internal host `10.8.0.2/24` with `PersistentKeepalive = 25`.
- VPS: ufw allow 80/443/51820/udp + rate-limited ssh; fail2ban sshd jail + optional `321gateway` jail on `GATE_OTP_FAIL` (e.g. 10 fails/10 min ⇒ 1 h ban).
- Secrets: `python3 -c 'import secrets; print(secrets.token_hex(32))'` for `GATE_SECRET_KEY` and `GATEWAY_SHARED_SECRET` (the latter identical on both hosts).

## 4. Implementation phases

1. **Main-app internal API + schema** — `init_db.py` (table ×3 + `TABLE_ORDER` + `serial_tables`), `app.py` (two endpoints + helpers), `install.sh` stub. Fully testable on LAN with `curl`, no VPS needed.
2. **Proxy correctness** — `app.py`: trusted-proxy middleware + `save_session` secure-flag tweak. Zero behavior change with env unset.
3. **Gateway app** — new `gateway/` directory; no existing files touched.
4. **Infrastructure** — execute the README runbook: WG, ufw both sides, Caddy, fail2ban, DNS, secrets.
5. **Integration test + cutover** (below).

## 5. Verification (all on one dev box — "the tunnel" is localhost)

Run: main app `:5400` with `GATEWAY_SHARED_SECRET=… TRUSTED_PROXY_IPS=127.0.0.1 GATEWAY_PEER_IPS=127.0.0.1`; gateway `:8100` with `GATE_APP_INTERNAL_URL=http://127.0.0.1:5400`; `caddy run` on localhost (internal CA gives local HTTPS).

- **Internal API:** wrong/absent/unset secret ⇒ 404; nonexistent email ⇒ identical body + comparable latency; 4th request in 15 min ⇒ throttled-but-ok; wrong code ×5 ⇒ correct code on attempt 6 still fails; reuse after success fails; expired fails; `is_locked=1` and blank-email users get no code; email matching two user rows gets a code.
- **Gateway:** happy path email→code→cookie→app login; tampered/expired cookie ⇒ redirect with correct `next`; `next=//evil.com` rejected; AJAX with dead cookie ⇒ 401 not 302.
- **Through Caddy:** logged-in form POST succeeds (proves Host passthrough / CSRF), labor-PDF download and attachment upload work, audit log shows real client IP, session cookie has `Secure` on HTTPS path while direct `:5400` HTTP login still works without it.
- **Edge policy:** `/register`, `/confirm-email/*`, `/internal/*` ⇒ 404 via Caddy but work on LAN; `/public/*` requires the gate cookie via Caddy but stays open on LAN.
- **Fail-closed:** stop PG ⇒ `/otp/request` returns ok, sends nothing, logs ERROR. Known blast radius: stopped gateway service = public outage (forward_auth runs per request); verify Caddy's error page is sane.

## Notes / accepted trade-offs

- Gate cookie and app session both last 12 h but on independent clocks — acceptable; sign-out page links both.
- The per-process `memory://` Flask-Limiter weakness on `/login` is pre-existing and out of scope (real client IPs at least fix its keying).
- LAN traffic stays HTTP; making `SESSION_COOKIE_SECURE` unconditional would require LAN HTTPS — future work.
