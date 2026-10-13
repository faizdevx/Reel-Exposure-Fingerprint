// Browser half of the performance benchmark (spec N1 and N2): `npm run bench` in extension/.
//
// It bundles the REAL content-script code (ig-adapter.ts, exposure/tracker.ts) with Vite, runs it in
// Chromium on a generated feed page and measures, at normal speed and with the CPU slowed 4x:
//   N1  detection latency: from the moment a Reel is 60% visible to the moment the tracker has
//       started its viewing instance (instant jumps are timed from the scroll command).
//   N2  work per observer callback (IntersectionObserver, MutationObserver) against the 5 ms budget,
//       and frame times and long tasks while the feed is scrolled, with and without the tracker.
// The page is synthetic (no Instagram, no network): see docs/performance-report.md for the limits.
//
//   node bench/browser-bench.mjs --out ../docs/performance/browser.json [--steps 40] [--seconds 8]

import { mkdirSync, writeFileSync } from 'node:fs';
import { cpus, platform, release, totalmem } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { chromium } from '@playwright/test';
import { build } from 'vite';

const here = dirname(fileURLToPath(import.meta.url));
const args = new Map();
for (let i = 2; i < process.argv.length; i += 2) args.set(process.argv[i], process.argv[i + 1]);
const out = resolve(args.get('--out') ?? resolve(here, '../../docs/performance/browser.json'));
const STEPS = Number(args.get('--steps') ?? 40);
const SECONDS = Number(args.get('--seconds') ?? 8);
const REELS = 60;
const FILLER = 150; // extra elements per Reel: the page is far heavier than a fixture

function percentile(sorted, q) {
  if (sorted.length === 0) return null;
  if (sorted.length === 1) return sorted[0];
  const rank = ((sorted.length - 1) * q) / 100;
  const lo = Math.floor(rank);
  const hi = Math.ceil(rank);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (rank - lo);
}

function summarize(values) {
  const v = values.filter(Number.isFinite).sort((a, b) => a - b);
  const r = (x) => (x === null ? null : Math.round(x * 1000) / 1000);
  if (v.length === 0) return { n: 0, min: null, mean: null, p50: null, p90: null, p95: null, p99: null, max: null };
  return {
    n: v.length,
    min: r(v[0]),
    mean: r(v.reduce((a, b) => a + b, 0) / v.length),
    p50: r(percentile(v, 50)),
    p90: r(percentile(v, 90)),
    p95: r(percentile(v, 95)),
    p99: r(percentile(v, 99)),
    max: r(v[v.length - 1]),
  };
}

async function bundle() {
  const result = await build({
    root: resolve(here, '..'),
    configFile: false,
    logLevel: 'silent',
    build: {
      write: false,
      minify: false,
      lib: { entry: resolve(here, 'harness-entry.ts'), formats: ['iife'], name: 'RefHarness', fileName: 'harness' },
    },
  });
  const outputs = Array.isArray(result) ? result.flatMap((r) => r.output) : result.output;
  const chunk = outputs.find((o) => o.type === 'chunk');
  if (!chunk) throw new Error('the harness did not bundle');
  return chunk.code;
}

const PAGE = '<!doctype html><html><head><meta charset="utf-8"><title>bench</title></head><body></body></html>';

async function freshPage(browser, code, throttle) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  await context.route('http://localhost/**', (route) => route.fulfill({ contentType: 'text/html', body: PAGE }));
  const page = await context.newPage();
  await page.goto('http://localhost/');
  await page.addScriptTag({ content: code });
  const session = await context.newCDPSession(page);
  await session.send('Emulation.setCPUThrottlingRate', { rate: throttle });
  return { context, page };
}

function frameStats(frames, longTasks) {
  const over = (ms) => frames.filter((f) => f > ms).length;
  return {
    frames: frames.length,
    frame_ms: summarize(frames),
    frames_over_20ms: over(20),
    frames_over_33ms: over(33),
    frames_over_50ms: over(50),
    long_tasks: longTasks.length,
    longest_task_ms: longTasks.length ? Math.max(...longTasks) : 0,
  };
}

async function scenario(browser, code, throttle) {
  const { context, page } = await freshPage(browser, code, throttle);
  const nodes = await page.evaluate(([n, f]) => window.__bench.build(n, f), [REELS, FILLER]);
  const result = { cpu_slowdown: throttle, dom_elements: nodes };

  // N1
  await page.evaluate(() => window.__bench.startTracker());
  await page.evaluate(() => new Promise((r) => setTimeout(r, 300)));
  const instant = await page.evaluate((n) => window.__bench.detection(n, 'instant'), STEPS);
  const smooth = await page.evaluate((n) => window.__bench.detection(n, 'smooth'), STEPS);
  result.detection_instant_jump = {
    from_60_percent_ms: summarize(instant.fromReference),
    from_scroll_command_ms: summarize(instant.fromCommand),
    missed: instant.missed,
  };
  result.detection_smooth_scroll = {
    from_60_percent_ms: summarize(smooth.fromReference),
    from_scroll_command_ms: summarize(smooth.fromCommand),
    missed: smooth.missed,
  };
  result.read_reel_metadata_ms = summarize(await page.evaluate(() => window.__bench.readCost(120)));

  // N2: observer callbacks during those scrolls
  const seen = await page.evaluate(() => window.__bench.snapshot());
  result.intersection_callback_ms = summarize(seen.io);
  result.intersection_callbacks_over_5ms = seen.io.filter((x) => x > 5).length;

  // N2: mutation batches from a virtualised feed
  await page.evaluate(() => window.__bench.startTracker()); // fresh counters
  await page.evaluate(() => window.__bench.stopTracker());
  const churn = {};
  for (const k of [1, 5, 10]) {
    await page.evaluate(() => window.__bench.startTracker());
    await page.evaluate(([kk]) => window.__bench.churn(kk, 25), [k]);
    const snap = await page.evaluate(() => window.__bench.snapshot());
    await page.evaluate(() => window.__bench.stopTracker());
    churn[`${k}_reels_added_per_batch`] = {
      mutation_callback_ms: summarize(snap.mo),
      over_5ms: snap.mo.filter((x) => x > 5).length,
    };
  }
  result.mutation_batches = churn;

  // N2: frames and long tasks while scrolling, tracker off and on, alternating
  const off = [];
  const on = [];
  for (let trial = 0; trial < 3; trial += 1) {
    off.push(await page.evaluate((s) => window.__bench.scrollFrames(s), SECONDS));
    await page.evaluate(() => window.__bench.startTracker());
    on.push(await page.evaluate((s) => window.__bench.scrollFrames(s), SECONDS));
    await page.evaluate(() => window.__bench.stopTracker());
  }
  const merge = (runs) => frameStats(runs.flatMap((r) => r.frames), runs.flatMap((r) => r.longTasks));
  result.scroll_without_tracker = merge(off);
  result.scroll_with_tracker = merge(on);
  await context.close();
  return result;
}

const code = await bundle();
const browser = await chromium.launch();
const results = [];
const THROTTLES = (args.get('--throttles') ?? '1,4').split(',').map(Number);
for (const throttle of THROTTLES) {
  console.log(`browser benchmark, CPU slowdown ${throttle}x ...`);
  results.push(await scenario(browser, code, throttle));
}
const version = browser.version();
await browser.close();

const report = {
  suite: 'browser',
  seed: 'none (deterministic generated page)',
  environment: {
    measured_at: new Date().toISOString(),
    chromium: version,
    headless: true,
    viewport: '1280x800',
    cpu: cpus()[0]?.model ?? 'unknown',
    logical_cpus: cpus().length,
    memory_gb: Math.round((totalmem() / 2 ** 30) * 10) / 10,
    platform: `${platform()} ${release()}`,
    node: process.version,
  },
  page: { reels: REELS, extra_elements_per_reel: FILLER, steps_per_detection_run: STEPS, scroll_seconds_per_trial: SECONDS },
  scenarios: results,
};
mkdirSync(dirname(out), { recursive: true });
writeFileSync(out, JSON.stringify(report, null, 1) + '\n');
console.log(`wrote ${out}`);
