# 3·2·1→THEATER — Production Management System

3·2·1→THEATER (321Theater) is a web-based production advance and day-of-show management tool built for Dr. Phillips Center for the Performing Arts (dpc). It provides a central place to fill out advance forms, build production schedules, record post-show notes, manage labor requests, track inventory and rentals, send schedule emails, and share documents with crew and clients.

---

## Version Numbering

**Current version: `2.26.0`**

This project uses **semantic versioning**: `MAJOR.MINOR.PATCH`

| Segment | When to increment |
|---------|------------------|
| **MAJOR** | Breaking schema changes, major architectural overhaul, or changes that require a full DB re-init |
| **MINOR** | New feature sets added (e.g. asset manager, user system enhancements, messaging system) |
| **PATCH** | Bug fixes, security patches, small UI tweaks, wording changes |

### Rules for AI coding sessions

> **IMPORTANT for future AI sessions:** Before committing any change, determine which version segment to increment and update `APP_VERSION` in `app.py`. The format is `'MAJOR.MINOR.PATCH'` as a string constant near the top of the file. Do not skip this step. The version displays in the sidebar footer of every page.
>
> - New feature → increment MINOR (reset PATCH to 0)
> - Bug fix only → increment PATCH
> - Schema changes requiring migration → evaluate MAJOR vs MINOR based on impact
> - Always commit the version bump in the same commit as the feature/fix

Version history:
- `2.26.0` — **Editable asset rental periods (date-only, bounded to the show window) + repricing.** The RENTAL PERIOD of an asset line can now be edited in place — on the show's Assets tab and on the Asset Approvals portal — instead of being fixed at add time. Periods are plain **date ranges**: the API now serialises rental dates as ISO dates (no more "Fri, 24 Jul 2026 00:00:00 GMT" timestamps, which PostgreSQL date columns produced through Flask's JSON encoder). Rental dates are **bounded to the show's production window** (load-in → load-out from the advance page, falling back to performance dates then the show date): the date pickers carry min/max and the server rejects out-of-window dates on both add and edit (a new shared `_show_rental_window()`/`_clean_rental_dates()` pair — the same window that supplies the rental defaults). Because the per-unit locked price is the rate-card formula over the rental duration, **changing the window re-locks the price** for the new duration (smart-cap daily/weekly math, consumables unaffected); any manual approver override was priced for the old window, so it is cleared and the usual approval reset flags the line for re-review — the approvals page updates the price cell, restore button and line total in place. Editing keeps all existing server-side enforcement: per-type availability, unit-pinned booking conflicts, System/Package component shortages, and the concurrent-writer rollback paths (which now also restore the price columns). Range edits are fluid: dragging start past end pulls end along (and vice versa) rather than erroring. Qty/notes edits on legacy lines whose dates predate the current show window are still allowed — the window check only runs when dates are actually being changed.
- `2.25.0` — **Technician overtime (40h per show/event) + per-day covering PM & day notes.** (1) **Overtime:** hours one technician works beyond **40 on a single show/event** now bill at **time and a half**, accumulated by total hours (never by days worked). The split is computed in the shared cost engines (`_calc_labor_cost_for_show` for estimates, `_calc_post_show_labor_cost` for settlement actuals), so it appears everywhere automatically: the Labor Requests estimate box, the Labor Estimate & Pre-Show Estimate PDFs, the Post-Show tab, and the Final/Combined Invoice PDFs — each OT portion as its own highlighted **"Overtime (1.5×)"** line right after the shift that crosses 40. Before scheduling, lines are grouped into technician tracks by scheduled crew member, else requested name, else per-position slot (the Nth line of a position each day is assumed to be the same tech across the run); settlement groups by the actual scheduled crew. The 1.5× premium applies to the **labor rate only** — per-crew pass-through charges (e.g. parking passes), including when folded invisibly into the hourly rate, ride on OT hours at 1× and are never multiplied. Training shifts never accrue OT. The Post-Show tab now reconciles its on-page grid and total against a new authoritative endpoint (`GET /shows/<id>/post-show-labor/cost`) so the page always matches the invoice PDF. (2) **PM on duty per day:** a new `show_labor_days` table stores, per show per work-date, a **covering PM** (picked from the same Production contacts pool as the show's PRODUCTION MANAGER field) and **day notes**. Both are editable from each day-block header on the show's Labor Requests tab and on the Labor Scheduler, travel with the day when it's re-dated, and surface on the **Labor Overview** (covering PM replaces the show PM with a "day" badge; day notes render alongside the show's labor notes) and in the scheduler API (`labor_days` per show). Endpoints: `GET/PUT /shows/<id>/labor-days`, `GET /api/production-managers`.
- `2.24.0` — **MOTD / site-message read receipts, plus a duplicate-banner fix.** (1) **Who has seen & dismissed each message:** a new `site_message_views` table records the first time each user is served a banner (tracked in `/api/messages`, the endpoint the browser actually renders from, via idempotent `INSERT OR IGNORE` so the original `seen_at` wins). Dismissals were already tracked in `site_message_dismissals`; this adds the missing "seen" half. Settings → Site-Wide Messages now shows a **"👁 seen · ✓ dismissed"** count per message, and a new admin-only endpoint (`GET /settings/messages/<id>/receipts`) powers a **Read Receipts** modal listing exactly which users viewed and which dismissed each message, with timestamps. (2) **Bug fix — MOTD showed twice on the home screen:** the dashboard rendered site messages server-side *and* `base.html` rendered the same messages globally into `#site-msg-container` (loaded via `/api/messages`), so every banner appeared twice on `/dashboard`. The dashboard's redundant server-side block (and its `dismissMotd` handler / `motd_messages` route argument) is removed; the single global banner path is now the only renderer.
- `2.23.0` — **Pre-show labor & asset estimating (client quoting before anything is scheduled).** Previously a show's labor carried no dollar value until a technician was scheduled, so there was no way to quote a client up front. This release adds a full estimate path. (1) **Estimate rate resolution:** the Staffing tab's "Estimated Labor Cost" box now produces real numbers before scheduling. Each labor line is priced by its position's **special rate** (`job_positions.override_rate`) when set, otherwise the **highest standard technician rate** — `_estimate_hourly_rate()` = `MAX(hourly_rate)` across pay-rate levels flagged for estimating. Once a tech is actually scheduled, that line switches to the assigned tech's real rate; still-unscheduled lines are tagged **"est."** in the table and PDFs. (2) **Include/exclude rate levels from estimates:** a new `pay_rate_levels.include_in_estimate` column (default on) with an **"In Estimate"** toggle column in Settings → Pay Rate Levels, so test/placeholder levels (e.g. an $834/hr "Super Technician") can stay in the system for testing without ever becoming the rate an estimate reaches for. Toggling writes a `PAY_LEVEL_ESTIMATE_TOGGLE` syslog line (and a `PAY_LEVEL_EDIT` audit entry). (3) **Per-crew billable extras + "fold into hourly rate" on the estimate:** the picker and fold toggle (previously only on the Post-Show tab) are surfaced next to the estimate box, sharing the same per-show settings — the estimate now honours both, using `_calc_labor_cost_for_show()` which was extended with the fold branch and now counts every chargeable (non-training) requested line as crew rather than only scheduled ones. (4) **Labor Estimate PDF:** an export button in the estimate box generates a branded labor estimate quote (`/shows/<id>/labor-estimate.pdf`, `LABOR_ESTIMATE_EXPORT` syslog). (5) **Pre-Show Estimate PDF:** a 5th card under Export & Files generates a **combined labor + asset estimate/quote** (`/shows/<id>/pre-show-estimate.pdf`, `PRE_SHOW_ESTIMATE_EXPORT` syslog). (6) **Assets "Invoice PDF" → "Asset Estimate PDF":** the Assets-tab button, the PDF's title/subtitle, and its download filename are relabelled as an estimate (route path and internal `asset_invoice` layout key unchanged). (7) **UI polish:** the Export History re-download control is now a labelled "↓ Download" primary button instead of a bare glyph; and the Post-Show Actual Labor "billed @ rate" (the folded effective rate) is rendered large, bold and in the accent color instead of tiny grey text.
- `2.22.0` — **Four scheduler/billing improvements plus a post-show labor fix.** (1) **Asset Approvals — "Hide shows with no requests" filter:** a toolbar checkbox on `/assets/approvals` collapses the "No requests" empty shows out of the list so the approver sees only shows that actually have assets/external rentals to review. Purely client-side, persisted in `sessionStorage` so it survives the full-page reloads that approve/add/remove trigger. (2) **No-labor alerts for schedulers:** a new leader-gated APScheduler job (`run_no_labor_alerts`, top of every hour, acts during the shared `pdf_email_send_hour`) warns schedulers about active shows approaching their date with **no labor requested yet**. Two independent, configurable windows (default 14 days and 7 days out), each firing once per show via all-time dedup in `email_send_log` (`trigger_type='no_labor'`); delivery is **both** a single digest email to all schedulers that lists every due show (the breakdown) and a per-show in-app notification linking to that show's Labor Requests. Settings live in Settings → Email → "Labor — No-Request Alerts" (enable + two day thresholds). Mirrors the scheduled-PDF-email architecture exactly, including the stale-SQLite-fallback guard. Adds the `NO_LABOR_ALERT_SENT` syslog action. A **"Shows without labor" subpage** (`/labor-scheduler/no-labor`, linked from the Labor Scheduler header) shows the same backlog as a live list any time — every active show with no labor requested, with its date, days-out, venue and PM, and an "Add labor" link straight to each show (upcoming + undated by default; a toggle includes past-dated shows). (3) **Load-in/out auto-fill:** on a show's advance, when the load-in/out window is blank but the show has performance dates, the earliest show date is assumed as load-in and the latest as load-out (a single show date makes them the same day); and typing a load-in date auto-fills a still-blank load-out to match. Both only fire when the field is blank, so they never overwrite a window someone set. (4) **Paperwork branding:** the Advance Sheet and Post-Show Final Invoice PDFs now match the Production Schedule's branding — navy `#1a4a7a` structural color, gold `#B8840A` accent, Arial, the Dr. Phillips Center identity and logo treatment (the invoice previously used an off-brand orange `#F57F20` / `3·2·1→THEATER` look). (5) **Bug fix — post-show labor re-sync now backfills a missing crew name/rate:** a labor line flagged scheduled before a tech was assigned snapshotted into the Post-Show "Actual Labor" table with a blank tech name and unresolved rate; "Re-sync from Schedule" previously only inserted new lines, so the gap was unrecoverable. Re-sync now also refreshes already-pulled lines that have since gained a crew assignment (backfilling the name, and the rate only when the PM hasn't already entered one), and reports how many lines it updated.
- `2.21.0` — **Labor Scheduler now groups each show by date.** A show that spans multiple days now renders one day-block per work date inside its section (mirroring the show staffing view) instead of cramming every date into a single table — each block has a date header, its own "+ Add Line" (new lines inherit that day's date), and an editable date input that re-dates every line in the block at once (the way to correct mis-dated lines). A "+ Add Day" button on the show header adds a new empty day-block, and the redundant per-row DATE column is dropped from show blocks (the block header carries the date; overhead sections keep their inline date column). Backend: the labor-request PUT now accepts `work_date` so a line/day can be re-dated. Shows with no lines still get one empty day-block so the first line can be added.
- `2.20.0` — **Labor Scheduler week navigation + responsive layout, plus two fixes.** (1) New ← Prev Week / This Week / Next Week → controls on the Labor Scheduler snap the From/To range to a whole Monday–Sunday work week (mirrors Labor Overview's week jump); the From/To inputs stay editable for custom multi-week ranges. (2) **Bug fix:** "+ Add Line" now dates a new labor line to the *show's own date* instead of the loaded range's start date, which previously mis-dated lines for any show that wasn't on the first day of the window (the row's date is still editable, and an undated show still falls back to the range start). (3) **Layout fix:** wide tables (scheduler / overview) now scroll horizontally inside their card instead of being clipped off the right edge on smaller windows, so the far-right columns — including the per-row delete × (which already existed but was getting cut off) — stay reachable; the sidebar is now collapsible to an icon rail (chevron at the top, preference saved in localStorage) and on narrow viewports (≤900px) it shrinks to that rail instead of disappearing entirely, handing the freed width to page content.
- `2.19.5` — Merge Duplicates moved off the homepage: the admin-only Merge Duplicate Shows tool now lives in the Settings tab bar (next to Prism Sync / Sidebar Editor, same admin-only route), the homepage header keeps just New Show, and the merge page's back link points to Settings.
- `2.19.4` — Combined Invoice… button removed from the Post Show tab as well — the sidebar entry (under Settings) is now the single way in. The page's `?preselect=` parameter still works for direct links/bookmarks.
- `2.19.3` — Export & Files polish: the four export cards (Advance, Schedule, Post-Show Report, Final Invoice) now sit in one row of four instead of 3 + 1 (two-up below 1200px, single column below 900px as before), and the Combined Invoice… link is removed from the Final Invoice card — it remains on the Post Show tab and in the sidebar under Settings.
- `2.19.2` — **Bug fix: sidebar global search dead on PostgreSQL.** Every query to `/api/search` 500'd with `operator does not exist: date ~~ unknown` because the shows query ran `show_date LIKE ?` against a `DATE` column — fine on SQLite (dates are stored as text), invalid on PG — so the search box silently did nothing after the PostgreSQL migration (the frontend swallowed non-OK responses). Fixed with `CAST(show_date AS TEXT) LIKE ?` (portable, date-fragment searches like "2026-07" work again); also fixed the latent crash right behind it (PG returns `date` objects, so the result sub-label `join` would have raised `TypeError`), the endpoint now closes its DB connection in `try/finally` (same leak-hardening as 2.18.0), and a failed search now shows "Search isn't responding" in the results panel instead of failing silently. Verified end-to-end on PostgreSQL 16 (reproduced the 500, then name/venue/date/contact/barcode-path searches green, both access-control branches) and on SQLite.
- `2.19.1` — Combined Invoice builder now defaults to **Active** shows: the status filter starts on Active instead of All statuses, so archived shows are hidden until you ask for them. Exception: arriving with an archived show preselected (via a show's "Combined Invoice…" button) automatically widens the filter back to all statuses so the preselected show isn't hidden. Archived shows remain fully searchable and billable — only the default filter changed.
- `2.19.0` — Asset DB Tools + phantom "group" assets fixed in the show picker: (1) **Bug fix — group rows no longer appear as pickable assets**: a parentless asset type that has children is really a tree *group* (the middle column of /assets), but `/api/asset-types` still returned it, so the show page's Add Asset search offered it as a bogus $0.00/day duplicate of its own children — exactly what happened when a type was created without a group and the real type was later parented under it (two "Vibraphone" cards, one real). Group rows are now excluded server-side using the same parentless-with-children rule the Asset Manager search already used client-side; standalone parentless types (no children) remain pickable. (2) **Asset DB Tools** (`/assets/db-tools`, linked from the Asset Manager header, asset-manager/content-admin/admin only) — raw but column-whitelisted access to every asset-side table (`asset_categories`, `asset_types`, `asset_items`, `show_assets`, `asset_type_system_members`, `warehouse_locations`, `asset_logs`, `asset_maintenance`) for fixing rows the finder can't reach: browse/search any row including retired and malformed ones (with role badges GROUP/LEAF/STANDALONE/SYSTEM and live reference counts), edit whitelisted columns with FK validation and parent-cycle protection, hard-delete with reference guards (rows referenced by show lines or child types are blocked with an explanation; cascade-style deletes require an explicit Force step that spells out what goes), **Merge** a duplicate type into the real one (units, show lines with locked prices, system links and memberships repointed, then the stray row deleted), and **Move children** to re-home everything filed under an accidental group. (3) **Integrity scan** on the same page — one-click anomaly report: group rows that also look like assets (the Vibraphone case), duplicate names within a category, types stranded under retired groups, types with missing categories/parents, available units under retired types, orphaned units/show lines, and broken system-member links — each finding jumps straight to the offending rows. (4) Every DB Tools mutation writes audit entries with before/after snapshots under the standard entity types (asset_log/asset_maintenance added to the undo map), so edits and deletes are undoable from /admin/audit; show-line edits/deletes also re-run the asset-approval reconciliation so approval state stays honest. Verified end-to-end on PostgreSQL 16 and SQLite (44-check scenario suite reproducing the original bug report).
- `2.18.2` — Sidebar polish: (1) **Anchored footer** — the user/logout footer (and the brand + search header) no longer scroll with the nav; scrolling moved from `.sidebar` to `.sidebar-nav`, so on short windows the links scroll behind the pinned footer (thin translucent scrollbar styled for the blue gradient). (2) **Compact link spacing** — sidebar links pack tighter by default; a new **Spacing** setting in the Sidebar Editor (Compact / Comfortable) controls it for everyone, stored as `density` in the `nav_layout` JSON (older saved layouts without the key coerce to compact; invalid values coerce too, never reject).
- `2.18.1` — Sidebar cleanup: **Prism Sync**, **PDF Designer**, and the **Sidebar Editor** are no longer sidebar items — they're admin tools, so they now live in the Settings tab bar instead (Prism Sync and Sidebar Editor for admins, PDF Designer for content admins, matching each page's existing route permission — page URLs and permissions unchanged). The three entries are removed from the sidebar catalog entirely, so any saved sidebar layout prunes them automatically on next load; the default sidebar shrinks to Dashboards / Labor / Assets / Settings (with Combined Invoice still nested under Settings). README Prism guide updated (Settings → Prism Sync).
- `2.18.0` — Final Invoice on Export & Files + arts-group PostgreSQL fixes: (1) **Final Invoice export card** — the show's Export & Files tab gains a Final Invoice PDF card (Export + Combined Invoice… link) so the post-show billing invoice is reachable from the same place as the other PDFs; the Post Show tab buttons are unchanged. (2) **Bug fix — Arts Groups settings 500'd on PostgreSQL**: adding a group returned "Internal server error" and opening the edit dialog showed "Failed to load group" because the contact/notes columns (`primary_contact_*`, `notes`) and the `arts_group_contacts` table had only ever been added to the SQLite schema/migrations — never to `PG_SCHEMA` or `migrate_db_postgres()` (syslog showed `UndefinedColumn: primary_contact_name`). Existing PG databases heal automatically on the next startup via idempotent `ADD COLUMN IF NOT EXISTS` backfills + table create; both arts tables are also now included in the SQLite→PostgreSQL data-copy order so a fresh migration no longer silently drops them. Verified against a real PostgreSQL 16 instance (reproduced the error, ran the migration twice, exercised every endpoint's SQL through db_adapter). (3) **Stability — connection-leak hardening**: every arts-group route and the Final Invoice generator now close their DB connection in `try/finally`; previously any exception (like the UndefinedColumn above) leaked a PostgreSQL connection per failed request until garbage collection. (4) **Syslog/audit coverage** — arts-group *contact* add/edit/delete now write `ARTS_GROUP_CONTACT_ADD/EDIT/DELETE` audit entries (with before/after snapshots, undo-capable) and syslog lines, matching the existing `ARTS_GROUP_*` actions. (5) **Latent bug fix — EDIT/DELETE audit rows silently dropped on PostgreSQL**: end-to-end testing revealed that any audit entry carrying a before/after row snapshot was never written on PG — snapshots contain `datetime` objects there (SQLite returns strings), `json.dumps` raised, and `log_audit`'s never-raise guard swallowed it, so every snapshot-bearing EDIT/DELETE audit entry (and its undo capability) was lost while plain ADD entries and syslog lines still appeared. `log_audit` now serialises with `default=str`, restoring audit/undo coverage for all entity types on PG. Both fixes verified end-to-end through the real Flask app against PostgreSQL 16 (startup self-heal, all endpoints, audit rows with snapshots, no connection growth across 150 requests including error paths) and smoke-tested on SQLite.
- `2.17.2` — Labor Scheduler: Overhead & Project Crew sections are now interleaved with show sections in one top-to-bottom chronological list (previously all shows rendered first with overhead dumped at the bottom behind a banner). Each section sorts by its earliest labor date in the range (shows added via "add labor to existing show…" with no lines yet still surface at the top); same-date ties render shows before overhead. Overhead sections keep their identity inline via a purple **OH / PROJECT** badge and a distinct section icon in place of the old banner.
- `2.17.1` — Bug fix: Document Viewer edit dialog opened with all saved venue/doc-type selections blank once a user had venues saved. `|tojson` emits double quotes, and the Edit button carried its `data-venues` / `data-doc-types` payloads inside double-quoted attributes — the browser truncated them at the first quote (`data-venues="["`) and the dialog's `JSON.parse` silently fell back to empty, so reopening showed nothing checked (and re-saving from that state would wipe the selections). Attributes are now single-quoted (same class of bug as the 2.16.1 Prism checkbox fix); also fixed the identical latent pattern on the Skill Tracker's `data-quals` attribute. Verified by HTML-parsing the rendered page: full JSON arrays now reach the browser intact.
- `2.17.0` — Prism status tags on shows + optional auto-import: (1) **Status tag** — new `shows.prism_status` column; importing a Prism event stamps the show with its event status, shown as a colored tag (HOLD amber, CONFIRMED green, settlement states dim) on homepage show cards and today cards, and the sync keeps it current — when a hold is confirmed in Prism, the next sync flips the tag and reports "updated the Prism status tag on N show(s)". A one-time backfill fills the tag on shows imported before this release. This is the single sanctioned field the sync writes to a real show (documented in CLAUDE.md). (2) **Auto-import** — new opt-in `prism_auto_import_enabled` setting: each sync imports every future-dated NEW staged event as a 321T show exactly like a manual Import (hidden venues excluded — they arrive pre-ignored; name+date duplicates are auto-ignored instead of retried forever; capped at 200/run), attributed to `auto-import`. (3) Help panel, README guide section, and sync result messages updated to match.
- `2.16.1` — Prism fixes + documentation: (1) **Bug fix — venue Visible checkboxes did nothing**: the checkbox handler used `|tojson` (which emits double quotes) inside a double-quoted HTML attribute, so the browser truncated the handler and clicks were silently lost; attribute is now single-quoted per the Flask-documented pattern. (2) Renamed the events-toolbar *Restore* button to **Restore to New** with tooltips on all three actions (it moves selected IGNORED events back to NEW — the undo for Ignore; it never touches shows). (3) New collapsible **"How this page works"** panel on /prism documenting the sync model, the NEW/IMPORTED/IGNORED lifecycle, buttons, badges, venue-visibility semantics (including why panel counts don't drop when hiding), and the troubleshooting tools. (4) README gains a full **Prism FM Integration** section under the Admin & Settings Guide.
- `2.16.0` — Prism venue catalog + per-venue visibility filter: every sync now also pulls Prism's venues API into a new `prism_venues` staging table (name, city/state, stages with capacities, active flag, raw payload) and the `/prism` page gains a collapsible **Venues & Stages** panel documenting everything found — catalogued stages, stage-less pseudo-venues (Prism reports things like "Holidays" as venues), and any venue names seen only on events — each with its staged-event count. Unchecking *Visible* on an entry filters its events from the staged list, sweeps its not-yet-imported NEW events to Ignored, and makes future syncs stage its events pre-ignored, so junk venues disappear in one click without ever losing data (imported rows are never touched; "Show hidden venues" + Restore reverses everything). The hidden set persists in `app_settings.prism_hidden_stages` (auto-saved, audited); sync results report an auto-ignored count.
- `2.15.2` — Prism API troubleshooting tools: (1) **`prism_bridge/fix_sdk_remove_genres.js`** — one-command workaround for Prism's API rejecting SDK 1.1.2's events query (`Unknown argument "genres" on field "emsList"`); strips the two `genres` lines from the installed bundle with a `.orig` backup, idempotent, refuses on unexpected SDK contents. Prefer a newer vendor tarball when available (see prism_bridge/README.md → Troubleshooting). (2) **"Raw API Fetch" button on /prism** — live 7-day events fetch returning the exact request arguments, the bridge/SDK exchange trace (timing, sizes, stderr chatter), and the raw JSON response, without writing to staging. (3) Sync debug logs and Test Connection now record the full bridge exchange (request args, duration, response bytes, SDK stderr) instead of stderr-on-failure only.
- `2.15.1` — PostgreSQL fixes for the Prism module + two latent infrastructure bugs it exposed, all verified against a real PostgreSQL 16 instance: (1) `/prism` 500'd on PG because psycopg2 interprets a literal `%` (`LIKE 'prism_%'`) as a placeholder when a params tuple is passed — the pattern is now bound as a parameter, **and** `db_adapter.execute()` passes `None` to psycopg2 when there are no params, which also repairs `reload_syslog_handler()` (same literal-`%` query — syslog settings had been silently failing to load on PG deployments). (2) The Prism stale-row cleanup contained a literal `?` inside a SQL string ("worker died?") that db_adapter blindly rewrote to `%s` — message now bound as a parameter, and the sync's preflight phase is wrapped so any failure returns a clean JSON error instead of a 500. (3) `init_db.py`: fresh `--init-postgres` could never complete because `PG_SCHEMA` isn't FK-ordered and a single-transaction pass rolled back all prior creates on each failure — schema statements are now applied with per-statement commits and multi-pass retry (`_apply_pg_schema`), used by both init and `migrate_db_postgres()`; also removed a semicolon inside a schema comment that the naive `;`-split turned into a syntax error, which had silently prevented `prism_events` from being created. CLAUDE.md documents all three PG-only traps.
- `2.15.0` — **Prism FM integration (sandboxed)**: new admin-only `/prism` page pulls the building schedule from Prism (the venue's primary scheduling system) into a staging area — a manual **Sync Now** button and an optional once-daily auto-sync each look `prism_lookahead_days` (default 365) ahead and upsert events into a new `prism_events` table keyed on Prism's event ID, so re-syncs update in place and never duplicate. Staged events are listed with search/state/past filters and NEW / IMPORTED / IGNORED states plus debug badges (CHANGED IN PRISM after import, NOT IN LAST SYNC for events that vanished from the feed); selected rows can be explicitly **imported** into 321T — creating a normal show with per-date performances (times included), advance-sheet seeds, venue mapped against the Settings venue list, and a SHOW_CREATE audit entry — with a duplicate guard that skips events matching an existing show's name + date. Nothing outside the staging tables is touched until an import is clicked. The module is isolated in `prism_module.py` + `prism_bridge/` (Node bridge scripts wrapping the official `@prismfm/prism-sdk`, architecture validated in the PrismSDKTest project; see `prism_bridge/README.md` for the one-time SDK install) and wired into `app.py` by a single `register()` call plus one leader-gated scheduler job that refuses to run on a stale SQLite fallback (same safety pattern as scheduled PDF emails). Every sync writes a `prism_sync_log` row with counts and a capped debug log, surfaced in the page's Sync History panel together with an Environment panel (Node.js / SDK / token / DB checks) and a live **Test Connection** button. Settings (enable, API token, schedule hour, lookahead, event-status filter, bridge dir/timeout) live in `app_settings` and are edited on the page itself. Requires Node.js ≥ 18 on the server only if the integration is enabled.
- `1.x` — Initial release through security hardening and red team audit
- `2.0.0` — Asset Manager (inventory tracking, rental pricing, show reservations, external rentals), Performance Company field, version numbering system
- `2.1.0` — User registration with CAPTCHA, password recovery via email, pending registration approval workflow, in-app git update system with rollback, site-wide messaging (MOTD/maintenance/alerts with dismissal), AI session concurrency management, asset availability dashboards (public/private), asset usage reports by company/date range, Dashboards and Asset Reports in sidebar nav
- `2.2.0` — Asset invoice PDF export, MOTD cards on dashboard home page, admin email notifications (new registration + asset over-allocation), password strength meter on register/reset, scheduled_for field in site messages, message Scheduled/Expired status display, read-only badge in users table, email + is_readonly in Add User form
- `2.2.1` — Security hardening: HTML sanitizer on message body_html (prevents stored XSS), access control on /api/assets/availability (respects show permissions for restricted users), unified registration error messages (prevents username enumeration), rate limiting on /register (10/min) and /forgot-password (5/min), exception details no longer exposed to users
- `2.3.0` — Asset Manager enhancements: condition rating (excellent/good/fair/poor/retired) per unit, supplier/vendor name and contact per item type, warranty expiry date, year purchased, purchase value, straight-line depreciation with live remaining-capital calculator, per-unit maintenance log (note/damage/service/usage entries with date, author, and body)
- `2.4.0` — Admin "View As" role switcher: admins can preview the site as Content Admin, User, or Read-only without logging out; amber preview banner shown while in preview mode; one-click return to admin
- `2.4.1` — Soft-retire instead of hard delete: asset types and individual units can only be retired (never deleted); full history preserved permanently; dedicated Retired Assets archive page (/assets/retired); show/hide retired toggle in Asset Manager; category delete blocked while types exist
- `2.4.2` — Asset Manager sort and search: sort type tree by name, unit count, or rental cost (asc/desc); filter units in items modal by barcode with leading-zero tolerance (normBarcode)
- `2.5.0` — Global site-wide search: persistent search box in sidebar (/ or Ctrl+K to focus) searches shows (access-controlled), contacts, asset types, and barcodes; grouped results panel with keyboard navigation (↑↓ Enter Escape); `<mark>` highlight on matching text; leading-zero barcode tolerance client- and server-side
- `2.5.1` — Security patch: XSS fix in Retired Assets JS template literals (esc() helper); rate limiting on /api/search (60/min); max query length guard; log_date ISO format validation; syslog coverage for ADMIN_VIEW_AS, ADMIN_VIEW_AS_RESET, ASSET_LOG_ADD, ASSET_LOG_DELETE
- `2.6.0` — RentalWorks bulk import script (`import_assets.py`): one-time migration from RentalWorks exports into Asset Manager with full 3-tier hierarchy, container/kit linking, daily+weekly rates, depreciation dates, and replacement costs. Kit/container feature: items can be flagged as containers and linked to their contents. Load-in/load-out dates on shows for smart asset rental pricing (weekly rate applies when load period ≥ 7 days; daily × days otherwise). Sidebar redesign: gradient background, scaled-up nav items, pill-style active state.
- `2.7.0` — PostgreSQL dual-schema support: user/auth tables live in a `shared` schema (reusable across apps) while theater-specific tables live in an `app` schema (default `theater321`). Database credentials stored in gitignored `db_config.ini`. CLI commands for schema init and SQLite→PostgreSQL data migration. Settings UI simplified to read-only database status. Fixed schema creation bug that prevented PostgreSQL init.
- `2.8.0` — Labor Scheduler: new cross-show scheduler view (`/labor-scheduler`) aggregates labor requests across every show in a chosen date range, grouped by show. Schedulers tick a per-row checkbox as positions are confirmed (TCO'd) and pick the actual technician from the crew roster (qualified-first dropdown) — stored separately from the PM's originally-requested name. Scheduled status and scheduled tech flow back to each show's Labor Requests tab as read-only columns so PMs can see progress. Per-request `work_date` lets multi-day runs track one labor request per day. New `scheduler_group` user-group type so admins can grant scheduler access without giving full staff privileges. Adds `LABOR_SCHEDULED` audit action with syslog coverage.
- `2.8.1` — Labor Requests UX: reorganised the Labor Requests tab so labor is grouped into **day blocks** instead of a wide table with a date column on every row. Each day block has its own date picker in the header that retimes every row under it, **+ Add Request** to append rows inside the day, **Clone to…** to duplicate a fully-populated day to another date (for multi-day runs), and **Delete Day** to drop the day plus its rows. Quick Fill continues to apply across rows in any day. No schema change — rows still carry `work_date` internally; the UI just groups them.
- `2.9.0` — Nine feature updates and fixes: (1) Per-contact email-type flags (Advance, Production, System) replacing single Recipient toggle — `_send_pdf_email()` selects recipients by type; (2) Pay Rate Levels admin UI in Settings → Technicians, per-technician level assignment, per-position rate override, estimated labor cost summary on Labor Requests tab, combined post-show invoice PDF (assets + external rentals + labor); (3) Job positions nested by venue — venue list in Settings syncs position categories automatically; (4) Registration bug fix — pending registrations shown regardless of email confirmation status; (5) User management edit — name/email/role/readonly editable inline; (6) Hide from PM moved from show add-asset flow to per-asset-type admin setting in Asset Manager; (7) Dashboard nav renamed to Home; (8) Dashboard availability API auth fix (public dashboards no longer error with "unexpected token"); (9) Asset usage report gains venue, asset category, and asset type filters.
- `2.14.1` — Internal cleanup (no behavior change): extracted the duplicated assets+external-rentals fetch/subtotal logic and the performance-company lookup shared by the asset invoice and the final invoice into `_fetch_show_assets_and_externals()` / `_show_performance_company()`; expanded the `app.py` module header documenting the file's responsibilities and conventions.
- `2.14.0` — Post-show billing + duplicate-show tools: (1) **Actual Labor on the Post-Show tab** — scheduled labor lines are auto-snapshotted into a separate `post_show_labor` table (pre-filled with their scheduled in/out/lunch times) the first time the post-show data is opened, so PMs tweak actuals to bill instead of starting from scratch; the labor scheduler is never mutated, add/delete is per-show, and the snapshot runs once so edits survive reloads. A "Re-sync from Schedule" button pulls in lines scheduled later. (2) **Final Invoice** — the combined post-show invoice now bills *actual* labor, hides the technician name, shows position/hours/breaks → cost, and is organised into Internal Assets, External Assets & Costs, actual labor, and a grand total. (3) **Merge Duplicate Shows** (`/admin/merge-shows`, admin only) — when one event was entered twice, move the duplicate's labor, post-show labor, assets, external rentals, performances, comments and files onto the keeper (labor keeps its date so a second day becomes its own day block), fill only the keeper's *blank* advance fields from the duplicate (side-by-side compare shown first), then delete the duplicate. (4) **Move a file to another show** — per-attachment "Move to another show" action (admin). Adds `POST_SHOW_LABOR_*`, `SHOW_MERGE`, and `ATTACHMENT_MOVE` audit/syslog coverage. Schema migrations are idempotent (`post_show_labor` created on startup via `migrate_db` / `migrate_db_postgres`).
- `2.11.0` — Six changes: (1) **DB-backed sessions** — Flask sessions moved from signed cookies to a server-side `app_sessions` table in the shared schema; the cookie now carries only a 256-bit random sid. Two apps pointed at the same PostgreSQL database share login state. Set `DISABLE_DB_SESSIONS=1` to revert; set `SESSION_COOKIE_SECURE=1` on HTTPS deployments. Sid is rotated on login (session-fixation defense) and the server never adopts client-supplied sids it doesn't already know. See `SHARED_SESSIONS.md` for the porting guide for the companion app. (2) **Schedule template apply prompt** — picking a template when the day already has timeline entries now asks Replace / Merge / Cancel instead of silently wiping them. (3) **Empty timeline cleanup** — removed the auto-generated "15:00 — 16:00 — Crew Called" placeholder row that used to appear whenever a day had no labor entries. Timeline rows now auto-sort by start time on edit and after a template is applied. (4) **Multi-select auto-check** — new per-field `auto_select_visible` toggle: when conditional filtering narrows a multi-select to ≤ 2 options, those options are pre-checked (won't override choices the user already toggled). (5) **Labor Scheduler — Add Show / Pick Show** — two new entry points in the scheduler header: a modal that creates a barebones show (name, PM, date, time, venue) so labor can be scheduled before the PM advances it, plus a dropdown of active shows in the visible range that have no labor entries yet. The labor-scheduler list API gained an `include_show_ids=` param to pull in such shows as empty sections. (6) **Document Viewer role** — `users.is_document_viewer` is a stricter variant of read-only with two JSON allow-lists (`viewer_venues`, `viewer_doc_types`). A `@before_request` gate redirects viewers to `/viewer` and the sidebar collapses to "Documents" + "Log out". Viewers see only PDFs whose venue and doc-type match both allow-lists. Document access writes a `VIEWER_DOC_ACCESS` syslog entry. Flipping the flag in admin invalidates the user's existing sessions so the new gate takes effect immediately. Adds the `auto_select_visible` and `is_document_viewer` / `viewer_venues` / `viewer_doc_types` columns; schema migrations are idempotent.

---

## Table of Contents

1. [System Requirements](#system-requirements)
2. [Installation](#installation)
3. [First Login](#first-login)
4. [User Guide](#user-guide)
   - [Dashboard](#dashboard)
   - [Global Search](#global-search)
   - [Advance Sheet](#advance-sheet)
   - [Production Schedule](#production-schedule)
   - [Post-Show Notes](#post-show-notes)
   - [Labor Requests](#labor-requests)
   - [Labor Scheduler](#labor-scheduler)
   - [Assets Tab](#assets-tab)
   - [Asset Availability Dashboards](#asset-availability-dashboards)
   - [Comments](#comments)
   - [Export & Files](#export--files)
   - [Email](#email)
   - [Public Show Page](#public-show-page)
5. [Admin & Settings Guide](#admin--settings-guide)
   - [Asset Manager](#asset-manager)
   - [Importing from RentalWorks](#importing-from-rentalworks)
   - [Asset Financial Tracking](#asset-financial-tracking)
   - [Asset Maintenance Log](#asset-maintenance-log)
   - [Retired Assets](#retired-assets)
   - [Asset Reports](#asset-reports)
   - [Pay Rate Levels & Labor Estimating](#pay-rate-levels--labor-estimating)
   - [Contacts](#contacts)
   - [Arts Groups](#arts-groups)
   - [Users & Roles](#users--roles)
   - [View As (Role Preview)](#view-as-role-preview)
   - [Registration Approval](#registration-approval)
   - [Show Archiving](#show-archiving)
   - [Form Field Customisation](#form-field-customisation)
   - [PDF Form Templates](#pdf-form-templates)
   - [Notification Bell](#notification-bell)
   - [Contacts ↔ Users](#contacts--users)
   - [Test Mode & Bulk Archive / Delete](#test-mode--bulk-archive--delete)
   - [Site-Wide Messages](#site-wide-messages)
   - [In-App Updates](#in-app-updates)
   - [Venues & Radio Channels](#venues--radio-channels)
   - [WiFi Defaults](#wifi-defaults)
   - [Organisation Logo](#organisation-logo)
   - [Upload Size Limit](#upload-size-limit)
   - [Email Settings](#email-settings)
   - [AI Extraction (Ollama)](#ai-extraction-ollama)
   - [AI Session Concurrency](#ai-session-concurrency)
   - [Syslog Settings](#syslog-settings)
   - [Database Backups](#database-backups)
   - [File Manager](#file-manager)
   - [God Mode](#god-mode)
   - [Prism FM Integration](#prism-fm-integration)
6. [Database Configuration](#database-configuration)
   - [SQLite (Default)](#sqlite-default)
   - [PostgreSQL (Dual-Schema)](#postgresql-dual-schema)
   - [Migrating from SQLite to PostgreSQL](#migrating-from-sqlite-to-postgresql)
7. [Multi-Server Deployment](#multi-server-deployment)
   - [Cluster Heartbeat & Leader Election](#cluster-heartbeat--leader-election)
   - [Adding a New Scheduled Task](#adding-a-new-scheduled-task)
8. [Security](#security)
9. [Troubleshooting](#troubleshooting)

---

## System Requirements

| Component | Minimum |
|-----------|---------|
| Python | 3.9+ (3.11 recommended) |
| OS | Linux (systemd) |
| RAM | 512 MB |
| Disk | 1 GB (for database and backups) |
| Network | LAN access for crew devices |
| Database | SQLite (built-in) or PostgreSQL 13+ (optional) |
| Node.js | 18+ (optional — only for the Prism FM integration, see `prism_bridge/README.md`) |

Python packages installed automatically: Flask, Werkzeug, gunicorn, WeasyPrint (PDF generation), APScheduler (backups), flask-limiter (login rate limiting), qrcode[pil] + Pillow (WiFi QR codes), dnspython (direct MX email delivery), pdfplumber + python-docx + openpyxl + xlrd + striprtf (document import/AI extraction), psycopg2-binary (optional PostgreSQL support).

### WeasyPrint system dependencies (Ubuntu/Debian)

```bash
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libffi-dev libcairo2
```

---

## Installation

```bash
# Clone to a sensible location, then install:
git clone https://github.com/ShowSysDan/ShowAdvance 321theater
cd 321theater

# Full install with systemd service (recommended):
sudo ./install.sh

# Without root (manual start only):
./install.sh
```

The installer: creates a Python venv, installs dependencies, initialises/migrates the SQLite database, creates backup directories, writes a systemd service unit (`321theater`), generates a SECRET_KEY, and starts the service. For PostgreSQL setup, see [Database Configuration](#database-configuration).

After installation the app is available at `http://<server-ip>:<port>` (default port **5400**).

**Useful service commands:**
```bash
systemctl status 321theater
journalctl -u 321theater -f
sudo systemctl restart 321theater
```

### Updating

Re-run `./install.sh` (or `sudo ./install.sh`). It detects the existing database and runs migrations automatically — no data is lost.

---

## First Login

Default credentials: **admin / admin123**

**Change the admin password immediately** via Settings → My Account → Change Password.

---

## User Guide

### Dashboard

Lists all active and archived shows. Click a show to open it. **New Show** creates a new show.

### Global Search

A persistent search box lives in the left sidebar (below the logo). Press **/** or **Ctrl+K** from anywhere to focus it.

- Searches **shows** (by name, venue, company, date — respects your show access permissions), **contacts** (name, department, email, title), **asset types** (name, manufacturer, model — admin only), and **asset barcodes** (admin only, with leading-zero tolerance)
- Results appear in a grouped panel with match highlighting
- Keyboard navigation: **↑ / ↓** to move, **Enter** to open, **Escape** to close
- Minimum 2 characters to trigger, maximum 255 characters

### Advance Sheet

Contains all pre-show information: show details, contacts, arrival & parking, security, hospitality, audio, video, backline, stage, wardrobe, special elements, and labor needs.

**Saving:** Changes are auto-saved as you type. The **Save** button forces an immediate save.

**Conditional fields:** Fields appear/hide based on related field values (e.g. "Rentalworks Order #" appears when "Rental Works?" = Yes).

**Contacts:** Contact dropdowns populate from the Contacts list in Settings.

**Load-In / Load-Out:** When this window is left blank and the show has performance dates, the earliest show date is assumed as load-in and the latest as load-out (a single show date makes them the same day). Typing a load-in date also fills a still-blank load-out to match. These only fill blank fields, so a window you set yourself is never overwritten.

**Venues / Radio Channels:** If configured in Settings, these show as dropdowns instead of free-text fields.

**Version History:** Click **History** to view, preview, and restore previous snapshots.

**Real-time collaboration:** Multiple users can work simultaneously. Changes sync every 5 seconds and each user's active field is highlighted.

### Production Schedule

**Venue & Tech Info** — WiFi network/code, parking/security info. Radio Channel and Mix Position are read-only from the Advance Sheet.

**Timeline** — Time rows with Start, End, Description, Notes. Times are auto-normalised to 24-hour format on blur (e.g. "4pm" → "16:00", "1600" → "16:00"). Rows auto-sort by start time after each edit and after a template is applied (so the timeline always reads chronologically). Empty days render with no placeholder row.

**Schedule templates** — Apply a saved template via the dropdown in the timeline header. If the day already has entries, you'll be prompted with **Replace / Merge / Cancel**: *Replace* wipes the existing rows, *Merge* appends the template rows after the existing ones and re-sorts by start time.

**Show Contacts** — All dpc contacts (PM, Hospitality, Programming, Event Manager, Education, Guest Services, Runner) are read-only, pulled from the Advance Sheet. Security Email is editable.

### Post-Show Notes

Record production manager (read-only from advance), crew call time, show notes, house notes, equipment issues, and miscellaneous notes. A collapsible schedule timeline is shown for reference.

The tab's **Actual Labor (billing)** grid bills settlement from actual hours worked. When one technician's actuals pass **40 hours on the show/event**, the excess bills at **time and a half** — the grid shows each affected shift's straight-time portion, adds a read-only **Overtime (1.5×)** block, and keeps the on-page total in lockstep with the Final Invoice PDF (both come from the same server-side math). Hidden per-crew charges (e.g. parking folded into the rate) are still recovered exactly once at 1× — the OT premium never touches them.

Click **Export PDF** to generate a Post-Show Notes PDF.

### Labor Requests

Labor is organised into **day blocks**. Click **+ Add Day**, pick a work date, then **+ Add Request** inside the day to add labor rows (position, in/out, break window, requested technician). Each day block's header has its own date picker — changing it retimes every row under that day.

For multi-day runs, fill out day 1 completely, then click **Clone to…** on the day's header to duplicate every row to day 2 / 3 / …. **Delete Day** removes the day and all rows on it.

The **SCHED** checkbox and **SCHEDULED TECH** column inside each day are read-only — they are set by the scheduler via the [Labor Scheduler](#labor-scheduler) page. PMs can see who has been confirmed for each position but cannot edit the scheduler's entries. Restricted (read-only) users can view but not modify labor requests.

**Estimated Labor Cost (client quoting before scheduling):** below the day blocks, the **Estimated Labor Cost** box prices the requested labor even before any technician is scheduled — useful for quoting a client. Each line is billed at its position's **special rate** if the position has one, otherwise at the **highest standard technician rate** (see [Pay Rate Levels & Labor Estimating](#pay-rate-levels--labor-estimating) for which rates count). Lines that are not yet scheduled are tagged **"est."**; once a tech is assigned, that line automatically shows the assigned tech's real rate. The **Per-Crew Billable Extras** picker and the **Fold extras into the hourly rate** toggle sit right above the box (the same settings shown on the Post-Show tab) so extras like parking are included in the estimate. Use **Labor Estimate PDF** in the box header to export the estimate as a branded quote.

**Overtime (40 hours per show/event):** hours a single technician works beyond **40 on one show/event** bill at **time and a half**, accumulated by total hours — never by days worked. The estimate splits the crossing shift and adds a highlighted **Overtime (1.5×)** line right below it; the same split appears on the Labor Estimate and Pre-Show Estimate PDFs. Before anything is scheduled, "one technician" is approximated per position slot (the Nth line of a position each day is assumed to be the same tech across the run); a requested name or a scheduled crew member makes the grouping exact. The 1.5× premium applies to the labor rate only — per-crew extras such as parking (even when folded into the hourly rate) are flat pass-throughs and are never multiplied.

**PM on duty & day notes (per day):** each day-block header also has a **Day PM** picker and a **day notes** field. Use Day PM when another PM covers the show for a day (long runs, PM out for a day) — the picker draws from the same Production contacts as the show's PRODUCTION MANAGER field. Both values follow the day if it is re-dated and surface on the [Labor Scheduler](#labor-scheduler) and the Labor Overview, where the covering PM replaces the show's PM (with a "day" badge) and the day notes render beside the show's labor notes.

### Labor Scheduler

Accessible from the sidebar (SYSTEM section) to admins, staff, and users with the **Scheduler** flag (Settings → Users). Pick a **From** and **To** date and the page pulls every labor request whose `work_date` falls in that range, grouped by show.

For each row the scheduler can:
- Tick the **SCHED ✓** checkbox once the position is confirmed (TCO'd).
- Pick the **SCHEDULED TECH** from the crew roster. The dropdown is split into "Qualified" and "Others" based on the position and the crew member's qualifications on the Skill Tracker.

Each show section groups its rows into per-date day blocks. The day header carries the same **Day PM** (covering PM for that date) picker and **day notes** field as the show's Labor Requests tab — edits save immediately and both surface on the Labor Overview.

All other fields (position, times, break, requested technician) are read-only on this page — the source of truth for those is the show's Labor Requests tab. The SCHED checkbox and scheduled technician flow back to that tab as read-only columns.

Rows with no `work_date` (legacy data) fall back to the show's primary date for range filtering. Scheduling changes write a `LABOR_SCHEDULED` entry to the audit log and syslog.

**Adding shows from the scheduler:** the page header has two extras for when a show hasn't been advanced yet:

- **+ Add show** — opens a modal asking for name, PM, date, time, and venue. Submitting creates a barebones show (and seeds the matching `advance_data` rows so the PM picks it up half-filled when they advance the show later). The new show appears as an empty section in the scheduler so labor can be added immediately.
- **+ Add labor to existing show…** — a dropdown of active shows in the visible date range that have zero labor entries. Picking one pulls it into the scheduler as an empty section.

The scheduler endpoint accepts an `include_show_ids=` query param to surface these zero-labor shows, which the front-end appends automatically.

**Shows without labor:** the header's **Shows without labor** link opens a subpage (`/labor-scheduler/no-labor`) listing every active show with no labor requested yet — each with its date, days-out, venue and PM, and an "Add labor" link straight to that show's Labor Requests. Upcoming and undated shows show by default; an "Include past shows" toggle widens it. This is the live view of the same backlog the scheduled no-labor alert emails.

### Assets Tab

The **Assets** tab on every show allows content admins to:
- **Search** the asset inventory and add items to the show
- Set **quantity**, **rental period** (defaults to show production dates), and **unit price** (locked at time of reservation — subsequent database price changes do not affect existing reservations)
- **Edit a line's rental period in place** (date-only range). Dates are bounded to the show's production window — load-in → load-out from the advance page — both in the picker and server-side. Changing the period **re-locks the unit price** for the new duration using the rate-card formula, re-checks availability/unit conflicts, and resets approval. The Asset Approvals portal has the same in-place date editors on every requested line
- Add **external rental line items** with optional PDF attachment (vendor quote, contract, etc.)
- View the combined **total cost** for internal + external rentals
- Export an **Asset Estimate PDF** (the header button) — a branded estimate/quote of internal assets + external rentals for the client (formerly labelled "Invoice PDF")
- **Hide** specific items from production managers (admin only) — useful when e.g. an admin needs to confirm a lens before adding it

Availability is checked in real time when adding items. Items that are over-allocated or in maintenance show their status clearly.

### Asset Availability Dashboards

Access via **Dashboards** in the sidebar. Create personal or public availability views showing real-time asset status across your date range.

- **Layouts:** Combined (all assets), By Category, or By Show
- **Public dashboards** get a shareable URL (`/d/<slug>`) accessible without login — useful for tour managers and external clients
- Each dashboard refreshes live from the `/api/assets/availability` endpoint

### Comments

Show-specific comment thread with `@mention` autocomplete. Visible to all authorised users. Admins can view comment edit history.

### Export & Files

| Action | Description |
|--------|-------------|
| Export Advance → vN | Generates Advance Sheet PDF |
| Export Schedule → vN | Generates Production Schedule PDF with timeline, contacts, WiFi QR code, and logo |
| Export PDF (postnotes tab) | Generates Post-Show Notes PDF |
| Final Invoice PDF | Generates the post-show billing invoice (internal & external assets + actual labor + costs). Also available on the Post Show tab. To bill several shows on one invoice, use Combined Invoice in the sidebar (under Settings) |
| Generate Pre-Show Estimate | Generates a combined **labor + asset estimate/quote** PDF — the client-facing counterpart to the Final Invoice, produced before anything is scheduled. Labor uses position special rates or the highest standard tech rate; assets use reserved quantities and locked prices |
| ↓ Download (history) | Re-downloads a previously generated PDF |

PDFs are stored in the database — use the **↓ Download** button in Export History to re-download without generating a new version.

**Attachments:** Drag-and-drop or click **+ Attach File**. Upload progress bar shown. Files stored in database.

**Read Receipts:** Tracks who opened the advance at which version.

### Email

Send production schedule PDFs to contacts directly from the app. Supports two delivery methods:

- **SMTP relay** — send via a configured mail server (Gmail, Outlook, etc.)
- **Direct MX delivery** — send directly to the recipient's mail server (no relay needed; requires DNS/MX access)

Configure email settings in Settings → Email. A test button verifies connectivity before sending.

### Public Show Page

`/public` — no login required. Lists all active shows with download links for the latest advance and schedule PDFs. Share with clients, tour managers, and crew who don't have an account.

---

## Admin & Settings Guide

### Asset Manager

Access via **Asset Manager** in the sidebar (admin only). The asset manager uses a three-level hierarchy:

```
Category (e.g. Video)
  └── Item Type (e.g. Laser Projector — Christie Crimson+3DLP)
        └── Individual Unit (ID:42, barcode: X1234)
```

**Categories** group related equipment. **Item Types** define a make/model with:
- Photo, storage location, rental cost per show, reserve count (units held back as spares)
- Consumable flag + optional quantity tracking
- Supplier/vendor name and contact

**Individual Units** are each tracked with a database ID (always unique, even without a barcode). Barcodes are optional.

**Search & Sort:** Use the search bar above the type tree to filter by name/manufacturer/model. Sort by name, unit count, or rental cost (ascending/descending). Within the units modal, filter units by barcode with leading-zero tolerance.

**Maintenance:** Remove a unit from service with a reason and notes. Return it to service when resolved. Both actions are captured in the Audit Log and Syslog.

**Retiring:** Asset types and individual units are **never deleted** — only retired. Retiring a type also retires all its available units. Use the **Show Retired** checkbox to view retired entries inline. The **Retired Archive** link opens the full retired-assets history page.

**Warehouse Locations:** Manage a central list of storage location names (click **Warehouse Locations** button). These appear as a dropdown when editing item types.

**Availability:** When a unit is added to a show, the system checks real-time availability for the rental period, accounting for maintenance units, reserved spares, and other shows requesting the same item type. Negative availability is displayed — it does not prevent allocation, but makes the over-allocation visible.

**Rental pricing:** Each item type has a base rental cost. When added to a show the price is **locked** immediately — if the database price is updated later, existing show reservations keep the original price. New reservations use the current price.

### Importing from RentalWorks

If your organisation previously used **RentalWorks** (rental management software by Wynne Systems / HelixIntel), you can bulk-import your entire inventory into the Asset Manager using the included migration script `import_assets.py`.

#### What you need

Two Excel exports from RentalWorks (exported via its reporting module):

| Export | File naming pattern | Contents |
|--------|--------------------|-|
| Rental Inventory | `RentalInventory_<date>.xlsx` | Item types: name, category, manufacturer, part number, daily/weekly rates, active/inactive |
| Items | `Item_<date>.xlsx` or `Items_<date>.xlsx` | Individual physical units: barcode, serial number, status, purchase date, replacement cost, depreciation date |

#### What gets imported

| Source | Destination | Notes |
|--------|------------|-------|
| `InventoryType` | `asset_categories.name` | Top-level groupings (e.g. Audio, Video, Lighting) |
| `Category` | `asset_types` (parent tier) | Mid-level categories within each type |
| `Description` | `asset_types` (leaf tier) | Specific make/model names |
| `Manufacturer` | `asset_types.manufacturer` | |
| `ManufacturerPartNumber` / `SubCategory` | `asset_types.model` | Part number preferred; SubCategory used as fallback |
| `DailyRate` | `asset_types.rental_cost` | Per-day rental price |
| `WeeklyRate` | `asset_types.weekly_rate` | Per-week rental price (enables smart rate calc on shows) |
| `Inactive` | `asset_types.is_retired` | Retired types are hidden from active inventory |
| `BarCode` / `SerialNumber` | `asset_items.barcode` | Uses barcode if tracked by barcode; serial number otherwise |
| `InventoryStatus` | `asset_items.status` | IN / IN CONTAINER / STAGED → `available`; IN REPAIR → `maintenance` |
| `PurchaseDate` | `asset_items.year_purchased` | Year extracted from date |
| `DepreciationStartDate` | `asset_items.depreciation_start_date` | |
| `ReplacementCost` | `asset_items.replacement_cost` | |
| Container assignments | `asset_items.container_item_id` | Physical cases/racks linked to their contents via `ContainerBarCode` |

#### Running the import

```bash
# From the ShowAdvance directory:
python3 import_assets.py \
  --inventory /path/to/RentalInventory_2026-03-30.xlsx \
  --items     /path/to/Items_2026-03-30.xlsx

# Options:
#   --inventory PATH   Path to RentalInventory export (required)
#   --items PATH       Path to Items export (required)
#   --db PATH          Path to database file (default: advance.db)
#   --force            Skip duplicate-data guard (use if re-running)
#   --dry-run          Print what would be imported without writing anything
```

Expected output (numbers will vary):
```
[1/4] Categories:   9 created
[2/4] Parent types: 46 created
[3/4] Leaf types:   334 created
[4/4] Items:        1503 created  (0 warnings)
      Containers:   99 assigned
Done. Import complete.
```

#### Notes

- The script creates a backup of your database (`advance.db.bak`) before writing anything.
- Run `python3 import_assets.py --dry-run` first to preview the import without modifying the database.
- If the Asset Manager already has data, the script will abort unless you pass `--force`.
- Re-running with `--force` will skip rows that would create duplicate category or type names — existing records are left unchanged.
- The three-tier hierarchy (`InventoryType → Category → Description`) maps cleanly to the existing Asset Manager structure using `parent_type_id` — no schema changes needed for the organisational hierarchy itself.

---

### Asset Financial Tracking

Each individual unit can store financial metadata:

| Field | Description |
|-------|-------------|
| Condition | excellent / good / fair / poor / retired |
| Year Purchased | Calendar year of acquisition |
| Purchase Value | Original cost in dollars |
| Depreciation (years) | Straight-line depreciation timeframe |
| Warranty Expires | Date warranty coverage ends |

When **Purchase Value** and **Depreciation Years** are both set, the unit detail panel shows a live **remaining capital value** with a color-coded bar (green → amber → red as the asset approaches full depreciation). The calculation is straight-line: `remaining = max(0, value − (value ÷ years) × age)`.

### Asset Maintenance Log

Each individual unit has a built-in log for recording its history. Access it from the **Log** tab in the unit detail pane.

| Log Type | Use for |
|----------|---------|
| `note` | General observations |
| `damage` | Damage noticed during use or inspection |
| `service` | Repairs, cleaning, calibration |
| `usage` | Notable usage events |

Each entry records a date, the author (logged-in user), and a free-text body. Admins can delete entries. Entries are preserved permanently even after a unit is retired.

### Retired Assets

Access via **Retired Archive** link in Asset Manager, or **Retired Assets** in the sidebar.

Retired assets are split into two sections:

1. **Retired Item Types** — the entire type was retired. Expand each row to view all units that belonged to that type.
2. **Individually Retired Units** — the parent type is still active, but this specific unit was retired. The table includes condition, purchase value, warranty, and a link to view the unit's full log history inline.

All records are **read-only** and preserved permanently.

### Asset Reports

Access via **Asset Reports** in the sidebar (admin only). Filter asset usage by performance company and date range. Export results as CSV.

- Summary cards show total revenue, line item count, show count, and categories used
- The **Performance Company** field on each show's advance sheet drives company-level filtering

### Pay Rate Levels & Labor Estimating

Settings → Staffing manages the labor cost inputs:

- **Pay Rate Levels** — named hourly rates (e.g. L1, L2, External) assigned to technicians. Each level has an **In Estimate** checkbox: when on (the default), the level's rate is a candidate for the pre-show labor estimate. The estimate bills each unscheduled position at the **highest** rate among the checked levels (unless the position carries its own special rate). Uncheck a level to keep it in the system for testing without letting it inflate estimates — e.g. a deliberately high "Super Technician" test level. Toggling is admin-only and is recorded in the Audit Log and syslog (`PAY_LEVEL_ESTIMATE_TOGGLE`).
- **Job Positions** — a position's optional **Rate ($/hr)** override is its *special rate*; when set it always wins over the pay-level/estimate rate for that position, both in estimates and in actual billing.
- **Per-Crew Billable Items** — flat per-crew charges (e.g. parking) selectable per show and shown on both the Labor Requests estimate and the Post-Show invoice, with an optional "fold into the hourly rate" mode.
- **Overtime** — hours beyond **40 per technician per show/event** bill at **1.5×** the labor rate, shown as separate "Overtime (1.5×)" lines on estimates and invoices. The premium never applies to per-crew billable items (they stay 1× even in fold-into-rate mode).

See [Labor Requests](#labor-requests) for how these feed a show's estimate, and the Export tab's **Pre-Show Estimate PDF** for the combined labor + asset quote.

### Contacts

Add, edit, delete dpc contacts. Fields: name, title, department, phone, email. Contacts appear in dropdowns on advance and schedule forms.

### Arts Groups

Settings → Arts Groups. Manage the touring companies / resident artists list used by `arts_group_dropdown` form fields (new values typed into those fields are auto-added here on save).

Each group stores a primary contact (name, email, phone), free-text notes, and optional **additional contacts** (name/email/phone rows managed inside the same Add/Edit dialog). Group and contact changes are captured in the Audit Log (undo-capable) and syslog as `ARTS_GROUP_*` / `ARTS_GROUP_CONTACT_*` actions. Deleting a group removes its additional contacts with it. Content-admin access required for changes.

### Users & Roles

| Role | Access |
|------|--------|
| `admin` | Full access: all shows, settings, user management |
| `staff` | Like user, plus content-admin power (form fields, contacts, technicians) |
| `user` | Access controlled by group membership |

**Extra permission flags** (toggled per-user in the Edit User modal, independent of role):

| Flag | Effect |
|------|--------|
| `is_readonly` | View-only; mutating endpoints respond 403 |
| `is_scheduler` | Grants access to the Labor Scheduler page |
| `is_asset_manager` | Grants access to Asset Manager, Approvals, Retired Archive, Reports |
| `is_document_viewer` | Read-only viewer locked to `/viewer`; sees only allowed venues & doc types |

Add users via Settings → Users. Admins can reset passwords.

#### Cross-application account flags (`is_app_user` / `is_app_admin`)

The `users` table is a **shared user directory** — on PostgreSQL it lives in the
`shared` schema (separate from this app's `theater321` schema), so several apps
that point at the same database authenticate against the same accounts. Two
columns exist purely for those *other* apps to read:

| Column | Meaning |
|--------|---------|
| `is_app_user`  | Account is a user of the shared application |
| `is_app_admin` | Account is an administrator of the shared application |

- Toggled per-user in the Edit User modal under **OTHER APPLICATIONS**.
- **321Theater never reads these columns.** They are not loaded into the session,
  never gate a route, and have no effect on this app — they are storage only,
  surfaced here because this is the shared user manager.
- Both default to `0`. A consuming app reads them straight off the shared `users`
  row, e.g. `SELECT is_app_user, is_app_admin FROM users WHERE username = %s`.

#### Document Viewer

A stricter variant of read-only for external stakeholders (touring crew, vendors, FOH supervisors) who only need to see a subset of documents.

- Toggle **Document Viewer** in the Edit User modal. The checkbox forces `is_readonly` on.
- Pick **Allowed Venues** (multi-select; empty = all venues) and **Allowed Document Types** (Advance, Production Schedule, Post-show Notes; empty = all).
- On login, viewers land on `/viewer` — a list of accessible shows grouped by venue. Each show page links to PDF downloads for the allowed doc types. The sidebar collapses to just **Documents** + **Log out**; the `@before_request` gate redirects any other endpoint back to `/viewer` (API calls return 403 JSON).
- Every PDF download is logged to syslog as `VIEWER_DOC_ACCESS` with the show id, doc type, viewer, and source IP.
- Flipping the doc-viewer flag in admin invalidates that user's active sessions so the change takes effect on their next request (no 5-minute wait).
- Admins cannot turn the flag on for their **own** account (anti-self-lockout guard).

The viewer's PDF route reuses the existing `export_advance` / `export_schedule` / `export_postnotes` builders — same documents, same content, just gated by both the user's group ACL and the viewer's venue + doc-type allow-lists.

### View As (Role Preview)

Admins can preview the site from another role's perspective without logging out. The **VIEW SITE AS** control appears at the bottom of the sidebar (admin only).

| Preview Mode | Simulates |
|---|---|
| **C.Admin** | Content Admin (can edit form fields, manage messages) |
| **User** | Standard user |
| **R/O** | Read-only user (view-only, no edits) |

An amber banner appears at the top of every page while in preview mode. Click **Exit Preview** (or **RETURN TO ADMIN** in the sidebar) to restore full admin access. The real session is preserved — no actual role change occurs in the database.

### Registration Approval

New users can self-register at `/register`. The flow:
1. User fills out registration form and completes the Dino CAPTCHA (score ≥ 1 to pass)
2. A confirmation email is sent — user must click the link to verify their address
3. Admin sees pending requests in Settings → Registrations (with badge count)
4. Admin selects a role and clicks **Approve** (or **Deny**)
5. User receives an approval email and can log in

**Forgot password:** Available at `/forgot-password`. Sends a 2-hour reset link via email. Also requires CAPTCHA.

### Show Archiving

Archiving hides a show from the active dashboard without deleting anything; an
archived show can be restored at any time.

- **Automatic** — a show is archived once its last performance date has passed
  (or its legacy show date, if it has no performances).
- **Manual** — Settings → **Shows** (admin/staff) lists every show with search
  and status filters, plus per-show **Archive** / **Restore** buttons. The
  dashboard also offers **Restore** on recently archived shows.

There is no delete from this page — hard deletion exists only behind the
admin-only bulk API and requires explicit confirmation.

### Form Field Customisation

Settings → Form Fields (admin or content_admin).

- Drag rows to reorder fields
- Edit field label, type, conditional logic, width
- Add fields and sections
- Changes are immediate across all shows

Field types: `text`, `textarea`, `date`, `time`, `number`, `yes_no`, `select`, `checkbox`, `contact_dropdown`, `arts_group_dropdown`, `file_upload`, `notes`, `pdf_form`

Conditional: `field_key=Value` (e.g. `runner_needed=Yes`)

**Multi-select fields** have two extras:
- **Allow multiple selections** — render as a checkbox popover instead of a single-pick dropdown. Values are stored as a JSON array.
- **Auto-check visible options when 2 or fewer are shown** — once a conditional filter narrows the list to 1 or 2 visible options, those options are pre-checked. Won't override a selection the user has already touched.

**`notes` field type** — read-only instructional block on the advance form for instructions, links, etc. The body lives in the field config (Notes Body), URLs are auto-linked, and the block never prints on the advance PDF.

**`pdf_form` field type** — references a PDF template (see *PDF Form Templates* below). On the advance, the field renders as a "Fill out form →" button; clicking opens a popup with the PDF and positioned inputs for each placed field. Values auto-save; the export endpoint stamps them into a downloadable filled PDF.

**`yes_no` on the PDF** — only `Yes` answers are printed on the advance PDF. Blank/`No` answers are skipped to keep paperwork uncluttered.

**Change Alerts (per field)** — any non-`notes`/non-`pdf_form` field can be configured to alert one or more **departments** and/or named **contacts** when its value changes. Alerts are debounced: a background job runs every 10 minutes and only sends after the value has been quiet for 5 minutes, so rapid retypes and toggle-backs don't fire duplicate emails. Recipients linked to a system user also get an in-app notification.

### PDF Form Templates

Settings → Form Fields → **PDF Form Templates →** (admin or content_admin).

- Upload a PDF (any flat / non-fillable PDF works)
- Drag rectangles onto the page to place fields; click an existing field to rename, change type, or resize
- Field types: `text`, `multiline`, `date` (selectable), `today` (auto-fills with today's date), `checkbox`, `signature` (cursive)
- **Pull from advance** — each PDF field can be linked to an advance form field by key. On every show, the PDF field is pre-filled with that show's current advance value. Users can still override it; their override is persisted to the submission.
- Use the **View advance field keys** button in the builder sidebar to see every available `field_key` + label without leaving the page
- Changes **auto-save** ~1.5 s after the last edit, with a save toast
- Save — the placements are stored as PDF-point coordinates so they stamp cleanly on export
- Reference the template from a `pdf_form` field on the advance

**Signature fields** render in the filler as a text input + font picker (Caveat, Dancing Script, Great Vibes — all self-hosted under `static/fonts/`, OFL-licensed). The signature is stamped on export using the chosen cursive font registered with PyMuPDF.

Each submission is scoped per (show, field). The first save snapshots the field config so later template edits don't invalidate existing submissions. Filled PDFs are streamed back as `application/pdf`.

### Notification Bell

A bell next to the dark/light toggle in the sidebar shows in-app notifications. The badge polls every 60 seconds; clicking opens a panel with the latest 100 entries, supports per-item mark-as-read and a "mark all read" action.

Notification kinds today:
- `field_alert` — a field with Change Alerts configured was updated on a show this user is associated with (via contact link).

The notifications table is generic (`kind/title/body/link_url`) so future event types — approvals, mentions, schedule changes — can be added without schema changes.

### Contacts ↔ Users

Every system user has a matching `contacts` row, used for emails and the field-alert recipient pickers. Behavior:

- Creating a user auto-creates a linked contact (name + email pre-filled).
- Editing a user syncs the linked contact's name + email.
- On boot, a one-time backfill ensures every existing user has a contact row.
- The contacts list shows a `USER` badge on linked rows; deleting one is blocked from the UI (delete the user instead).

### Test Mode & Bulk Archive / Delete

- **`is_test` flag on shows** — mark a show as a test / demo so reports can filter it out.
  - Endpoint: `POST /shows/<id>/test-mode` with `{ "is_test": true|false }`
- **Bulk archive** — `POST /settings/shows/bulk-archive` with `{ "show_ids": [...] }`
- **Bulk delete** — `POST /settings/shows/bulk-delete` with `{ "show_ids": [...], "confirm": "DELETE" }` (admin only, requires the literal `DELETE` confirm string).

### Venues & Radio Channels

Settings → Syslog → **Venue & Channel Lists**

One item per line. These populate the Venue and Radio Channel dropdowns on the advance form.

### WiFi Defaults

Settings → Syslog → **WiFi Defaults**

Set default WiFi SSID and password. Appears on Schedule PDFs as text and QR code.

### Organisation Logo

Settings → Syslog → **Organisation Logo**

Upload PNG/JPG/SVG logo (max 2 MB). Shown in PDF headers.

### Upload Size Limit

Settings → Syslog → **Upload Size Limit**

Maximum file attachment size (default 20 MB, max 500 MB).

### Email Settings

Settings → Email. Configure outbound email for sending schedule PDFs to contacts.

| Setting | Description |
|---------|-------------|
| Provider | `smtp` (relay) or `direct` (MX delivery) |
| SMTP Host / Port | Mail server address and port (default 587) |
| SMTP User / Pass | Authentication credentials |
| From Address | Sender address |
| Use TLS | Enable STARTTLS (recommended) |
| EHLO Hostname | Custom EHLO hostname for direct delivery |
| Display Name | Friendly name shown in the From field |

The Email tab also hosts the automated-send schedules: **Advance Sheet** and **Production Schedule** auto-emails, the shared **send hour**, and **Labor — No-Request Alerts**. The labor alert warns schedulers (users with the Scheduler permission) about active shows that are approaching their date with **no labor requested yet**, so they can follow up. Configure an enable toggle plus up to two day-before windows (default 14 and 7 days out, the second is optional — 0 disables it). Each show alerts once per window — a single digest email to every scheduler with an address lists all the due shows (the breakdown), **and** each show also gets its own in-app notification linking to its Labor Requests — and a show that already has any labor requested is skipped. Alerts fire at the shared send hour; the same stale-SQLite-fallback and leader-gating safeguards as the PDF auto-emails apply, and each send writes a `NO_LABOR_ALERT_SENT` syslog line.

### AI Extraction (Ollama)

Settings → AI. Connect to a local [Ollama](https://ollama.com) instance for AI-powered data extraction from uploaded documents (PDF, DOCX, XLSX, RTF, TXT). Configure the Ollama server URL and enable/disable the feature. The AI can pre-populate advance form fields from uploaded rider documents.

### AI Session Concurrency

Settings → AI → **Max Concurrent AI Sessions**. Limits how many AI extraction jobs can run simultaneously across all Gunicorn workers (stored in DB, shared across processes). Default: 2. The AI extract button is dynamically disabled in the UI when all slots are busy.

### Site-Wide Messages

Settings → Messages. Create banners visible to all logged-in users.

| Field | Description |
|-------|-------------|
| Type | `MOTD` (message of the day), `Maintenance` (scheduled downtime notice), `Alert` (urgent) |
| Dismissible by | `user` (anyone can dismiss) or `admin` (only admins, persists for regular users) |
| Expires at | Automatically hides after this datetime |
| Show on login | Display prominently on the login page |

Messages are fetched via `/api/messages` on every page load and appear as dismissible flash banners at the top of the main content area. Admins can deactivate a message for **all** users at once with the **✕ All** button.

### In-App Updates

Settings → Updates. Pull the latest release from git and auto-restart the service.

1. Click **Auto-Detect** to identify the systemd service name (or enter it manually)
2. Click **Check for Updates** to see pending commits and changed files
3. Click **Apply Update** to:
   - Archive all changed files to `backups/pre_update_<timestamp>/` (rollback point)
   - Run `git pull`
   - Run `python init_db.py --migrate` (applies any schema changes)
   - Restart the systemd service
   - If any step fails, the archived files are restored and the service restarted

The update progress log is displayed live in the browser. If the service restarts, the page automatically detects when Flask comes back up.

### Syslog Settings

Settings → Syslog. Send audit events to a remote syslog server via UDP.

Events: LOGIN/LOGOUT · SHOW_CREATE/ARCHIVE/DELETE/RESTORE · FORM_SAVE · PDF_EXPORT · USER_CREATE/DELETE/PASSWORD_CHANGE · GROUP_ASSIGN/REMOVE · BACKUP_CREATED · SETTINGS_CHANGE · REGISTER_PENDING · EMAIL_CONFIRMED · USER_APPROVED · USER_DENIED · PASSWORD_RESET_REQUEST · PASSWORD_RESET_COMPLETE · APP_UPDATE_START · MESSAGE_CREATE · MESSAGE_EDIT · MESSAGE_DELETE · ASSET_TYPE_RETIRE · ASSET_ITEM_RETIRE · ASSET_LOG_ADD · ASSET_LOG_DELETE · ADMIN_VIEW_AS · ADMIN_VIEW_AS_RESET

### Database Backups

Settings → Backups. Automatic hourly (keeps 24) and daily at midnight (keeps 30) SQLite backups in `backups/`. Click **Run Backup Now** for immediate backup.

**SQLite Restore:**
```bash
cp backups/daily/advance_YYYYMMDD_0000.db advance.db
sudo systemctl restart 321theater
```

**PostgreSQL Backups:** When using PostgreSQL, use standard `pg_dump` for database backups. The in-app backup system backs up the SQLite bootstrap file (`advance.db`) only.
```bash
pg_dump -h localhost -U showadvance 321theater > backup_$(date +%Y%m%d).sql
```

### File Manager

Settings → Files (admin only). View and delete all file attachments across all shows.

### God Mode

Settings → God Mode (admin only).

- **Active Sessions** — users on a show page in the last 5 minutes (user, show, tab, last seen)
- **User Last Login** — last login timestamp per user

### Prism FM Integration

Settings → Prism Sync (admin only). Pulls the building schedule from **Prism**
(the venue's primary scheduling system) into a staging area, from which
selected events can be imported as normal 321T shows. The module is
**sandboxed**: syncing only writes to its own staging tables — nothing in
321Theater changes until an admin explicitly imports.

**One-time server setup** (Node.js + the official Prism SDK tarball + an API
token) is documented in [`prism_bridge/README.md`](prism_bridge/README.md),
including the troubleshooting workaround for the SDK 1.1.2 `genres` query
error. The page's Environment panel verifies every prerequisite live.

**Syncing.** *Sync Now*, or a daily leader-gated auto-sync at a configurable
hour, fetches every event from today out to the configured lookahead (default
365 days), filtered by event status (Hold / Confirmed / In Settlement /
Settled). Events are deduplicated by Prism event ID — re-syncs update staged
rows in place, never duplicate. Each sync also refreshes the venue/stage
catalog and writes a debug log (request, timing, SDK output, per-event
decisions) viewable under Sync History.

**Event lifecycle.** Every staged event is NEW (awaiting review), IMPORTED
(a 321T show was created from it — linked, and flagged CHANGED IN PRISM if
Prism edits it afterwards), or IGNORED (dismissed, kept so it doesn't return
as NEW). *Import selected → 321T* creates one show per event with the venue
mapped against the Settings venue list and one performance per Prism date;
already-imported events and name+date matches against existing shows are
skipped. *Ignore* dismisses; *Restore to New* un-dismisses (it only flips the
staging state — it never touches shows).

**Prism status tag.** Imported shows carry their Prism event status
(`HOLD` / `CONFIRMED` / `IN SETTLEMENT` / `SETTLED`) as a colored tag on the
homepage show cards (`shows.prism_status`). The sync keeps it current — when
a hold is confirmed in Prism, the next sync updates the tag automatically.
This is the only field the sync ever writes to a real show.

**Auto-import (optional).** With *Auto-import new events* enabled in the
page's Settings panel (`prism_auto_import_enabled`, off by default), each
sync also imports every future-dated NEW event as a 321T show — identical to
selecting everything and clicking Import, attributed to `auto-import`.
Hidden-venue events never qualify (they arrive pre-ignored), and events
caught by the name+date duplicate guard are auto-ignored rather than retried
forever (Restore to New re-arms one). Capped at 200 imports per sync; the
remainder is picked up by the next run.

**Venues & Stages panel.** Documents everything Prism reports — stages with
capacities, stage-less pseudo-venues (Prism lists things like "Holidays" as
venues), inactive venues, and names seen only on events — with staged-event
counts. Unchecking *Visible* on an entry hides its events from the list,
moves its current NEW events to IGNORED, and stages its future events
pre-ignored, so junk venues disappear in one click. Fully reversible: tick
*Show hidden venues*, select, *Restore to New*. The hidden set persists in
settings and is audited.

**Troubleshooting.** *Test Connection* exercises the full pipeline with a
live venues fetch; *Raw API Fetch* shows the exact request and raw response
for the next 7 days; the `{ }` button on any staged row shows its raw Prism
payload; and an expandable *How this page works* panel on the page itself
documents all of the above for operators.

---

## Database Configuration

321Theater supports two database backends: **SQLite** (default, zero-config) and **PostgreSQL** (recommended for production and multi-app environments).

### SQLite (Default)

Out of the box, all data lives in a single file: `advance.db`. No configuration needed. The installer handles initialization and migrations automatically.

SQLite is ideal for single-server installs and development.

### PostgreSQL (Dual-Schema)

PostgreSQL mode uses **two schemas** within one database:

| Schema | Default Name | Contents | Purpose |
|--------|-------------|----------|---------|
| **Shared** | `shared` | `users`, `app_settings`, `password_reset_tokens`, `user_pending_registration`, `site_messages`, `site_message_dismissals`, `app_sessions` | User/auth + session data — designed to be shared across multiple apps |
| **App** | `theater321` | Shows, schedules, contacts, forms, assets, labor, exports, comments, active_sessions, audit_log, and all other theater-specific tables | App-specific data |

This separation means another app can connect to the same PostgreSQL database and share the user/auth system without touching theater data. With the shared `app_sessions` table, logging in to either app authenticates the user in both — see **[SHARED_SESSIONS.md](SHARED_SESSIONS.md)** for the porting guide for the companion app.

#### Setup

1. **Create a PostgreSQL database and user** on your server:
   ```sql
   CREATE DATABASE "321theater";
   CREATE USER showadvance WITH PASSWORD 'your_secure_password';
   GRANT ALL PRIVILEGES ON DATABASE "321theater" TO showadvance;
   -- Grant schema creation permission:
   ALTER DATABASE "321theater" OWNER TO showadvance;
   ```

2. **Create `db_config.ini`** in the app directory (copy from the example):
   ```bash
   cp db_config.ini.example db_config.ini
   nano db_config.ini
   ```

   ```ini
   [postgresql]
   host           = localhost
   port           = 5432
   dbname         = 321theater
   user           = showadvance
   password       = your_secure_password
   app_schema     = theater321
   shared_schema  = shared
   ```

   This file is **gitignored** — credentials are never committed.

3. **Initialize the PostgreSQL schemas and tables:**
   ```bash
   python3 init_db.py --init-postgres
   ```
   This creates both schemas and all tables. Safe to run multiple times (uses `IF NOT EXISTS`).

4. **Set the app to use PostgreSQL:**
   ```bash
   # In the SQLite database, set db_type to 'postgres':
   sqlite3 advance.db "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('db_type', 'postgres');"
   ```

5. **Restart the app:**
   ```bash
   sudo systemctl restart 321theater
   ```

#### Configuration Reference

| Key | Default | Description |
|-----|---------|-------------|
| `host` | `localhost` | PostgreSQL server hostname |
| `port` | `5432` | PostgreSQL server port |
| `dbname` | `321theater` | Database name |
| `user` | — | Database user |
| `password` | — | Database password |
| `app_schema` | `theater321` | Schema for theater-specific tables |
| `shared_schema` | `shared` | Schema for user/auth tables (shared across apps) |

Legacy note: the old `schema` key is still accepted as a fallback for `app_schema`.

#### How it Works at Runtime

When the app connects to PostgreSQL, it sets `search_path` to `"app_schema", "shared_schema"`. This means all SQL queries work with unqualified table names — no code changes needed. Foreign key references (e.g., `shows.created_by → users.id`) resolve correctly across schemas.

SQLite remains the "bootstrap" database — it always stores the `db_type` setting so the app knows which backend to use on startup.

### Migrating from SQLite to PostgreSQL

Two options: **CLI** (recommended) or **Web UI**.

#### CLI Migration

```bash
# 1. Ensure db_config.ini is configured (see above)

# 2. Initialize PostgreSQL schemas and tables:
python3 init_db.py --init-postgres

# 3. Copy all data from SQLite to PostgreSQL:
python3 init_db.py --migrate-to-postgres

# 4. Set the app to use PostgreSQL:
sqlite3 advance.db "INSERT OR REPLACE INTO app_settings (key, value) VALUES ('db_type', 'postgres');"

# 5. Restart:
sudo systemctl restart 321theater
```

The migration is **idempotent** — duplicate rows are skipped via `ON CONFLICT DO NOTHING`. You can safely re-run it if interrupted. Tables are copied in foreign-key dependency order, and serial sequences are synced after copy so new inserts get correct IDs.

Each table is routed to the correct schema: shared tables go to the `shared` schema, app tables go to the `theater321` schema.

#### Web UI Migration

If the app is already set to `db_type=postgres`, go to **Settings → Database** and click **Migrate Now**. This runs the same migration as the CLI command. Progress and per-table stats are shown in the browser.

#### CLI Reference

| Command | Description |
|---------|-------------|
| `python3 init_db.py` | Fresh SQLite init (skips if DB exists) |
| `python3 init_db.py --force` | Destroy and reinitialize SQLite |
| `python3 init_db.py --migrate` | Run schema migrations on existing SQLite DB |
| `python3 init_db.py --init-postgres` | Create PostgreSQL schemas + tables from `db_config.ini` |
| `python3 init_db.py --migrate-to-postgres` | Copy all SQLite data → PostgreSQL |

---

## Multi-Server Deployment

321Theater can run on multiple servers behind a load balancer or VRRP/keepalived virtual IP. Both instances connect to the same shared PostgreSQL database, which already handles all persistent state. Sessions are signed cookies (no sticky-sessions required) and file uploads go to S3/SeaweedFS, so most of the app needs zero changes to run clustered.

The one thing that **does** need coordination is **scheduled tasks** — without it, both servers would run the hourly job at the same time and recipients would get duplicate emails. 321Theater solves this with **cluster heartbeat + leader election** stored in PostgreSQL.

### Cluster Heartbeat & Leader Election

**How it works:**

1. Each running app instance (each Gunicorn worker process counts as one instance) generates a UUID at startup and writes a row to the `cluster_instances` table every ~10 seconds with `last_seen=NOW()`.
2. Any code that needs to know "am I the leader?" runs a query: `SELECT … FROM cluster_instances WHERE last_seen > NOW() - INTERVAL '30 seconds' ORDER BY ip, instance_id` and the row at the top of the result is the leader.
3. Lowest IPv4 address wins. Ties (e.g. multiple Gunicorn workers on the same server) are broken by the lowest `instance_id` UUID.
4. If the leader crashes, its `last_seen` stops updating. Within 30 seconds the row is filtered out by the `WHERE` clause and the next-lowest IP becomes leader on the very next read — **no failover script, no coordination message**, just the natural result of every instance reading the same table.
5. On a graceful shutdown (SIGTERM), the instance `DELETE`s its own row via `atexit`, so failover is instant rather than waiting 30 seconds.

**View / configure cluster status:** `Settings → System → Cluster` (admin only). Shows every detected instance, its uptime, and which one is currently in charge. From here you can:
- Toggle the heartbeat on or off (off = single-server mode, this instance is sole leader)
- Tune the heartbeat interval and peer-timeout window
- Force this instance to be `always` or `never` leader (operational escape hatch — leave on `auto` in normal operation)

**What runs only on the leader:**
- `run_scheduled_pdf_emails` (hourly tick that emails the advance / production schedule PDFs)

**What runs on every instance:**
- `run_hourly_backup` and `run_daily_backup` — each server keeps its own `/backups/` directory so a failing primary doesn't take its backup history with it. The duplication is intentional.

**Single-server installs are unaffected.** If only one instance is alive, that instance is its own leader by definition and scheduled tasks fire as before. If the heartbeat is disabled entirely (`cluster_heartbeat_enabled = 0`), `am_i_leader()` returns `True` unconditionally — same behaviour as before this feature existed.

**Defence-in-depth:** Even if leader election fails (e.g. a network partition causes split-brain), the existing `email_send_log` per-show/per-day deduplication in `run_scheduled_pdf_emails` provides a second line of defence against duplicate sends.

### Adding a New Scheduled Task

If you add a new APScheduler job, decide whether it should run **once cluster-wide** (most jobs) or **on every instance** (rare; usually only for instance-local writes like local file backups).

**Step 1 — Register the job** in `start_scheduler()` near the bottom of `app.py`:

```python
scheduler.add_job(my_periodic_task, 'interval', hours=1, id='my_periodic_task')
```

**Step 2 — Decide on leader-gating:**

| What the task does | Leader-gate? |
|---|---|
| Sends email / SMS / webhook / external API call | **Yes** |
| Modifies shared DB rows that should change exactly once per tick | **Yes** |
| Writes only to instance-local state (logs, on-disk backups) | No |

**Step 3 — If leader-gated, make the first line of the task body:**

```python
def my_periodic_task():
    if not am_i_leader():
        app.logger.info('my_periodic_task skipped — not cluster leader')
        return
    # … real work here …
```

That is the only change required. With the default 4 Gunicorn workers per server and 2 servers, the job will fire 8x per tick at the APScheduler level, but only the single global leader executes it; the other 7 return immediately. No locks, no Redis, no Celery.

The leader check is cached for 3 seconds per process, so calling it at the start of a job that fires every minute is essentially free.

**Existing examples to copy:**
- `run_scheduled_pdf_emails` (`app.py`) — leader-gated; sends external email
- `run_hourly_backup` (`app.py`) — NOT gated; writes to local disk on every server

### Load-Balancer / VRRP Setup Notes

The app's HTTP layer is stateless beyond the signed-cookie session, so any standard load balancer works (HAProxy, nginx, a router-held VIP via keepalived/VRRP, etc.). A few practical points:

- **Same `SECRET_KEY` on every instance.** Otherwise sessions issued by one instance won't validate on the other. Copy the value from `.env` between machines.
- **Same `db_config.ini`** so every instance points at the shared PostgreSQL.
- **S3 / SeaweedFS for uploads** — when configured in `Settings → System → Files`, attachments and PDF exports land in shared storage. Without it, uploads fall back to database blobs, which is also safe (just slower).
- **Backups remain per-instance.** Either accept the redundancy (each server keeps its own copy) or mount `/backups/` on shared NFS / send to S3.

---

## Security

Passwords are hashed using Werkzeug's `generate_password_hash` (scrypt with Werkzeug 3.x+, pbkdf2:sha256 with older versions). Passwords are **never** stored in plaintext.

The installer generates a cryptographically random `SECRET_KEY` and stores it in `.env` (chmod 600). This key signs Flask session cookies.

Login rate limiting (15 attempts/minute per IP) is enforced via `flask-limiter`.

An audit log records all significant actions (logins, show changes, exports, user management) with timestamps and IP addresses. View via Settings → Audit Log (admin only).

For a detailed security assessment, see [SECURITY_AUDIT.md](SECURITY_AUDIT.md).

---

## Troubleshooting

**Settings page tabs don't work** — Clear browser cache and reload.

**Backup fails with PermissionError:**
```bash
sudo chown -R <service_user>:<service_user> /path/to/ShowAdvance/backups
# Or re-run: sudo ./install.sh
```

**PDF generation fails:** Install WeasyPrint system dependencies:
```bash
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2 libffi-dev
```

**Port change doesn't take effect:** Restart the service:
```bash
sudo systemctl restart 321theater
```

**Service logs:**
```bash
journalctl -u 321theater -f
journalctl -u 321theater -n 100
```

**SQLite migration errors:**
```bash
venv/bin/python init_db.py --migrate
```

**PostgreSQL "no schema has been selected to create in":** Ensure your `db_config.ini` has valid `app_schema` and `shared_schema` values. The database user must have permission to create schemas. Re-run:
```bash
python3 init_db.py --init-postgres
```

**PostgreSQL connection refused:** Check that PostgreSQL is running, the host/port/credentials in `db_config.ini` are correct, and `pg_hba.conf` allows connections from the app server.

**Falling back to SQLite:** If the app logs `PostgreSQL connection failed — falling back to SQLite`, check `db_config.ini` credentials and PostgreSQL server status. The app silently falls back to SQLite when PostgreSQL is unreachable.

**Login rate limiting:** After 15 failed login attempts per minute from an IP, further attempts return HTTP 429. Wait 60 seconds or restart the app.

---

## Transition Notes (ShowAdvance → 321Theater)

The git repository and codebase were previously named **ShowAdvance**. The rename to **321Theater** is in progress. For the current transition period:

- The **service name** on new installs is `321theater` (old installs still use `showadvance` — both are auto-detected)
- The **SQLite database file** remains `advance.db` as the bootstrap database (stores `db_type` setting even when using PostgreSQL)
- The **PostgreSQL database** is named `321theater` with schemas `theater321` (app data) and `shared` (user/auth data)
- The **syslog identifier** (`showadvance`) will update to `321theater` on the new server install — update any syslog filters at that time
- The **folder** should be cloned as `321theater/` on new servers (`git clone <url> 321theater`)
- Internal table names are generic (`shows`, `asset_types`, etc.) and require no renaming
- The `shared` schema is designed for future multi-app use — other apps can share the same user/auth system by connecting to the same database and setting their `search_path` to include the `shared` schema

---

## License

Released under the [MIT License](LICENSE) — free to use, modify, and
distribute, including for a hosted offering. Copyright (c) 2026 Dr. Phillips
Center for the Performing Arts; portions copyright (c) 2026 Thauma Systems, LLC.
