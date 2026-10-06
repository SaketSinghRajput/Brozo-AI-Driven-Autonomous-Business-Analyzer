import { expect, test, type Page } from '@playwright/test';
import { requireStack, signIn, uploadFixture } from './helpers';

test.beforeEach(() => requireStack());

// One turn on the local LLM takes 30-60 s (more under load); two turns plus setup.
test.setTimeout(600_000);
const TURN_TIMEOUT = 240_000;

async function ask(page: Page, question: string): Promise<void> {
  await page.getByLabel('Your question').fill(question);
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Stop' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop' })).toHaveCount(0, {
    timeout: TURN_TIMEOUT,
  });
}

/** Number segments in an answer: evidence buttons whose text is the value, not "Evidence →". */
const numberButtons = (page: Page) =>
  page
    .getByRole('main')
    .getByRole('button', { name: /^Evidence for / })
    .filter({ hasNotText: 'Evidence →' });

test('(d) grounded answer with evidence, then a "why" answer with contributions', async ({
  page,
}) => {
  await signIn(page);
  await uploadFixture(page, 'retail/orders.csv');

  await page.goto('/assistant');
  const newConversation = page.getByRole('button', { name: 'New conversation' });
  const datasetPicker = page.getByLabel('Dataset', { exact: true });
  // First visit shows the picker directly; later visits list earlier conversations first.
  await expect(newConversation.or(datasetPicker)).toBeVisible();
  if (await newConversation.isVisible()) await newConversation.click();
  await datasetPicker.selectOption({ label: 'orders.csv' });
  await expect(page.getByText(/semantic model v\d+/)).toBeVisible();
  await page.getByRole('button', { name: 'Start conversation' }).click();
  await expect(page.getByLabel('Your question')).toBeVisible();

  await ask(page, 'What was revenue in North in August 2026?');
  await expect(page.getByRole('main').getByText('Validated').first()).toBeVisible();
  const number = numberButtons(page).first();
  await expect(number).toBeVisible();
  test.info().annotations.push({ type: 'answer-number', description: await number.innerText() });
  await page.screenshot({ path: 'test-results/d-answer.png', fullPage: true });

  await number.click();
  const drawer = page.getByRole('dialog', { name: 'Evidence' });
  await expect(drawer).toBeVisible();
  await expect(drawer.getByText(/version dsv_\w+/)).toBeVisible();
  await page.screenshot({ path: 'test-results/d-answer-evidence.png', fullPage: true });
  await drawer.getByRole('button', { name: 'Close' }).click();

  await ask(page, 'Why did revenue drop in North in August 2026?');
  await expect(page.getByRole('heading', { name: 'Contributing factors' })).toBeVisible();
  await expect(
    page.getByText('Causal certainty: not established — these are the largest contributors'),
  ).toBeVisible();
  await expect(page.getByRole('main')).toContainText(/contribut/i);
  const rca = page.locator('section').filter({
    has: page.getByRole('heading', { name: 'Contributing factors' }),
  });
  await rca.scrollIntoViewIfNeeded();
  await page.screenshot({ path: 'test-results/d-why.png' });
  await rca.screenshot({ path: 'test-results/d-why-rca.png' });
});
