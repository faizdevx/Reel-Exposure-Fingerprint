import { expect, test } from '@playwright/test';

import { makePrediction, mockApi, topics } from './mock-api';

const base = {
  loggedIn: true,
  exposure: topics({ Cricket: 0.6, Food: 0.4 }),
  engagement: topics({ Cricket: 0.5, Food: 0.5 }),
};

const panel = (page: import('@playwright/test').Page) =>
  page.getByRole('region', { name: 'Predicted next observed topic' });

const sum = (values: number[]) => Math.round(values.reduce((a, b) => a + b, 0) * 10) / 10;

test.describe('the prediction panel', () => {
  test('is named "Predicted next observed topic" and says it is not about what you like', async ({ page }) => {
    await mockApi(page, { ...base, prediction: makePrediction() });
    await page.goto('/');
    await expect(panel(page).getByRole('heading', { name: 'Predicted next observed topic', level: 2 })).toBeVisible();
    await expect(panel(page)).toContainText('It is not a prediction of what you like');
  });

  test('shows a distribution that adds up to 100%, the top topics first', async ({ page }) => {
    await mockApi(page, { ...base, prediction: makePrediction() });
    await page.goto('/');
    const table = panel(page).getByRole('table', { name: 'Predicted next observed topic, combined model' });
    const rows = table.locator('tbody tr');
    await expect(rows).toHaveCount(6); // five topics and the rest
    await expect(rows.first()).toContainText('Cricket');
    await expect(rows.first()).toContainText('41.0%');
    const values = (await table.locator('tbody td').allInnerTexts()).map((t) => Number.parseFloat(t));
    const topFive = values.slice(0, 5);
    expect(topFive).toEqual([...topFive].sort((a, b) => b - a)); // most likely first, then the rest
    expect(sum(values)).toBe(100);
    await expect(table.getByTestId('prediction-total')).toHaveText('100.0%');
    await expect(panel(page)).toContainText('From 300 Reels. The last observed topic was Tech and coding');
  });

  test('says when the fingerprint stood in for too few transitions, and when it did not', async ({ page }) => {
    await mockApi(page, { ...base, prediction: makePrediction({ backoff: true }) });
    await page.goto('/');
    await expect(panel(page).getByTestId('prediction-basis')).toContainText(
      'After Tech and coding you have 7 recorded follow-ups, fewer than the 20 needed, so your fingerprint is used instead.',
    );
    const other = await page.context().newPage();
    await mockApi(other, { ...base, prediction: makePrediction({ backoff: false }) });
    await other.goto('/');
    await expect(other.getByRole('region', { name: 'Predicted next observed topic' }).getByTestId('prediction-basis')).toContainText(
      'Mixes your fingerprint',
    );
  });
});

test.describe('no success is claimed before the evaluation is complete', () => {
  test('the banner says how far the evaluation is and that the figures are preliminary', async ({ page }) => {
    await mockApi(page, { ...base, prediction: makePrediction({ reels: 300 }) });
    await page.goto('/');
    const banner = panel(page).getByTestId('evaluation-banner');
    await expect(banner).toContainText('Evaluation not complete: 300 of 1,500 Reels scored');
    await expect(banner).toContainText('preliminary');
    const text = ((await panel(page).innerText()) ?? '').toLowerCase();
    for (const word of ['success', 'accurate', 'outperform', 'beats', 'you prefer', 'your preference']) {
      expect(text).not.toContain(word);
    }
    await expect(panel(page).locator('.comparisons')).toHaveCount(0); // no comparison yet
  });

  test('shows every model scored, rolling and all-time, baselines included', async ({ page }) => {
    await mockApi(page, { ...base, prediction: makePrediction() });
    await page.goto('/');
    const table = panel(page).getByRole('table', { name: 'How earlier predictions scored' });
    await expect(table.locator('tbody tr')).toHaveCount(5);
    for (const name of [
      'Every topic equally likely',
      'Your most frequent topics',
      'Your fingerprint (recent Reels weigh more)',
      'What usually follows the last topic',
      'Fingerprint and what follows, combined',
    ]) {
      await expect(table.getByRole('row', { name })).toBeVisible();
    }
    await expect(table.getByRole('columnheader', { name: /Top-1 hits, last 100/ })).toBeVisible();
    await expect(table.getByRole('columnheader', { name: 'Reels scored' })).toBeVisible();
  });

  test('once complete, states the comparison with intervals, and plainly when the baselines were not beaten', async ({ page }) => {
    await mockApi(page, { ...base, prediction: makePrediction({ reels: 1600, complete: true, beats: false }) });
    await page.goto('/');
    const banner = panel(page).getByTestId('evaluation-banner');
    await expect(banner).toContainText('Evaluation complete: 1600 Reels scored');
    await expect(banner).toContainText('was not clearly lower than both required baselines');
    await expect(panel(page).locator('.comparisons li')).toHaveCount(4);
    await expect(panel(page).locator('.comparisons li').first()).toContainText('95% interval');
  });

  test('and says so when the hybrid was clearly lower than both required baselines', async ({ page }) => {
    await mockApi(page, { ...base, prediction: makePrediction({ reels: 2000, complete: true, beats: true }) });
    await page.goto('/');
    await expect(panel(page).getByTestId('evaluation-banner')).toContainText(
      "log loss was clearly lower than both required baselines",
    );
  });
});

test.describe('states', () => {
  test('before any prediction there is an empty state', async ({ page }) => {
    await mockApi(page, base);
    await page.goto('/');
    await expect(panel(page)).toContainText('No predictions yet');
    await expect(panel(page).getByRole('table')).toHaveCount(0);
  });

  test('loading, then the panel', async ({ page }) => {
    await mockApi(page, { ...base, prediction: makePrediction(), delayMs: 500 });
    await page.goto('/');
    await expect(panel(page).getByRole('status')).toBeVisible();
    await expect(panel(page).getByTestId('prediction-total')).toBeVisible();
  });

  test('an error offers a retry that works', async ({ page }) => {
    await mockApi(page, { ...base, prediction: makePrediction(), failPredictionFirst: 3 });
    await page.goto('/');
    const alert = panel(page).getByRole('alert');
    await expect(alert).toContainText('Could not load the prediction', { timeout: 15_000 });
    await alert.getByRole('button', { name: 'Try again' }).click();
    await expect(panel(page).getByTestId('prediction-total')).toBeVisible();
  });

  test('has no horizontal scroll', async ({ page }) => {
    await mockApi(page, { ...base, prediction: makePrediction({ complete: true, reels: 1600 }) });
    await page.goto('/');
    await expect(panel(page).getByTestId('prediction-total')).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });
});
