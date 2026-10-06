import { describe, expect, it } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { axeViolations } from '../test/axe';
import { HEALTH_OK, envelope, mockApi, session, versionBody } from '../test/mockApi';
import { renderApp } from '../test/renderApp';

/**
 * TASK-UI-024 (partial) keyboard/focus checks on the new screens. Colour contrast of the
 * tokens is covered by src/test/contrast.test.ts.
 */
function api() {
  mockApi({
    'GET /api/v1/auth/me': { body: session('analyst') },
    'GET /api/v1/health': HEALTH_OK,
    'GET /api/v1/datasets': { body: [] },
    'GET /samples/index.json': { body: [] },
  });
}

describe('keyboard and focus (TASK-UI-024 partial)', () => {
  it('offers a skip link as the first tab stop', async () => {
    api();
    const user = userEvent.setup();
    renderApp('/');
    await screen.findByRole('heading', { name: 'Home' });

    await user.tab();
    expect(screen.getByRole('link', { name: 'Skip to main content' })).toHaveFocus();
  });

  it('moves focus to the main content after keyboard navigation', async () => {
    api();
    const user = userEvent.setup();
    renderApp('/');
    const nav = await screen.findByRole('navigation', { name: 'Primary' });

    within(nav).getByRole('link', { name: 'Datasets' }).focus();
    await user.keyboard('{Enter}');

    await screen.findByRole('heading', { name: 'Datasets' });
    await waitFor(() => expect(screen.getByRole('main')).toHaveFocus());
  });

  it('opens and closes the notification centre by keyboard, returning focus', async () => {
    api();
    const user = userEvent.setup();
    renderApp('/');
    const bell = await screen.findByRole('button', { name: 'Notifications' });

    bell.focus();
    await user.keyboard('{Enter}');
    expect(bell).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('region', { name: 'Notification centre' })).toHaveTextContent(
      'No notifications yet.',
    );

    await user.keyboard('{Escape}');
    expect(bell).toHaveAttribute('aria-expanded', 'false');
    expect(bell).toHaveFocus();
  });

  it('reaches the name field right after the file picker on the upload page', async () => {
    api();
    const user = userEvent.setup();
    renderApp('/datasets/upload');
    const picker = await screen.findByLabelText('Choose a file');

    picker.focus();
    await user.tab();
    expect(screen.getByLabelText('Dataset name (optional)')).toHaveFocus();
  });
});

const DATASET = {
  id: 'ds_1',
  name: 'Orders',
  workspace_id: 'ws_abc123',
  source_type: 'file_upload',
  created_at: '2026-09-28T10:00:00Z',
};
const VERSION = '/api/v1/datasets/ds_1/versions/dsv_1';

function workspaceApi() {
  mockApi({
    'GET /api/v1/auth/me': { body: session('analyst') },
    'GET /api/v1/health': HEALTH_OK,
    'GET /api/v1/datasets': { body: [DATASET] },
    'GET /api/v1/datasets/ds_1': { body: DATASET },
    'GET /api/v1/datasets/ds_1/versions': { body: [versionBody('READY', { row_count: 3 })] },
    [`GET ${VERSION}/profile`]: {
      body: {
        dataset_version_id: 'dsv_1',
        engine_version: 'test',
        row_count: 3,
        duplicate_rows: 0,
        sampled: false,
        sample_size: null,
        columns: [],
        correlations: [],
      },
    },
    [`GET ${VERSION}/semantic-model`]: {
      body: {
        semantic_model_id: 'sm_1',
        dataset_version_id: 'dsv_1',
        version: 1,
        measures: [],
        dimensions: [],
        entities: [],
        metrics: [],
        overrides: [],
      },
    },
    [`GET ${VERSION}/capabilities`]: {
      body: {
        dataset_version_id: 'dsv_1',
        semantic_model_version: 1,
        rules_version: '1',
        available: [],
        unavailable: [],
      },
    },
    [`GET ${VERSION}/dashboard`]: envelope(404, 'NOT_FOUND', 'not built yet'),
    'GET /api/v1/chat/conversations': { body: [] },
  });
}

/**
 * axe-core over whole rendered screens (TASK-UI-024). The dashboard itself is scanned in
 * features/dashboard (fixture and backend specs, evidence drawer open).
 */
describe('automated WCAG checks (axe-core)', () => {
  it('detects a violation (self-check of the helper)', async () => {
    const { container } = render(
      <main>
        <button type="button" />
        {/* eslint-disable-next-line jsx-a11y/alt-text -- the violation axe must report */}
        <img src="x.png" />
      </main>,
    );
    const found = await axeViolations(container);
    expect(found.map((line) => line.split(' ')[0])).toEqual(['button-name', 'image-alt']);
  });

  it('login', async () => {
    mockApi({
      'GET /api/v1/auth/me': envelope(401, 'AUTH_ERROR', 'not authenticated'),
      'GET /api/v1/health': HEALTH_OK,
    });
    renderApp('/login');
    await screen.findByRole('heading', { level: 1 });
    expect(await axeViolations()).toEqual([]);
  });

  it('home', async () => {
    api();
    renderApp('/');
    await screen.findByRole('heading', { name: 'Home' });
    expect(await axeViolations()).toEqual([]);
  });

  it.each(['overview', 'dashboard', 'understanding', 'profile', 'history'])(
    'dataset workspace: %s tab',
    async (tab) => {
      workspaceApi();
      renderApp(`/datasets/ds_1/${tab}`);
      await screen.findByRole('heading', { name: 'Orders' });
      await screen.findByRole('navigation', { name: 'Dataset workspace' });
      expect(await axeViolations()).toEqual([]);
    },
  );

  it('assistant', async () => {
    workspaceApi();
    renderApp('/assistant');
    await screen.findByRole('heading', { level: 1 });
    expect(await axeViolations()).toEqual([]);
  });
});
