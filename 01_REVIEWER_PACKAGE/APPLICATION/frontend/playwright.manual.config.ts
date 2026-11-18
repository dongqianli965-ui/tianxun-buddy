import { defineConfig } from '@playwright/test';
/** Real business acceptance against the externally started TEST_ONLY runtime. */
export default defineConfig({
  testDir: './manual-e2e', workers: 1, retries: 0, timeout: 180000,
  expect: { timeout: 30000 },
  reporter: [['list'], ['json', { outputFile: '../reports/v426_manual/BROWSER_RESULTS.json' }]],
  outputDir: '../reports/v426_manual/browser-artifacts',
  use: { baseURL: 'http://127.0.0.1:5173', channel: 'chrome', headless: true,
    viewport: { width: 1366, height: 768 }, screenshot: 'only-on-failure', trace: 'off' },
});