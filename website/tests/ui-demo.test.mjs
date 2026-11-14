import { test, expect, devices } from '@playwright/test';
import { readFileSync } from 'node:fs';

const { pages, promotion } = JSON.parse(
  readFileSync(new URL('../../ui/src/lib/demo/recordings.json', import.meta.url), 'utf8'),
);
const staged = promotion.deploymentId;
const chooseRecording = () => sessionStorage.setItem('sqltide-demo-recording', '1');

test('every UI demo page renders from recordings, and writes stay read-only', async ({ page }) => {
  // One pass over every recorded page and the views each loads on demand.
  test.setTimeout(120_000);
  await page.addInitScript(chooseRecording);
  const failures = [];
  page.on('pageerror', (error) => failures.push(`${page.url()}: ${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error' && message.text().startsWith('UI demo has no recording')) {
      failures.push(`${page.url()}: ${message.text()}`);
    }
  });
  // Pages the promotion created, such as its run, open from a reload or a shared link too.
  for (const path of [...pages, ...promotion.pages]) {
    await page.goto(`/demo${path}`, { waitUntil: 'networkidle' });
    await expect(page.getByRole('note')).toContainText('UI demo with recorded example data');
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
  await expect(page.getByRole('note')).toContainText('You chose the recording.');
  await page.locator(`a[href="/demo/deployments/${staged}"]`).first().click();
  await page.getByRole('button', { name: 'Promote', exact: true }).click();
  await expect(page.getByText('This is a read-only UI demo with example data.')).toBeVisible();
});

test('the real engine runs the story in the page, and the promotion switches the query', async ({ page }) => {
  test.setTimeout(240_000);
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => message.type() === 'error' && errors.push(message.text()));
  await page.goto('/demo/');
  expect(await page.evaluate(() => crossOriginIsolated)).toBe(true);
  await expect(page.getByRole('progressbar', { name: 'Engine loading' })).toBeVisible();

  // On a screen this narrow the panel starts as a rail beside the page.
  const panel = page.getByRole('complementary', { name: 'Live engine' });
  await panel.getByRole('button', { name: 'Show live engine' }).click({ timeout: 180_000 });
  const reads = panel.getByTestId('engine-reads');
  await expect(reads).toContainText('Reads the current version (all orders)', { timeout: 180_000 });
  await expect(page.getByRole('note')).toContainText('Plan, diff and run history are recorded.');
  // The source holds three orders; new ones reach the query while the change is staged.
  const rows = async () => Number((await reads.textContent()).match(/(\d+) rows/)[1]);
  const before = await rows();
  expect(before).toBeGreaterThanOrEqual(3);
  await expect.poll(rows, { timeout: 20_000 }).toBeGreaterThan(before);

  // The deployment list reads its sizes from the engine, so the staged version has rows too.
  await panel.getByRole('link', { name: 'its page' }).click();
  await expect(page).toHaveURL(new RegExp(`/demo/deployments/${staged}$`));
  await page.getByRole('button', { name: 'Promote', exact: true }).click();
  await expect(page).toHaveURL(/\/demo\/runs\//);
  await expect(reads).toContainText('Reads the new version (completed orders only)', { timeout: 20_000 });
  const completed = await page.evaluate(() => document.querySelectorAll('[aria-label="Live engine"] tbody tr').length);
  expect(completed).toBeGreaterThan(0);
  await expect(panel.locator('tbody')).not.toContainText('pending');

  await page.goto('/demo/deployments');
  expect(errors).toEqual([]);
});

test('without cross-origin isolation the demo says so and plays the recording', async ({ page }) => {
  await page.route('**/demo/**', async (route) => {
    const response = await route.fetch();
    const headers = response.headers();
    delete headers['cross-origin-embedder-policy'];
    delete headers['cross-origin-opener-policy'];
    await route.fulfill({ response, headers });
  });
  await page.goto('/demo/');
  await expect(page.getByRole('note')).toContainText('isn’t cross-origin isolated');
  await expect(page.getByRole('complementary', { name: 'Live engine' })).toHaveCount(0);
});

test('when the engine fails to start the demo says so and offers the recording', async ({ page }) => {
  await page.route('**/demo/engine/**/chdb.wasm.gz.1', (route) => route.fulfill({ status: 404, body: 'gone' }));
  await page.goto('/demo/');
  const dialog = page.getByRole('alertdialog', { name: 'The real engine couldn’t start' });
  await expect(dialog).toContainText('HTTP 404');
  await dialog.getByRole('button', { name: 'Use the recording' }).click();
  await expect(page.getByRole('note')).toContainText('The real engine couldn’t start here, so this is the recording.');
  await page.locator('a[href="/demo/deployments"]').first().click();
  await page.locator(`a[href="/demo/deployments/${staged}"]`).first().click();
  await page.getByRole('button', { name: 'Promote', exact: true }).click();
  await expect(page.getByText('This is a read-only UI demo with example data.')).toBeVisible();
});

test.describe('on a phone', () => {
  const { defaultBrowserType: _, ...pixel } = devices['Pixel 7'];
  test.use(pixel);

  test('the demo plays the recording and says why', async ({ page }) => {
    await page.goto('/demo/');
    await expect(page.getByRole('note')).toContainText('phones get the recording');
  });
});
