// M0 throwaway probe service worker. NOT production code.
// Stores probe logs locally in chrome.storage.local and runs the
// captureVisibleTab experiment (frame test B). No network calls.

const MAX_LOG = 20000;
let writeChain = Promise.resolve();

function appendLog(entries) {
  writeChain = writeChain.then(async () => {
    const { log = [] } = await chrome.storage.local.get('log');
    log.push(...entries);
    if (log.length > MAX_LOG) log.splice(0, log.length - MAX_LOG);
    await chrome.storage.local.set({ log });
  }).catch((e) => console.error('appendLog', e));
  return writeChain;
}

async function cropDataUrl(dataUrl, rect, dpr, wantData) {
  const blob = await (await fetch(dataUrl)).blob();
  const bmp = await createImageBitmap(blob);
  // Screenshot pixels may not equal CSS px * dpr exactly (zoom); derive scale.
  const sx = Math.max(0, Math.round(rect.x * dpr));
  const sy = Math.max(0, Math.round(rect.y * dpr));
  const sw = Math.min(bmp.width - sx, Math.round(rect.w * dpr));
  const sh = Math.min(bmp.height - sy, Math.round(rect.h * dpr));
  if (sw <= 0 || sh <= 0) return { ok: false, error: 'empty_crop', shotW: bmp.width, shotH: bmp.height };
  const scale = Math.min(1, 512 / Math.min(sw, sh));
  const ow = Math.round(sw * scale), oh = Math.round(sh * scale);
  const oc = new OffscreenCanvas(ow, oh);
  oc.getContext('2d').drawImage(bmp, sx, sy, sw, sh, 0, 0, ow, oh);
  const out = await oc.convertToBlob({ type: 'image/jpeg', quality: 0.7 });
  let data = null;
  if (wantData) {
    const buf = new Uint8Array(await out.arrayBuffer());
    let bin = '';
    for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
    data = `data:image/jpeg;base64,${btoa(bin)}`;
  }
  return { ok: true, shotW: bmp.width, shotH: bmp.height, w: ow, h: oh, bytes: out.size, dataUrl: data };
}

async function captureAndCrop(windowId, rect, dpr, wantData, path) {
  const t0 = performance.now();
  try {
    const shot = await chrome.tabs.captureVisibleTab(windowId, { format: 'jpeg', quality: 90 });
    const t1 = performance.now();
    const res = rect ? await cropDataUrl(shot, rect, dpr, wantData) : { ok: true, note: 'no_rect_full_shot_only' };
    const t2 = performance.now();
    return { path, captureMs: Math.round(t1 - t0), cropMs: Math.round(t2 - t1), ...res };
  } catch (e) {
    return { path, ok: false, error: String(e && e.message ? e.message : e), captureMs: Math.round(performance.now() - t0) };
  }
}

async function rateTest(windowId) {
  // 5 back-to-back calls to find the per-second quota error.
  const results = [];
  for (let i = 0; i < 5; i++) {
    const t0 = performance.now();
    try {
      await chrome.tabs.captureVisibleTab(windowId, { format: 'jpeg', quality: 50 });
      results.push({ i, ok: true, ms: Math.round(performance.now() - t0) });
    } catch (e) {
      results.push({ i, ok: false, ms: Math.round(performance.now() - t0), error: String(e && e.message ? e.message : e) });
    }
  }
  return results;
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.kind === 'log') {
    appendLog([msg.entry]);
    return false;
  }
  if (msg.kind === 'captureTest') {
    // Path "host_permission": triggered by the content script, no user gesture.
    captureAndCrop(sender.tab.windowId, msg.rect, msg.dpr, msg.wantData, 'host_permission_auto')
      .then(sendResponse);
    return true;
  }
  if (msg.kind === 'popupCapture') {
    // Path "activeTab": triggered from the popup (user gesture on the action).
    (async () => {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      let info = null;
      try { info = await chrome.tabs.sendMessage(tab.id, { kind: 'getPrimaryRect' }); } catch (_) { /* no CS */ }
      const res = await captureAndCrop(tab.windowId, info && info.rect, info ? info.dpr : 1, false, 'activeTab_popup');
      await appendLog([{ t: Date.now(), type: 'capture_popup', ...res }]);
      sendResponse(res);
    })();
    return true;
  }
  if (msg.kind === 'rateTest') {
    (async () => {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const res = await rateTest(tab.windowId);
      await appendLog([{ t: Date.now(), type: 'capture_rate', results: res }]);
      sendResponse(res);
    })();
    return true;
  }
  return false;
});

async function syncBadge() {
  const { recording, consent } = await chrome.storage.local.get(['recording', 'consent']);
  const on = !!(recording && consent);
  await chrome.action.setBadgeText({ text: on ? 'REC' : '' });
  await chrome.action.setBadgeBackgroundColor({ color: '#c62828' });
}
chrome.storage.onChanged.addListener((c) => { if (c.recording || c.consent) syncBadge(); });
chrome.runtime.onStartup.addListener(syncBadge);
chrome.runtime.onInstalled.addListener(syncBadge);
