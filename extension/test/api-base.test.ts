import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  DEFAULT_API_BASE,
  hostPermissionFor,
  hostPermissions,
  validateApiBase,
} from '../src/api-base';

describe('validateApiBase (audit H-4)', () => {
  it('defaults to the local server', () => {
    expect(validateApiBase(undefined)).toBe('http://127.0.0.1:8000');
    expect(validateApiBase('')).toBe(DEFAULT_API_BASE);
    expect(validateApiBase('   ')).toBe(DEFAULT_API_BASE);
  });

  it.each(['https://ref.example.org', 'https://ref.example.org:8443', 'https://203.0.113.7'])(
    'accepts the HTTPS server %s',
    (url) => {
      expect(validateApiBase(url)).toBe(url);
    },
  );

  it.each(['http://127.0.0.1:8000', 'http://localhost:8000', 'http://[::1]:8000'])(
    'accepts plain HTTP for this computer only: %s',
    (url) => {
      expect(validateApiBase(url)).toBe(url);
    },
  );

  it('drops a trailing slash and nothing else', () => {
    expect(validateApiBase('https://ref.example.org/')).toBe('https://ref.example.org');
  });

  it.each([
    'http://ref.example.org',
    'http://203.0.113.7:8000',
    'http://127.0.0.1.evil.example',
    'http://localhost.evil.example',
    'ftp://ref.example.org',
    'ws://127.0.0.1:8000',
  ])('refuses %s: the token would travel in clear text or to the wrong place', (url) => {
    expect(() => validateApiBase(url)).toThrow(/https|127\.0\.0\.1/);
  });

  it.each([
    'https://user:pass@ref.example.org',
    'https://ref.example.org/api',
    'https://ref.example.org/?x=1',
    'https://ref.example.org/#frag',
    'not a url',
    'ref.example.org',
  ])('refuses %s', (url) => {
    expect(() => validateApiBase(url)).toThrow();
  });
});

describe('host permissions', () => {
  it('the default build keeps the two loopback names and Instagram', () => {
    expect(hostPermissions(DEFAULT_API_BASE)).toEqual([
      'https://www.instagram.com/*',
      'http://127.0.0.1/*',
      'http://localhost/*',
    ]);
  });

  it('a hosted build asks for its HTTPS server alone, never for localhost', () => {
    const permissions = hostPermissions('https://ref.example.org:8443');
    expect(permissions).toEqual(['https://www.instagram.com/*', 'https://ref.example.org/*']);
    expect(hostPermissionFor('https://ref.example.org:8443')).toBe('https://ref.example.org/*');
  });
});

describe('the manifest follows the build address', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  async function manifestFor(base: string | undefined) {
    vi.resetModules();
    if (base === undefined) vi.stubEnv('VITE_REF_API_BASE', '');
    else vi.stubEnv('VITE_REF_API_BASE', base);
    const module = (await import('../manifest.config')) as { default: unknown };
    const m = module.default;
    return (
      typeof m === 'function' ? await (m as (e: unknown) => unknown)({ command: 'build', mode: 'production' }) : m
    ) as { host_permissions: string[] };
  }

  it('asks for the hosted server only when built for it', async () => {
    expect((await manifestFor('https://ref.example.org')).host_permissions).toEqual([
      'https://www.instagram.com/*',
      'https://ref.example.org/*',
    ]);
  });

  it('refuses to build for an insecure remote server', async () => {
    await expect(manifestFor('http://ref.example.org')).rejects.toThrow(/https/);
  });
});
