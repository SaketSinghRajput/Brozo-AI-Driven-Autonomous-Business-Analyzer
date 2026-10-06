import { expect, test } from '@playwright/test';
import { EMAIL, PASSWORD, openDashboard, requireStack, uploadFixture } from './helpers';

test.beforeEach(() => requireStack());

/**
 * Heuristic pre-pilot checks (docs/ux/usability-protocol.md §6) for the UX acceptance criteria
 * that can be observed without participants. Not a substitute for usability sessions.
 */
test('UX-AC-11/16/02/03: keyboard sign-in, no external requests, domain with its reasons', async ({
  page,
  baseURL,
}) => {
  const origin = new URL(baseURL ?? 'http://127.0.0.1:8080').origin;
  const external: string[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.protocol.startsWith('http') && url.origin !== origin) external.push(request.url());
  });

  // UX-AC-11 (partial): sign in with the keyboard only.
  await page.goto('/login');
  await page.keyboard.press('Tab');
  await expect(page.getByLabel('Email')).toBeFocused();
  await page.keyboard.type(EMAIL);
  await page.keyboard.press('Tab');
  await page.keyboard.type(PASSWORD);
  await page.keyboard.press('Enter');
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();

  const version = await uploadFixture(page, 'retail/orders.csv');
  await openDashboard(page, version);
  await page.goto('/assistant');
  await expect(page.getByRole('heading', { name: 'Assistant' })).toBeVisible();

  // UX-AC-02/03: the detected domain and why it was detected.
  await page.goto(`/datasets/${version.datasetId}/understanding?version=${version.versionId}`);
  await expect(page.getByRole('heading', { name: 'Business domain' })).toBeVisible();
  await expect(page.getByRole('heading', { name: /^Why/ })).toBeVisible();
  await page.screenshot({ path: 'test-results/ux-domain.png', fullPage: true });

  // UX-AC-16: everything (fonts, scripts, API) came from the app's own origin.
  expect(external).toEqual([]);
});
