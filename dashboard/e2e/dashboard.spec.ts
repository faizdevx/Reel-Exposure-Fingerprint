import { expect, test, type Locator } from '@playwright/test';

import { EMAIL, mockApi, PASSWORD, topics, type MockSession } from './mock-api';

const EXPOSURE = topics({ Cricket: 0.4, 'Tech & coding': 0.25, Food: 0.2, Travel: 0.1, Music: 0.05 });
const ENGAGEMENT = topics({ 'Tech & coding': 0.55, Cricket: 0.25, Food: 0.2 });

const SESSIONS: MockSession[] = [
  {
    id: 's2',
    started_at: '2026-10-02T09:00:00Z',
    ended_at: '2026-10-02T09:45:00Z',
    status: 'stopped',
    stop_reason: 'time_cap',
    n_reels: 118,
  },
  {
    id: 's1',
    started_at: '2026-10-01T18:30:00Z',
    ended_at: '2026-10-01T18:42:00Z',
    status: 'stopped',
    stop_reason: 'user',
    n_reels: 31,
  },
  {
    id: 's3',
    started_at: '2026-10-03T07:00:00Z',
    ended_at: null,
    status: 'active',
    stop_reason: null,
    n_reels: null,
  },
];

/** Every percentage in a share table, as numbers (the Total row excluded). */
async function shareValues(table: Locator): Promise<number[]> {
  const cells = await table.locator('tbody td').allInnerTexts();
  return cells.map((text) => Number.parseFloat(text.replace('%', '')));
}

const sum = (values: number[]) => Math.round(values.reduce((a, b) => a + b, 0) * 10) / 10;

test.describe('login', () => {
  test('a logged-out visitor is sent to /login, a wrong password is refused, a right one gets in', async ({
    page,
  }) => {
    const api = await mockApi(page, { exposure: EXPOSURE, engagement: ENGAGEMENT });

    await page.goto('/');
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole('heading', { name: 'Log in' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Log in' })).toBeDisabled();

    await page.getByLabel('Email').fill(EMAIL);
    await page.getByLabel('Password').fill('wrong password here');
    await page.getByRole('button', { name: 'Log in' }).click();
    await expect(page.getByRole('alert')).toHaveText('Invalid email or password.');
    await expect(page).toHaveURL(/\/login$/);

    await page.getByLabel('Password').fill(PASSWORD);
    await page.getByRole('button', { name: 'Log in' }).click();
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole('heading', { name: 'Your fingerprints' })).toBeVisible();

    // The server requires this header on cookie-authenticated state changes.
    expect(api.loginHeaders.at(-1)?.['x-requested-with']).toBe('ref-dashboard');
    expect(api.loginBodies.at(-1)).toEqual({ email: EMAIL, password: PASSWORD });
  });

  test('log out returns to the login page and closes the dashboard', async ({ page }) => {
    await mockApi(page, { loggedIn: true, exposure: EXPOSURE, engagement: ENGAGEMENT });
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Your fingerprints' })).toBeVisible();

    await page.getByRole('button', { name: 'Log out' }).click();
    await expect(page).toHaveURL(/\/login$/);

    await page.goto('/');
    await expect(page).toHaveURL(/\/login$/);
  });
});

test.describe('dashboard load', () => {
  test('shows loading placeholders first, then both fingerprints', async ({ page }) => {
    await mockApi(page, { loggedIn: true, exposure: EXPOSURE, engagement: ENGAGEMENT, delayMs: 600 });
    await page.goto('/');

    await expect(page.getByRole('status')).toHaveCount(3); // two fingerprints and the prediction
    await expect(page.getByRole('heading', { name: 'Exposure fingerprint' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Engagement fingerprint' })).toBeVisible();

    await expect(page.getByRole('table', { name: 'Exposure fingerprint topic shares' })).toBeVisible();
    await expect(page.getByRole('table', { name: 'Engagement fingerprint topic shares' })).toBeVisible();
    await expect(page.getByRole('status')).toHaveCount(0);
    await expect(page.getByText('server ok')).toBeVisible();
  });

  test('shows an empty state when no snapshot exists yet', async ({ page }) => {
    await mockApi(page, { loggedIn: true });
    await page.goto('/');
    await expect(page.getByText(/No exposure fingerprint yet/)).toBeVisible();
    await expect(page.getByText(/No engagement fingerprint yet/)).toBeVisible();
    await expect(page.getByRole('table')).toHaveCount(0);
  });

  test('shows an error per card and recovers on retry', async ({ page }) => {
    const api = await mockApi(page, {
      loggedIn: true,
      exposure: EXPOSURE,
      engagement: ENGAGEMENT,
      failFingerprints: 3, // first request and its two automatic retries
    });
    await page.goto('/');
    const failed = page.getByRole('alert').filter({ hasText: 'Could not load' });
    await expect(failed).toHaveCount(1, { timeout: 15_000 });

    await failed.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'Could not load' })).toHaveCount(0);
    await expect(page.getByRole('table')).toHaveCount(2);
    expect(api.requests.filter((u) => u.includes('/fingerprint/')).length).toBeGreaterThanOrEqual(5);
  });
});

test.describe('fingerprint display', () => {
  test('topic shares of each fingerprint are normalised: they add up to 100%', async ({ page }) => {
    await mockApi(page, { loggedIn: true, exposure: EXPOSURE, engagement: ENGAGEMENT });
    await page.goto('/');

    const exposure = page.getByRole('table', { name: 'Exposure fingerprint topic shares' });
    await expect(exposure.getByRole('row')).toHaveCount(1 + 5 + 1); // header, 5 topics, total
    expect(await shareValues(exposure)).toEqual([40, 25, 20, 10, 5]);
    expect(sum(await shareValues(exposure))).toBe(100);
    await expect(exposure.getByTestId('share-total')).toHaveText('100.0%');

    const engagement = page.getByRole('table', { name: 'Engagement fingerprint topic shares' });
    expect(await shareValues(engagement)).toEqual([55, 25, 20]);
    expect(sum(await shareValues(engagement))).toBe(100);
    await expect(engagement.getByTestId('share-total')).toHaveText('100.0%');
  });

  test('the two fingerprints differ: engagement puts tech first, exposure puts cricket first', async ({
    page,
  }) => {
    await mockApi(page, { loggedIn: true, exposure: EXPOSURE, engagement: ENGAGEMENT });
    await page.goto('/');
    const exposure = page.getByRole('table', { name: 'Exposure fingerprint topic shares' });
    const engagement = page.getByRole('table', { name: 'Engagement fingerprint topic shares' });
    await expect(exposure.locator('tbody tr').first()).toContainText('Cricket');
    await expect(engagement.locator('tbody tr').first()).toContainText('Tech & coding');
  });

  test('thirds are rounded so the displayed total stays exactly 100.0%', async ({ page }) => {
    await mockApi(page, {
      loggedIn: true,
      exposure: topics({ A: 1 / 3, B: 1 / 3, C: 1 / 3 }),
      engagement: [],
    });
    await page.goto('/');
    const table = page.getByRole('table', { name: 'Exposure fingerprint topic shares' });
    await expect(table.getByTestId('share-total')).toHaveText('100.0%'); // wait for the data first
    expect(sum(await shareValues(table))).toBe(100);
  });

  test('a long tail is folded into one row and the total is still 100%', async ({ page }) => {
    const many = topics(Object.fromEntries(Array.from({ length: 14 }, (_, i) => [`Topic ${i + 1}`, 1 / 14])));
    await mockApi(page, { loggedIn: true, exposure: many, engagement: [] });
    await page.goto('/');
    const table = page.getByRole('table', { name: 'Exposure fingerprint topic shares' });
    await expect(table.getByRole('row')).toHaveCount(1 + 8 + 1 + 1); // header, 8, rest, total
    await expect(table.getByRole('row', { name: /6 other topics/ })).toBeVisible();
    expect(sum(await shareValues(table))).toBe(100);
  });

  test('draws one bar per row, and never labels a topic with an independent score', async ({ page }) => {
    await mockApi(page, { loggedIn: true, exposure: EXPOSURE, engagement: ENGAGEMENT });
    await page.goto('/');
    const card = page.getByRole('region', { name: 'Exposure fingerprint' });
    await expect(card.locator('.recharts-bar-rectangle')).toHaveCount(5);
    // Every percentage on the page belongs to a table whose rows add up to 100.
    const all = await page.locator('table.shares tbody td').allInnerTexts();
    expect(all).toHaveLength(8);
  });
});

test.describe('session list', () => {
  test('lists sessions with length, reel count and why they ended', async ({ page }) => {
    await mockApi(page, { loggedIn: true, sessions: SESSIONS });
    await page.goto('/');
    await page.getByRole('link', { name: 'Sessions' }).click();
    await expect(page).toHaveURL(/\/sessions$/);

    const items = page.getByRole('list', { name: 'Monitoring sessions' }).getByRole('listitem');
    await expect(items).toHaveCount(3);
    await expect(items.nth(0)).toContainText('2 Oct 2026');
    await expect(items.nth(0)).toContainText('45 min');
    await expect(items.nth(0)).toContainText('118');
    await expect(items.nth(0)).toContainText('60 minute limit');
    await expect(items.nth(1)).toContainText('12 min');
    await expect(items.nth(1)).toContainText('Stopped by you');
    await expect(items.nth(2)).toContainText('in progress');
    await expect(items.nth(2)).toContainText('not processed yet');
    await expect(items.nth(2)).toContainText('Running');
  });

  test('the current page is marked in the navigation', async ({ page }) => {
    await mockApi(page, { loggedIn: true, sessions: SESSIONS });
    await page.goto('/sessions');
    await expect(page.getByRole('link', { name: 'Sessions' })).toHaveAttribute('aria-current', 'page');
    await expect(page.getByRole('link', { name: 'Dashboard' })).not.toHaveAttribute('aria-current', 'page');
  });

  test('shows loading, empty and error states', async ({ page }) => {
    await mockApi(page, { loggedIn: true, sessions: [], delayMs: 500 });
    await page.goto('/sessions');
    await expect(page.getByRole('status')).toBeVisible();
    await expect(page.getByText(/No sessions yet/)).toBeVisible();

    const failing = await page.context().newPage();
    await mockApi(failing, { loggedIn: true, failSessions: true });
    await failing.goto('/sessions');
    await expect(failing.getByText('Could not load your sessions.')).toBeVisible();
    await expect(failing.getByRole('button', { name: 'Try again' })).toBeVisible();
  });

  test('a logged-out visitor to /sessions is sent to /login', async ({ page }) => {
    await mockApi(page, { sessions: SESSIONS });
    await page.goto('/sessions');
    await expect(page).toHaveURL(/\/login$/);
  });
});

test.describe('responsive layout', () => {
  for (const path of ['/', '/sessions', '/login']) {
    test(`${path} has no horizontal scroll`, async ({ page }) => {
      await mockApi(page, {
        loggedIn: path !== '/login',
        exposure: EXPOSURE,
        engagement: ENGAGEMENT,
        sessions: SESSIONS,
      });
      await page.goto(path);
      await page.waitForLoadState('networkidle');
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow).toBeLessThanOrEqual(0);
    });
  }

  test('the cards sit side by side on a wide screen and stack on a narrow one', async ({ page }) => {
    await mockApi(page, { loggedIn: true, exposure: EXPOSURE, engagement: ENGAGEMENT });
    await page.goto('/');
    const exposure = page.getByRole('region', { name: 'Exposure fingerprint' });
    const engagement = page.getByRole('region', { name: 'Engagement fingerprint' });
    await expect(engagement).toBeVisible();
    const a = await exposure.boundingBox();
    const b = await engagement.boundingBox();
    if (!a || !b) throw new Error('cards are not laid out');
    const viewport = page.viewportSize();
    if (viewport && viewport.width >= 860) expect(Math.abs(a.y - b.y)).toBeLessThan(2);
    else expect(b.y).toBeGreaterThan(a.y + a.height - 2);
  });
});
