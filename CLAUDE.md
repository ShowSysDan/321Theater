# 321Theater — notes for Claude

3·2·1→Theater is a Flask production-advance / day-of-show management tool for
Dr. Phillips Center. Most of the app is in a single large `app.py`; HTML lives
in `templates/`, static assets in `static/`.

## Database: ALWAYS PostgreSQL (do not "fuss about SQLite")

**Production runs on PostgreSQL, always.** Treat PostgreSQL as the one source
of truth for all data *and* `app_settings`.

The SQLite file (`advance.db`) is **only a bootstrap**. It exists so the app can
discover two things before any real connection is opened:
- `db_type` (stored in the SQLite `app_settings` table — set to `postgres`), and
- PostgreSQL credentials, read from `db_config.ini` (gitignored) next to it.

Everything else — shows, schedules, settings saved in the UI (SMTP config,
auto-email flags, etc.) — is written to and read from **PostgreSQL**. The SQLite
bootstrap does **not** contain those rows, so reading app settings from SQLite
returns stale/default values (e.g. `advance_email_enabled` → `'0'`).

### The trap to watch for
`db_adapter.connect()` **silently falls back to a SQLite connection** if the
PostgreSQL connect fails for any reason. On this always-PG deployment that means
code can silently start reading STALE bootstrap data instead of erroring. This
already caused a real bug: the scheduled-email background job would fall back to
SQLite, see `advance_email_enabled='0'`, and send nothing — while the Settings
"Next scheduled send" preview (running in a request while PG was reachable)
showed a correct plan. The fallback now logs at ERROR, and the email scheduler
explicitly refuses to act when `db.db_type != 'postgres'`. Keep that pattern:
**background jobs must verify they're actually on PostgreSQL before acting on
settings, rather than trusting a silent SQLite fallback.**

### SQL portability
`db_adapter.py` adapts SQLite-style SQL for PostgreSQL automatically: `?`
placeholders → `%s`, `INSERT OR REPLACE/IGNORE` → `ON CONFLICT …`, and
`datetime('now', …)` → `NOW() ± INTERVAL`. New `INSERT OR REPLACE` targets need
their conflict columns added to `_CONFLICT_COLS`. PostgreSQL returns `date`/
`datetime` objects where SQLite returns ISO strings — coerce with `_as_date()`
(app.py) before doing date math, and `json.dumps` row snapshots with
`default=str` (a bare dumps raises on PG and, inside never-raise helpers like
`log_audit`, the row silently vanishes — this dropped every snapshot-bearing
EDIT/DELETE audit entry on PG until 2.18.0).

Traps that only bite on PostgreSQL (each has caused a real 500):
- **Literal `%` in SQL** (e.g. `LIKE 'prefix_%'`): bind the pattern as a
  parameter instead. db_adapter now passes `None` to psycopg2 when there are
  no params (so param-less literals work), but a query that mixes a literal
  `%` WITH bound params will still break — psycopg2 interprets `%` as a
  placeholder marker.
- **Literal `?` anywhere in SQL text** — including inside quoted string
  literals and prose ("worker died?") — is rewritten to `%s` by db_adapter's
  blind `replace('?', '%s')`. Bind any text containing `?` as a parameter.
- **`PG_SCHEMA` in init_db.py is split on `;` with no real parser** — never
  put a semicolon inside a schema comment, and keep every statement
  self-contained. SQLite's `executescript` parses properly, so the mistake
  passes SQLite testing and only fails on PG init/migrate.

## Cross-app user flags (`is_app_user` / `is_app_admin`) — NEVER used in this app
Two columns on the (shared-schema) `users` table — `is_app_user` and
`is_app_admin` — exist **only** for OTHER applications that share this user
directory. 321Theater lets an admin set them (Settings → user list → Edit User
modal, persisted in `edit_user()`) and reads them back **only** to render their
badges + modal checkboxes. They carry **no** behavior in this app.
**Never gate any 321Theater logic on these flags** — not auth, routes, sessions,
`@*_required` decorators, background jobs, or feature visibility. They are not a
permission system for this app; treat them as opaque values owned by the sister
apps. For a 321Theater access change, use this app's own flags instead
(`role` / `is_readonly` / `is_scheduler` / `is_asset_manager` /
`is_document_viewer`), never `is_app_*`.

## Runtime / deployment
- Served by **Gunicorn, 4 workers × 4 threads** (`start.sh`); each worker imports
  the module independently, so `start_scheduler()` runs once per worker.
- Background jobs use **APScheduler** (`start_scheduler()` in app.py) and are
  **leader-gated** via `am_i_leader()` (cluster heartbeat in `cluster_instances`)
  so only one worker fires side-effecting jobs. Jobs with external side effects
  (email/SMS) must start with `if not am_i_leader(): return`.
- `get_db()` is context-free (fresh connection per call) — safe to call from
  background threads, not just request handlers.

## Scheduled auto-emails (advance / production schedule PDFs)
- Job: `run_scheduled_pdf_emails()` — cron, top of every hour; does work at or
  after `pdf_email_send_hour`.
- Planner: `_plan_scheduled_emails(db, target_date)` — shared by the job and the
  Settings preview so they never disagree. Trigger is "due" when
  `0 <= days_until <= trigger_days`; dedup is all-time per
  `(show, pdf_type, trigger_days)` via `email_send_log`.
- Preview endpoint: `GET /settings/pdf-emails/preview` (Settings → Email → "Next
  Scheduled Send"). NOTE: the preview only exercises the planner + settings read
  in a request context. It does **not** prove the background send path works.
- Actual send: `_send_pdf_email()`. SMTP/recipient failures are recorded in the
  `email_send_errors` table and the Settings "Email Send Errors" panel.

## Labor billing math lives in TWO shared engines — never fork it
- `_calc_labor_cost_for_show()` (estimates) and `_calc_post_show_labor_cost()`
  (settlement actuals) feed the show-page tables AND every labor PDF
  (labor-estimate, pre-show-estimate, post-show-invoice, combined-invoice).
  Change billing behavior there, not in a template or JS.
- **Overtime:** >40h per technician per Monday–Sunday work week (within a
  show/event; the accumulator resets each Monday) bills at 1.5×, split by
  `_allocate_overtime()` with tech identity from `_ot_shift_key()` (crew id →
  requested name → per-position day-slot). The 1.5× premium applies to the
  labor rate ONLY — per-crew billable extras (parking), including the
  fold-into-rate "hidden" mode, ride on OT hours at 1× and must never be
  multiplied. Training shifts neither bill nor accrue OT hours.
- The Post-Show tab's on-page total is reconciled against
  `GET /shows/<id>/post-show-labor/cost` (server math) — don't reintroduce a
  JS-only total.
- Per-day covering PM + day notes live in `show_labor_days`
  (`GET/PUT /shows/<id>/labor-days`); cover PM stores the contact NAME, same
  convention as the `production_manager` advance field.

## Per-page performance stats (admin Settings → Performance)
- `db_adapter.query_timer_hook` stopwatches every `execute()`/`executemany()`;
  app.py's collector (`_perf_record_query` / `_perf_finish_request` /
  `_perf_flush`) rolls finished requests up per `(day, endpoint)` in memory and
  flushes ~once a minute per worker with an ADDITIVE upsert into
  `perf_page_stats`, plus individual queries ≥ `perf_slow_query_ms` (default
  100 ms) into `perf_slow_queries`. Admin UI: `/admin/performance`.
- The upsert merges (counters add, min/max/slowest compare) so concurrent
  workers can flush the same row — don't replace it with INSERT OR REPLACE,
  which would clobber. Flushes on a stale SQLite fallback drop the batch
  (background-write rule). Retention is trimmed in `run_hourly_maintenance`.
- Background-job queries (no request context) are intentionally not tracked.
  Keep the hook path allocation-free and never let it raise.

## DB snapshot inspection & recovery (Settings → DB Snapshots)
- `snapshot_module.py` + `templates/snapshots.html`, wired by one
  `snapshot_module.register(app, …)` call next to the Prism registration.
  Reads the hourly/daily backups written by `run_hourly_backup` /
  `run_daily_backup` (plain `pg_dump` .sql.gz on PG, file copy .db on SQLite;
  per-server local disk).
- Dumps are parsed in **pure Python** (streaming COPY-block parser) — a
  snapshot is never loaded into the PostgreSQL server. Diff is keyed on the
  table's primary key (parsed from the dump / PRAGMA); values are normalized
  to COPY text form before comparing.
- Restore is preview → confirm → apply: apply re-derives the plan and
  compares its hash against the previewed one (409 on drift), runs in ONE
  transaction, audit-logs every row with before-images, and on PG re-syncs
  id sequences after inserts. Two modes: per-show rollback/resurrection
  (`shows` row + `SHOW_CHILD_TABLES`) and row cherry-pick from the diff view.
- `RESTORE_BLOCKED` tables (users/sessions/tokens/audit/email_send_log/
  perf/cluster) are inspect-only — don't widen without being asked; restoring
  `email_send_log` would re-send advance emails, `audit_log` would falsify
  history, `users` is the shared cross-app directory. Restore also refuses on
  a stale SQLite fallback and on snapshot↔live backend mismatch.

## Prism FM integration (SANDBOXED — keep it that way)
Prism is the building's primary scheduling system. The integration lives in
`prism_module.py` + `prism_bridge/` + `templates/prism.html`, wired into
app.py by ONE `prism_module.register(app, …)` call near the bottom plus the
`prism_auto_sync` scheduler job. Rules:
- The module only writes to its own tables (`prism_events`, `prism_sync_log`,
  `prism_venues`) and `prism_*` keys in `app_settings`. It touches main-app
  tables (`shows` / `show_performances` / `advance_data`) **only** inside
  `import_staged_events()` — manual import on `/prism`, or every pending NEW
  event when the opt-in `prism_auto_import_enabled` setting is on — plus ONE
  sanctioned sync write-through: `shows.prism_status` (the Hold/Confirmed tag
  on homepage cards) is kept current for linked shows. Don't widen that
  write-through surface without being asked.
- Prism's SDK is Node-only (GraphQL under the hood) — Python shells out to
  `prism_bridge/*.js` subprocesses (pattern validated in the PrismSDKTest
  repo). The SDK itself is installed from a vendor tarball and gitignored;
  see `prism_bridge/README.md`. `prism_bridge_dir` in settings can point at
  a stub directory for testing without credentials.
- Dedup is by `prism_events.prism_event_id` (unique). Re-syncs upsert;
  `content_hash` drives the "changed since import" badge.
- The scheduled job follows the background-job rules above: leader-gated AND
  refuses to act when configured-postgres ≠ active backend (stale SQLite
  fallback). Manual sync, settings, and import are admin-only routes.
- Debugging: every sync writes a `prism_sync_log` row with a capped debug
  log; the `/prism` page shows env checks (node/SDK/token/DB), sync history,
  and a raw-payload viewer per staged event.

## Sessions & the expiry watchdog (two clocks — don't conflate them)
- **App session** (DB-backed, `app_sessions`, 12 h): SLIDES with activity —
  the 5-minute role refresh (`_refresh_session_roles`) marks the session
  modified, which rewrites `expires_at = now + 12 h` and re-issues the cookie.
  Any open tab's polls keep it alive; it only runs out after a real gap
  (sleep/closed tab).
- **Gateway cookie** (`__Host-321gate`, signed, HttpOnly, 12 h): HARD deadline
  from the email-code verify. By design it cannot be extended in place — only
  re-verified. Don't add sliding behavior to it.
- **Watchdog** (`_initSessionWatch` at the bottom of `static/js/app.js`, active
  on any page with `.app-layout`): resyncs both clocks on load / tab focus /
  every 5 min via `GET /api/session/status` (app) and `GET /__gate/status`
  (gateway; HTTPS origins only — LAN would just 404), warns at 15/10/5 min,
  red under 60 s, verifies with the server then auto-reloads at zero (20 s
  cancellable grace). "Stay signed in" → `POST /api/session/extend`.
- **Keep `/api/session/status` side-effect-free**: it must never mark the
  session modified or set cookies — it's polled by idle tabs and must not
  keep sessions alive by itself (`/api/*` is also excluded from hover
  prefetch; keep it that way).
- Syslog events: `SESSION_EXPIRED` (expired sid presented, fires once),
  `SESSION_HARVEST count=N` (hourly sweep), `SESSION_EXTEND`, and
  `GATE_SESSION_EXPIRED` in the gateway journal (HTML navigations only —
  XHR polls stay silent).

## Advance sync & field presence (polling — no websockets anywhere)
- The show page's multi-user behavior is ALL HTTP polling from app.js:
  advance tab → `GET /shows/<id>/sync/advance` every **2 s** (merges other
  users' field values, returns presence); other tabs → `POST /shows/<id>/
  heartbeat` every 15 s (presence + "someone saved" banner only).
- **2 s is the floor, not a dial**: saves are debounced 1.5 s so faster
  polling can't deliver edits sooner, and every poll WRITES (presence upsert
  + prune into `active_sessions`). Don't lower it; don't remove the
  `_syncInFlight` overlap guard.
- Per-field presence: focusin/focusout in `bindAdvanceForm()` sets
  `_focusedField`, which rides on every poll into
  `active_sessions.focused_field` (one row per user per show — one focused
  field per user by design) and renders on other clients as the chip +
  typing dots in `_renderFieldIndicators()`. That renderer must keep
  removing `.field-presence-row` containers each poll (removing only the
  chips leaks empty rows), and keep `CSS.escape()` on the incoming field
  key. Server side, `_upsert_active_session()` clamps client-supplied
  tab/focused_field — it's the single choke point for both callers; keep it.
- Presence visibility window is 45 s (prune at 60 s): someone closing their
  tab mid-focus leaves a chip for up to ~45 s. Known/accepted.
- Conflict model is still last-write-wins with NO per-field versioning; the
  "don't echo my own writes" filter is per-show (`shows.last_saved_by`),
  not per-field. Any future live-typing work needs per-field authorship
  first — see the 2.34.0 README entry before touching this.

## Mobile view (shared templates + mobile.css) — check EVERY UI change in both modes
The site has a mobile presentation (iPhone is the reference device). It is NOT
a separate set of templates — the same Jinja templates render both modes, and
that is deliberate (no dual-maintenance drift). What switches:
- `_resolve_view_mode()` in app.py picks `mobile`/`desktop` per request:
  `?site=mobile|desktop` (one-off, prefetch-safe, no cookie) → `view_mode`
  cookie (set by POST `/account/view-mode`; `auto` clears it) → User-Agent
  sniff (phones yes, iPads deliberately desktop).
- In mobile mode base.html adds `class="mobile-view"` on `<html>`, loads
  `static/css/mobile.css`, and renders extra chrome: fixed top header
  (`m-header`), show-page tab strip (`m-showtabs`), bottom tab bar
  (`m-tabbar`), and reuses the desktop sidebar as a slide-in drawer
  (`m-drawer-open` on `<html>`). The desktop rail/collapse script and
  `force-rail` are skipped entirely in mobile mode.
- ALL mobile styling lives in `static/css/mobile.css` and every rule is
  scoped under `html.mobile-view` — don't put phone tweaks in style.css and
  don't put desktop styles in mobile.css.
- The "Switch to mobile/desktop site" links live in the sidebar/drawer
  footer next to the version number (`setSiteMode()` in base.html).

**Rule for future UI changes: any change to templates, style.css, or app.js
UI behavior must be checked in BOTH modes** (append `?site=mobile` /
`?site=desktop` to the URL to flip without a phone). New page chrome,
modals, or wide tables usually need a companion rule in mobile.css. Do not
gate features by view mode — mobile hides nothing; it only restyles.

## Hover preloading (Speculation Rules)
Logged-in pages carry a `<script type="speculationrules">` block (base.html)
that prefetches same-origin links on hover so navigation feels instant on
Chromium — but ONLY in a secure context (HTTPS via the gateway, or
localhost). Plain-HTTP LAN access (http://10.x.x.x) is served by the
`<link rel=prefetch>` hover fallback in app.js (also used by Firefox), and
`_prefetch_cache_window` in app.py marks prefetch-purpose responses
(`Sec-Purpose: prefetch`) privately cacheable for 30 s so the click can
reuse the hover's copy — don't widen that window or its conditions. Rules:
- Prefetch fetches HTML only — no JS runs on hover, so sync polls, presence,
  heartbeats, and read receipts are never triggered by a hover.
- **Any new GET route with side effects or expensive generation (PDFs,
  downloads, exports) must be added to BOTH exclusion lists** — the
  speculationrules block in base.html and the EXCLUDE regexes in app.js's
  `_initHoverPrefetch` — or given `class="no-prefetch"` on its links. (Better:
  make mutating routes POST, as the rest of the app does.)
- Multi-user staleness is handled two ways: the advance tab's first sync poll
  silently merges the freshest field values, and app.js reloads once any page
  served from a prefetch older than 30 s — measured by comparing the
  `page_rendered_at` stamp (context processor in app.py) against a HEAD
  request's Date header, both server clocks, so client clock skew can't
  cause false reloads. Don't remove the stamp from `inject_version()`.
- Ordinary responses must stay non-cacheable (no Cache-Control on HTML) —
  the ONLY exception is the 30 s prefetch-purpose window above. Chrome's
  speculation prefetch cache is separate and capped at ~5 min. Don't add
  Set-Cookie to ordinary GETs — a cookie change invalidates pending
  prefetches (the DB session interface already only sets cookies when the
  session changes).

## Two deployment targets — ALWAYS tell the user what to redeploy
This project ships to **two** machines, and a change often only affects one.
At the end of any change that touches code/config, **state plainly which
side(s) need to be redeployed** and give the commands. Never leave the user
to guess.

- **Main app** (internal server, e.g. `10.201.2.101`): anything in `app.py`,
  `init_db.py`, `db_adapter.py`, `templates/`, `static/`, `prism_*`,
  `start.sh`, `install.sh`, or the app's `.env`.
  → `cd <app dir> && git pull && sudo systemctl restart 321theater`
  (schema migrations auto-apply on startup — no manual `init_db --migrate`).

- **VPS gateway** (public box `cyclorama`): anything under `gateway/`
  (`gateway_app.py`, its templates/static, `Caddyfile.example`,
  `321gateway.service`, `install.sh`, `gateway.env.example`), or the VPS's
  `/etc/321gateway/gateway.env`.
  → `cd /opt/321gateway-src && git pull && sudo bash gateway/install.sh`
  (add `--rewrite-caddy` only when the `GATE_APP_INTERNAL_URLS` server list
  changed).

- **Both**: a change spanning the internal OTP API *and* the gateway client,
  or a shared secret / cookie-name / server-list change — say so and give
  both command blocks, and flag anything that must stay in sync between the
  two `.env` files (`GATEWAY_SHARED_SECRET` == `GATE_SHARED_SECRET`).

- **Neither / docs-only**: say that too, so the user knows no redeploy is
  needed.

## Git
Develop on the branch you were assigned; commit with clear messages; push with
`git push -u origin <branch>`. Do not open a PR unless explicitly asked.
