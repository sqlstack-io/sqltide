import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  workers: 2,
  use: {
    baseURL: 'http://127.0.0.1:4174',
    ...(process.env.CHROME_PATH ? { launchOptions: { executablePath: process.env.CHROME_PATH } } : {}),
  },
  webServer: {
    command: 'npm run build:review && npm run check:links && node scripts/serve.mjs dist 4174',
    url: 'http://127.0.0.1:4174',
  },
});
