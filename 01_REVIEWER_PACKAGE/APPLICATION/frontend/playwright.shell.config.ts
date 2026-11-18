import { defineConfig } from '@playwright/test';

/** Frontend-only regression with API routes mocked in Chromium; no backend or database. */
export default defineConfig({
  testDir: './e2e',
  testMatch: 'manual-shell.spec.ts',
  workers: 1,
  retries: 0,
  timeout: 120000,
  expect: { timeout: 30000 },
  reporter: [['list'], ['json', { outputFile: '../reports/v426_manual/FRONTEND_UI_RESULTS.json' }]],
  outputDir: '../reports/v426/manual-ui-artifacts',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    channel: 'chrome',
    headless: true,
    viewport: { width: 1366, height: 768 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npm.cmd run preview -- --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173/',
    reuseExistingServer: false,
    timeout: 60000,
  },
});
