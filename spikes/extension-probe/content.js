// M0 throwaway probe. NOT production code. See spikes/README.md.
// Boundary: reads rendered DOM and <video> pixels only. Never touches
// document.cookie, storage of instagram.com, network requests or responses.
// Never scrolls, clicks, plays or otherwise drives the page.
(() => {
  'use strict';

  const PROBE_VERSION = 'm0-probe-0.1.0';
  const PRIMARY_MIN_RATIO = 0.6;
  const FRAME_OFFSETS_S = [0.5, 2, 4, 7, 11];
  const CAPTURE_B_OFFSET_S = 2; // one captureVisibleTab per exposure (quota ~2/s)
  const MAX_SAVED_FRAMES = 200;
  const RESERVED_SEGMENTS = new Set([
    'explore', 'reels', 'reel', 'p', 'stories', 'direct', 'accounts', 'about',
    'legal', 'developer', 'tv', 'web', 'emails', 'challenge', 'session',
    'privacy', 'terms', 'api', 'graphql', 'ar', 'directory', 'lite', 'topics',
    'your_activity', 'notifications', 'audio', 'locations', 'create', 'oauth',
  ]);

  // ---------- state ----------
  let consent = false;
  let recording = false;
  let saveFrames = false;
  let active = false; // recording && consent && on Reels surface

  let lastPath = location.pathname;
  let exposureSeq = 0;
  let cur = null; // current exposure
  const ratios = new Map(); // video -> {ratio, area}
  const tracked = new WeakSet();
  const savedFrames = [];
  const perf = { mutation: [], io: [], drawA: [], encodeA: [], meta: [], longtasks: 0, longtaskMs: 0 };

  const now = () => performance.now();
  const round = (x) => Math.round(x * 10) / 10;

  function send(type, data) {
    if (!recording || !consent) return;
    try {
      chrome.runtime.sendMessage({
        kind: 'log',
        entry: { t: Date.now(), pt: round(now()), type, ...data },
      }).catch(() => {});
    } catch (_) { /* extension reloaded */ }
  }

  // ---------- URL / surface ----------
  function parsePath(p) {
    const m = p.match(/^\/(reels?|p)\/([A-Za-z0-9_-]+)\/?/);
    if (m) return { surface: m[1], code: m[2] };
    if (/^\/reels\/?$/.test(p)) return { surface: 'reels-root', code: null };
    return { surface: 'other', code: null };
  }
  const isReelSurface = (p) => /^\/reels?\//.test(p) || /^\/reels\/?$/.test(p);

  function pollUrl() {
    const p = location.pathname;
    if (p === lastPath) return;
    const prev = parsePath(lastPath);
    const next = parsePath(p);
    lastPath = p;
    // Never log full non-Reel paths (could be profile names); surface only.
    send('url', {
      from: prev.surface, to: next.surface, code: next.code,
      primaryAgeMs: cur ? round(now() - cur.startPt) : null,
      curId: cur ? cur.id : null,
    });
    if (cur && next.code) {
      cur.urlCodes.push({ code: next.code, lagMs: round(now() - cur.startPt) });
    }
    updateActive();
  }

  // ---------- in-page marker (F14) ----------
  let marker = null;
  function showMarker(on) {
    if (on && !marker) {
      const host = document.createElement('div');
      host.style.cssText = 'position:fixed;top:8px;left:8px;z-index:2147483647;pointer-events:none;';
      const root = host.attachShadow({ mode: 'closed' });
      const pill = document.createElement('div');
      pill.textContent = '● M0 probe recording';
      pill.style.cssText = 'font:12px system-ui;background:#c62828;color:#fff;padding:4px 8px;border-radius:12px;opacity:.9;';
      root.appendChild(pill);
      document.documentElement.appendChild(host);
      marker = host;
    } else if (!on && marker) {
      marker.remove();
      marker = null;
    }
  }

  // ---------- video tracking ----------
  const io = new IntersectionObserver((entries) => {
    const s = now();
    let latestEntryTime = 0;
    for (const e of entries) {
      ratios.set(e.target, {
        ratio: e.intersectionRatio,
        area: e.intersectionRect.width * e.intersectionRect.height,
      });
      latestEntryTime = Math.max(latestEntryTime, e.time);
    }
    recomputePrimary('io', latestEntryTime);
    perf.io.push(now() - s);
  }, { threshold: [0, 0.25, 0.5, 0.6, 0.75, 0.9, 1] });

  function onMediaEvent(ev) {
    if (cur && ev.target === cur.video) {
      updateDwell();
      cur.mediaEvents[ev.type] = (cur.mediaEvents[ev.type] || 0) + 1;
    }
  }

  function trackVideos() {
    const vids = document.getElementsByTagName('video');
    for (const v of vids) {
      if (tracked.has(v)) continue;
      tracked.add(v);
      io.observe(v);
      for (const t of ['play', 'playing', 'pause', 'waiting', 'ended', 'seeking', 'seeked', 'stalled']) {
        v.addEventListener(t, onMediaEvent, { passive: true });
      }
    }
    for (const v of ratios.keys()) {
      if (!v.isConnected) { ratios.delete(v); io.unobserve(v); }
    }
  }

  const mo = new MutationObserver((records) => {
    const s = now();
    let added = false;
    for (const r of records) { if (r.addedNodes.length) { added = true; break; } }
    if (added) trackVideos();
    if (cur && !cur.video.isConnected) recomputePrimary('detached', 0);
    perf.mutation.push(now() - s);
  });

  function recomputePrimary(cause, entryTime) {
    let best = null;
    let bestArea = -1;
    for (const [v, r] of ratios) {
      if (!v.isConnected || r.ratio < PRIMARY_MIN_RATIO) continue;
      if (r.area > bestArea) { best = v; bestArea = r.area; }
    }
    if (best === (cur ? cur.video : null)) return;
    const detectLatency = entryTime ? round(now() - entryTime) : null;
    if (cur) endExposure('primary_change');
    if (best) startExposure(best, cause, detectLatency);
  }

  // ---------- playing / visible / dwell ----------
  const isPlaying = (v) => !v.paused && !v.ended && v.readyState >= 3;
  const isVisible = () => document.visibilityState === 'visible';

  function updateDwell() {
    if (!cur) return;
    const t = now();
    const dt = t - cur.lastTick;
    if (cur.lastState === 'counting') cur.dwellMs += dt;
    else if (cur.lastState === 'hidden') cur.hiddenMs += dt;
    else if (cur.lastState === 'paused') cur.pausedMs += dt;
    cur.lastTick = t;
    cur.lastState = !isVisible() ? 'hidden' : (isPlaying(cur.video) ? 'counting' : 'paused');
  }

  document.addEventListener('visibilitychange', () => {
    updateDwell();
    send('visibility', { state: document.visibilityState, curId: cur ? cur.id : null });
  });
  window.addEventListener('focus', () => send('focus', { focused: true }));
  window.addEventListener('blur', () => send('focus', { focused: false }));

  // ---------- metadata (no class-name selectors) ----------
  function pathOf(a) {
    try { return new URL(a.getAttribute('href'), location.origin).pathname; } catch (_) { return null; }
  }

  function findContainer(video) {
    // Highest ancestor that still contains exactly one <video>.
    let el = video;
    let depth = 0;
    while (el.parentElement && el.parentElement !== document.body && depth < 30) {
      const parent = el.parentElement;
      if (parent.getElementsByTagName('video').length > 1) break;
      el = parent;
      depth++;
    }
    return { el, depth };
  }

  function extractMeta(video) {
    const s = now();
    const { el, depth } = findContainer(video);
    const anchors = Array.from(el.querySelectorAll('a[href]'));

    const handles = [];
    const tagLinks = [];
    const codeLinks = [];
    for (const a of anchors) {
      const p = pathOf(a);
      if (!p) continue;
      let m;
      if ((m = p.match(/^\/explore\/tags\/([^/]+)\/?$/))) {
        tagLinks.push(decodeURIComponent(m[1]));
      } else if ((m = p.match(/^\/(reels?|p)\/([A-Za-z0-9_-]+)\/?/))) {
        codeLinks.push(m[2]);
      } else if ((m = p.match(/^\/([A-Za-z0-9._]{1,30})\/?$/)) && !RESERVED_SEGMENTS.has(m[1].toLowerCase())) {
        handles.push(m[1]);
      }
    }
    const handleCounts = {};
    for (const h of handles) handleCounts[h] = (handleCounts[h] || 0) + 1;
    const handleRanked = Object.entries(handleCounts).sort((a, b) => b[1] - a[1]);
    const handle = handleRanked.length ? handleRanked[0][0] : null;

    // Caption candidates: h1 first, then elements carrying dir="auto"
    // (user-authored text), longest text not equal to a handle.
    let caption = null;
    let captionStrategy = null;
    const h1 = el.querySelector('h1');
    if (h1 && h1.innerText.trim()) {
      caption = h1.innerText.trim();
      captionStrategy = 'h1';
    } else {
      let bestLen = 0;
      for (const n of el.querySelectorAll('[dir="auto"]')) {
        const txt = (n.innerText || '').trim();
        if (!txt || handleCounts[txt] || txt.length <= bestLen) continue;
        if (n.closest('a') && !txt.includes(' ')) continue; // bare link label
        caption = txt;
        bestLen = txt.length;
        captionStrategy = 'dir-auto-longest';
      }
    }

    // Truncation marker ("more"), detected but never clicked.
    let truncated = false;
    for (const b of el.querySelectorAll('[role="button"], button, span')) {
      const t = (b.textContent || '').trim().toLowerCase();
      if (t === 'more' || t === '… more' || t === '...more' || t === 'और' || t === '… और') { truncated = true; break; }
    }

    const regexTags = caption ? Array.from(caption.matchAll(/#([\p{L}\p{M}\p{N}_]+)/gu), (m) => m[1]) : [];

    // Accessibility-label strategy for handle, as a cross-check only.
    let altHandle = null;
    for (const img of el.querySelectorAll('img[alt]')) {
      const m = img.getAttribute('alt').match(/^([A-Za-z0-9._]{1,30})'s profile picture$/);
      if (m) { altHandle = m[1]; break; }
    }

    const ms = now() - s;
    perf.meta.push(ms);
    return {
      containerDepth: depth,
      anchorCount: anchors.length,
      handle, handleCandidates: handleRanked.slice(0, 5), altHandle,
      caption, captionLen: caption ? caption.length : 0, captionStrategy, truncated,
      tagLinks: Array.from(new Set(tagLinks)), regexTags: Array.from(new Set(regexTags)),
      domCodes: Array.from(new Set(codeLinks)),
      ms: round(ms),
    };
  }

  // ---------- frame test A: drawImage(video) -> canvas -> blob ----------
  function frameSize(v) {
    const w = v.videoWidth, h = v.videoHeight;
    if (!w || !h) return null;
    const scale = Math.min(1, 512 / Math.min(w, h));
    return { w: Math.round(w * scale), h: Math.round(h * scale), srcW: w, srcH: h };
  }

  function lumaStats(src) {
    const c = document.createElement('canvas');
    c.width = 16; c.height = 16;
    const ctx = c.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(src, 0, 0, 16, 16);
    const d = ctx.getImageData(0, 0, 16, 16).data; // throws SecurityError if tainted
    let sum = 0, sumSq = 0;
    for (let i = 0; i < d.length; i += 4) {
      const y = 0.299 * d[i] + 0.587 * d[i + 1] + 0.114 * d[i + 2];
      sum += y; sumSq += y * y;
    }
    const n = d.length / 4;
    const mean = sum / n;
    return { mean: round(mean), std: round(Math.sqrt(Math.max(0, sumSq / n - mean * mean))) };
  }

  async function frameTestA(exp, offsetS) {
    const v = exp.video;
    const r = { offsetS, method: 'A' };
    try {
      if (!isPlaying(v)) r.note = 'not_playing';
      const size = frameSize(v);
      if (!size) { r.ok = false; r.error = 'no_video_dimensions'; return r; }
      Object.assign(r, size);
      const t0 = now();
      const oc = new OffscreenCanvas(size.w, size.h);
      oc.getContext('2d').drawImage(v, 0, 0, size.w, size.h);
      const t1 = now();
      perf.drawA.push(t1 - t0);
      const blob = await oc.convertToBlob({ type: 'image/jpeg', quality: 0.7 });
      const t2 = now();
      perf.encodeA.push(t2 - t1);
      r.drawMs = round(t1 - t0);
      r.encodeMs = round(t2 - t1);
      r.bytes = blob.size;
      r.luma = lumaStats(v); // taint + black-frame check
      r.ok = true;
      // Spec wording: HTMLCanvasElement.toBlob. Test it once per exposure too.
      if (offsetS === FRAME_OFFSETS_S[0] || !exp.canvasToBlobTested) {
        exp.canvasToBlobTested = true;
        r.canvasToBlob = await new Promise((resolve) => {
          try {
            const c = document.createElement('canvas');
            c.width = size.w; c.height = size.h;
            c.getContext('2d').drawImage(v, 0, 0, size.w, size.h);
            c.toBlob((b) => resolve(b ? { ok: true, bytes: b.size } : { ok: false, error: 'null_blob' }), 'image/jpeg', 0.7);
          } catch (e) { resolve({ ok: false, error: `${e.name}: ${e.message}` }); }
        });
      }
      if (saveFrames && savedFrames.length < MAX_SAVED_FRAMES) {
        savedFrames.push({ exposureId: exp.id, code: currentCode(exp), method: 'A', offsetS, dataUrl: await blobToDataUrl(blob) });
      }
    } catch (e) {
      r.ok = false;
      r.error = `${e.name}: ${e.message}`;
    }
    return r;
  }

  // ---------- frame test B: captureVisibleTab via service worker ----------
  async function frameTestB(exp, offsetS) {
    const r = { offsetS, method: 'B' };
    if (!isVisible()) { r.ok = false; r.error = 'tab_hidden_skipped'; return r; }
    const rect = exp.video.getBoundingClientRect();
    try {
      const res = await chrome.runtime.sendMessage({
        kind: 'captureTest',
        rect: { x: rect.x, y: rect.y, w: rect.width, h: rect.height },
        dpr: window.devicePixelRatio,
        viewport: { w: window.innerWidth, h: window.innerHeight },
        wantData: saveFrames && savedFrames.length < MAX_SAVED_FRAMES,
      });
      Object.assign(r, res, { dataUrl: undefined });
      if (res && res.ok && res.dataUrl) {
        savedFrames.push({ exposureId: exp.id, code: currentCode(exp), method: 'B', offsetS, dataUrl: res.dataUrl });
      }
    } catch (e) {
      r.ok = false;
      r.error = `${e.name}: ${e.message}`;
    }
    return r;
  }

  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(fr.result);
      fr.onerror = () => reject(fr.error);
      fr.readAsDataURL(blob);
    });
  }

  // ---------- exposure lifecycle ----------
  function currentCode(exp) {
    const u = exp.urlCodes.length ? exp.urlCodes[exp.urlCodes.length - 1].code : exp.urlCodeAtStart;
    return u || (exp.metaEnd || exp.metaStart || {}).domCodes?.[0] || null;
  }

  function startExposure(video, cause, detectLatency) {
    const t = now();
    cur = {
      id: `${PROBE_VERSION}-${Date.now()}-${++exposureSeq}`,
      video, cause, detectLatency,
      startPt: t, startWall: Date.now(), lastTick: t,
      lastState: !isVisible() ? 'hidden' : (isPlaying(video) ? 'counting' : 'paused'),
      dwellMs: 0, hiddenMs: 0, pausedMs: 0,
      urlCodeAtStart: parsePath(location.pathname).code,
      urlCodes: [],
      mediaEvents: {},
      frames: [],
      timers: [],
      metaStart: null, metaEnd: null,
    };
    const exp = cur;
    try { exp.metaStart = extractMeta(video); } catch (e) { exp.metaStart = { error: `${e.name}: ${e.message}` }; }
    for (const off of FRAME_OFFSETS_S) {
      exp.timers.push(setTimeout(async () => {
        if (cur !== exp) return;
        exp.frames.push(await frameTestA(exp, off));
      }, off * 1000));
    }
    exp.timers.push(setTimeout(async () => {
      if (cur !== exp) return;
      exp.frames.push(await frameTestB(exp, CAPTURE_B_OFFSET_S));
    }, CAPTURE_B_OFFSET_S * 1000 + 100));
    exp.timers.push(setTimeout(() => {
      if (cur !== exp) return;
      try { exp.metaLate = extractMeta(video); } catch (_) { /* recorded at end */ }
    }, 1000));
  }

  function endExposure(reason) {
    const exp = cur;
    if (!exp) return;
    updateDwell();
    cur = null;
    for (const t of exp.timers) clearTimeout(t);
    let metaEnd = null;
    try { metaEnd = exp.video.isConnected ? extractMeta(exp.video) : null; } catch (e) { metaEnd = { error: `${e.name}: ${e.message}` }; }
    const meta = metaEnd && !metaEnd.error ? metaEnd : (exp.metaLate || exp.metaStart);
    const d = exp.video.duration;
    send('exposure', {
      id: exp.id,
      reason,
      cause: exp.cause,
      detectLatencyMs: exp.detectLatency,
      primaryMs: round(now() - exp.startPt),
      dwellMs: round(exp.dwellMs),
      hiddenMs: round(exp.hiddenMs),
      pausedMs: round(exp.pausedMs),
      videoDurationMs: Number.isFinite(d) ? Math.round(d * 1000) : String(d),
      loop: exp.video.loop,
      mediaEvents: exp.mediaEvents,
      urlCodeAtStart: exp.urlCodeAtStart,
      urlCodes: exp.urlCodes,
      urlCodeAtEnd: parsePath(location.pathname).code,
      metaStart: exp.metaStart,
      metaLate: exp.metaLate || null,
      metaEnd: meta,
      metaChanged: JSON.stringify(exp.metaStart && exp.metaStart.caption) !== JSON.stringify(meta && meta.caption),
      frames: exp.frames,
    });
  }

  // ---------- activation ----------
  let urlTimer = null;
  let flushTimer = null;
  let longTaskObs = null;

  function updateActive() {
    const want = consent && recording && isReelSurface(location.pathname);
    if (want === active) return;
    active = want;
    if (active) {
      trackVideos();
      mo.observe(document.body, { childList: true, subtree: true });
      flushTimer = setInterval(flushPerf, 10000);
      try {
        longTaskObs = new PerformanceObserver((list) => {
          for (const e of list.getEntries()) { perf.longtasks++; perf.longtaskMs += e.duration; }
        });
        longTaskObs.observe({ type: 'longtask', buffered: false });
      } catch (_) { /* unsupported */ }
      send('active', { on: true, surface: parsePath(location.pathname).surface });
    } else {
      if (cur) endExposure(isReelSurface(location.pathname) ? 'stop' : 'left_surface');
      mo.disconnect();
      for (const v of ratios.keys()) io.unobserve(v);
      ratios.clear();
      clearInterval(flushTimer);
      if (longTaskObs) longTaskObs.disconnect();
      flushPerf();
      send('active', { on: false, surface: parsePath(location.pathname).surface });
    }
    showMarker(consent && recording);
  }

  function pct(arr, p) {
    if (!arr.length) return null;
    const s = [...arr].sort((a, b) => a - b);
    return round(s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))]);
  }

  function flushPerf() {
    const out = {};
    for (const k of ['mutation', 'io', 'drawA', 'encodeA', 'meta']) {
      const a = perf[k];
      out[k] = { n: a.length, p50: pct(a, 50), p95: pct(a, 95), max: a.length ? round(Math.max(...a)) : null };
      perf[k] = [];
    }
    out.longtasks = perf.longtasks;
    out.longtaskMs = round(perf.longtaskMs);
    perf.longtasks = 0; perf.longtaskMs = 0;
    send('perf', out);
  }

  window.addEventListener('pagehide', () => {
    if (cur) endExposure('pagehide');
  });

  // ---------- messages from popup / SW ----------
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.kind === 'getFrames') {
      sendResponse({ frames: savedFrames });
    } else if (msg.kind === 'clearFrames') {
      savedFrames.length = 0;
      sendResponse({ ok: true });
    } else if (msg.kind === 'getPrimaryRect') {
      if (!cur) { sendResponse({ rect: null }); return; }
      const r = cur.video.getBoundingClientRect();
      sendResponse({
        rect: { x: r.x, y: r.y, w: r.width, h: r.height },
        dpr: window.devicePixelRatio,
        viewport: { w: window.innerWidth, h: window.innerHeight },
      });
    } else if (msg.kind === 'status') {
      sendResponse({
        active, surface: parsePath(location.pathname).surface,
        videos: ratios.size, current: cur ? { id: cur.id, code: currentCode(cur), dwellMs: round(cur.dwellMs) } : null,
        savedFrames: savedFrames.length,
      });
    }
    return false;
  });

  // ---------- boot ----------
  chrome.storage.local.get(['consent', 'recording', 'saveFrames']).then((s) => {
    consent = !!s.consent;
    recording = !!s.recording;
    saveFrames = !!s.saveFrames;
    send('cs_loaded', { version: PROBE_VERSION, surface: parsePath(location.pathname).surface, ua: navigator.userAgent });
    updateActive();
  });
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    if (changes.consent) consent = !!changes.consent.newValue;
    if (changes.recording) recording = !!changes.recording.newValue;
    if (changes.saveFrames) saveFrames = !!changes.saveFrames.newValue;
    updateActive();
  });
  setInterval(() => {
    pollUrl();
    if (active) updateDwell();
  }, 200);
})();
