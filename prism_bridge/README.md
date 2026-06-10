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

## Notes

- The token is passed to these scripts via the `PRISM_TOKEN` environment
  variable per invocation; it is never written to disk by the bridge.
- Scripts write clean JSON to stdout. Errors are a JSON object on stderr
  with exit code 1 — `prism_module.py` surfaces them in the sync debug log.
- No npm packages other than the Prism SDK are required.
