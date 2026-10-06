import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page } from '@playwright/test';

export const EMAIL = process.env.E2E_EMAIL ?? '';
export const PASSWORD = process.env.E2E_PASSWORD ?? '';

/** Every journey needs a live stack and a demo user; without credentials the suite skips. */
export function requireStack(): void {
  test.skip(!EMAIL || !PASSWORD, 'E2E_EMAIL / E2E_PASSWORD not set (no live stack)');
}

const here = path.dirname(fileURLToPath(import.meta.url));
export const fixture = (relative: string) => path.resolve(here, '../../evals/fixtures', relative);

export async function signIn(page: Page, email = EMAIL, password = PASSWORD): Promise<void> {
  await page.goto('/login');
  await page.getByLabel('Email').fill(email);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
}

/**
 * Signs in through the API with an explicit workspace (the login form has no workspace picker
 * yet). page.request shares the browser context's cookie jar, so the page is signed in too.
 */
export async function signInToWorkspace(
  page: Page,
  email: string,
  password: string,
  workspaceId: string,
): Promise<void> {
  const response = await page.request.post('/api/v1/auth/login', {
    data: { email, password, workspace_id: workspaceId },
  });
  expect(response.status()).toBe(200);
}

export interface UploadedVersion {
  datasetId: string;
  versionId: string;
}

/**
 * Uploads a fixture through the UI and waits for the processing timeline to finish. A file that
 * was uploaded before resolves to the existing version (the backend dedupes by content).
 */
export async function uploadFixture(page: Page, relative: string): Promise<UploadedVersion> {
  await page.goto('/datasets/upload');
  await page.locator('input[type="file"]').setInputFiles(fixture(relative));
  await page.getByRole('button', { name: 'Upload', exact: true }).click();
  await expect(page.getByRole('list', { name: 'Processing steps' })).toBeVisible();
  await expect(page.getByText('Processing finished.')).toBeVisible({ timeout: 200_000 });
  const href = await page.getByRole('link', { name: 'View the data profile' }).getAttribute('href');
  const match = /\/datasets\/(ds_\w+)\/profile\?version=(dsv_\w+)/.exec(href ?? '');
  expect(match, `profile link ${href}`).not.toBeNull();
  return { datasetId: match![1], versionId: match![2] };
}

export async function openDashboard(page: Page, version: UploadedVersion): Promise<void> {
  await page.goto(`/datasets/${version.datasetId}/dashboard?version=${version.versionId}`);
  await expect(page.getByRole('region', { name: 'Dashboard' })).toBeVisible({ timeout: 60_000 });
}
