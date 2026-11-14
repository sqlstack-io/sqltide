import { test, expect, devices } from '@playwright/test';
import { readFileSync } from 'node:fs';

const { pages, responses } = JSON.parse(
  readFileSync(new URL('../../ui/src/lib/demo/recordings.json', import.meta.url), 'utf8'),
);
const staged = responses['/api/deployments'].body.deployments.find((deployment) => deployment.state === 'staged').deploymentId;
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
  for (const path of pages) {
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

test('on the real engine Execute, Promote and Roll back run, and every page reads the engine', async ({ page }) => {
  test.setTimeout(420_000);
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
  await expect(reads).toContainText('rows', { timeout: 60_000 });
  const banner = page.getByRole('note');
  await expect(banner).toContainText('Worked out ahead by sqt');
  await expect(banner).toContainText('isn’t atomic');
  const readsNow = async () => (await reads.textContent()).match(/Reads deployment (\S+):/)[1];
  const published = await readsNow();

  // The Overview counts the engine's orders, and the count moves as new ones arrive.
  // The source's row count on its Overview card, not the engine panel's counts beside the page.
  const sourceRows = async () =>
    Number((await page.locator('div.ml-auto.shrink-0.text-right').getByText(/^\d+ rows$/).first().textContent()).split(' ')[0]);
  const before = await sourceRows();
  expect(before).toBeGreaterThanOrEqual(3);
  await expect.poll(sourceRows, { timeout: 30_000 }).toBeGreaterThan(before);
  // Its warehouse card shows nothing a browser engine cannot measure.
  await expect(page.getByTestId('warehouse-health-summary')).not.toContainText('Disk capacity');
  await expect(page.getByTestId('warehouse-health-summary')).not.toContainText('Inodes');

  // The engine lives in this tab, so from here on the test moves with the app's own links: a
  // reload would start the story over.
  const sidebar = (name) => page.getByRole('link', { name, exact: true }).first().click();

  // Warehouse shows the engine's own version, and no disk, inodes or memory.
  await sidebar('Warehouse');
  await expect(page.getByTestId('demo-no-capacity')).toBeVisible();
  await expect(page.getByText('clickhouse 25.8')).toHaveCount(0);

  // The playground defines no audits or tests, and Quality says so.
  await sidebar('Quality');
  await expect(page.getByTestId('demo-no-audits')).toContainText('sqt playground --template streaming');

  // A name the demo has no plan for says so; the pipeline plans and executes on the engine.
  await sidebar('Plan');
  const picker = page.getByRole('combobox');
  await picker.fill('orders_by_day');
  await expect(page.getByTestId('demo-unprepared-selector')).toContainText('sqt playground');
  await picker.fill('pipeline:pl__orders');
  await page.getByRole('option', { name: /pipeline:pl__orders/ }).click();
  await page.getByRole('button', { name: 'Execute' }).click({ timeout: 60_000 });
  await expect(page).toHaveURL(/\/demo\/runs\//);
  await expect(page.getByText('succeeded').first()).toBeVisible({ timeout: 120_000 });

  // Promote switches the query to the new deployment; Roll back switches it back.
  await sidebar('Deployments');
  const newest = async () =>
    (await page.locator('a[href^="/demo/deployments/2"]').evaluateAll((links) => links.map((link) => link.pathname.split('/').pop())))
      .sort()
      .at(-1);
  await expect.poll(newest).not.toBe(published);
  const built = await newest();
  await page.locator(`a[href="/demo/deployments/${built}"]`).first().click();
  await page.getByRole('button', { name: 'Promote', exact: true }).click();
  // The page opens the run once the engine has run the switch's statements.
  await expect(page).toHaveURL(/\/demo\/runs\//, { timeout: 30_000 });
  await expect.poll(readsNow, { timeout: 30_000 }).toBe(built);
  await expect(panel.locator('tbody')).not.toContainText('pending', { timeout: 30_000 });

  await sidebar('Deployments');
  await page.locator(`a[href="/demo/deployments/${published}"]`).first().click();
  await page.getByRole('button', { name: 'Roll back', exact: true }).click();
  await expect(page).toHaveURL(/\/demo\/runs\//, { timeout: 30_000 });
  await expect.poll(readsNow, { timeout: 30_000 }).toBe(published);

  await sidebar('Runs');
  await page.getByRole('button', { name: /^Deployments \d+$/ }).click();
  await expect(page.getByText('deployment rollback').first()).toBeVisible();
  expect(errors).toEqual([]);
});

test('without cross-origin isolation the demo says so and plays the recording', async ({ page }) => {
  // Only the page's own headers decide isolation; other requests the page may drop mid-flight.
  await page.route('**/demo/**', async (route) => {
    if (route.request().resourceType() !== 'document') return route.continue();
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
