// M0 throwaway probe popup. NOT production code.
const $ = (id) => document.getElementById(id);

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function tabMessage(msg) {
  const tab = await activeTab();
  try { return await chrome.tabs.sendMessage(tab.id, msg); } catch (_) { return null; }
}

function download(obj, name) {
  const blob = new Blob([JSON.stringify(obj)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

const stamp = () => new Date().toISOString().replace(/[:.]/g, '-');

async function refresh() {
  const s = await chrome.storage.local.get(['consent', 'recording', 'saveFrames', 'groundTruth', 'log']);
  $('consent').checked = !!s.consent;
  $('recording').checked = !!s.recording;
  $('recording').disabled = !s.consent;
  $('saveFrames').checked = !!s.saveFrames;
  $('groundTruth').value = s.groundTruth ?? '';
  const log = s.log || [];
  const exposures = log.filter((e) => e.type === 'exposure');
  const tabStatus = await tabMessage({ kind: 'status' });
  $('status').textContent = JSON.stringify({
    logEntries: log.length,
    exposures: exposures.length,
    uniqueCodes: new Set(exposures.map((e) => e.urlCodeAtEnd || e.urlCodeAtStart)).size,
    tab: tabStatus,
  }, null, 1);
}

$('consent').addEventListener('change', async (e) => {
  const on = e.target.checked;
  await chrome.storage.local.set(on ? { consent: true } : { consent: false, recording: false });
  refresh();
});
$('recording').addEventListener('change', async (e) => {
  await chrome.storage.local.set({ recording: e.target.checked });
  refresh();
});
$('saveFrames').addEventListener('change', async (e) => {
  await chrome.storage.local.set({ saveFrames: e.target.checked });
});
$('groundTruth').addEventListener('change', async (e) => {
  await chrome.storage.local.set({ groundTruth: e.target.value === '' ? null : Number(e.target.value) });
});

$('capture').addEventListener('click', async () => {
  const res = await chrome.runtime.sendMessage({ kind: 'popupCapture' });
  $('status').textContent = JSON.stringify(res, null, 1);
});
$('rate').addEventListener('click', async () => {
  const res = await chrome.runtime.sendMessage({ kind: 'rateTest' });
  $('status').textContent = JSON.stringify(res, null, 1);
});

$('exportLog').addEventListener('click', async () => {
  const s = await chrome.storage.local.get(['log', 'groundTruth']);
  download({
    probeExport: 'log',
    exportedAt: new Date().toISOString(),
    userAgent: navigator.userAgent,
    groundTruth: s.groundTruth ?? null,
    log: s.log || [],
  }, `m0-probe-log-${stamp()}.json`);
});
$('exportFrames').addEventListener('click', async () => {
  const res = await tabMessage({ kind: 'getFrames' });
  download({ probeExport: 'frames', exportedAt: new Date().toISOString(), frames: (res && res.frames) || [] },
    `m0-probe-frames-${stamp()}.json`);
});
$('clear').addEventListener('click', async () => {
  await chrome.storage.local.set({ log: [] });
  await tabMessage({ kind: 'clearFrames' });
  refresh();
});

refresh();
