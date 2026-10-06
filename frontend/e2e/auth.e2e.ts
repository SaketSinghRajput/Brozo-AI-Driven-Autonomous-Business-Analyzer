import { readFile } from 'node:fs/promises';
import { expect, request, test } from '@playwright/test';
import { EMAIL, PASSWORD, fixture, requireStack, signIn, signInToWorkspace } from './helpers';

test.beforeEach(() => requireStack());

test('(a) sign in, sign out, and a wrong password shows a generic error', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('Email').fill(EMAIL);
  await page.getByLabel('Password').fill(`${PASSWORD}-wrong`);
  await page.getByRole('button', { name: 'Sign in' }).click();
  // Same text for an unknown email and a wrong password (no account enumeration).
  await expect(page.getByRole('alert')).toHaveText('Email or password is incorrect.');
  await page.screenshot({ path: 'test-results/a-bad-password.png' });

  await signIn(page);
  await expect(page.getByText(EMAIL)).toBeVisible();
  await page.screenshot({ path: 'test-results/a-signed-in.png' });

  await page.getByRole('button', { name: 'Sign out' }).click();
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
  // The session is revoked server-side, not just forgotten by the page.
  expect((await page.request.get('/api/v1/auth/me')).status()).toBe(401);
});

const VIEWER_EMAIL = process.env.E2E_VIEWER_EMAIL ?? '';
const VIEWER_PASSWORD = process.env.E2E_VIEWER_PASSWORD ?? '';

test('(f) a Viewer cannot upload', async ({ page, baseURL }) => {
  test.skip(!VIEWER_EMAIL || !VIEWER_PASSWORD, 'E2E_VIEWER_EMAIL / E2E_VIEWER_PASSWORD not set');
  // The Viewer is a member of the admin's workspace; find its id through the admin's session.
  const admin = await request.newContext({ baseURL });
  const login = await admin.post('/api/v1/auth/login', {
    data: { email: EMAIL, password: PASSWORD },
  });
  const { workspace_id: workspaceId } = (await login.json()) as { workspace_id: string };
  await admin.dispose();

  await signInToWorkspace(page, VIEWER_EMAIL, VIEWER_PASSWORD, workspaceId);
  await page.goto('/datasets');
  await expect(page.getByRole('banner')).toContainText(/Viewer/); // role label, case-sensitive
  await expect(page.getByRole('button', { name: 'Upload dataset' })).toBeDisabled();
  await expect(page.getByRole('link', { name: 'Upload dataset' })).toHaveCount(0);
  await page.screenshot({ path: 'test-results/f-viewer-datasets.png' });

  await page.goto('/datasets/upload');
  await expect(page.getByRole('alert')).toContainText(
    'Uploading data is not available for your role',
  );
  await expect(page.locator('input[type="file"]')).toHaveCount(0);
  await page.screenshot({ path: 'test-results/f-viewer-upload.png' });

  // The backend enforces it too: a direct upload with a valid CSRF token is refused.
  const me = (await (await page.request.get('/api/v1/auth/me')).json()) as {
    role: string;
    csrf_token: string;
  };
  expect(me.role).toBe('viewer');
  const upload = await page.request.post('/api/v1/datasets', {
    headers: { 'X-CSRF-Token': me.csrf_token },
    multipart: {
      file: {
        name: 'sales.csv',
        mimeType: 'text/csv',
        buffer: await readFile(fixture('insufficient/sales.csv')),
      },
    },
  });
  expect(upload.status()).toBe(403);
});
