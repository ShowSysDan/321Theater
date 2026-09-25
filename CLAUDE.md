# 321Theater — notes for Claude

3·2·1→Theater is a Flask production-advance / day-of-show management tool for
Dr. Phillips Center. Most of the app is in a single large `app.py`; HTML lives
in `templates/`, static assets in `static/`.

## EVERY session: bump the version and write a README changelog entry
Before committing ANY code/template/config change (docs-only changes exempt):
1. Bump `APP_VERSION` in `app.py` — semantic versioning: bug fix → PATCH,
   new feature → MINOR (reset PATCH to 0), breaking schema / re-init → MAJOR.
2. Update README.md: the **Current version** line near the top AND a new
   entry at the top of the **Version history** list describing what changed
   and why (match the existing entries' style; end with which deployment
   target(s) need redeploying).
3. Include the bump + README entry in the same commit as the change.
Full rules live in README.md → "Version Numbering". Do not skip this — the
version shows in every page's sidebar footer and is how the user verifies a
deploy actually took.

## Database: PostgreSQL-native, PostgreSQL ONLY (3.0.0+)

**There is no SQLite anywhere.** The SQLite backend, the `advance.db`
bootstrap, `db_type`, the SQL-dialect translation layer and the silent
fallback connection were all removed in 3.0.0. Don't reintroduce any of them,
and don't write "portable" SQLite/PG code or `if backend == …` branches.

- **Config:** `db_config.ini` `[postgresql]` in the app dir (gitignored), or
  the path in `THEATER_DB_CONFIG`. `db_adapter.read_db_settings()` parses it
  (cached 30 s). That file is the ONLY bootstrap: `app_settings` and all
  data live in PostgreSQL.
- **No fallback, fail loud:** `db_adapter.connect()` / `get_db()` RAISE
  `db_adapter.DatabaseUnavailable` when PG is unconfigured or unreachable.
  Requests get a 503 (app-level errorhandler; JSON for API/XHR). Background
  jobs catch it and skip the run with an ERROR log (see
  `run_scheduled_pdf_emails`, `run_no_labor_alerts`, prism auto-sync). The
  gateway OTP API fails closed via `_gateway_db()`. `get_app_setting()`
  returns the caller's default on an outage (logged), so a job can never act
  on stale settings; it only sees defaults and then fails at `get_db()`.
  History: the old silent SQLite fallback made the scheduled-email job read
  `advance_email_enabled='0'` from the bootstrap and send nothing. That class
  of bug is now impossible. Keep it that way: **never catch
  DatabaseUnavailable and carry on with made-up data.**
- `get_db()` hands out a connection from a small **per-process pool**
  (3.3.0, `db_adapter._ConnectionPool`; search_path set via startup
  `options`), so it's safe from background threads. Always `close()` — that
  RETURNS it (rolled back first, so an uncommitted write is discarded exactly
  as a real close did). Opening a connection costs ~10 ms (TCP/TLS + SCRAM +
  backend fork) and cold backends run their first queries slowly; before the
  pool, connects were most of every page's time (show page: 10 connects,
  ~100 of its ~160 ms). Pool rules — keep them:
  - Nothing waits on the pool: empty → open a new connection; full → close
    the returned one. `[postgresql] pool_max_idle` in db_config.ini caps
    IDLE connections per process (default 4 → 16 per 4-worker server; 0 =
    the old connect-per-call behavior). Mind PG's `max_connections`
    (default 100) across servers + the sister apps sharing the DB.
  - A connection that touched SESSION state (session-level advisory lock,
    `SET`, `LISTEN`, temp table, `PREPARE`, or `.raw`) is closed, not pooled
    (`_leaves_session_state()`) — e.g. file_store's migration lock. Don't
    add session-state SQL expecting it to persist across `get_db()` calls.
  - A dead idle connection (PG restart) is replaced transparently on its
    FIRST statement only (safe: nothing in that transaction can have
    committed); later failures surface as before. A still-down PG raises
    DatabaseUnavailable from that reconnect, so outages still 503.
  - After `close()` the wrapper refuses all use (InterfaceError) — the real
    connection may already be lent to another thread.
  - Explicit-settings `db_adapter.connect(settings)` and `raw_connect()`
    (init_db migrations, tests) are never pooled.

### Writing SQL (native psycopg2, sent VERBATIM)
`db_adapter.DBConnection.execute()` hands SQL straight to psycopg2 (DictCursor
rows: `row['col']`, `row[0]`, `.get()`, `dict(row)`). No rewriting happens, so:
- Placeholders are `%s` (never `?`). IN-lists: `','.join(['%s'] * len(ids))`
  (NOT `'%s' * n`, which `join` splits into characters).
- Upserts: `INSERT … ON CONFLICT (cols) DO UPDATE SET c = EXCLUDED.c` or
  `ON CONFLICT DO NOTHING`. The conflict target must match a real unique
  index/PK.
- New ids: `INSERT … RETURNING id` then `cur.fetchone()['id']`. There is no
  `lastrowid` and no hidden `lastval()` call. NEVER re-find a new row with
  `SELECT … ORDER BY id DESC LIMIT 1` — with 4×4 workers it can return another
  request's row (eight such lookups were fixed in 3.0.1).
- **Types:** never use `REAL` — in PostgreSQL it's 32-bit float4 (~7 digits;
  SQLite's REAL was 64-bit). Money/rates/hours are `DOUBLE PRECISION` (what
  the Python float math expects; `NUMERIC` would hand back `Decimal` and break
  float arithmetic + JSON). Booleans stay `INTEGER` 0/1 (compare `= 1`).
- **Clocks:** `CURRENT_TIMESTAMP`/`NOW()` are the PG server's *local* time
  (SQLite's were UTC). A column must be written and compared on the SAME
  clock: DB-defaulted timestamps → compare with `NOW() - INTERVAL …`;
  columns you stamp from Python → compare with the same Python clock.
- **Precision:** PG timestamps carry microseconds and `jsonify()` renders a
  datetime as a whole-second HTTP date — any timestamp used as a round-trip
  cursor must be sent as `isoformat()` (see `_sync_cursor()`), or `> since`
  keeps re-matching the last row.
- Time math: `NOW() - INTERVAL '60 seconds'`. String literals are
  single-quoted: `status="active"` is a COLUMN reference in PG (that bug 500'd
  the public show PDFs until 3.0.0). Case-insensitive match: `ILIKE`.
- **Literal `%` in SQL that also has params must be `%%`**, or better, bind
  the pattern (`LIKE %s` with `'prefix_%'`). With no params the adapter passes
  `None`, so param-less literals like `LIKE 'syslog_%'` are fine.
- A `?` in SQL text is now just a `?` (it used to be blindly rewritten), but
  bind user-facing text as a parameter anyway.
- PG returns `date`/`datetime`/`Decimal` objects. Coerce with `_as_date()` /
  `_as_dt()` (app.py) before date math, and `json.dumps` row snapshots with
  `default=str` (a bare dumps raises and, inside never-raise helpers like
  `log_audit`, the row silently vanishes; this dropped every
  snapshot-bearing EDIT/DELETE audit entry on PG until 2.18.0).
- `DATE` columns can't be compared to `''` or `LIKE`d without a cast
  (`CAST(show_date AS TEXT) ILIKE %s`).
- Errors: any exception rolls the transaction back before re-raising (PG
  aborts the whole transaction on error). UniqueViolation →
  `db_adapter.DBIntegrityError`; other integrity errors are psycopg2's own.

### Schema (init_db.py)
- `PG_SCHEMA` (CREATE TABLE/INDEX IF NOT EXISTS) + `_apply_column_migrations`
  (ADD COLUMN IF NOT EXISTS …) are the whole schema. `migrate_db_postgres()`
  runs both on every startup. `python3 init_db.py` also seeds a FRESH
  install: seeds only go into EMPTY tables, and admin/admin123 only when
  `users` is empty (shared cross-app directory). Never make startup seed.
- **Startup migrations run in every worker on every start.** They're
  serialized by an advisory lock and must stay LOCK-FREE when there's nothing
  to do: `_already_applied()` skips any ADD COLUMN / DROP NOT NULL / CREATE
  INDEX / CREATE TABLE the catalog already shows, because PG takes the table
  lock before honouring `IF NOT EXISTS` (that deadlocked live requests and
  locked the cross-app `shared.users` on every restart until 3.0.1). New
  migration statements must be one of those recognised forms, or be a
  data backfill that only touches rows it changes, wrapped via `_backfill()`
  (savepoint). One-time backfills must be gated on "table just created" —
  the old every-start billable-items backfill silently re-enabled charges
  PMs had removed.
- New table → add to `PG_SCHEMA` (and `SHARED_TABLES` if it belongs in the
  shared schema). New column on an existing table → add it to the
  CREATE TABLE *and* an `ADD COLUMN IF NOT EXISTS` line, so fresh installs
  and upgrades converge (a fresh install and an upgraded DB were verified
  column-for-column identical in 3.0.0; keep it so).
- **`PG_SCHEMA` is split on `;` with no real parser.** Keep every statement
  self-contained and never put a `;` inside a string literal. Full-line `--`
  comments are stripped before the split (2.42.0). A `;` in 2.38.0's
  venue_colors comment once glued comment-tail onto the CREATE TABLE, so the
  table was never created ("Failed to load venue list").

### Verifying SQL changes
There's no SQLite to test against: test against a real PostgreSQL (the 3.0.0
work used a local PG 16). A cheap static check that catches syntax, unknown
tables/columns and bad ON CONFLICT targets is to `PREPARE` each statement
(with `%s` → `$n`) against a migrated schema.

## PDF rendering (WeasyPrint) — always pass the shared font config
Every `HTML(...).write_pdf(...)` must pass `font_config=_wp_font_config()`
(security_module gets it as the `pdf_font_config` dep). Without it WeasyPrint
builds a new FontConfiguration per render and leaks ~250 KB of native memory
per PDF (found by the 3.0.1 soak). It's per-THREAD on purpose (Pango font
maps aren't shared across threads). Reuse is only safe because no PDF
template uses `@font-face` — if one ever does, revisit this.

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
- `get_db()` is context-free (a pooled connection per call, never shared
  while checked out) — safe to call from background threads, not just
  request handlers.

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
- **Labor day presets** (2.37.0): `labor_presets` + `labor_preset_rows`
  (position, quantity, times, notes) — authored in Settings → Job Positions
  (`@scheduler_required`), applied from a day-block header dropdown. Apply is
  a SERVER-side transaction (`POST /shows/<id>/labor-presets/<pid>/apply`)
  that appends `quantity`×rows to `labor_requests` — deliberately unlike
  schedule templates' client-side DOM apply, because labor rows autosave
  individually (no bulk form save exists to catch a half-applied day).
  Presets never delete or alter existing requests.

## Show file attachments — deletion is ARCHIVE, never destroy (2.36.0)
- `show_attachments` soft-deletes: `deleted_at`/`deleted_by` stamp the row,
  DB-stored blobs are gzip-compressed in place (`is_compressed`; decompressed
  transparently on download and on restore), and the S3 object is KEPT.
  Only admin `…/purge` (Settings → System → Files) deletes bytes — and only
  for rows already archived, so a purge can never hit a live file. Don't
  reintroduce a direct hard delete, and never S3-delete on archive.
- ANY editor with show access may archive any file (PM team shares shows) —
  the uploader-only check is gone on purpose; readonly/restricted still 403.
- Every query that feeds users or PDFs must filter `deleted_at IS NULL`
  (show/field lists, the PDF attachment merge + its content-hash fingerprint,
  the migrate-to-S3 backfill — compressed bytes must never be uploaded as-is).
  The admin File Manager is the ONE surface that shows archived rows (sorted
  first — they're the designated first candidates when freeing space).
- Freshness: `attachments_rev` (`count:max(id)` of live rows —
  `_ATTACHMENTS_REV_COLS` selected inside each poll's one `FROM shows s`
  statement, indexed by `idx_show_attachments_show`) rides on the 2 s advance
  sync and 15 s heartbeat responses; app.js `_checkAttachmentsRev()` reloads file
  lists when it changes. This is also the cross-INSTANCE refresh path (all
  instances share one PostgreSQL) — keep it in both poll responses.
- Syslog: FILE_ARCHIVE / FILE_RESTORE / FILE_PURGE (+ existing FILE_UPLOAD).

## S3 / SeaweedFS storage (s3_storage.py) — config source + failover (2.39.0)
- Config source is chosen by `s3_config_source` app_setting: `ini` (default —
  `db_config.ini [seaweedfs]`, single endpoint, the historical behavior) or
  `gui` (app_settings keys `s3_endpoints` JSON list / `s3_access_key` /
  `s3_secret_key` / `s3_bucket`, edited in Settings → System → Database →
  File Storage). app.py injects the GUI reader via
  `s3_storage.set_settings_provider(_s3_gui_settings)` — s3_storage must
  NEVER import app.py, and a provider failure falls back to the ini
  (logged `S3_SETTINGS_PROVIDER_ERROR`), so a DB hiccup can't break storage.
- Multiple endpoints = redundant SeaweedFS S3 gateways fronting ONE cluster:
  `upload/download/delete` go through `_with_failover()` (try in order,
  remember the last endpoint that worked; syslog `S3_ENDPOINT_ERROR` and
  `S3_FAILOVER`). `test_connection()` deliberately tests every endpoint
  WITHOUT failover so a dead gateway is visible while its sibling covers.
- Settings are cached 30 s; call `s3_storage.clear_settings_cache()` after
  writing S3 settings. The secret key is write-only in the UI (blank keeps).

## File redundancy S3 <-> PostgreSQL (file_store.py, 3.1.0)
- A stored file = a row in one of five tables, each with a bytes column AND
  an S3 key column (`file_store.KINDS`: show_attachments.file_data/s3_key,
  export_log.pdf_data/s3_key, asset_types.photo/photo_s3_key,
  show_external_rentals.pdf_data/s3_key, pdf_templates.pdf_data/s3_key) plus
  `content_sha256` (hash of the UNCOMPRESSED content). A row can have either
  copy or both. Don't add a central file table without being asked; the
  user chose the existing columns.
- **Every read goes through `file_store.read_bytes(kind, row)`**. It follows
  `file_read_preference` ('s3' default | 'db') and falls back to the other
  copy (syslog FILE_READ_FALLBACK). It raises FileMissing (→ 404) or
  FileUnavailable (→ 503). Never add a new `s3_storage.download_file()` /
  `bytes(row['pdf_data'])` read path. The row must carry both columns (and
  `id`; `is_compressed` for attachments that can be archived).
- **Write paths**: after a successful S3 upload, clear the DB copy ONLY when
  `file_store.keep_db_copy()` is False (dual-write off = pre-3.1.0 behavior).
  Export pushes use `_mark_export_in_s3()`. Always stamp `content_sha256`.
- Migration tool (`/settings/file-storage/*`, admin): COPY ONLY. It never
  clears or deletes the source copy; don't add a "move" mode without being
  asked (the old move-to-S3 route was removed on purpose). Runs are background
  threads holding pg advisory lock `MIGRATION_LOCK_KEY` on their own
  connection (one run cluster-wide; a dead worker frees it and the run reads
  'interrupted'). Progress/errors live in `file_migration_runs` (restore-blocked
  in snapshots). Duplicate-awareness = skip rows whose target exists +
  SHA-256 checks + conditional UPDATEs (`… AND key IS NULL AND md5(blob)=…`).
- Archived attachments are gzip in the DB: S3→DB stores them compressed
  (`is_compressed=1`), DB→S3 uploads decompressed bytes.
- `_advance_attachments_fingerprint()` must stay storage-independent (no
  s3_key / blob length), or a copy would cut a new advance version.
- No user file may live on an app server's local disk (a second instance
  couldn't reach it; audited 3.2.2, table in README → "Where stored data
  lives"). Logos (`app_settings.logo_data`, `venue_logos.logo_data`) are
  data-URL TEXT in PG only, not a file_store kind. Any new upload goes into
  one of the KINDS tables (or PG), never to disk.

## PDF paperwork theming (per-venue colors, 2.38.0)
- `venue_colors` table (venue_name PK, mirrors `venue_logos`) +
  `_get_venue_pdf_colors(db, venue)` → palette dict (primary/secondary plus
  derived tints via `_mix_hex`) or None. All 8 PDF builders pass `pdf_colors`;
  the combined invoice themes only when every billed show shares one venue.
- Templates in `templates/pdf/` define Jinja vars (`C1`, `C2`, `C1_BG`, …)
  at the top whose fallbacks are each template's ORIGINAL literal hexes —
  with no colors configured, output must stay byte-identical (this was
  verified by diffing rendered CSS). When adding a new color to a pdf
  template, use the vars (or add a tint to the helper), never a raw brand
  hex; and give any new var a fallback matching the un-themed look.
- Admin UI: Settings → System → Branding & Paperwork → the combined **Venue
  Branding** panel (2.42.0): ONE dataset (`GET /settings/venue-branding` —
  venues + logo + colors; the old venue-logos/venue-colors GET lists are
  gone) with the unchanged POSTs (`/settings/venue-logos[/delete]`,
  `/settings/venue-colors` — hex-validated, blank-both = delete row). Colors
  land in the rendered HTML, so the export content-hash correctly cuts a new
  PDF version when a venue's colors change.

## Optional feature modules (`APP_MODULES` + Settings → System → Modules)
- `APP_MODULES` registry + `module_enabled(key)` in app.py; flags live in
  `app_settings` as `module_<key>_enabled` ('1'/'0', missing = the entry's
  default), toggled at runtime by an admin (`GET/POST /settings/modules`,
  syslog `MODULE_TOGGLE`) — no restart, all instances. A disabled module's
  routes 404 (checked per request) and its UI hides; data is always kept.
- First module: **Security Sign-In Sheets** (`security_module.py`, 2.41.0;
  key `security_signin`, default ON). Sandboxed like Prism/Snapshots: one
  `register(app, **deps)` call, owns only `security_signin_names` (included
  in show merge moves, FK-cascade delete, snapshot per-show restore). Editor
  UI = show page **Security tab** (show.html `tab-security` pane, between
  Labor Requests and Assets; also in base.html's mobile `m-showtabs`), which
  only talks to the module's JSON endpoints (`/shows/<id>/security/*`);
  paste + CSV import share ONE server parser (`…/security/parse`). The PDF
  (`…/security/sheet.pdf`) themes via `pdf_colors` like all paperwork.
  Syslog: SECURITY_SIGNIN_SAVE / SECURITY_SIGNIN_EXPORT.

## Maintenance notices & message audiences (3.2.0)
- Settings → System → Messages. `maintenance_notices` (app schema) owns the
  notice; its banners are ordinary `site_messages` rows linked by
  `message_id` (maintenance banner) / `completion_message_id` (24 h
  "complete" banner), both ON DELETE SET NULL, so an admin deleting a banner
  under Site-Wide Messages can't break the notice. Routes:
  `/settings/maintenance[/<id>[/complete]]` + `/settings/maintenance/audience`
  (per-group counts), all `@admin_required`.
- **Audience groups** (`AUDIENCE_GROUPS` in app.py): admin / staff / user /
  viewer. `_audience_group_of(role, is_document_viewer)` — the viewer FLAG
  wins over the role. Never use `is_app_*` for this. `site_messages.audience`
  is a JSON list, **NULL = everyone** (the shared table may be read by other
  apps; all four groups are stored as NULL on purpose).
  `get_active_messages()` filters by the session's group; doc viewers reach
  `/api/messages` via `_VIEWER_ALLOWED_ENDPOINTS`.
- Message `scheduled_for` / `expires_at` are local wall-clock (datetime-local
  input) → compare with `datetime.now()`, never `utcnow()`.
- **Sign-in page messages** (3.2.1): `site_messages.show_on_login = 1` AND
  `audience IS NULL` → rendered on login.html via `_login_page_messages()`
  (all login renders go through `_render_login()`). Pre-auth there is no
  group, so a targeted message must NEVER appear there; the create/edit routes
  and `_maint_banner_upsert()` force the flag to 0 unless audience is NULL.
  An outage returns no messages (display-only) so the sign-in page never
  503s; don't extend that catch to anything that acts on data.
- Sends are synchronous admin actions (no leader gate), BCC'd in batches of
  `_MAINT_EMAIL_BATCH`, recipients from `_audience_emails()` (skips locked /
  pending / unconfirmed) + validated extras. The DB connection is closed
  across the SMTP round-trips. `_send_email(..., high_priority=True)` sets
  X-Priority / Importance / X-MSMail-Priority on both providers.
- `error_context` keys are splatted into `_log_email_error()`, so any new key
  must be accepted there (`purpose` was missing until 3.2.0 and turned every
  failed send with a purpose into a TypeError).

## Asset availability — batch it in loops (3.3.2)
- `_get_asset_availability_many(db, type_ids, start, end)` and
  `_component_demand_many(db, type_ids, start, end)` answer any number of
  types in ≤5 / 2 queries; the single-type `_get_asset_availability` /
  `_component_demand` are wrappers over them (keep it that way, so single
  and batched can't drift). Anything that loops over types, lines or shows
  must call the `_many` form (group by date window when windows differ)
  and `_show_rental_windows()` for many shows. Test/demo shows stay
  excluded inside the demand query.

## Audit-log Undo — explicit list only (3.3.1)
- `audit_undo()` reverses only actions in `_UNDO_ACTIONS` (action → (entity_type,
  kind)). Never go back to guessing the kind from the action suffix: that made
  ASSET_MEMBER_ADD (logged against the PARENT system type) delete the system
  and ASSET_ITEM_ADD (TYPE id logged as the item id) delete an unrelated item.
  To add an action: its entity_id must be the row it created/changed in
  `UNDO_TABLE_MAP[entity_type]`, and for update/delete `before=` must be
  `_snapshot_row()` of that row.
- Create-undo is refused while any FK row references the target
  (`_undo_blocking_refs`, catalog-driven; only `_UNDO_INCIDENTAL_REFS` are
  ignored). `_snapshot_row()` omits BYTEA columns; restores write only real
  non-BYTEA columns. Syslog: AUDIT_UNDO / AUDIT_UNDO_REFUSED / AUDIT_UNDO_FAILED.

## Per-page performance stats (admin Settings → Performance)
- `db_adapter.query_timer_hook` stopwatches every `execute()`/`executemany()`;
  app.py's collector (`_perf_record_query` / `_perf_finish_request` /
  `_perf_flush`) rolls finished requests up per `(day, endpoint)` in memory and
  flushes ~once a minute per worker with an ADDITIVE upsert into
  `perf_page_stats`, plus individual queries ≥ `perf_slow_query_ms` (default
  100 ms) into `perf_slow_queries`. Admin UI: `/admin/performance`.
- The upsert merges (counters add, min/max/slowest compare) so concurrent
  workers can flush the same row — don't replace it with INSERT OR REPLACE,
  which would clobber. A flush that can't reach PostgreSQL drops its batch. Retention is trimmed in `run_hourly_maintenance`.
- Background-job queries (no request context) are intentionally not tracked.
  Keep the hook path allocation-free and never let it raise.

## DB snapshot inspection & recovery (Settings → DB Snapshots)
- `snapshot_module.py` + `templates/snapshots.html`, wired by one
  `snapshot_module.register(app, …)` call next to the Prism registration.
  Reads the hourly/daily backups written by `run_hourly_backup` /
  `run_daily_backup` (plain `pg_dump` .sql.gz, per-server local disk; legacy
  SQLite-era .db files are ignored).
- Dumps are parsed in **pure Python** (streaming COPY-block parser) — a
  snapshot is never loaded into the PostgreSQL server. Diff is keyed on the
  table's primary key (parsed from the dump); values are normalized
  to COPY text form before comparing.
- Restore is preview → confirm → apply: apply re-derives the plan and
  compares its hash against the previewed one (409 on drift), runs in ONE
  transaction, audit-logs every row with before-images, and re-syncs
  id sequences after inserts. Two modes: per-show rollback/resurrection
  (`shows` row + `SHOW_CHILD_TABLES`) and row cherry-pick from the diff view.
- `RESTORE_BLOCKED` tables (users/sessions/tokens/audit/email_send_log/
  perf/cluster) are inspect-only — don't widen without being asked; restoring
  `email_send_log` would re-send advance emails, `audit_log` would falsify
  history, `users` is the shared cross-app directory.

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
  skips the run (logged) when PostgreSQL is unreachable. Manual sync, settings, and import are admin-only routes.
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
  into `active_sessions`; the 60 s prune runs at most every 30 s per
  worker). Don't lower it; don't remove the `_syncInFlight` overlap guard.
- Each poll reads all its state in ONE `FROM shows s` statement (changed
  fields as `json_object_agg`, the cursor's MAX(updated_at), last-saved,
  attachments_rev) — one snapshot, so a save committing mid-poll can't move
  the cursor past an edit the client never got. A deleted show → 404 (the
  presence upsert would 500 on the FK). `save_advance` writes only fields
  whose value changed (one `unnest` upsert), so `updated_at` — and thus what
  other tabs receive — moves only for real edits; keep it that way.
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
