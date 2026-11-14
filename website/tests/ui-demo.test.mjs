import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';

const { pages, responses } = JSON.parse(
  readFileSync(new URL('../../ui/src/lib/demo/recordings.json', import.meta.url), 'utf8'),
);
const staged = Object.values(responses).find(({ body }) => body?.state === 'staged').body.deploymentId;

test('every UI demo page renders from recordings, and writes stay read-only', async ({ page }) => {
  // One pass over every recorded page and the views each loads on demand.
  test.setTimeout(120_000);
  const failures = [];
  page.on('pageerror', (error) => failures.push(`${page.url()}: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && message.text().startsWith('UI demo has no recording')) {
      failures.push(`${page.url()}: ${message.text()}`);
    }
  });
  for (const path of pages) {
    await page.goto(`/demo${path}`, { waitUntil: 'networkidle' });
    await expect(page.getByRole('note')).toContainText('UI demo with example data');
    // The views a page loads on demand, which the recorder opens too.
    const views = page.locator('button[data-statement-sequence]');
    for (let index = 0; index < (await views.count()); index += 1) await views.nth(index).click();
    const diff = page.getByRole('button', { name: 'Diff', exact: true });
    if ((await diff.count()) > 0) await diff.click();
    await page.waitForLoadState('networkidle');
  }
  // Links whose target pages need the query or role the recorder followed.
  for (const [from, link] of [['/', 'Users'], ['/catalog/order_report', 'Plan rebuild']]) {
    await page.goto(`/demo${from}`, { waitUntil: 'networkidle' });
    await page.getByRole('link', { name: link }).first().click();
    await page.waitForLoadState('networkidle');
  }
  expect(failures).toEqual([]);

  await page.goto('/demo/deployments');
  await page.locator(`a[href="/demo/deployments/${staged}"]`).first().click();
  await page.getByRole('button', { name: 'Promote', exact: true }).click();
  await expect(page.getByText('This is a read-only UI demo with example data.')).toBeVisible();
});
