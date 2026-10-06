import { describe, expect, it } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter, Route, Routes } from 'react-router';
import { createQueryClient } from '../../lib/queryClient';
import { CapabilityReportView } from './CapabilityReportView';
import { DashboardDemoPage, DashboardView } from './demo/DashboardDemoPage';
import { agricultureFixture } from './fixtures/agriculture';
import { invalidSpecFixture, unknownWidgetFixture } from './fixtures';
import { retailFixture } from './fixtures/retail';
import { saasFixture } from './fixtures/saas';
import type { FixtureBundle } from './fixtures/build';
import { axeViolations } from '../../test/axe';

function renderBundle(bundle: FixtureBundle) {
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <DashboardView bundle={bundle} />
    </QueryClientProvider>,
  );
}

function region(name: string): HTMLElement {
  return screen.getByRole('region', { name });
}

describe('DashboardRenderer with domain fixtures (no domain hardcoding)', () => {
  it('renders the retail spec: KPI from evidence, comparison, forecast estimate, anomaly, table', async () => {
    renderBundle(retailFixture);

    const revenue = region('Revenue');
    const value = await within(revenue).findByRole('button', {
      name: /^Revenue: ₹4,210,000\. Open evidence$/,
    });
    expect(value).toBeInTheDocument();
    expect(within(revenue).getByText(/Higher than/)).toBeInTheDocument();
    expect(within(revenue).getByText('Calculated')).toBeInTheDocument();
    expect(within(revenue).getByText('Validated')).toBeInTheDocument();

    const trend = region('Revenue by month');
    expect(within(trend).getByText('Estimate')).toBeInTheDocument();
    expect(await within(trend).findByText(/2026-04: ₹4,480,000/)).toBeInTheDocument();
    expect(within(trend).getByText('Unusual values detected')).toBeInTheDocument();

    const products = region('Top products');
    const table = await within(products).findByRole('table', { name: 'Top products' });
    expect(within(table).getByRole('cell', { name: 'Tea 500g' })).toBeInTheDocument();

    const unavailable = region('Customer retention');
    expect(within(unavailable).getByText('Not available: insufficient data')).toBeInTheDocument();
    expect(
      within(unavailable).getByText('A customer ID column on each order.'),
    ).toBeInTheDocument();
  });

  it('renders the SaaS spec with different widgets and validation states', async () => {
    renderBundle(saasFixture);

    expect(
      await within(region('MRR by plan')).findByText(
        /Stacked bar chart of Basic, Pro, Enterprise by month/,
      ),
    ).toBeInTheDocument();
    expect(
      await within(region('Active accounts')).findByText(/Area chart of active_accounts/),
    ).toBeInTheDocument();
    expect(
      await within(region('Seats vs MRR per account')).findByText(/Scatter plot/),
    ).toBeInTheDocument();

    const churn = region('Logo churn rate');
    expect(within(churn).getByText('Unverified')).toBeInTheDocument();
    expect(
      await within(churn).findByRole('button', { name: /3\.20%\. Open evidence$/ }),
    ).toBeInTheDocument();

    const ltv = region('Customer lifetime value');
    expect(within(ltv).getByText('Needs review')).toBeInTheDocument();
    expect(within(ltv).getByText(/numbers are not shown/)).toBeInTheDocument();
    expect(within(ltv).queryByText(/\$/)).not.toBeInTheDocument();
  });

  it('renders the agriculture spec including a heatmap table', async () => {
    renderBundle(agricultureFixture);
    const heat = region('Yield by district and crop (t/ha)');
    const table = await within(heat).findByRole('table');
    expect(within(table).getByRole('rowheader', { name: 'Onion' })).toBeInTheDocument();
    expect(within(table).getByRole('cell', { name: '27.1 t/ha' })).toBeInTheDocument();
    expect(screen.queryByText('Revenue')).not.toBeInTheDocument();
  });

  it('shows a safe error state for a spec that fails its contract', () => {
    renderBundle(invalidSpecFixture);
    expect(screen.getByText("This dashboard couldn't be displayed.")).toBeInTheDocument();
    expect(screen.getByText(/must have required property 'dashboard_id'/)).toBeInTheDocument();
    expect(
      screen.getByText("The list of possible analyses couldn't be displayed."),
    ).toBeInTheDocument();
  });

  it('shows placeholders for unknown and malformed widgets while the rest renders', async () => {
    renderBundle(unknownWidgetFixture);
    expect(screen.getByText('Widget not supported')).toBeInTheDocument();
    expect(screen.getByText(/type “gauge”/)).toBeInTheDocument();
    expect(screen.getByText('Widget could not be displayed')).toBeInTheDocument();
    expect(
      await screen.findByRole('button', { name: /^Row count: 1,200\. Open evidence$/ }),
    ).toBeInTheDocument();
  });
});

describe('Evidence drawer (≤2 clicks from any number)', () => {
  it('opens from a KPI value in one click, shows the evidence chain, then closes', async () => {
    const user = userEvent.setup();
    renderBundle(retailFixture);

    await user.click(
      await within(region('Revenue')).findByRole('button', {
        name: /^Revenue: ₹4,210,000\. Open evidence$/,
      }),
    );

    const drawer = await screen.findByRole('dialog', { name: 'Evidence' });
    expect(within(drawer).getByText('Revenue: ₹4,210,000')).toBeInTheDocument();
    expect(await within(drawer).findByText(/version dsv_retail_v4/)).toBeInTheDocument();
    expect(within(drawer).getByText('Sum of sales amount, June 2026')).toBeInTheDocument();
    expect(within(drawer).getByText('None (deterministic calculation)')).toBeInTheDocument();
    expect(within(drawer).getByText('High confidence')).toBeInTheDocument();
    expect(within(drawer).getByRole('list', { name: 'Provenance chain' })).toBeInTheDocument();
    expect(
      within(drawer).getByText('SELECT SUM(sales_amount) FROM sales WHERE month = ?'),
    ).toBeInTheDocument();

    await user.click(within(drawer).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it("opens from a chart's evidence link", async () => {
    const user = userEvent.setup();
    renderBundle(retailFixture);
    await user.click(screen.getByRole('button', { name: 'Evidence for Revenue by month' }));
    const drawer = await screen.findByRole('dialog', { name: 'Evidence' });
    expect(await within(drawer).findByText('Sum of sales amount per month')).toBeInTheDocument();
  });

  it("opens from an insight's evidence link and shows the model", async () => {
    const user = userEvent.setup();
    renderBundle(agricultureFixture);
    const insight = screen.getByRole('article', { name: 'Insight' });
    await user.click(within(insight).getAllByRole('button', { name: /Evidence for/ })[0]);
    const drawer = await screen.findByRole('dialog', { name: 'Evidence' });
    expect(await within(drawer).findByText('iqr · version 1.0')).toBeInTheDocument();
  });
});

describe('Chart accessibility fallback', () => {
  it('has a keyboard-operable data table toggle including forecast estimate columns', async () => {
    const user = userEvent.setup();
    renderBundle(retailFixture);
    const trend = region('Revenue by month');
    const toggle = await within(trend).findByRole('button', { name: 'Show data table' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');

    toggle.focus();
    await user.keyboard('{Enter}');

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    const table = within(trend).getByRole('table', { name: 'Revenue by month (data)' });
    expect(
      within(table).getByRole('columnheader', { name: 'Forecast (estimate)' }),
    ).toBeInTheDocument();
    expect(within(table).getByRole('cell', { name: '2026-09' })).toBeInTheDocument();
    expect(
      // The summary describes observed data only; forecast points are not counted as data.
      within(trend).getByRole('img', { name: /Line chart of revenue by month: 6 data points/ }),
    ).toBeInTheDocument();
  });
});

describe('Insights and executive summary', () => {
  it('shows only VALID insights with observed and possible explanation separated', () => {
    renderBundle(retailFixture);
    expect(
      screen.getByText('March 2026 revenue was the lowest of the half-year.'),
    ).toBeInTheDocument();
    expect(
      screen.queryByText('This unverified insight must not be shown.'),
    ).not.toBeInTheDocument();
    const insight = screen.getByRole('article', { name: 'Insight' });
    expect(within(insight).getByText('Observed')).toBeInTheDocument();
    expect(
      within(insight).getByText(/Possible explanation \(largest contributors, not proven causes\)/),
    ).toBeInTheDocument();
  });

  it('withholds summary statements that need review', () => {
    renderBundle(retailFixture);
    const summary = region('Executive summary');
    expect(within(summary).getByText('AI interpretation')).toBeInTheDocument();
    expect(within(summary).queryByText(/Returns may have lowered/)).not.toBeInTheDocument();
    expect(within(summary).getByText(/1 statement is held for review/)).toBeInTheDocument();
  });
});

describe('Capability report', () => {
  it('lists available and unavailable analyses with the missing requirement', () => {
    renderBundle(saasFixture);
    const report = region('Analyses for this data');
    expect(within(report).getByText('Correlation')).toBeInTheDocument();
    expect(within(report).getAllByText('Insufficient data')).toHaveLength(2);
    expect(
      within(report).getByText('Needs at least 12 months of history; you have 4.'),
    ).toBeInTheDocument();
    expect(within(report).getByText('At least 8 more months of history.')).toBeInTheDocument();
  });

  const REPORT = {
    schema_version: '1.0',
    dataset_version_id: 'dsv_test',
    semantic_model_version: 1,
    rules_version: 'capability-rules@1',
    available: ['kpi', 'customer_segmentation'],
    unavailable: [
      {
        analysis: 'churn_analysis',
        reason_code: 'NO_CUSTOMER_IDENTIFIER',
        message: 'Needs a Customer identifier column.',
        would_enable: 'a customer id',
      },
      {
        analysis: 'churn_analysis',
        reason_code: 'NO_RETENTION_HISTORY',
        message: 'Needs repeated, dated activity per customer.',
      },
    ],
    attempted: [
      {
        analysis: 'customer_segmentation',
        reason_code: 'LOW_MODEL_QUALITY',
        message: 'No segmentation passed the quality gate.',
      },
    ],
  };

  it('lists an analysis once with every unmet requirement (U-2)', () => {
    render(<CapabilityReportView report={REPORT} />);
    const report = region('Analyses for this data');
    expect(within(report).getAllByText('Churn analysis')).toHaveLength(1);
    expect(within(report).getByText('Needs a Customer identifier column.')).toBeInTheDocument();
    expect(
      within(report).getByText('Needs repeated, dated activity per customer.'),
    ).toBeInTheDocument();
  });

  it('marks an available analysis the last run could not use (U-4)', () => {
    render(<CapabilityReportView report={REPORT} />);
    const report = region('Analyses for this data');
    const item = within(report).getByText('Customer segmentation').closest('li');
    expect(item).not.toBeNull();
    expect(item?.textContent).toContain('Attempted, not usable on this data');
    expect(item?.textContent).toContain('No segmentation passed the quality gate.');
    expect(within(report).getByText('Kpi').closest('li')?.textContent).not.toContain('Attempted');
  });
});

describe('automated WCAG checks (axe-core, TASK-UI-024)', () => {
  it.each([
    ['retail', retailFixture],
    ['saas', saasFixture],
    ['agriculture', agricultureFixture],
  ])('%s dashboard has no violations, data tables open', async (_name, bundle) => {
    const user = userEvent.setup();
    const { container } = renderBundle(bundle);
    for (const toggle of await screen.findAllByRole('button', { name: 'Show data table' })) {
      await user.click(toggle);
    }
    expect(await axeViolations(container)).toEqual([]);
  });

  it('the open evidence drawer has no violations', async () => {
    const user = userEvent.setup();
    renderBundle(retailFixture);
    await user.click(
      await within(region('Revenue')).findByRole('button', { name: /Open evidence$/ }),
    );
    const drawer = await screen.findByRole('dialog', { name: 'Evidence' });
    await within(drawer).findByText('Sum of sales amount, June 2026');
    expect(await axeViolations(drawer)).toEqual([]);
  });
});

describe('Demo route', () => {
  it('renders the fixture chosen in the URL and switches fixtures', async () => {
    const user = userEvent.setup();
    render(
      <QueryClientProvider client={createQueryClient()}>
        <MemoryRouter initialEntries={['/dashboards/demo?fixture=agriculture']}>
          <Routes>
            <Route path="/dashboards/demo" element={<DashboardDemoPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
    expect(screen.getByRole('region', { name: 'Farm segments' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'SaaS subscriptions (sample)' }));
    expect(screen.getByRole('region', { name: 'MRR forecast' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Farm segments' })).not.toBeInTheDocument();
  });
});
