'use strict';
/**
 * Bridge script: fetch venues from the Prism FM API.
 * Used by the /prism "Test connection" button as a cheap end-to-end check.
 *
 * Usage: node get_venues.js '<json_args>'
 * JSON args (optional): includeInactive - boolean (default false)
 *
 * Outputs a JSON array of venue objects on stdout; JSON error on stderr.
 * Requires the PRISM_TOKEN environment variable (set by prism_module.py).
 */

const { getPrism, fail, parseArgs } = require('./_sdk_loader');

const args = parseArgs();

async function main() {
  const prism = getPrism();
  const venues = await prism.getVenues(args);
  process.stdout.write(JSON.stringify(venues));
}

main().catch(fail);
