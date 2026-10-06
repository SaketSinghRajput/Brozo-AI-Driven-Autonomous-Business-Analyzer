import { byId, evidence, layoutItem, type FixtureBundle } from './build';

const V = 'dsv_saas_v2';
const months = ['2026-03', '2026-04', '2026-05', '2026-06'];
const plans: Array<[string, number[]]> = [
  ['Basic', [21000, 22500, 23100, 24000]],
  ['Pro', [38000, 40100, 41800, 44200]],
  ['Enterprise', [52000, 52000, 56500, 58000]],
];

export const saasFixture: FixtureBundle = {
  label: 'SaaS subscriptions (sample)',
  spec: {
    schema_version: '1.1',
    dashboard_id: 'db_saas',
    dataset_version_id: V,
    plan_id: 'pl_saas',
    generated_at: '2026-09-28T10:00:00Z',
    widgets: [
      {
        id: 'w_mrr',
        widget_type: 'kpi_card',
        title: 'Monthly recurring revenue',
        metric: 'mrr',
        value_ref: { evidence_id: 'ev_ss_mrr', field: 'result.value' },
        comparison_ref: { evidence_id: 'ev_ss_mrr_prev', field: 'result.value', label: 'May 2026' },
        validation_status: 'VALID',
        operation_id: 'B1-S1',
      },
      {
        id: 'w_churn',
        widget_type: 'kpi_card',
        title: 'Logo churn rate',
        metric: 'churn_rate',
        value_ref: { evidence_id: 'ev_ss_churn', field: 'result.value' },
        validation_status: 'UNCERTAIN',
        operation_id: 'B1-S2',
      },
      {
        id: 'w_ltv',
        widget_type: 'kpi_card',
        title: 'Customer lifetime value',
        metric: 'ltv',
        value_ref: { evidence_id: 'ev_ss_ltv', field: 'result.value' },
        validation_status: 'REQUIRES_REVIEW',
        operation_id: 'B1-S3',
      },
      {
        id: 'w_mrr_plan',
        widget_type: 'stacked_bar',
        title: 'MRR by plan',
        validation_status: 'VALID',
        operation_id: 'B2-S1',
        chart: {
          type: 'stacked_bar',
          x: { field: 'month', type: 'time' },
          y: [{ field: 'mrr', type: 'numeric', unit: 'currency:USD' }],
          series: { field: 'plan', type: 'category' },
          aggregation: 'sum',
          source_operation: 'B2-S1',
          evidence_id: 'ev_ss_mrr_plan',
          format: { unit: 'currency:USD', currency: 'USD', precision: 0, locale: 'en-US' },
        },
      },
      {
        id: 'w_accounts',
        widget_type: 'area_chart',
        title: 'Active accounts',
        validation_status: 'VALID',
        operation_id: 'B3-S1',
        chart: {
          type: 'area',
          x: { field: 'month', type: 'time' },
          y: [{ field: 'active_accounts', type: 'numeric' }],
          aggregation: 'count_distinct',
          source_operation: 'B3-S1',
          evidence_id: 'ev_ss_accounts',
        },
      },
      {
        id: 'w_seats',
        widget_type: 'scatter',
        title: 'Seats vs MRR per account',
        validation_status: 'VALID',
        operation_id: 'B4-S1',
        chart: {
          type: 'scatter',
          x: { field: 'seats', type: 'numeric' },
          y: [{ field: 'mrr', type: 'numeric', unit: 'currency:USD' }],
          aggregation: 'none',
          source_operation: 'B4-S1',
          evidence_id: 'ev_ss_seats',
          format: { unit: 'currency:USD', currency: 'USD', precision: 0, locale: 'en-US' },
        },
      },
      {
        id: 'w_forecast',
        widget_type: 'unavailable',
        title: 'MRR forecast',
        reason_code: 'INSUFFICIENT_HISTORY',
        message: 'Forecasting needs at least 12 months of history; this data has 4.',
        would_enable: 'Upload at least 8 more months of subscription history.',
      },
    ],
    summary: [
      {
        text: 'MRR in June 2026 was higher than in May 2026.',
        evidence_ids: ['ev_ss_mrr', 'ev_ss_mrr_prev'],
        validation_status: 'VALID',
      },
      {
        text: 'Logo churn could not be fully confirmed because cancellation dates are incomplete.',
        evidence_ids: ['ev_ss_churn'],
        validation_status: 'UNCERTAIN',
      },
    ],
    insights: [
      {
        id: 'in_ss_1',
        validation_status: 'VALID',
        observed: {
          text: 'The Pro plan grew MRR in every month shown.',
          evidence_ids: ['ev_ss_mrr_plan'],
        },
        possible_explanations: [
          { text: 'More accounts active each month', evidence_ids: ['ev_ss_accounts'] },
          { text: 'Accounts with more seats pay more', evidence_ids: ['ev_ss_seats'] },
        ],
      },
    ],
    layout: {
      columns: 12,
      items: [
        layoutItem('w_mrr', 0, 0, 4),
        layoutItem('w_churn', 4, 0, 4),
        layoutItem('w_ltv', 8, 0, 4),
        layoutItem('w_mrr_plan', 0, 1, 6, 2),
        layoutItem('w_accounts', 6, 1, 6, 2),
        layoutItem('w_seats', 0, 3, 6, 2),
        layoutItem('w_forecast', 6, 3, 6),
      ],
    },
  },
  capabilities: {
    schema_version: '1.0',
    dataset_version_id: V,
    semantic_model_version: 2,
    rules_version: '1.0',
    available: ['kpi_summary', 'breakdown', 'correlation'],
    unavailable: [
      {
        analysis: 'forecast',
        reason_code: 'INSUFFICIENT_HISTORY',
        message: 'Needs at least 12 months of history; you have 4.',
        would_enable: 'At least 8 more months of history.',
      },
      {
        analysis: 'segmentation',
        reason_code: 'INSUFFICIENT_ENTITIES',
        message: 'Needs at least 50 accounts; this data has 31.',
        would_enable: null,
      },
    ],
  },
  evidence: byId([
    evidence('ev_ss_mrr', V, {
      value: 126200,
      unit: 'currency:USD',
      operation: 'kpi.sum',
      calculation: 'Sum of monthly plan price over active subscriptions, June 2026',
      args: { period: '2026-06' },
    }),
    evidence('ev_ss_mrr_prev', V, {
      value: 121400,
      unit: 'currency:USD',
      operation: 'kpi.sum',
      calculation: 'Same, May 2026',
      args: { period: '2026-05' },
    }),
    evidence('ev_ss_churn', V, {
      value: 0.032,
      unit: 'pct',
      operation: 'kpi.ratio',
      calculation: 'Cancelled accounts divided by accounts at start of month',
      confidence: { level: 'low', score: 0.35, basis: ['18% of cancellations lack a date'] },
    }),
    evidence('ev_ss_ltv', V, {
      value: 5400,
      unit: 'currency:USD',
      operation: 'kpi.ratio',
      calculation: 'Average MRR per account divided by churn rate',
    }),
    evidence('ev_ss_mrr_plan', V, {
      rows: 12,
      operation: 'breakdown.by_period_and_dimension',
      calculation: 'Sum of MRR per month and plan',
    }),
    evidence('ev_ss_accounts', V, {
      rows: 4,
      operation: 'trend.by_period',
      calculation: 'Distinct active accounts per month',
    }),
    evidence('ev_ss_seats', V, {
      rows: 8,
      operation: 'relationship.pairs',
      calculation: 'Seats and MRR per account, June 2026',
    }),
  ]),
  rows: {
    ev_ss_mrr_plan: plans.flatMap(([plan, values]) =>
      months.map((month, i) => ({ month, plan, mrr: values[i] })),
    ),
    ev_ss_accounts: months.map((month, i) => ({
      month,
      active_accounts: [212, 219, 226, 231][i],
    })),
    ev_ss_seats: [
      [5, 250],
      [12, 480],
      [20, 900],
      [8, 320],
      [40, 1700],
      [3, 150],
      [25, 1050],
      [60, 2300],
    ].map(([seats, mrr]) => ({ seats, mrr })),
  },
};
