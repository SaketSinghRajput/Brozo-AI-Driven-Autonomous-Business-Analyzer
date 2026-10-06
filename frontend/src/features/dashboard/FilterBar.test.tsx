import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import '../../api/client'; // configures the generated client (base URL), as the app shell does
import { createQueryClient } from '../../lib/queryClient';
import { mockApi } from '../../test/mockApi';
import { DashboardView } from './demo/DashboardDemoPage';
import { bindQuery, withClause } from './FilterBar';
import { evidence, type FixtureBundle } from './fixtures/build';
import { retailFixture } from './fixtures/retail';

const VERSION = 'dsv_retail_v4';

// A re-query returns a new evidence record (value + rows); the fixture source serves it like
// GET /evidence/{id} and /evidence/{id}/data would.
const bundle: FixtureBundle = {
  ...retailFixture,
  evidence: {
    ...retailFixture.evidence,
    ev_filtered: evidence('ev_filtered', VERSION, {
      value: 100,
      unit: 'currency:INR',
      operation: 'analytics.metric_query',
      calculation: 'sum of the measure',
    }),
  },
  rows: {
    ...retailFixture.rows,
    ev_filtered: [
      { region: 'North', revenue: 70 },
      { region: 'East', revenue: 30 },
    ],
  },
};

interface Posted {
  request: object;
  semantic_model_version: number;
}

function backend() {
  return mockApi({
    'POST /api/v1/analytics/query': {
      body: {
        evidence_id: 'ev_filtered',
        value: 100,
        unit: 'currency:INR',
        table: { columns: [], rows: [] },
        truncated: false,
      },
    },
  });
}

function renderView(semanticModelVersion?: number) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <DashboardView bundle={bundle} semanticModelVersion={semanticModelVersion} />
    </QueryClientProvider>,
  );
}

async function pickRegions(...names: string[]) {
  const user = userEvent.setup();
  const filters = screen.getByRole('region', { name: 'Filters' });
  const group = within(filters).getByRole('group', { name: 'region' });
  for (const name of names) await user.click(await within(group).findByLabelText(name));
  await user.click(within(filters).getByRole('button', { name: 'Apply' }));
  return user;
}

async function postedBodies(calls: Request[]): Promise<Posted[]> {
  const posts = calls.filter((call) => call.method === 'POST');
  return Promise.all(posts.map((post) => post.clone().json() as Promise<Posted>));
}

afterEach(() => vi.unstubAllGlobals());

describe('FilterBinder rule (mirrors backend app/dashboard/filters.py)', () => {
  const definitions = [
    { id: 'f_time', dimension: 'day', type: 'date_range' as const },
    { id: 'f_region', dimension: 'region', type: 'multi_select' as const },
  ];

  it('replaces a widget filter on the same dimension and keeps the others first', () => {
    const query = {
      metric: 'revenue',
      filters: [
        { dimension: 'region', op: 'eq' as const, value: 'West' },
        { dimension: 'channel', op: 'eq' as const, value: 'web' },
      ] as [
        { dimension: string; op: 'eq'; value: string },
        { dimension: string; op: 'eq'; value: string },
      ],
    };
    const active = [{ dimension: 'region', op: 'in' as const, value: ['North'] }];
    expect(bindQuery(query, active).filters).toEqual([
      { dimension: 'channel', op: 'eq', value: 'web' },
      { dimension: 'region', op: 'in', value: ['North'] },
    ]);
  });

  it('orders clauses by filter id and removes a clause with null', () => {
    const region = { dimension: 'region', op: 'in' as const, value: ['North'] };
    const day = { dimension: 'day', op: 'between' as const, value: ['2026-01-01', '2026-03-31'] };
    const both = withClause([region], definitions, 'day', day);
    expect(both.map((f) => f.dimension)).toEqual(['region', 'day']); // f_region < f_time
    expect(withClause(both, definitions, 'day', null)).toEqual([region]);
  });
});

describe('Dashboard filters (TASK-UI-013)', () => {
  it('re-queries KPI cards and charts with the bound query; marks others not filterable', async () => {
    const { calls } = backend();
    renderView(1);
    await pickRegions('North', 'East');

    expect(screen.getByRole('list', { name: 'Active filters' })).toHaveTextContent(
      'region in North, East',
    );
    // Applying keeps the control mounted: keyboard focus stays on Apply.
    expect(
      within(screen.getByRole('region', { name: 'Filters' })).getByRole('button', {
        name: 'Apply',
      }),
    ).toHaveFocus();
    expect(
      (await screen.findAllByRole('button', { name: /: ₹100\. Open evidence$/ })).length,
    ).toBeGreaterThan(0);
    expect(await screen.findAllByText('Filtered: region in North, East')).not.toHaveLength(0);
    // The chart re-renders from the new evidence's rows.
    expect(
      await within(screen.getByRole('region', { name: 'Revenue by region' })).findByText(
        /Bar chart of revenue by region: 2 data points/,
      ),
    ).toBeInTheDocument();
    // Orders has no query: shown unfiltered and labelled so.
    expect(screen.getAllByText('Not filterable: shows all data.').length).toBeGreaterThan(0);

    const bodies = await postedBodies(calls);
    expect(bodies.every((body) => body.semantic_model_version === 1)).toBe(true);
    expect(bodies.map((body) => body.request)).toEqual(
      expect.arrayContaining([
        {
          metric: 'revenue',
          filters: [{ dimension: 'region', op: 'in', value: ['North', 'East'] }],
        },
        // The chart keeps its own month filter and gets the dashboard clause appended.
        {
          metric: 'revenue',
          dimensions: ['region'],
          filters: [
            { dimension: 'month', op: 'eq', value: '2026-06' },
            { dimension: 'region', op: 'in', value: ['North', 'East'] },
          ],
        },
      ]),
    );
  });

  it('drills down from a chart value and clears the filters', async () => {
    const { calls } = backend();
    renderView(1);
    const user = userEvent.setup();
    const chart = screen.getByRole('region', { name: 'Revenue by region' });
    const picker = await within(chart).findByLabelText(
      /Drill down: filter the dashboard by region/,
    );
    await user.selectOptions(picker, 'South');

    const filters = screen.getByRole('region', { name: 'Filters' });
    expect(within(filters).getByRole('list', { name: 'Active filters' })).toHaveTextContent(
      'region in South',
    );
    // The picker's chart is replaced by the re-queried one: focus moves to the filter region,
    // whose pick list now shows the drilled value as chosen.
    expect(filters).toHaveFocus();
    expect(within(filters).getByLabelText('South')).toBeChecked();
    await screen.findAllByText('Filtered: region in South');
    expect((await postedBodies(calls)).length).toBeGreaterThan(0);

    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(screen.queryByRole('list', { name: 'Active filters' })).not.toBeInTheDocument();
    expect(filters).toHaveFocus();
    expect(within(filters).getByLabelText('South')).not.toBeChecked();
  });

  it('removes a filter and returns to the spec results (sample data, no backend)', async () => {
    mockApi({});
    renderView();
    const user = await pickRegions('North');

    expect(screen.getByText(/show the unfiltered sample/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Remove filter region in North' }));
    expect(screen.queryByRole('list', { name: 'Active filters' })).not.toBeInTheDocument();
  });
});
