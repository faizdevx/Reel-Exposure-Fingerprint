import { expect, test } from '@playwright/test';

import { mockApi, topics } from './mock-api';

const base = {
  loggedIn: true,
  exposure: topics({ Cricket: 0.6, Food: 0.4 }),
  engagement: topics({ Cricket: 0.5, Food: 0.5 }),
};

const heading = (page: import('@playwright/test').Page) =>
  page.getByRole('heading', { name: 'Before you see your fingerprints' });

async function rate(page: import('@playwright/test').Page, ratings: Record<string, string>) {
  for (const [topic, value] of Object.entries(ratings)) {
    await page.getByRole('group', { name: topic }).getByLabel(value, { exact: true }).check();
  }
}

test.describe('the self-report survey', () => {
  test('comes first and hides every fingerprint until answered', async ({ page }) => {
    const api = await mockApi(page, { ...base, survey: 'pending' });
    await page.goto('/');
    await expect(heading(page)).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Your fingerprints', exact: true })).toHaveCount(0);
    await expect(page.getByRole('table')).toHaveCount(0);
    expect(api.requests.some((u) => u.includes('/fingerprint') || u.includes('/predictions'))).toBe(false);
  });

  test('also guards the history page', async ({ page }) => {
    await mockApi(page, { ...base, survey: 'pending' });
    await page.goto('/history');
    await expect(heading(page)).toBeVisible();
  });

  test('saves only when every topic is rated, then shows the dashboard', async ({ page }) => {
    const api = await mockApi(page, { ...base, survey: 'pending' });
    await page.goto('/');
    const save = page.getByRole('button', { name: 'Save my ratings' });
    await expect(save).toBeDisabled();
    await rate(page, { Cricket: '5', Tech: '3' });
    await expect(save).toBeDisabled();
    await expect(page.getByText('2 of 3 topics rated.')).toBeVisible();
    await rate(page, { Food: '1' });
    await expect(save).toBeEnabled();
    await save.click();
    await expect(page.getByRole('heading', { name: 'Your fingerprints', exact: true })).toBeVisible();
    expect(api.surveyWrites).toHaveLength(1);
    expect(JSON.parse(api.surveyWrites[0]?.body ?? '{}')).toEqual({
      ratings: { '1': 5, '2': 3, '3': 1 },
    });
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Your fingerprints', exact: true })).toBeVisible(); // not asked again
    await expect(heading(page)).toHaveCount(0);
  });

  test('can be skipped', async ({ page }) => {
    const api = await mockApi(page, { ...base, survey: 'pending' });
    await page.goto('/');
    await page.getByRole('button', { name: 'Skip this' }).click();
    await expect(page.getByRole('heading', { name: 'Your fingerprints', exact: true })).toBeVisible();
    expect(api.surveyWrites.map((w) => w.method)).toEqual(['POST']);
  });

  test('says so, and keeps the answers, when saving fails', async ({ page }) => {
    await mockApi(page, { ...base, survey: 'pending', failSurveySave: true });
    await page.goto('/');
    await rate(page, { Cricket: '4', Tech: '4', Food: '4' });
    await page.getByRole('button', { name: 'Save my ratings' }).click();
    await expect(page.getByRole('alert')).toContainText('Could not save your answers');
    await expect(page.getByRole('group', { name: 'Cricket' }).getByLabel('4', { exact: true })).toBeChecked();
    await expect(page.getByRole('button', { name: 'Save my ratings' })).toBeEnabled();
  });

  test('can be completed with the keyboard and has no horizontal scroll', async ({ page }) => {
    await mockApi(page, { ...base, survey: 'pending' });
    await page.goto('/');
    await page.getByRole('group', { name: 'Cricket' }).getByLabel('1', { exact: true }).focus();
    await page.keyboard.press('ArrowRight'); // moves to 2 inside the radio group
    await expect(page.getByRole('group', { name: 'Cricket' }).getByLabel('2', { exact: true })).toBeChecked();
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
});
