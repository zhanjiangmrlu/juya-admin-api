// Isolated, read-only Chromium CORS check. Input signatures remain in memory.
const fs = require('node:fs');
const { createRequire } = require('node:module');
const fromAdmin = createRequire(require('node:path').join(__dirname, '../../juya-admin/package.json'));
let chromium;
try {
  ({ chromium } = fromAdmin('@playwright/test'));
  if (process.argv.includes('--check-dependencies')) {
    if (!fs.existsSync(chromium.executablePath())) throw new Error('Chromium unavailable');
    console.log(JSON.stringify({ ready: true }));
    process.exit(0);
  }
} catch (error) {
  console.log(JSON.stringify({ skipped: 'Sibling admin Playwright dependencies or Chromium unavailable' }));
  process.exit(77);
}

async function main() {
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const origin of input.origins) {
      const context = await browser.newContext();
      const page = await context.newPage();
      const cdp = await context.newCDPSession(page);
      await cdp.send('Network.enable');
      const pending = new Map();
      const preflights = [];
      cdp.on('Network.requestWillBeSent', ({ requestId, request }) => {
        if (request.method !== 'OPTIONS') return;
        const headers = Object.fromEntries(Object.entries(request.headers).map(([key, value]) => [key.toLowerCase(), value]));
        pending.set(requestId, { method: 'OPTIONS',
          requested_method: headers['access-control-request-method'],
          requested_headers: headers['access-control-request-headers'] || null });
      });
      cdp.on('Network.responseReceived', ({ requestId, response }) => {
        if (!pending.has(requestId)) return;
        const headers = Object.fromEntries(Object.entries(response.headers).map(([key, value]) => [key.toLowerCase(), value]));
        preflights.push({ ...pending.get(requestId), status: response.status,
          allow_origin: headers['access-control-allow-origin'] || null });
      });
      await page.goto(origin, { waitUntil: 'domcontentloaded' });
      const result = await page.evaluate(async ({ getUrl, headUrl, sha256 }) => {
        const read = { actual_origin: location.origin };
        try {
          const response = await fetch(getUrl, { signal: AbortSignal.timeout(20000) });
          const bytes = await response.arrayBuffer();
          const hash = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
            .map((value) => value.toString(16).padStart(2, '0')).join('');
          read.get = { status: response.status, bytes: bytes.byteLength, hash_match: hash === sha256,
            etag_exposed: Boolean(response.headers.get('etag')) };
        } catch (error) { read.get = { blocked: true, error_type: error.name }; }
        try {
          // This non-safelisted Content-Type forces a real HEAD preflight.
          const response = await fetch(headUrl, { method: 'HEAD',
            headers: { 'Content-Type': 'application/octet-stream' }, signal: AbortSignal.timeout(20000) });
          read.head = { status: response.status, etag_exposed: Boolean(response.headers.get('etag')) };
        } catch (error) { read.head = { blocked: true, error_type: error.name }; }
        return read;
      }, input);
      results.push({ ...result, preflights });
      await context.close();
    }
  } finally { await browser.close(); }
  console.log(JSON.stringify({ read_only: true, synthetic_existing_fixture: true, results }));
}

main().catch((error) => { console.log(JSON.stringify({ failed: true, error_type: error.constructor.name })); process.exitCode = 1; });
