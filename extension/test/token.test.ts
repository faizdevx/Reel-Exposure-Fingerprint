import { describe, expect, it } from 'vitest';

import {
  TOKEN_KEY,
  clearToken,
  hasToken,
  parseToken,
  readToken,
  saveToken,
  type TokenStorage,
} from '../src/auth/token';

const TOKEN = 'AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_AbCde'; // 43 URL-safe characters

function memoryStorage(initial: Record<string, unknown> = {}): TokenStorage & {
  data: Record<string, unknown>;
} {
  const data = { ...initial };
  return {
    data,
    get: (keys) => Promise.resolve(Object.fromEntries(keys.filter((k) => k in data).map((k) => [k, data[k]]))),
    set: (items) => {
      Object.assign(data, items);
      return Promise.resolve();
    },
    remove: (keys) => {
      for (const k of keys) Reflect.deleteProperty(data, k);
      return Promise.resolve();
    },
  };
}

describe('parseToken', () => {
  it('accepts a 43 character URL-safe token and trims whitespace', () => {
    expect(TOKEN).toHaveLength(43);
    expect(parseToken(TOKEN)).toBe(TOKEN);
    expect(parseToken(`  ${TOKEN}\n`)).toBe(TOKEN);
  });

  it.each(['', 'short', `${TOKEN}x`, TOKEN.slice(1), `${TOKEN.slice(1)}!`, `${TOKEN.slice(1)} `, 'a b'.repeat(15)])(
    'rejects %j',
    (input) => {
      expect(parseToken(input)).toBeNull();
    },
  );
});

describe('token storage', () => {
  it('saves a valid token and reports it', async () => {
    const storage = memoryStorage();
    expect(await hasToken(storage)).toBe(false);
    expect(await saveToken(storage, TOKEN)).toBe(true);
    expect(await hasToken(storage)).toBe(true);
    expect(await readToken(storage)).toBe(TOKEN);
  });

  it('does not store an invalid token', async () => {
    const storage = memoryStorage();
    expect(await saveToken(storage, 'nope')).toBe(false);
    expect(storage.data).toEqual({});
  });

  it('removes the token', async () => {
    const storage = memoryStorage({ [TOKEN_KEY]: TOKEN });
    await clearToken(storage);
    expect(await hasToken(storage)).toBe(false);
    expect(await readToken(storage)).toBeNull();
  });

  it('ignores a corrupted stored value', async () => {
    const storage = memoryStorage({ [TOKEN_KEY]: 12345 });
    expect(await hasToken(storage)).toBe(false);
    expect(await readToken(storage)).toBeNull();
  });
});
