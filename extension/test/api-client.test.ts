import { describe, expect, it, vi } from 'vitest';

import { ApiError, createApiClient, parseRetryAfter } from '../src/api/client';
import { TOKEN } from './helpers';

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });

type FetchMock = ReturnType<typeof vi.fn<typeof fetch>>;

function client(fetchFn: FetchMock, token: string | null = TOKEN) {
  return createApiClient({
    baseUrl: 'http://127.0.0.1:8000',
    getToken: () => Promise.resolve(token),
    fetchFn,
  });
}

const mockFetch = (respond: () => Promise<Response>): FetchMock => vi.fn<typeof fetch>(respond);

describe('API client', () => {
  it('sends the token as a Bearer header and never in the URL or body', async () => {
    const fetchFn = mockFetch(() => Promise.resolve(json({ policy_version: 'v' })));
    await client(fetchFn).putConsent('v');
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://127.0.0.1:8000/api/v1/consent');
    expect(init.method).toBe('PUT');
    expect((init.headers as Record<string, string>)['Authorization']).toBe(`Bearer ${TOKEN}`);
    expect(url).not.toContain(TOKEN);
    expect(init.body).toBe(JSON.stringify({ policy_version: 'v' }));
  });

  it('makes no request without a token', async () => {
    const fetchFn = mockFetch(() => Promise.resolve(json({})));
    await expect(client(fetchFn, null).startSession()).rejects.toMatchObject({
      status: 0,
      code: 'no_token',
    });
    expect(fetchFn).not.toHaveBeenCalled();
  });

  it('maps the endpoints and bodies', async () => {
    const fetchFn = mockFetch(() => Promise.resolve(json({})));
    const api = client(fetchFn);
    await api.startSession();
    await api.stopSession('abc/def', 'tab_closed');
    const calls = fetchFn.mock.calls as unknown as [string, RequestInit][];
    expect(calls[0]?.[0]).toBe('http://127.0.0.1:8000/api/v1/sessions');
    expect(calls[0]?.[1].method).toBe('POST');
    expect(calls[1]?.[0]).toBe('http://127.0.0.1:8000/api/v1/sessions/abc%2Fdef');
    expect(calls[1]?.[1].method).toBe('PATCH');
    expect(calls[1]?.[1].body).toBe(
      JSON.stringify({ status: 'stopped', stop_reason: 'tab_closed' }),
    );
  });

  it('turns a string detail into the error code', async () => {
    const fetchFn = mockFetch(() => Promise.resolve(json({ detail: 'consent_required' }, 403)));
    await expect(client(fetchFn).startSession()).rejects.toMatchObject({
      status: 403,
      code: 'consent_required',
    });
  });

  it('falls back to a generic code for other bodies', async () => {
    const validation = mockFetch(() => Promise.resolve(json({ detail: [{ msg: 'bad' }] }, 422)));
    await expect(client(validation).startSession()).rejects.toMatchObject({ code: 'http_422' });
    const html = mockFetch(() => Promise.resolve(new Response('<html>', { status: 502 })));
    await expect(client(html).startSession()).rejects.toMatchObject({ code: 'http_502' });
  });

  it('reports a network failure as code network, without leaking the token', async () => {
    const fetchFn = mockFetch(() => Promise.reject(new TypeError('Failed to fetch')));
    const error = await client(fetchFn)
      .deleteConsent()
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 0, code: 'network' });
    expect(String(error)).not.toContain(TOKEN);
  });

  it('accepts an empty 204', async () => {
    const fetchFn = mockFetch(() => Promise.resolve(new Response(null, { status: 204 })));
    await expect(client(fetchFn).deleteConsent()).resolves.toBeUndefined();
  });
});

describe('Retry-After (audit H-1)', () => {
  it('reads whole seconds and nothing else', () => {
    expect(parseRetryAfter('7')).toBe(7000);
    expect(parseRetryAfter(' 120 ')).toBe(120_000);
    for (const bad of [null, '', '-1', '1.5', 'soon', 'Wed, 21 Oct 2026 07:28:00 GMT', '1e3', '9999999']) {
      expect(parseRetryAfter(bad)).toBeNull();
    }
  });

  it('puts the wait on the error for 429 and 503, and leaves it null otherwise', async () => {
    const limited = mockFetch(() =>
      Promise.resolve(json({ detail: 'rate_limited' }, 429, { 'Retry-After': '9' })),
    );
    await expect(client(limited).startSession()).rejects.toMatchObject({
      status: 429,
      code: 'rate_limited',
      retryAfterMs: 9000,
    });
    const busy = mockFetch(() => Promise.resolve(json({ detail: 'busy' }, 503, { 'Retry-After': '1' })));
    await expect(client(busy).startSession()).rejects.toMatchObject({ status: 503, retryAfterMs: 1000 });
    const plain = mockFetch(() => Promise.resolve(json({ detail: 'x' }, 500)));
    await expect(client(plain).startSession()).rejects.toMatchObject({ retryAfterMs: null });
  });
});
