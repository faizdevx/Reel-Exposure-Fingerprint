import { describe, expect, it } from 'vitest';

import manifest from '../manifest.config';

// defineManifest may return a function (CRXJS env handling); resolve both shapes.
const resolved = (
  typeof manifest === 'function'
    ? await manifest({ command: 'build', mode: 'production' } as never)
    : manifest
) as unknown as Record<string, unknown> & {
  permissions: string[];
  host_permissions: string[];
  content_scripts: { matches: string[]; js: string[]; all_frames?: boolean }[];
};

describe('manifest', () => {
  it('is Manifest V3 with a service worker, a popup and one content script', () => {
    expect(resolved['manifest_version']).toBe(3);
    expect(resolved['background']).toMatchObject({ service_worker: 'src/background/index.ts' });
    expect(resolved['action']).toMatchObject({ default_popup: 'src/popup/index.html' });
    expect(resolved.content_scripts).toHaveLength(1);
  });

  it('asks for no permission that could read cookies or Instagram traffic', () => {
    expect([...resolved.permissions].sort()).toEqual(['alarms', 'storage']);
    for (const banned of [
      'cookies',
      'webRequest',
      'webRequestBlocking',
      'debugger',
      'scripting',
      'tabs',
      'history',
    ]) {
      expect(resolved.permissions).not.toContain(banned);
    }
  });

  it('injects only on instagram.com, in the top frame', () => {
    const [script] = resolved.content_scripts;
    expect(script?.matches).toEqual(['https://www.instagram.com/*']);
    expect(script?.all_frames).toBe(false);
    expect(script?.js).toEqual(['src/content/index.ts']);
  });

  it('has host access to Instagram and the local REF server only', () => {
    expect([...resolved.host_permissions].sort()).toEqual([
      'http://127.0.0.1/*',
      'http://localhost/*',
      'https://www.instagram.com/*',
    ]);
  });
});
