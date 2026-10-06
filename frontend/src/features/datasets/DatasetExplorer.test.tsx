import { describe, expect, it } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HEALTH_OK, envelope, mockApi, session, versionBody, type Route } from '../../test/mockApi';
import { renderApp } from '../../test/renderApp';

const DATASET = {
  id: 'ds_1',
  name: 'Quarterly export',
  workspace_id: 'ws_abc123',
  source_type: 'file_upload',
  created_at: '2026-09-28T10:00:00Z',
};

function api(role: 'viewer' | 'analyst', routes: Record<string, Route>) {
  return mockApi({
    'GET /api/v1/auth/me': { body: session(role) },
    'GET /api/v1/health': HEALTH_OK,
    ...routes,
  });
}

describe('DatasetExplorer list (TASK-UI-005)', () => {
  it('lists datasets and links to their workspace', async () => {
    api('analyst', { 'GET /api/v1/datasets': { body: [DATASET] } });
    renderApp('/datasets');

    const link = await screen.findByRole('link', { name: 'Quarterly export' });
    expect(link).toHaveAttribute('href', '/datasets/ds_1/overview');
    expect(screen.getByRole('link', { name: 'Upload dataset' })).toHaveAttribute(
      'href',
      '/datasets/upload',
    );
  });

  it('gives a viewer a disabled upload action with the reason', async () => {
    api('viewer', { 'GET /api/v1/datasets': { body: [] } });
    renderApp('/datasets');

    expect(await screen.findByText(/No datasets yet/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Upload dataset' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Upload dataset' })).toBeDisabled();
    expect(screen.getByText('Requires Analyst role')).toBeInTheDocument();
  });

  it('redirects to login with an expiry notice when a data call returns 401', async () => {
    api('analyst', {
      'GET /api/v1/datasets': envelope(401, 'AUTH_ERROR', 'session expired'),
    });
    const { router } = renderApp('/datasets');

    expect(await screen.findByText(/Your session has expired/)).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/login');
    expect(router.state.location.search).toBe(`?next=${encodeURIComponent('/datasets')}`);
  });

  it('shows a permission message on 403', async () => {
    api('analyst', {
      'GET /api/v1/datasets': envelope(403, 'AUTH_ERROR', 'forbidden'),
    });
    renderApp('/datasets');

    expect(await screen.findByRole('alert')).toHaveTextContent(
      "You don't have permission for this action",
    );
  });
});

describe('DatasetExplorer workspace (TASK-UI-005)', () => {
  const v1 = versionBody('INGESTION_FAILED', {
    failure: { code: 'TOO_MANY_BAD_ROWS', message: '[TOO_MANY_BAD_ROWS] stopped' },
  });
  const v2 = versionBody('PROFILED', {
    id: 'dsv_2',
    version_no: 2,
    row_count: 1200,
    review_flags: ['DATE_FORMAT_DMY:order_date', 'MALFORMED_ROW_FIELD_COUNT', 'SOMETHING_NEW'],
  });
  const routes = {
    'GET /api/v1/datasets/ds_1': { body: DATASET },
    'GET /api/v1/datasets/ds_1/versions': { body: [v1, v2] },
    'GET /api/v1/datasets/ds_1/versions/dsv_2/quarantine': {
      body: {
        version_id: 'dsv_2',
        rows_seen: 1203,
        rows_rejected: 3,
        rejected_sample: [{ row_number: 17, reason: 'expected 9 fields, got 7' }],
        flags: [],
      },
    },
  };

  it('shows the newest version with review flags explained and rejected rows', async () => {
    api('viewer', routes);
    renderApp('/datasets/ds_1/overview');

    expect(await screen.findByRole('heading', { name: 'Quarterly export' })).toBeInTheDocument();
    expect(screen.getByText('Needs your review (3)')).toBeInTheDocument();
    expect(
      screen.getByText('Dates in "order_date" were read as day/month/year. Confirm this is right.'),
    ).toBeInTheDocument();
    expect(screen.getByText('Needs review: SOMETHING_NEW')).toBeInTheDocument();
    expect(await screen.findByText(/3 of 1,203 rows/)).toBeInTheDocument();
    expect(screen.getByText('expected 9 fields, got 7')).toBeInTheDocument();
  });

  it('switches versions through the selector and explains a failure', async () => {
    api('viewer', routes);
    const user = userEvent.setup();
    const { router } = renderApp('/datasets/ds_1/overview');

    await user.selectOptions(await screen.findByLabelText('Version'), 'dsv_1');

    await waitFor(() => expect(router.state.location.search).toBe('?version=dsv_1'));
    expect(await screen.findByText(/Too many rows couldn't be read/)).toBeInTheDocument();
    expect(screen.getByText('Reason code: TOO_MANY_BAD_ROWS')).toBeInTheDocument();
  });

  it('lists all versions with their lifecycle state', async () => {
    api('viewer', routes);
    renderApp('/datasets/ds_1/history');

    const table = await screen.findByRole('table', { name: /Versions of this dataset/ });
    expect(within(table).getByText('Profiled')).toBeInTheDocument();
    expect(within(table).getByText('Import failed')).toBeInTheDocument();
    expect(within(table).getByText('1,200')).toBeInTheDocument();
  });
});
