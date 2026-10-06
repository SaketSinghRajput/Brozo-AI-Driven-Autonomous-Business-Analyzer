import { expect, test, type Page } from '@playwright/test';
import { openDashboard, requireStack, signIn, uploadFixture } from './helpers';

test.beforeEach(() => requireStack());

const revenueKpi = (page: Page) =>
  page
    .getByRole('region', { name: 'Dashboard' })
    .getByRole('button', { name: /^Revenue: .*Open evidence$/ });

test('(b) retail upload reaches READY and the dashboard opens evidence', async ({ page }) => {
  await signIn(page);
  const version = await uploadFixture(page, 'retail/orders.csv');
  await page.screenshot({ path: 'test-results/b-processing-finished.png', fullPage: true });

  await openDashboard(page, version);
  const dashboard = page.getByRole('region', { name: 'Dashboard' });
  const kpis = dashboard.getByRole('button', { name: /Open evidence$/ });
  await expect(kpis.first()).toBeVisible();
  expect(await kpis.count()).toBeGreaterThan(0);
  await expect(dashboard.getByRole('img', { name: /^Revenue by month\./ })).toBeVisible();
  await page.screenshot({ path: 'test-results/b-dashboard.png', fullPage: true });

  await revenueKpi(page).click();
  const drawer = page.getByRole('dialog', { name: 'Evidence' });
  await expect(drawer).toBeVisible();
  // Source: dataset id + version id; Query / tool: operation + tool version.
  await expect(drawer.getByText(`version ${version.versionId}`)).toBeVisible();
  await expect(drawer.getByRole('definition').filter({ hasText: /\(tool .+\)/ })).toBeVisible();
  await expect(drawer.getByRole('definition').getByText('Validated')).toBeVisible();
  await drawer.getByText('Technical details').click();
  await expect(drawer.locator('pre')).toContainText(/SELECT/i);
  await page.screenshot({ path: 'test-results/b-evidence-drawer.png', fullPage: true });
});

test('(c) applying a filter changes the KPI value', async ({ page }) => {
  await signIn(page);
  const version = await uploadFixture(page, 'retail/orders.csv');
  await openDashboard(page, version);

  const before = await revenueKpi(page).getAttribute('aria-label');
  const regionForm = page
    .getByRole('region', { name: 'Filters' })
    .locator('form')
    .filter({ has: page.getByRole('group', { name: 'Region' }) });
  await regionForm.getByRole('checkbox', { name: 'North' }).check();
  await regionForm.getByRole('button', { name: 'Apply' }).click();

  await expect(page.getByRole('list', { name: 'Active filters' })).toContainText('North');
  await expect(revenueKpi(page)).not.toHaveAttribute('aria-label', before ?? '');
  const after = await revenueKpi(page).getAttribute('aria-label');
  test.info().annotations.push({ type: 'kpi', description: `${before} -> ${after}` });
  await page.screenshot({ path: 'test-results/c-filtered.png', fullPage: true });
});

test('(e) the insufficient fixture shows "Insufficient data" with reasons', async ({ page }) => {
  await signIn(page);
  const version = await uploadFixture(page, 'insufficient/sales.csv');
  await page.goto(`/datasets/${version.datasetId}/dashboard?version=${version.versionId}`);

  const capabilities = page.locator('section[aria-labelledby="capabilities-heading"]');
  await expect(capabilities).toBeVisible({ timeout: 60_000 });
  const unavailable = capabilities.getByRole('listitem').filter({ hasText: 'Insufficient data' });
  await expect(unavailable.first()).toBeVisible();
  // Each unavailable analysis carries a reason line next to the label.
  await expect(unavailable.filter({ hasText: /Forecast/ })).toBeVisible();
  const reasons = await unavailable.allInnerTexts();
  test.info().annotations.push({ type: 'reasons', description: reasons.join(' | ') });
  await capabilities.screenshot({ path: 'test-results/e-insufficient.png' });
});
