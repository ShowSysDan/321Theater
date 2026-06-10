'use strict';
/**
 * Environment probe used by the /prism debug panel: reports whether the
 * Prism SDK is resolvable from this directory, and from where.
 * Never calls the API — safe to run with no token.
 *
 * Outputs: {"ok": true, "version": "1.1.2", "path": "..."} or {"ok": false, "detail": "..."}
 */

const { loadSdk } = require('./_sdk_loader');

try {
  const { source } = loadSdk();
  let version = 'unknown';
  try {
    version = require(
      require.resolve('@prismfm/prism-sdk/package.json')
    ).version;
  } catch (e) {
    try {
      version = require(source + '/package.json').version;
    } catch (e2) { /* keep 'unknown' */ }
  }
  process.stdout.write(JSON.stringify({ ok: true, version: version, path: source }));
} catch (err) {
  process.stdout.write(JSON.stringify({ ok: false, detail: err.message }));
}
