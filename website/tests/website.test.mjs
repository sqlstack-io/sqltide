import { test, expect, devices } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { docsRoutes } from '../src/navigation.mjs';

test('all documentation routes render, legacy routes retain query and fragment', async ({ page, request }) => {
  for (const route of docsRoutes) {
    const response = await request.get(route);
    expect(response.ok(), route).toBe(true);
    expect(await response.text()).toContain('<h1');
  }
  await page.goto('/concepts/models?from=legacy#terminal-views');
  await expect(page).toHaveURL(/\/docs\/concepts\/models\/\?from=legacy#terminal-views$/);
  await expect(page.getByRole('heading', { name: 'Terminal views', exact: true })).toBeVisible();
});

test('keyboard navigation, copying, search and cross-page theme persistence', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/');
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
  await page.getByRole('button', { name: 'Copy install command' }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('pip install sqltide');
  await page.getByRole('button', { name: 'Switch to dark theme' }).click();
  await page.getByRole('navigation', { name: 'Main', exact: true }).getByRole('link', { name: 'Docs', exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await expect(page.getByRole('button', { name: 'Search', exact: true })).toBeEnabled();
  await page.keyboard.press('Control+k');
  const search = page.getByRole('textbox', { name: 'Search', exact: true });
  await expect(search).toBeFocused();
  await search.fill('cap_at_column');
  await expect(page.locator('.pagefind-ui__result-link').first()).toBeVisible();
  await expect(page.locator('.pagefind-ui__results')).toContainText('Retention');
  await page.keyboard.press('Escape');
  await expect(page.locator('dialog')).not.toBeVisible();
  await page.getByRole('button', { name: 'Switch to light theme' }).click();
  await page.getByRole('link', { name: 'SQLTide home' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
});

for (const theme of ['light', 'dark']) {
  for (const mobile of [false, true]) {
    test(`${theme} ${mobile ? 'touch' : 'desktop'} layout and accessibility`, async ({ browser, baseURL }) => {
      const context = await browser.newContext({
        baseURL, colorScheme: theme,
        ...(mobile ? devices['Pixel 7'] : { viewport: { width: 1440, height: 1000 } }),
      });
      const page = await context.newPage();
      for (const route of ['/', '/concepts/sql-plan/', '/concepts/ingestion-replay/', '/docs/quickstart/', '/docs/concepts/models/', '/docs/examples/staged-workflow/']) {
        await page.goto(route);
        await page.evaluate(() => document.fonts.ready);
        await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), route).toBe(true);
        await expect(page.getByRole('link', { name: 'by SQLStack', exact: true })).toHaveAttribute('href', 'https://sqlstack.io');
        await expect(page.getByRole('navigation', { name: 'SQLStack tools' }).getByRole('link')).toHaveText(['SQLBuild', 'SQLClean', 'SQLTide', 'Fensu']);
        const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa']).analyze();
        expect(result.violations, route).toEqual([]);
      }
      if (mobile) {
        expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true);
        await page.getByRole('button', { name: 'Menu', exact: true }).tap();
        await expect(page.getByRole('link', { name: 'Retention policies', exact: true })).toBeVisible();
        await page.getByRole('link', { name: 'Retention policies', exact: true }).tap();
        await expect(page.getByRole('heading', { level: 1 })).toHaveText('Retention policies');
      }
      await context.close();
    });
  }
}

test('blocked storage and clipboard remain usable and honest', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', { get() { throw new Error('Unavailable'); } });
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: () => Promise.reject(new Error('Denied')) } });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Copy install command' }).click();
  await expect(page.getByRole('status')).toContainText('Copy unavailable');
  await page.getByRole('button', { name: /Switch to .* theme/ }).click();
  await expect(page.getByRole('button', { name: /Switch to .* theme/ })).toBeVisible();
});

test('homepage hero and review alternatives can be paused, stepped, replayed, and read without motion', async ({ page }) => {
  await page.clock.install();
  for (const route of ['/', '/concepts/sql-plan/', '/concepts/ingestion-replay/']) {
    await page.goto(route);
    const figure = page.locator('[data-showcase]');
    const virtual = route === '/';
    const play = page.getByRole('button', { name: virtual ? 'Play story' : 'Play', exact: true });
    if (virtual) {
      await expect(figure.locator('.version-a')).toHaveText('Current version');
      await expect(figure.locator('.status-a')).toHaveText('Serving queries');
      await expect(figure.locator('.status-b')).toHaveText('Receiving live data');
      await page.clock.fastForward(10000);
      await expect(figure).toHaveAttribute('data-stage', '2');
    }
    await figure.locator('[data-step="0"]').click();
    await expect(figure).toHaveAttribute('data-stage', '0');
    await play.click();
    await page.clock.fastForward(3600);
    await expect(figure).toHaveAttribute('data-stage', '1');
    await page.getByRole('button', { name: virtual ? 'Pause story' : 'Pause', exact: true }).click();
    await page.clock.fastForward(10000);
    await expect(figure).toHaveAttribute('data-stage', '1');
    if (virtual) {
      await expect(figure.locator('.binding-status')).toHaveText('Reads the current version');
      await expect(figure.locator('.status-b')).toHaveText('Receiving live data');
    }
    await figure.locator('[data-step="2"]').click();
    await expect(figure).toHaveAttribute('data-stage', '2');
    if (virtual) {
      await expect(figure.locator('.binding-status')).toHaveText('Reads the current version');
      await expect(figure.locator('.stage-description')).toContainText('Compare results before switching');
    }
    await play.click();
    await page.clock.fastForward(3600);
    await expect(figure).toHaveAttribute('data-stage', '3');
    await expect(play).toBeVisible();
    if (virtual) {
      await expect(figure.locator('.binding-status')).toHaveText('Now reads the new version');
      await expect(figure.locator('.consumer-binding code')).toHaveText('SELECT * FROM tbl__order_report');
      await expect(figure.locator('figcaption')).toContainText('one view at a time');
      await expect(page.locator('.steps .step-number')).toHaveText(['01 · Write & validate', '02 · Plan', '03 · Build & inspect', '04 · Promote']);
      await expect(page.locator('#technical-details')).toHaveCount(0);
      await expect(page.locator('main')).not.toContainText('20261101T');
      await expect(page.getByRole('link', { name: 'Try this workflow →', exact: true })).toHaveAttribute('href', '/docs/examples/staged-workflow/');
    } else {
      await expect(figure.locator('.stage-description')).toContainText(/cutoff|both paths/);
    }
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await expect(play).not.toBeVisible();
    await figure.locator('[data-step="0"]').focus();
    await page.keyboard.press('Enter');
    await expect(figure).toHaveAttribute('data-stage', '0');
    expect(await figure.evaluate(el => el.getAnimations({ subtree: true }).length)).toBe(0);
    await page.emulateMedia({ reducedMotion: 'no-preference' });
    if (virtual) {
      await page.getByRole('link', { name: 'Start with virtual mode', exact: true }).first().click();
      await expect(page.getByRole('heading', { level: 1 })).toHaveText('Stage a pipeline change');
      await expect(page.getByRole('heading', { name: '4. Promote', exact: true })).toBeVisible();
    }
  }
});

test('homepage explains both versions without JavaScript or playback', async ({ browser, baseURL }) => {
  const context = await browser.newContext({ baseURL, javaScriptEnabled: false, reducedMotion: 'reduce' });
  const page = await context.newPage();
  await page.goto('/');
  const figure = page.locator('[data-showcase]');
  await expect(figure.locator('.version-a')).toHaveText('Current version');
  await expect(figure.locator('.status-a')).toHaveText('Serving queries');
  await expect(figure.locator('.deployment-b')).toContainText('New version');
  await expect(figure.locator('.status-b')).toHaveText('Receiving live data');
  await expect(figure.locator('.consumer-binding code')).toHaveText('SELECT * FROM tbl__order_report');
  await expect(figure.locator('.stage-description')).toContainText('Compare results before switching');
  await expect(figure.locator('figcaption')).toContainText('one view at a time');
  await expect(figure.locator('[data-play]')).not.toBeVisible();
  await context.close();
});
