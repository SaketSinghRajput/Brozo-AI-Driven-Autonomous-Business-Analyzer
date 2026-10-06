import { screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { HEALTH_OK, envelope, mockApi, session, versionBody, type Route } from '../../test/mockApi';
import { renderApp } from '../../test/renderApp';
import { axeViolations } from '../../test/axe';
import { evidence } from './fixtures/build';

const DATASET = {
  id: 'ds_1',
  name: 'Orders',
  workspace_id: 'ws_abc123',
  source_type: 'file_upload',
  created_at: '2026-09-28T10:00:00Z',
};
const BASE = '/api/v1/datasets/ds_1/versions/dsv_1';

/** A DashboardSpec 1.1 as the backend builds it (app/dashboard/selector.py shapes). */
const SPEC = {
  schema_version: '1.1',
  dashboard_id: 'db_1',
  dataset_version_id: 'dsv_1',
  plan_id: 'pl_1',
  generated_at: '2026-09-28T10:00:00Z',
  filters: [{ id: 'f_region', dimension: 'region', type: 'multi_select' }],
  widgets: [
    {
      id: 'w_S1',
      widget_type: 'kpi_card',
      title: 'Return rate',
      metric: 'return_rate',
      value_ref: { evidence_id: 'ev_rate', field: 'result.value' },
      validation_status: 'VALID',
      operation_id: 'pl_1-S1',
      format: { unit: 'pct', precision: 1, currency: null },
      query: { metric: 'return_rate' },
    },
    {
      id: 'w_S2',
      widget_type: 'bar_chart',
      title: 'Revenue by region',
      validation_status: 'VALID',
      operation_id: 'pl_1-S2',
      query: { metric: 'revenue', dimensions: ['region'] },
      chart: {
        schema_version: '1.1',
        type: 'bar',
        x: { field: 'region', type: 'category' },
        y: [{ field: 'value', type: 'numeric', unit: 'currency:INR' }],
        aggregation: 'none',
        source_operation: 'pl_1-S2',
        evidence_id: 'ev_region',
        format: { unit: 'currency:INR', precision: 2, currency: 'INR' },
      },
    },
  ],
  summary: [
    {
      text: 'Return rate was 14.2%.',
      evidence_ids: ['ev_rate'],
      validation_status: 'VALID',
    },
  ],
  insights: [],
  layout: {
    columns: 12,
    items: [
      { id: 'w_S1', x: 0, y: 0, w: 4, h: 1 },
      { id: 'w_S2', x: 0, y: 1, w: 12, h: 2 },
    ],
  },
};

function setup(state: string, extra: Record<string, Route> = {}) {
  return mockApi({
    'GET /api/v1/auth/me': { body: session('analyst') },
    'GET /api/v1/health': HEALTH_OK,
    'GET /api/v1/datasets/ds_1': { body: DATASET },
    'GET /api/v1/datasets/ds_1/versions': { body: [versionBody(state, { row_count: 10 })] },
    [`GET ${BASE}/semantic-model`]: {
      body: {
        semantic_model_id: 'sm_1',
        dataset_version_id: 'dsv_1',
        version: 2,
        measures: [],
        dimensions: [],
        entities: [],
        metrics: [],
        overrides: [],
      },
    },
    ...extra,
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('Dashboard tab (TASK-INTEGRATION-006)', () => {
  it('renders the backend spec with evidence values, /data rows and the spec summary', async () => {
    const mocked = setup('READY', {
      [`GET ${BASE}/dashboard`]: { body: SPEC },
      'GET /api/v1/evidence/ev_rate': {
        body: evidence('ev_rate', 'dsv_1', {
          value: 0.142,
          unit: 'pct',
          operation: 'analytics.metric_query',
          calculation: 'returned orders / orders',
        }),
      },
      'GET /api/v1/evidence/ev_region': {
        body: evidence('ev_region', 'dsv_1', {
          rows: 2,
          unit: 'currency:INR',
          operation: 'analytics.metric_query',
          calculation: 'sum of revenue by region',
        }),
      },
      'GET /api/v1/evidence/ev_region/data': {
        body: {
          evidence_id: 'ev_region',
          columns: [
            { name: 'region', type: 'string', unit: null },
            { name: 'value', type: 'float', unit: 'currency:INR' },
          ],
          rows: [
            { region: 'North', value: 1200.5 },
            { region: 'South', value: 800 },
          ],
          truncated: false,
          row_count: 2,
        },
      },
      [`GET ${BASE}/dimensions/region/values`]: {
        body: {
          dimension: 'region',
          values: ['North', 'South'],
          truncated: true,
          semantic_model_version: 2,
        },
      },
    });
    const { calls } = mocked;
    renderApp('/datasets/ds_1/dashboard');

    const rate = await screen.findByRole('region', { name: 'Return rate' });
    expect(
      await within(rate).findByRole('button', { name: /^Return rate: 14\.2%\. Open evidence$/ }),
    ).toBeInTheDocument();
    const chart = screen.getByRole('region', { name: 'Revenue by region' });
    expect(
      await within(chart).findByText(
        'Bar chart of value by region: 2 data points, from ₹1,200.50 at North to ₹800.00 at South.',
      ),
    ).toBeInTheDocument();
    expect(within(chart).getByText(/· INR/)).toBeInTheDocument();
    expect(screen.getByText('Return rate was 14.2%.')).toBeInTheDocument();

    // Pick list from GET …/dimensions/region/values?limit=200, with the truncation note.
    const filters = screen.getByRole('region', { name: 'Filters' });
    expect(await within(filters).findByLabelText('South')).toBeInTheDocument();
    expect(within(filters).getByText('Showing the first 2 values.')).toBeInTheDocument();
    const valuesCall = calls.find((call) => call.url.includes('/dimensions/region/values'));
    expect(new URL(valuesCall?.url ?? 'http://x').searchParams.get('limit')).toBe('200');
    expect(await axeViolations()).toEqual([]);
  });

  it('shows the processing stage, not an error, before the version is READY', async () => {
    setup('SEMANTICALLY_MAPPED');
    renderApp('/datasets/ds_1/dashboard');
    expect(
      await screen.findByText(/Preparing the dashboard: planning analyses…/),
    ).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows a regenerating state while the dashboard is being rebuilt (404)', async () => {
    setup('READY', {
      [`GET ${BASE}/dashboard`]: envelope(404, 'NOT_FOUND', 'dashboard not built yet'),
    });
    renderApp('/datasets/ds_1/dashboard');
    expect(
      await screen.findByText(/Regenerating the dashboard for the latest changes/),
    ).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
