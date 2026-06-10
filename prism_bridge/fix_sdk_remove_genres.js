'use strict';
/**
 * Workaround for the sync error:
 *   Unknown argument "genres" on field "emsList" of type "Query".
 *
 * Prism's GraphQL API dropped the `genres` filter, but SDK 1.1.2 bakes it
 * into its events query unconditionally, so every getEvents() call is
 * rejected server-side. This script removes the two `genres` lines from the
 * installed SDK bundle (a `.orig` backup is written alongside it first).
 *
 * Usage:   cd prism_bridge && node fix_sdk_remove_genres.js
 * Safe to re-run (no-ops once patched). Re-run after any SDK reinstall.
 * Prefer installing a NEWER SDK tarball from Prism if one exists — this is
 * only for when you're stuck on a version that predates the API change.
 */

const fs = require('fs');

function resolveBundle() {
  try {
    return require.resolve('@prismfm/prism-sdk');
  } catch (e) { /* fall through */ }
  const p = process.env.PRISM_SDK_PATH;
  if (p) {
    try {
      return require.resolve(p);
    } catch (e) { /* fall through */ }
  }
  console.error('Prism SDK not found — install it first (see README.md).');
  process.exit(1);
}

const DECL = '\t\t$genres: [Int]\n';      // variable declaration in the query
const USAGE = '\t\t\tgenres: $genres\n';  // argument passed to emsList(...)

const bundle = resolveBundle();
let src = fs.readFileSync(bundle, 'utf8');

if (!src.includes('$genres')) {
  console.log('Nothing to do — the SDK at');
  console.log('  ' + bundle);
  console.log('has no $genres in its events query (already patched, or a newer SDK).');
  process.exit(0);
}

const nDecl = src.split(DECL).length - 1;
const nUsage = src.split(USAGE).length - 1;
if (nDecl !== 1 || nUsage !== 1) {
  console.error(`Unexpected SDK contents (declaration x${nDecl}, usage x${nUsage}) — `);
  console.error('not patching. This SDK version differs from 1.1.2; check Prism for a');
  console.error('newer tarball or contact engineering@prism.fm.');
  process.exit(1);
}

const backup = bundle + '.orig';
if (!fs.existsSync(backup)) {
  fs.writeFileSync(backup, src);
}
fs.writeFileSync(bundle, src.replace(DECL, '').replace(USAGE, ''));
console.log('Patched: ' + bundle);
console.log('Removed the genres argument from the events query. Backup: ' + backup);
console.log('Retry the sync from /prism now.');
