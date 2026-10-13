import { clearToken, saveToken } from '../auth/token';
import { POLICY, POLICY_VERSION } from '../consent/policy';
import type { ActionResponse, Request, StatusResponse } from '../shared/messages';
import { describeError, viewFor } from './render';

async function send<T>(request: Request): Promise<T | null> {
  try {
    return (await chrome.runtime.sendMessage(request)) as T;
  } catch {
    return null;
  }
}

function mustFind<T extends HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (!el) throw new Error(`popup markup is missing #${id}`);
  return el as T;
}

function renderPolicy(): void {
  const root = mustFind('policy');
  root.replaceChildren();
  const intro = document.createElement('p');
  intro.textContent = POLICY.intro;
  root.append(intro);
  const sections: [string, readonly string[]][] = [
    ['What is recorded', POLICY.collected],
    ['What is worked out from it', POLICY.derived],
    ['What is never recorded', POLICY.never],
    ['When it runs', POLICY.when],
    ['Your control', POLICY.control],
  ];
  for (const [title, items] of sections) {
    const h = document.createElement('h3');
    h.textContent = title;
    const ul = document.createElement('ul');
    for (const item of items) {
      const li = document.createElement('li');
      li.textContent = item;
      ul.append(li);
    }
    root.append(h, ul);
  }
  mustFind('policy-version').textContent = POLICY_VERSION;
}

let monitoring = false;

async function refresh(error = ''): Promise<void> {
  const view = viewFor(await send<StatusResponse>({ type: 'getStatus' }));
  monitoring = view.sessionButtonLabel === 'Stop monitoring';
  mustFind('badge').hidden = !view.badge;
  mustFind('status').textContent = view.statusText;
  mustFind('token-status').textContent = view.tokenText;
  mustFind('note').textContent = view.note;
  mustFind('error').textContent = error;
  const toggle = mustFind<HTMLButtonElement>('toggle');
  toggle.textContent = view.sessionButtonLabel;
  toggle.disabled = view.sessionButtonDisabled;
  mustFind('consent-screen').hidden = !view.consentScreen;
  mustFind('consent-given').hidden = view.consentSummary === '';
  mustFind('consent-summary').textContent = view.consentSummary;
}

async function act(request: Request): Promise<void> {
  const result = await send<ActionResponse>(request);
  const error = result === null ? 'Cannot reach the extension background.' : !result.ok ? describeError(result.error) : '';
  await refresh(error);
}

mustFind<HTMLInputElement>('consent-check').addEventListener('change', (event) => {
  mustFind<HTMLButtonElement>('consent-grant').disabled = !(event.target as HTMLInputElement).checked;
});
mustFind('consent-grant').addEventListener('click', () => {
  const check = mustFind<HTMLInputElement>('consent-check');
  if (!check.checked) return;
  check.checked = false;
  mustFind<HTMLButtonElement>('consent-grant').disabled = true;
  void act({ type: 'grantConsent' });
});
mustFind('consent-revoke').addEventListener('click', () => void act({ type: 'revokeConsent' }));
mustFind('toggle').addEventListener(
  'click',
  () => void act({ type: monitoring ? 'stopSession' : 'startSession' }),
);

mustFind('token-save').addEventListener('click', () => {
  const input = mustFind<HTMLInputElement>('token-input');
  void saveToken(chrome.storage.local, input.value).then(async (saved) => {
    mustFind('token-error').textContent = saved
      ? ''
      : 'That is not a valid token (expected 43 URL-safe characters).';
    if (saved) input.value = ''; // never leave the secret in the field
    await refresh();
  });
});
mustFind('token-clear').addEventListener('click', () => {
  void clearToken(chrome.storage.local).then(async () => {
    mustFind('token-error').textContent = '';
    await refresh();
  });
});

renderPolicy();
void refresh();
