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
(app.py) before doing date math.

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

## Prism FM integration (SANDBOXED — keep it that way)
Prism is the building's primary scheduling system. The integration lives in
`prism_module.py` + `prism_bridge/` + `templates/prism.html`, wired into
app.py by ONE `prism_module.register(app, …)` call near the bottom plus the
`prism_auto_sync` scheduler job. Rules:
- The module only writes to its own tables (`prism_events`, `prism_sync_log`)
  and `prism_*` keys in `app_settings`. It touches main-app tables
  (`shows` / `show_performances` / `advance_data`) **only** inside
  `import_staged_events()`, which runs when an admin explicitly imports
  selected events on `/prism`. Don't add automatic write-through to shows
  without being asked.
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
