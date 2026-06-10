'use strict';
/**
 * Shared SDK resolver for the prism_bridge scripts.
 *
 * Resolution order:
 *   1. @prismfm/prism-sdk installed in prism_bridge/node_modules
 *      (npm install ./prismfm-prism-sdk-x.y.z.tar — see README.md)
 *   2. PRISM_SDK_PATH env var pointing at an extracted SDK package directory
 *      (e.g. an existing PrismSDKTest checkout's copy)
 */

function loadSdk() {
  const tried = [];
  try {
    return { sdk: require('@prismfm/prism-sdk'), source: '@prismfm/prism-sdk' };
  } catch (e) {
    tried.push('@prismfm/prism-sdk: ' + e.message.split('\n')[0]);
  }
  const p = process.env.PRISM_SDK_PATH;
  if (p) {
    try {
      return { sdk: require(p), source: p };
    } catch (e) {
      tried.push(p + ': ' + e.message.split('\n')[0]);
    }
  }
  const err = new Error(
    'Prism SDK not installed — see prism_bridge/README.md. Tried: ' +
    tried.join(' | ')
  );
  err.name = 'SDKNotInstalled';
  throw err;
}

function getPrism() {
  if (!process.env.PRISM_TOKEN) {
    const err = new Error(
      'PRISM_TOKEN is empty — set the API token on the /prism settings panel.'
    );
    err.name = 'MissingToken';
    throw err;
  }
  const { sdk } = loadSdk();
  const { getPrismSDK } = sdk.default || sdk;
  return getPrismSDK({});
}

function fail(err) {
  process.stderr.write(JSON.stringify({
    error: err.message,
    name: err.name,
    validationErrors: err.validationErrors || null,
  }) + '\n');
  process.exit(1);
}

function parseArgs() {
  try {
    return JSON.parse(process.argv[2] || '{}');
  } catch (e) {
    fail(new Error('Invalid JSON args: ' + e.message));
  }
}

module.exports = { loadSdk, getPrism, fail, parseArgs };
