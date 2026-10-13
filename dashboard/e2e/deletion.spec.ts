import { expect, test } from '@playwright/test';

import { mockApi, topics, type MockSession } from './mock-api';

const SESSIONS: MockSession[] = [
  { id: 's2', started_at: '2026-10-02T09:00:00Z', ended_at: '2026-10-02T09:45:00Z', status: 'stopped', stop_reason: 'user', n_reels: 40 },
  { id: 's1', started_at: '2026-10-01T18:30:00Z', ended_at: '2026-10-01T18:42:00Z', status: 'stopped', stop_reason: 'user', n_reels: 12 },
];

const base = {
  loggedIn: true,
  sessions: SESSIONS,
  exposure: topics({ Cricket: 0.6, Food: 0.4 }),
  engagement: topics({ Cricket: 0.5, Food: 0.5 }),
};

test.describe('delete one session', () => {
  test('asks first, says what goes, and can be cancelled without deleting anything', async ({ page }) => {
    const api = await mockApi(page, base);
    await page.goto('/sessions');
    const first = page.getByRole('listitem').first();
    await first.getByRole('button', { name: /Delete the session of/ }).click();

    const confirm = first.getByRole('group', { name: 'Confirm deleting this session' });
    await expect(confirm).toContainText('its 40 Reels');
    await expect(confirm).toContainText('stored frames');
    await expect(confirm).toContainText('cannot be undone');

    await confirm.getByRole('button', { name: 'Cancel' }).click();
    await expect(confirm).toHaveCount(0);
    await expect(page.getByRole('listitem')).toHaveCount(2);
    expect(api.deletions).toEqual([]); // nothing was sent
  });

  test('deletes, removes it from the list, and reports what was verified', async ({ page }) => {
    const api = await mockApi(page, base);
    await page.goto('/sessions');
    const items = page.getByRole('list', { name: 'Monitoring sessions' }).getByRole('listitem');
    await expect(items).toHaveCount(2);

    await items.first().getByRole('button', { name: /Delete the session of/ }).click();
    await items.first().getByRole('button', { name: 'Yes, delete this session' }).click();

    await expect(items).toHaveCount(1);
    await expect(items.first()).toContainText('1 Oct 2026');
    const notice = page.getByRole('status').filter({ hasText: 'Verified' });
    await expect(notice).toContainText('Deleted: 1 sessions, 12 watched Reels, 12 processing jobs, 24 frame files');
    await expect(notice).toContainText('Verified: nothing left in any table or on disk');

    expect(api.deletions).toHaveLength(1);
    expect(api.deletions[0]).toMatchObject({ method: 'DELETE', csrf: 'ref-dashboard' });
    expect(api.deletions[0]?.url).toContain('/api/v1/sessions/s2');
  });

  test('an unfinished deletion is shown as unfinished, with what is left, and a retry finishes it', async ({
    page,
  }) => {
    const api = await mockApi(page, { ...base, deletion: { incompleteFirst: 1, remaining: { frame_files: 3 } } });
    await page.goto('/sessions');
    const items = page.getByRole('list', { name: 'Monitoring sessions' }).getByRole('listitem');
    await items.first().getByRole('button', { name: /Delete the session of/ }).click();
    await items.first().getByRole('button', { name: 'Yes, delete this session' }).click();

    const alert = page.getByRole('alert');
    await expect(alert).toContainText('The deletion is not finished: 3 stored frame files is still there');
    await expect(items).toHaveCount(2); // the list still has it: it is not gone
    await expect(page.getByRole('status').filter({ hasText: 'Verified' })).toHaveCount(0);

    await alert.getByRole('button', { name: 'Try again' }).click();
    await expect(items).toHaveCount(1);
    await expect(page.getByRole('status').filter({ hasText: 'Verified' })).toBeVisible();
    expect(api.deletions).toHaveLength(2);
  });

  test('reads the fingerprints again afterwards, since they were rebuilt', async ({ page }) => {
    const api = await mockApi(page, base);
    await page.goto('/sessions');
    const before = api.requests.length;
    const items = page.getByRole('list', { name: 'Monitoring sessions' }).getByRole('listitem');
    await items.first().getByRole('button', { name: /Delete the session of/ }).click();
    await items.first().getByRole('button', { name: 'Yes, delete this session' }).click();
    await expect(items).toHaveCount(1);
    expect(api.requests.length).toBeGreaterThan(before); // the sessions list was fetched again
  });

  test('the last session can be deleted and the empty state appears', async ({ page }) => {
    await mockApi(page, { ...base, sessions: [SESSIONS[0] as MockSession] });
    await page.goto('/sessions');
    const item = page.getByRole('listitem').first();
    await item.getByRole('button', { name: /Delete the session of/ }).click();
    await item.getByRole('button', { name: 'Yes, delete this session' }).click();
    await expect(page.getByText(/No sessions yet/)).toBeVisible();
    await expect(page.getByRole('status').filter({ hasText: 'Verified' })).toBeVisible();
  });
});

test.describe('delete all my data', () => {
  test('lists what goes and needs the word DELETE typed before it can be used', async ({ page }) => {
    const api = await mockApi(page, base);
    await page.goto('/account');
    await expect(page.getByRole('heading', { name: 'Delete all my data' })).toBeVisible();
    for (const text of ['every monitoring session', 'your fingerprints', 'stored frames', 'your account itself', 'cannot be undone']) {
      await expect(page.getByRole('main')).toContainText(text);
    }
    const button = page.getByRole('button', { name: 'Delete all my data' });
    await expect(button).toBeDisabled();
    await page.getByLabel(/Type DELETE/).fill('delete');
    await expect(button).toBeDisabled(); // case matters
    await page.getByLabel(/Type DELETE/).fill('DELETE');
    await expect(button).toBeEnabled();
    expect(api.deletions).toEqual([]);
  });

  test('deletes everything, shows the verified result, and the login is gone', async ({ page }) => {
    const api = await mockApi(page, base);
    await page.goto('/account');
    await page.getByLabel(/Type DELETE/).fill('DELETE');
    await page.getByRole('button', { name: 'Delete all my data' }).click();

    await expect(page.getByRole('heading', { name: 'Your data is deleted' })).toBeVisible();
    await expect(page.getByRole('status').filter({ hasText: 'Verified' })).toContainText(
      'Verified: nothing left in any table or on disk',
    );
    expect(api.deletions).toHaveLength(1);
    expect(api.deletions[0]).toMatchObject({ method: 'DELETE', csrf: 'ref-dashboard' });
    expect(api.deletions[0]?.url).toContain('/api/v1/me/data');

    await page.goto('/');
    await expect(page).toHaveURL(/\/login$/); // the account no longer exists
  });

  test('an unfinished deletion keeps the page, says what is left, and can be retried', async ({ page }) => {
    const api = await mockApi(page, { ...base, deletion: { incompleteFirst: 1, remaining: { frame_files: 5, content_features: 2 } } });
    await page.goto('/account');
    await page.getByLabel(/Type DELETE/).fill('DELETE');
    await page.getByRole('button', { name: 'Delete all my data' }).click();

    const alert = page.getByRole('alert');
    await expect(alert).toContainText('not finished');
    await expect(alert).toContainText('5 stored frame files');
    await expect(alert).toContainText('2 cached Reel features');
    await expect(page.getByRole('heading', { name: 'Your data is deleted' })).toHaveCount(0);

    await alert.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByRole('heading', { name: 'Your data is deleted' })).toBeVisible();
    expect(api.deletions).toHaveLength(2);
  });

  test('has no horizontal scroll', async ({ page }) => {
    await mockApi(page, base);
    await page.goto('/account');
    await expect(page.getByRole('heading', { name: 'Delete all my data' })).toBeVisible();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test('the Account link is in the navigation', async ({ page }) => {
    await mockApi(page, base);
    await page.goto('/');
    await page.getByRole('link', { name: 'Account' }).click();
    await expect(page).toHaveURL(/\/account$/);
    await expect(page.getByRole('link', { name: 'Account' })).toHaveAttribute('aria-current', 'page');
  });
});
