// Where does the time go when a Reel becomes primary? (`npm run bench:profile`)
//
// Runs `readReel` (the metadata read the exposure tracker does on every viewing instance) many times
// on the generated feed under the Chrome DevTools sampling profiler and prints the functions with
// the most self time. Used to decide what to change before changing it (docs/performance-report.md).
//
//   node bench/profile-read-reel.mjs [--rounds 600] [--out profile.json]

import { writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { chromium } from '@playwright/test';
import { build } from 'vite';

const here = dirname(fileURLToPath(import.meta.url));
const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) args.set(process.argv[i], process.argv[i + 1]);
const ROUNDS = Number(args.get('--rounds') ?? 600);

const result = await build({
  root: resolve(here, '..'),
  configFile: false,
  logLevel: 'silent',
  build: {
    write: false,
    minify: false,
    sourcemap: false,
    lib: { entry: resolve(here, 'harness-entry.ts'), formats: ['iife'], name: 'RefHarness', fileName: 'harness' },
  },
});
const outputs = Array.isArray(result) ? result.flatMap((r) => r.output) : result.output;
const code = outputs.find((o) => o.type === 'chunk').code;

const PAGE = '<!doctype html><html><head><meta charset="utf-8"></head><body></body></html>';
const browser = await chromium.launch();
const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
await context.route('http://localhost/**', (route) => route.fulfill({ contentType: 'text/html', body: PAGE }));
const page = await context.newPage();
await page.goto('http://localhost/');
await page.addScriptTag({ content: code });
await page.evaluate(() => window.__bench.build(60, 150));

const cdp = await context.newCDPSession(page);
await cdp.send('Profiler.enable');
await cdp.send('Profiler.setSamplingInterval', { interval: 100 });
await cdp.send('Profiler.start');
const times = await page.evaluate((n) => window.__bench.readCost(n), ROUNDS);
const { profile } = await cdp.send('Profiler.stop');

const parent = new Map();
for (const n of profile.nodes) for (const c of n.children ?? []) parent.set(c, n.id);
const callerOf = (node) => {
  const up = byIdEarly.get(parent.get(node.id));
  return up ? `${up.callFrame.functionName || '(anonymous)'}:${up.callFrame.lineNumber + 1}` : '(root)';
};
const byIdEarly = new Map(profile.nodes.map((n) => [n.id, n]));
const callers = new Map();
const self = new Map();
const byId = new Map(profile.nodes.map((n) => [n.id, n]));
profile.samples.forEach((id, i) => {
  const node = byId.get(id);
  const f = node.callFrame;
  const key = `${f.functionName || '(anonymous)'} ${f.url.split('/').pop()}:${f.lineNumber + 1}`;
  self.set(key, (self.get(key) ?? 0) + (profile.timeDeltas[i] ?? 0));
  if (f.functionName === 'querySelectorAll' || f.functionName === 'querySelector') {
    const who = callerOf(node);
    callers.set(`${f.functionName} called from ${who}`, (callers.get(`${f.functionName} called from ${who}`) ?? 0) + (profile.timeDeltas[i] ?? 0));
  }
});
const total = [...self.values()].reduce((a, b) => a + b, 0);
const rows = [...self.entries()].sort((a, b) => b[1] - a[1]).slice(0, 15);
const mean = times.reduce((a, b) => a + b, 0) / times.length;
console.log(`readReel: ${ROUNDS} calls, mean ${mean.toFixed(2)} ms; profiler total ${(total / 1000).toFixed(0)} ms`);
for (const [key, us] of rows) console.log(`${((us / total) * 100).toFixed(1).padStart(5)}%  ${(us / ROUNDS / 1000).toFixed(3).padStart(7)} ms/call  ${key}`);
console.log('');
console.log('querySelector[All] self time by caller:');
for (const [key, us] of [...callers.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8)) {
  console.log(`${((us / total) * 100).toFixed(1).padStart(5)}%  ${(us / ROUNDS / 1000).toFixed(3).padStart(7)} ms/call  ${key}`);
}
if (args.get('--out')) {
  writeFileSync(
    resolve(args.get('--out')),
    JSON.stringify({ rounds: ROUNDS, mean_ms: mean, top: rows.map(([key, us]) => ({ key, ms_per_call: us / ROUNDS / 1000, share: us / total })) }, null, 1),
  );
}
await browser.close();
