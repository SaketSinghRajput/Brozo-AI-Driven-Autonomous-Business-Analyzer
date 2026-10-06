import { describe, expect, it } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HEALTH_OK, mockApi, session, versionBody } from '../../test/mockApi';
import { renderApp } from '../../test/renderApp';

const PROFILE = {
  dataset_version_id: 'dsv_1',
  engine_version: 'test',
  row_count: 1000,
  duplicate_rows: 4,
  sampled: false,
  sample_size: null,
  columns: [
    {
      name: 'amount',
      physical_type: 'float',
      null_ratio: 0.1,
      cardinality: 870,
      unique_ratio: 0.87,
      parse_success_ratio: 1,
      numeric: {
        min: -5,
        max: 900,
        mean: 120.5,
        median: 99,
        std: 30,
        var: 900,
        skew: 1.2,
        zero_ratio: 0,
        neg_ratio: 0.01,
        quantiles: { '0.25': 50, '0.75': 150 },
      },
      outliers: { count: 12, method: 'iqr', sample_indices: [3, 88] },
    },
    {
      name: 'contact',
      physical_type: 'string',
      null_ratio: 0,
      cardinality: 990,
      unique_ratio: 0.99,
      parse_success_ratio: 1,
      categorical: {
        masked: true,
        values: [{ value: 'a***@***', count: 2, frequency: 0.002 }],
      },
    },
    {
      name: 'booked_on',
      physical_type: 'date',
      null_ratio: 0,
      cardinality: 365,
      unique_ratio: 0.365,
      parse_success_ratio: 1,
      temporal: {
        min: '2025-01-01T00:00:00Z',
        max: '2025-12-31T00:00:00Z',
        granularity: 'day',
        regularity: 0.98,
        gaps: 0,
        tz_mixed: false,
      },
    },
  ],
  correlations: [
    { a: 'amount', b: 'units', coefficient: 0.1, method: 'pearson', n: 950 },
    { a: 'amount', b: 'discount', coefficient: -0.83, method: 'spearman', n: 900 },
  ],
};

function renderProfile() {
  mockApi({
    'GET /api/v1/auth/me': { body: session('viewer') },
    'GET /api/v1/health': HEALTH_OK,
    'GET /api/v1/datasets/ds_1': {
      body: {
        id: 'ds_1',
        name: 'Export',
        workspace_id: 'ws_abc123',
        source_type: 'file_upload',
        created_at: '2026-09-28T10:00:00Z',
      },
    },
    'GET /api/v1/datasets/ds_1/versions': { body: [versionBody('PROFILED', { row_count: 1000 })] },
    'GET /api/v1/datasets/ds_1/versions/dsv_1/profile': { body: PROFILE },
  });
  return renderApp('/datasets/ds_1/profile?version=dsv_1');
}

describe('ProfileViewer (TASK-UI-006)', () => {
  it('summarises completeness, duplicates, issues and time coverage', async () => {
    renderProfile();

    expect(await screen.findByText('Duplicate rows')).toBeInTheDocument();
    expect(screen.getByText('96.7%')).toBeInTheDocument(); // 1 - mean(0.1, 0, 0)
    expect(screen.getByText('1 of 3')).toBeInTheDocument(); // only "amount" has issues
    expect(screen.getByText('2025-01-01 – 2025-12-31')).toBeInTheDocument();
  });

  it('lists columns with type, completeness, distinct count and issues; filters by issues', async () => {
    const user = userEvent.setup();
    renderProfile();

    const table = await screen.findByRole('table', { name: /Column profile/ });
    const amountRow = within(table).getByRole('row', { name: /amount/ });
    expect(amountRow).toHaveTextContent('Decimal number');
    expect(amountRow).toHaveTextContent('90%');
    expect(amountRow).toHaveTextContent('870');
    expect(amountRow).toHaveTextContent('10% missing · 12 outliers');

    await user.click(screen.getByLabelText('Only columns with issues'));
    expect(within(table).queryByRole('button', { name: 'contact' })).not.toBeInTheDocument();
  });

  it('shows masked values as masked in the column detail', async () => {
    const user = userEvent.setup();
    renderProfile();

    await user.click(await screen.findByRole('button', { name: 'contact' }));
    const detail = screen.getByRole('region', { name: 'Column detail: contact' });
    expect(within(detail).getByText(/look like personal data/)).toBeInTheDocument();
    expect(within(detail).getByText('(masked)')).toBeInTheDocument();
  });

  it('shows outliers and temporal grain in the column detail', async () => {
    const user = userEvent.setup();
    renderProfile();

    await user.click(await screen.findByRole('button', { name: 'amount' }));
    expect(screen.getByText(/12 outliers found \(interquartile range rule\)/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'booked_on' }));
    const detail = screen.getByRole('region', { name: 'Column detail: booked_on' });
    expect(within(detail).getByText('day')).toBeInTheDocument();
  });

  it('lists correlations strongest first with their n', async () => {
    renderProfile();

    const table = await screen.findByRole('table', { name: /Correlation between column pairs/ });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows[0]).toHaveTextContent('amount ↔ discount');
    expect(rows[0]).toHaveTextContent('-0.83');
    expect(rows[0]).toHaveTextContent('900');
    expect(rows[1]).toHaveTextContent('950');
  });
});
