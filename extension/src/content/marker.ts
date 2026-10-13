/**
 * The in-page monitoring marker (spec F14). Our own element, in a shadow root so Instagram's
 * styles cannot hide or restyle it. It reads nothing from the page.
 */

export const MARKER_ID = 'ref-monitoring-marker';
const LABEL = 'Reel Exposure Fingerprint: monitoring is on';

const STYLE = `
  :host { all: initial; }
  .marker {
    position: fixed; left: 12px; bottom: 12px; z-index: 2147483647;
    display: flex; align-items: center; gap: 6px;
    padding: 6px 10px; border-radius: 999px;
    background: #c62828; color: #fff;
    font: 600 12px/1 system-ui, sans-serif;
    pointer-events: none; user-select: none;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #fff; }
`;

export function showMarker(doc: Document): void {
  if (doc.getElementById(MARKER_ID) !== null) return;
  const host = doc.createElement('div');
  host.id = MARKER_ID;
  const root = host.attachShadow({ mode: 'open' });
  const style = doc.createElement('style');
  style.textContent = STYLE;
  const marker = doc.createElement('div');
  marker.className = 'marker';
  marker.setAttribute('role', 'status');
  const dot = doc.createElement('span');
  dot.className = 'dot';
  const text = doc.createElement('span');
  text.textContent = LABEL;
  marker.append(dot, text);
  root.append(style, marker);
  doc.documentElement.append(host);
}

export function hideMarker(doc: Document): void {
  doc.getElementById(MARKER_ID)?.remove();
}
