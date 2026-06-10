# prism_bridge — Node.js bridge for the Prism FM SDK

These scripts let 321Theater talk to Prism (the building's scheduling system)
through the **official `@prismfm/prism-sdk`**, which is Node-only. Python
(`prism_module.py`) spawns a short-lived `node` process per call; the script
prints JSON to stdout. This is the same architecture validated in the
[PrismSDKTest](https://github.com/ShowSysDan/PrismSDKTest) project.

| Script | Purpose |
| --- | --- |
| `check_sdk.js` | Probe used by the `/prism` debug panel — is the SDK resolvable? |
| `get_events.js` | Fetch event summaries for a date window (the sync). |
| `get_venues.js` | Fetch venues — used by the "Test connection" button. |
| `_sdk_loader.js` | Shared SDK resolution + error plumbing. |

## One-time server setup

1. **Install Node.js ≥ 18** (the SDK is tested by Prism against Node 22):

   ```bash
   node --version   # should print v18+ — if not, install via your distro / nvm
   ```

2. **Download the Prism SDK tarball** from Prism → Settings → Developer
   (the same page where API tokens are generated). You'll get a file like
   `prismfm-prism-sdk-1.1.2.tar`.

3. **Install it into this directory:**

   ```bash
   cd /path/to/321Theater/prism_bridge
   npm install --no-save ./prismfm-prism-sdk-1.1.2.tar
   ```

   `--no-save` matters: it unpacks the SDK into `node_modules/` (gitignored)
   WITHOUT recording the dependency in `package.json`, so the install never
   modifies a tracked file and the app's git-based update flow stays clean.
   The tarball itself is also gitignored — neither it nor the SDK is ever
   committed to this repo. (Consequence: re-running a bare `npm install`
   won't restore the SDK; re-run the command above if `node_modules/` is
   ever wiped.)

   *Alternative:* if the SDK is already installed elsewhere (e.g. a
   PrismSDKTest checkout), skip the install and point the app at it by
   setting the environment variable
   `PRISM_SDK_PATH=/path/to/PrismSDKTest/node_scripts/node_modules/@prismfm/prism-sdk`
   for the Gunicorn service.

4. **Generate an API token** in Prism → Settings → Developer with scopes
   `read-events` and `read-venues`, then paste it into
   **321Theater → Prism → Settings** (it is stored in `app_settings` in
   PostgreSQL, not in any file).

5. Open **321Theater → Prism** — the Environment panel re-checks all of the
   above and the **Test connection** button does a live venues fetch.

## Troubleshooting

### Sync fails with: `Unknown argument "genres" on field "emsList" of type "Query"`

Prism's GraphQL API no longer accepts the `genres` filter that SDK 1.1.2
bakes into its events query, so every `getEvents()` call is rejected
server-side (venues still work — only the events query is affected).

1. **First** check Prism → Settings → Developer for a **newer SDK tarball**.
   If one exists, install it and retry — that's the proper fix:
   ```bash
   cd prism_bridge && npm install --no-save ./prismfm-prism-sdk-x.y.z.tar
   ```
2. If you're stuck on 1.1.2, run the bundled workaround, which strips the
   two `genres` lines from the installed SDK's query (a `.orig` backup is
   written next to the bundle; the script is a no-op once patched):
   ```bash
   cd prism_bridge && node fix_sdk_remove_genres.js
   ```
   Re-run it after any SDK reinstall. If it reports unexpected contents,
   the SDK version differs from 1.1.2 — contact engineering@prism.fm.

### Seeing what is actually sent and received

- **/prism → "Raw API Fetch"** — live 7-day events fetch showing the exact
  request arguments, the bridge/SDK exchange (timing, sizes, stderr
  chatter), and the raw JSON response. Touches nothing in the database.
- **Sync History debug logs** — every sync records its request args, timing,
  response size, SDK stderr, and per-event NEW/UPDATED decisions.
- **`{ }` button on a staged event** — the raw payload as last synced.
- From a shell, the bridge scripts run standalone:
  `PRISM_TOKEN=… node get_events.js '{"startDate":"2026-06-10","endDate":"2026-06-17"}'`

## Notes

- The token is passed to these scripts via the `PRISM_TOKEN` environment
  variable per invocation; it is never written to disk by the bridge.
- Scripts write clean JSON to stdout. Errors are a JSON object on stderr
  with exit code 1 — `prism_module.py` surfaces them in the sync debug log.
- No npm packages other than the Prism SDK are required.
