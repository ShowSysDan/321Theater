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
- **Overtime:** >40h per technician per show/event bills at 1.5×, split by
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

## Git
Develop on the branch you were assigned; commit with clear messages; push with
`git push -u origin <branch>`. Do not open a PR unless explicitly asked.
