import { expect, test, type Page } from '@playwright/test';

import { makeHistory, mockApi, topics, withSessionSnapshots, type MockOptions, type MockSession } from './mock-api';

const DAYS = 6;
const EXPOSURE_NOW = topics({ Cricket: 0.4, 'Tech and coding': 0.25, Food: 0.2, Travel: 0.1, Music: 0.05 });
const ENGAGEMENT_NOW = topics({ 'Tech and coding': 0.5, Cricket: 0.2, Food: 0.2, Travel: 0.1 });

const SESSIONS: MockSession[] = [
  { id: 's2', started_at: '2026-10-02T09:00:00Z', ended_at: '2026-10-02T09:45:00Z', status: 'stopped', stop_reason: 'user', n_reels: 40 },
  { id: 's1', started_at: '2026-10-01T18:30:00Z', ended_at: '2026-10-01T18:42:00Z', status: 'stopped', stop_reason: 'user', n_reels: 12 },
  { id: 's3', started_at: '2026-10-03T07:00:00Z', ended_at: null, status: 'active', stop_reason: null, n_reels: null },
];

function fullOptions(days = DAYS) {
  return {
    loggedIn: true,
    exposure: EXPOSURE_NOW,
    engagement: ENGAGEMENT_NOW,
    sessions: SESSIONS,
    history: {
      exposure: withSessionSnapshots(makeHistory('exposure', days), ['s1', 's2']),
      engagement: makeHistory('engagement', days),
    },
  };
}

const sum = (values: number[]) => Math.round(values.reduce((a, b) => a + b, 0) * 10) / 10;

async function open(page: Page, options: MockOptions = fullOptions()) {
  const api = await mockApi(page, options);
  await page.goto('/history');
  return api;
}

test.describe('navigation and layout', () => {
  test('the History link opens the page with its four sections', async ({ page }) => {
    await mockApi(page, fullOptions());
    await page.goto('/');
    await page.getByRole('link', { name: 'History' }).click();
    await expect(page).toHaveURL(/\/history$/);
    await expect(page.getByRole('link', { name: 'History' })).toHaveAttribute('aria-current', 'page');
    for (const name of ['Topic share over time', 'Exposure versus engagement', 'Fingerprint history', 'Session timeline']) {
      await expect(page.getByRole('heading', { name })).toBeVisible();
    }
  });

  test('shows loading placeholders first, then the data', async ({ page }) => {
    await mockApi(page, { ...fullOptions(), delayMs: 600 });
    await page.goto('/history');
    await expect(page.getByRole('status').first()).toBeVisible();
    await expect(page.getByRole('table', { name: 'exposure daily snapshots' })).toBeVisible();
    await expect(page.getByRole('status')).toHaveCount(0);
  });

  test('a logged-out visitor is sent to /login', async ({ page }) => {
    await mockApi(page, { ...fullOptions(), loggedIn: false });
    await page.goto('/history');
    await expect(page).toHaveURL(/\/login$/);
  });

  test('has no horizontal scroll', async ({ page }) => {
    await open(page);
    await expect(page.getByRole('table', { name: 'exposure daily snapshots' })).toBeVisible();
    await page.getByText('Show the data as a table').click();
    await page.waitForLoadState('networkidle');
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
});

test.describe('topic share over time', () => {
  test('draws a stacked series per top topic plus the rest, in a fixed colour order', async ({ page }) => {
    await open(page);
    const legend = page.getByRole('list', { name: 'Topics' });
    await expect(legend.getByRole('listitem')).toHaveCount(7); // six topics and "All other topics"
    await expect(legend.getByRole('listitem').last()).toHaveText('All other topics');
    const chart = page.getByRole('img', { name: 'exposure topic share over time' });
    await expect(chart.locator('.recharts-area')).toHaveCount(7);
  });

  test('every day adds up to 100%, and the table has one row per day', async ({ page }) => {
    await open(page);
    await page.getByText('Show the data as a table').click();
    const table = page.getByRole('table', { name: 'exposure topic share by day' });
    await expect(table.locator('tbody tr')).toHaveCount(DAYS);
    for (const total of await table.getByTestId('row-total').allInnerTexts()) expect(total).toBe('100.0%');
    const rows = table.locator('tbody tr');
    for (let i = 0; i < DAYS; i += 1) {
      const texts = await rows.nth(i).locator('td').allInnerTexts();
      const values = texts.slice(0, -1).map((t) => Number.parseFloat(t.replace('%', '')));  // all but the Total
      expect(sum(values)).toBe(100);
    }
  });

  test('days are in time order, oldest on the left of the table', async ({ page }) => {
    await open(page);
    await page.getByText('Show the data as a table').click();
    const days = await page
      .getByRole('table', { name: 'exposure topic share by day' })
      .locator('tbody th')
      .allInnerTexts();
    expect(days).toEqual([...days].sort());
    expect(days[0]).toBe('2026-10-01');
  });

  test('the fingerprint filter switches the data and keeps each topic on its own series', async ({ page }) => {
    await open(page);
    const legend = page.getByRole('list', { name: 'Topics' }).getByRole('listitem');
    await expect(page.getByRole('table', { name: 'exposure daily snapshots' })).toBeVisible();
    await expect(legend.first()).toBeVisible(); // wait for all the data before reading it
    const before = await legend.allInnerTexts();
    const swatches = async () =>
      page.getByRole('list', { name: 'Topics' }).locator('.swatch').evaluateAll((els) =>
        els.map((el) => (el as HTMLElement).style.background),
      );
    const colours = await swatches();

    await page.getByRole('radio', { name: 'Engagement' }).check();
    await expect(page.getByRole('img', { name: 'engagement topic share over time' })).toBeVisible();
    await expect(page.getByRole('table', { name: 'engagement daily snapshots' })).toBeVisible();

    expect(await legend.allInnerTexts()).toEqual(before); // same topics, same order
    expect(await swatches()).toEqual(colours); // same colours
  });

  test('one day is not drawn as a trend, but its numbers are there', async ({ page }) => {
    await open(page, fullOptions(1));
    await expect(page.getByText(/One day so far/)).toBeVisible();
    await expect(page.getByRole('img', { name: 'exposure topic share over time' })).toHaveCount(0);
    await page.getByText('Show the data as a table').click();
    await expect(page.getByRole('table', { name: 'exposure topic share by day' }).locator('tbody tr')).toHaveCount(1);
  });
});

test.describe('exposure versus engagement', () => {
  test('both fingerprints add up to 100% and the gap adds up to zero', async ({ page }) => {
    await open(page);
    const table = page.getByRole('table', { name: 'Exposure and engagement share by topic' });
    await expect(table.getByTestId('total-exposure')).toHaveText('100.0%');
    await expect(table.getByTestId('total-engagement')).toHaveText('100.0%');
    const gaps = (await table.locator('tbody tr td:last-child').allInnerTexts()).map((t) =>
      Number.parseFloat(t.replace('−', '-').replace('+', '')),
    );
    expect(sum(gaps)).toBe(0);
  });

  test('says where your time and the feed disagree, and draws a bar per share', async ({ page }) => {
    await open(page);
    const summary = page.getByTestId('gap-summary');
    await expect(summary).toContainText('Tech and coding'); // 25% of the feed, 50% of the time
    await expect(summary).toContainText('Cricket'); // 40% of the feed, 20% of the time
    const chart = page.getByRole('img', { name: 'Exposure and engagement share by topic' });
    await expect(chart.locator('.recharts-bar-rectangle')).toHaveCount(9); // 5 topics x 2 bars; Music has no engagement, and a zero bar is not drawn
  });

  test('shows an empty state without a fingerprint', async ({ page }) => {
    await open(page, { ...fullOptions(), exposure: [], engagement: [] });
    await expect(page.getByText(/Nothing to compare yet/)).toBeVisible();
  });
});

test.describe('fingerprint history and session timeline', () => {
  test('the daily snapshots list newest first with their leading topic', async ({ page }) => {
    await open(page);
    const table = page.getByRole('table', { name: 'exposure daily snapshots' });
    await expect(table.locator('tbody tr')).toHaveCount(6); // wait for the data before reading it
    const days = await table.locator('tbody th').allInnerTexts();
    expect(days).toEqual(['2026-10-06', '2026-10-05', '2026-10-04', '2026-10-03', '2026-10-02', '2026-10-01']);
    await expect(table.locator('tbody tr').first()).toContainText('72'); // 6 days of 12 Reels so far
    await expect(table.locator('tbody tr').first()).toContainText('Cricket'); // exposure leans to it
  });

  test('the timeline lists sessions oldest first and what led after each', async ({ page }) => {
    await open(page);
    const list = page.getByRole('list', { name: 'Session timeline, oldest first' });
    const items = list.getByRole('listitem');
    await expect(items).toHaveCount(3);
    await expect(items.nth(0)).toContainText('1 Oct 2026');
    await expect(items.nth(0)).toContainText('12 min, 12 Reels');
    await expect(items.nth(0)).toContainText('Then led by');
    await expect(items.nth(1)).toContainText('45 min, 40 Reels');
    await expect(items.nth(2)).toContainText('in progress, not processed yet');
    await expect(items.nth(2)).not.toContainText('Then led by'); // nothing invented
    await expect(page.getByRole('img', { name: 'Reels counted per session over time' }).locator('.recharts-bar-rectangle')).toHaveCount(2); // the unprocessed session has nothing to draw
  });

  test('empty states say what is missing', async ({ page }) => {
    await open(page, { loggedIn: true, sessions: [], exposure: [], engagement: [] });
    await expect(page.getByText(/No history yet/)).toBeVisible();
    await expect(page.getByText(/No exposure snapshots yet/)).toBeVisible();
    await expect(page.getByText(/No sessions yet/)).toBeVisible();
  });

  test('a failing history shows an error with retry, and the other sections still work', async ({ page }) => {
    await open(page, { ...fullOptions(), failHistory: true });
    await expect(page.getByRole('alert').filter({ hasText: 'Could not load the topic share history' })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('alert').filter({ hasText: 'Could not load the daily snapshots' })).toBeVisible();
    await expect(page.getByRole('table', { name: 'Exposure and engagement share by topic' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Try again' }).first()).toBeVisible();
  });
});

test.describe('reads', () => {
  test('the page reads snapshots only, in a handful of requests however long the history is', async ({ page }) => {
    const api = await open(page, fullOptions(60));
    await expect(page.getByRole('table', { name: 'exposure daily snapshots' }).locator('tbody tr')).toHaveCount(60);
    await page.getByRole('radio', { name: 'Engagement' }).check();
    await expect(page.getByRole('table', { name: 'engagement daily snapshots' })).toBeVisible();
    const paths = api.requests.map((u) => new URL(u).pathname);
    expect(new Set(paths)).toEqual(
      new Set(['/api/v1/fingerprint/history', '/api/v1/fingerprint/current', '/api/v1/sessions', '/api/v1/self-report']),
    );
    expect(api.requests.length).toBeLessThanOrEqual(10); // not one request per day or per session
  });
});
