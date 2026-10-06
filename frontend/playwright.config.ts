import { defineConfig, devices } from '@playwright/test';

/**
 * Browser E2E against a running compose stack (TASK-TEST-004). Not part of `npm test` (vitest):
 * specs are named *.e2e.ts. Without E2E_EMAIL / E2E_PASSWORD every spec skips, so CI without a
 * stack stays green. Run: E2E_EMAIL=... E2E_PASSWORD=... npx playwright test
 */
export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.e2e.ts',
  outputDir: './test-results',
  // Journeys share one workspace and the single local LLM; run them one at a time.
  workers: 1,
  fullyParallel: false,
  retries: 0,
  // A chat turn on the local LLM takes 30-60 s; uploads run the whole pipeline.
  timeout: 240_000,
  expect: { timeout: 30_000 },
  reporter: [['list'], ['html', { outputFolder: './playwright-report', open: 'never' }]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'http://127.0.0.1:8080',
    trace: 'on',
    screenshot: 'on',
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
