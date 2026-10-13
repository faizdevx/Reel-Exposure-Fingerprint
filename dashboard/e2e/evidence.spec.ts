import { expect, test } from '@playwright/test';

import { makeEvidence, mockApi, topics, type MockEvidenceRequest } from './mock-api';

const EXPOSURE = topics({ Cricket: 0.4, 'Tech and coding': 0.25, Food: 0.2, Travel: 0.1, Music: 0.05 });
const ENGAGEMENT = topics({ 'Tech and coding': 0.5, Cricket: 0.2, Food: 0.2, Travel: 0.1 });

function options(total = 25) {
  return {
    loggedIn: true,
    exposure: EXPOSURE,
    engagement: ENGAGEMENT,
    evidence: (request: MockEvidenceRequest) => makeEvidence(request, { total }),
  };
}

async function openCricket(page: import('@playwright/test').Page, kind: 'exposure' | 'engagement' = 'exposure') {
  await page.goto('/');
  const card = page.getByRole('region', { name: `${kind === 'exposure' ? 'Exposure' : 'Engagement'} fingerprint` });
  await card.getByRole('button', { name: /Why Cricket\?/ }).click();
  return page.getByRole('dialog');
}

test.describe('opening the evidence', () => {
  test('selecting a topic opens a panel that says why it is there', async ({ page }) => {
    await mockApi(page, options());
    const dialog = await openCricket(page);
    await expect(dialog.getByRole('heading', { name: 'Why Cricket is in your exposure fingerprint' })).toBeVisible();
    const summary = dialog.getByRole('region', { name: 'Why this topic appears' });
    await expect(summary).toContainText('40.0% of your exposure fingerprint');
    await expect(summary).toContainText('Every Reel the feed showed you counts the same');
    await expect(summary).toContainText('contribution = topic score x gain x recency');
    await expect(summary).toContainText('The half-life is 7 days');
    await expect(dialog.getByTestId('coverage')).toContainText('25 Reels contribute');
    await expect(dialog.getByTestId('coverage')).toContainText('100.0% of this topic');
  });

  test('the engagement fingerprint explains itself by dwell', async ({ page }) => {
    await mockApi(page, options());
    const dialog = await openCricket(page, 'engagement');
    await expect(dialog.getByRole('heading', { name: /in your engagement fingerprint/ })).toBeVisible();
    await expect(dialog).toContainText('how long you stayed');
  });

  test('the "other topics" row cannot be opened, only real topics', async ({ page }) => {
    const many = topics(Object.fromEntries(Array.from({ length: 14 }, (_, i) => [`Topic ${i + 1}`, 1 / 14])));
    await mockApi(page, { ...options(), exposure: many });
    await page.goto('/');
    const card = page.getByRole('region', { name: 'Exposure fingerprint' });
    await expect(card.getByRole('row', { name: /other topics/ }).getByRole('button')).toHaveCount(0);
    await expect(card.getByRole('button', { name: /^Why Topic/ })).toHaveCount(8);
  });
});

test.describe('what each Reel shows', () => {
  test('link, caption, observed time, dwell, scores, gain, recency, contribution and share', async ({ page }) => {
    await mockApi(page, options());
    const dialog = await openCricket(page);
    const list = dialog.getByRole('list', { name: 'Reels behind Cricket, biggest contribution first' });
    const first = list.getByRole('listitem').first();
    const link = first.getByRole('link', { name: 'Open Reel Creel000' });
    await expect(link).toHaveAttribute('href', 'https://www.instagram.com/reel/Creel000/');
    await expect(link).toHaveAttribute('target', '_blank');
    await expect(link).toHaveAttribute('rel', /noopener/);
    await expect(first).toContainText('Caption of reel number 0');
    await expect(first).toContainText('Observed 1 Oct 2026');
    await expect(first).toContainText('8.0 s of 15.0 s');
    await expect(first).toContainText('Topic score');
    await expect(first).toContainText('60.0%');
    await expect(first).toContainText('text 70.0% visual 50.0%');
    await expect(first).toContainText('Gain');
    await expect(first).toContainText('1.00');
    await expect(first).toContainText('Recency');
    await expect(first).toContainText('0.90');
    await expect(first).toContainText('Contribution');
    await expect(first).toContainText('0.500'); // 1 / 2
    await expect(first).toContainText('Share of this topic');
  });

  test('says so when a caption or a dwell was not recorded, and marks a cut caption', async ({ page }) => {
    await mockApi(page, options());
    const dialog = await openCricket(page);
    const items = dialog.getByRole('listitem');
    await expect(items.nth(1)).toContainText('Caption of reel number 1…'); // truncated
    await expect(items.nth(3)).toContainText('No caption was captured');
    await expect(items.nth(4)).toContainText('Dwell');
    await expect(items.nth(4)).toContainText('not recorded');
  });

  test('is ordered by contribution, biggest first', async ({ page }) => {
    await mockApi(page, options());
    const dialog = await openCricket(page);
    const values = (await dialog.locator('.evidence-item').evaluateAll((els) =>
      els.map((el) => Number.parseFloat(el.querySelector('dl div:nth-child(5) dd')?.textContent ?? 'NaN')),
    ));
    expect(values).toHaveLength(10);
    expect(values).toEqual([...values].sort((a, b) => b - a));
  });
});

test.describe('pagination', () => {
  test('pages through every Reel once with Next and Previous', async ({ page }) => {
    const api = await mockApi(page, options(25));
    const dialog = await openCricket(page);
    await expect(dialog.getByTestId('range')).toHaveText('Showing 1–10 of 25');
    await expect(dialog.getByRole('button', { name: 'Previous' })).toBeDisabled();

    const seen: string[] = [];
    const collect = async () => {
      seen.push(...(await dialog.getByRole('link', { name: /Open Reel/ }).allInnerTexts()));
    };
    await collect();
    await dialog.getByRole('button', { name: 'Next' }).click();
    await expect(dialog.getByTestId('range')).toHaveText('Showing 11–20 of 25');
    await collect();
    await dialog.getByRole('button', { name: 'Next' }).click();
    await expect(dialog.getByTestId('range')).toHaveText('Showing 21–25 of 25');
    await collect();
    await expect(dialog.getByRole('button', { name: 'Next' })).toBeDisabled();

    expect(seen).toHaveLength(25);
    expect(new Set(seen).size).toBe(25);
    await dialog.getByRole('button', { name: 'Previous' }).click();
    await expect(dialog.getByTestId('range')).toHaveText('Showing 11–20 of 25');

    const asked = api.requests.filter((u) => u.includes('/evidence')).map((u) => new URL(u).searchParams.get('offset'));
    expect(asked).toEqual(expect.arrayContaining(['0', '10', '20']));
  });

  test('one page only has no Next', async ({ page }) => {
    await mockApi(page, options(4));
    const dialog = await openCricket(page);
    await expect(dialog.getByTestId('range')).toHaveText('Showing 1–4 of 4');
    await expect(dialog.getByRole('button', { name: 'Next' })).toBeDisabled();
    await expect(dialog.getByRole('button', { name: 'Previous' })).toBeDisabled();
  });

  test('a topic that is open for another fingerprint starts again at the first page', async ({ page }) => {
    await mockApi(page, options(25));
    let dialog = await openCricket(page);
    await dialog.getByRole('button', { name: 'Next' }).click();
    await dialog.getByRole('button', { name: 'Close' }).click();
    dialog = await openCricket(page, 'engagement');
    await expect(dialog.getByTestId('range')).toHaveText('Showing 1–10 of 25');
  });
});

test.describe('states', () => {
  test('shows loading, then the evidence', async ({ page }) => {
    await mockApi(page, { ...options(), delayMs: 500 });
    await page.goto('/');
    await page.getByRole('region', { name: 'Exposure fingerprint' }).getByRole('button', { name: /Why Cricket\?/ }).click();
    await expect(page.getByRole('dialog').getByRole('status')).toBeVisible();
    await expect(page.getByRole('dialog').getByTestId('range')).toBeVisible();
  });

  test('shows an error with retry', async ({ page }) => {
    let fail = true;
    await mockApi(page, {
      ...options(),
      evidence: (request) => (fail ? null : makeEvidence(request, { total: 3 })),
    });
    await page.goto('/');
    await page.getByRole('region', { name: 'Exposure fingerprint' }).getByRole('button', { name: /Why Cricket\?/ }).click();
    const alert = page.getByRole('dialog').getByRole('alert');
    await expect(alert).toContainText('Could not load the evidence for Cricket', { timeout: 15_000 });
    fail = false;
    await alert.getByRole('button', { name: 'Try again' }).click();
    await expect(page.getByRole('dialog').getByTestId('range')).toHaveText('Showing 1–3 of 3');
  });

  test('explains an empty answer instead of showing nothing', async ({ page }) => {
    await mockApi(page, {
      ...options(),
      evidence: (request) => ({
        ...makeEvidence(request, { total: 0 }),
        evidence_available: false,
        reason: 'snapshot_without_provenance',
        explanation: null,
        items: [],
      }),
    });
    const dialog = await openCricket(page);
    await expect(dialog).toContainText('saved before the evidence was recorded');
    await expect(dialog.getByRole('listitem')).toHaveCount(0);
  });
});

test.describe('the panel itself', () => {
  test('Escape, the Close button and a click outside close it, and focus goes back', async ({ page }) => {
    await mockApi(page, options());
    await page.goto('/');
    const opener = page.getByRole('region', { name: 'Exposure fingerprint' }).getByRole('button', { name: /Why Cricket\?/ });

    await opener.click();
    await expect(page.getByRole('dialog')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Close' })).toBeFocused();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(opener).toBeFocused();

    await opener.click();
    await page.getByRole('button', { name: 'Close' }).click();
    await expect(page.getByRole('dialog')).toHaveCount(0);

    if ((page.viewportSize()?.width ?? 0) > 700) {
      await opener.click();
      await page.mouse.click(5, 300); // the dimmed area, left of the panel
      await expect(page.getByRole('dialog')).toHaveCount(0);
    }
  });

  test('has no horizontal scroll and fits the screen', async ({ page }) => {
    await mockApi(page, options());
    const dialog = await openCricket(page);
    await expect(dialog.getByTestId('range')).toBeVisible();
    const sizes = await page.evaluate(() => {
      const el = document.querySelector('[role="dialog"]') as HTMLElement;
      return { scroll: el.scrollWidth - el.clientWidth, panel: el.getBoundingClientRect().width, view: window.innerWidth };
    });
    expect(sizes.scroll).toBeLessThanOrEqual(0);
    expect(sizes.panel).toBeLessThanOrEqual(sizes.view);
  });

  test('a logged-out visitor is sent to /login when the evidence answers 401', async ({ page }) => {
    const api = await mockApi(page, options());
    await page.goto('/');
    await expect(page.getByRole('table', { name: 'Exposure fingerprint topic shares' })).toBeVisible();
    api.loggedIn = false; // the session ended while the page was open
    await page.getByRole('region', { name: 'Exposure fingerprint' }).getByRole('button', { name: /Why Cricket\?/ }).click();
    await expect(page).toHaveURL(/\/login$/);
  });
});
