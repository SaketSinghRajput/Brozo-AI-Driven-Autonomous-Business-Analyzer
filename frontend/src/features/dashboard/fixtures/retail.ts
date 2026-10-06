import { byId, evidence, layoutItem, type FixtureBundle } from './build';

const V = 'dsv_retail_v4';
const months = ['2026-01', '2026-02', '2026-03', '2026-04', '2026-05', '2026-06'];
const monthly = [4100000, 3950000, 3610000, 4480000, 4020000, 4210000];

export const retailFixture: FixtureBundle = {
  label: 'Retail sales (sample)',
  spec: {
    schema_version: '1.1',
    dashboard_id: 'db_retail',
    dataset_version_id: V,
    plan_id: 'pl_retail',
    generated_at: '2026-09-28T10:00:00Z',
    filters: [{ id: 'f_region', dimension: 'region', type: 'multi_select' }],
    widgets: [
      {
        id: 'w_revenue',
        widget_type: 'kpi_card',
        title: 'Revenue',
        metric: 'revenue',
        value_ref: { evidence_id: 'ev_rt_revenue', field: 'result.value' },
        comparison_ref: {
          evidence_id: 'ev_rt_revenue_prev',
          field: 'result.value',
          label: 'May 2026',
        },
        format: { unit: 'currency:INR', currency: 'INR', precision: 0, locale: 'en-IN' },
        validation_status: 'VALID',
        operation_id: 'A1-S1',
        query: { metric: 'revenue' },
      },
      {
        id: 'w_orders',
        widget_type: 'kpi_card',
        title: 'Orders',
        metric: 'orders',
        value_ref: { evidence_id: 'ev_rt_orders', field: 'result.value' },
        validation_status: 'VALID',
        operation_id: 'A1-S2',
      },
      {
        id: 'w_aov',
        widget_type: 'kpi_card',
        title: 'Average order value',
        metric: 'aov',
        value_ref: { evidence_id: 'ev_rt_aov', field: 'result.value' },
        validation_status: 'UNCERTAIN',
        operation_id: 'A1-S3',
      },
      {
        id: 'w_trend',
        widget_type: 'line_chart',
        title: 'Revenue by month',
        validation_status: 'VALID',
        operation_id: 'A2-S1',
        chart: {
          type: 'line',
          x: { field: 'month', type: 'time' },
          y: [{ field: 'revenue', type: 'numeric', unit: 'currency:INR' }],
          aggregation: 'sum',
          source_operation: 'A2-S1',
          evidence_id: 'ev_rt_trend',
          format: { unit: 'currency:INR', currency: 'INR', precision: 0, locale: 'en-IN' },
          overlays: [
            { type: 'forecast_band', evidence_id: 'ev_rt_forecast' },
            { type: 'anomaly_markers', evidence_id: 'ev_rt_anomaly' },
          ],
        },
      },
      {
        id: 'w_region',
        widget_type: 'bar_chart',
        title: 'Revenue by region',
        validation_status: 'VALID',
        operation_id: 'A3-S1',
        query: {
          metric: 'revenue',
          dimensions: ['region'],
          filters: [{ dimension: 'month', op: 'eq', value: '2026-06' }],
        },
        chart: {
          type: 'bar',
          x: { field: 'region', type: 'category' },
          y: [{ field: 'revenue', type: 'numeric', unit: 'currency:INR' }],
          aggregation: 'sum',
          source_operation: 'A3-S1',
          evidence_id: 'ev_rt_region',
          format: { unit: 'currency:INR', currency: 'INR', precision: 0, locale: 'en-IN' },
          filters: [{ dimension: 'month', op: 'eq', value: '2026-06' }],
        },
      },
      {
        id: 'w_products',
        widget_type: 'table',
        title: 'Top products',
        validation_status: 'VALID',
        operation_id: 'A4-S1',
        chart: {
          type: 'table',
          x: { field: 'product', type: 'category' },
          y: [
            { field: 'revenue', type: 'numeric', unit: 'currency:INR' },
            { field: 'units', type: 'numeric' },
          ],
          aggregation: 'sum',
          source_operation: 'A4-S1',
          evidence_id: 'ev_rt_products',
        },
      },
      {
        id: 'w_churn',
        widget_type: 'unavailable',
        title: 'Customer retention',
        reason_code: 'NO_CUSTOMER_IDENTIFIER',
        message: 'The dataset has no column that identifies a customer across orders.',
        would_enable: 'A customer ID column on each order.',
      },
    ],
    summary: [
      {
        text: 'Revenue in June 2026 was higher than in May 2026.',
        evidence_ids: ['ev_rt_revenue', 'ev_rt_revenue_prev'],
        validation_status: 'VALID',
      },
      {
        text: 'April 2026 revenue was unusually high for this period.',
        evidence_ids: ['ev_rt_anomaly'],
        validation_status: 'VALID',
      },
      {
        text: 'Returns may have lowered net revenue in March.',
        evidence_ids: ['ev_rt_trend'],
        validation_status: 'REQUIRES_REVIEW',
      },
    ],
    insights: [
      {
        id: 'in_rt_1',
        validation_status: 'VALID',
        observed: {
          text: 'March 2026 revenue was the lowest of the half-year.',
          evidence_ids: ['ev_rt_trend'],
        },
        possible_explanations: [
          { text: 'Fewer orders in the North region in March', evidence_ids: ['ev_rt_region'] },
        ],
      },
      {
        id: 'in_rt_hidden',
        validation_status: 'UNCERTAIN',
        observed: {
          text: 'This unverified insight must not be shown.',
          evidence_ids: ['ev_rt_trend'],
        },
        possible_explanations: [],
      },
    ],
    layout: {
      columns: 12,
      items: [
        layoutItem('w_revenue', 0, 0, 4),
        layoutItem('w_orders', 4, 0, 4),
        layoutItem('w_aov', 8, 0, 4),
        layoutItem('w_trend', 0, 1, 8, 2),
        layoutItem('w_region', 8, 1, 4, 2),
        layoutItem('w_products', 0, 3, 8),
        layoutItem('w_churn', 8, 3, 4),
      ],
    },
  },
  dimensionValues: { region: ['East', 'North', 'South', 'West'] },
  capabilities: {
    schema_version: '1.0',
    dataset_version_id: V,
    semantic_model_version: 1,
    rules_version: '1.0',
    available: ['kpi_summary', 'trend', 'forecast', 'anomaly_detection', 'breakdown'],
    unavailable: [
      {
        analysis: 'retention',
        reason_code: 'NO_CUSTOMER_IDENTIFIER',
        message: 'No column identifies a customer across orders.',
        would_enable: 'A customer ID column on each order.',
      },
    ],
  },
  evidence: byId([
    evidence('ev_rt_revenue', V, {
      value: 4210000,
      unit: 'currency:INR',
      operation: 'kpi.sum',
      calculation: 'Sum of sales amount, June 2026',
      args: { period: '2026-06', measure: 'sales_amount' },
      sql: 'SELECT SUM(sales_amount) FROM sales WHERE month = ?',
    }),
    evidence('ev_rt_revenue_prev', V, {
      value: 4020000,
      unit: 'currency:INR',
      operation: 'kpi.sum',
      calculation: 'Sum of sales amount, May 2026',
      args: { period: '2026-05' },
    }),
    evidence('ev_rt_orders', V, {
      value: 18342,
      operation: 'kpi.count_distinct',
      calculation: 'Distinct order IDs, June 2026',
      args: { period: '2026-06' },
    }),
    evidence('ev_rt_aov', V, {
      value: 229.53,
      unit: 'currency:INR',
      operation: 'kpi.ratio',
      calculation: 'Revenue divided by orders, June 2026',
      confidence: { level: 'low', score: 0.4, basis: ['12% of orders have no amount'] },
    }),
    evidence('ev_rt_trend', V, {
      rows: 6,
      operation: 'trend.by_period',
      calculation: 'Sum of sales amount per month',
    }),
    evidence('ev_rt_region', V, {
      rows: 4,
      operation: 'breakdown.by_dimension',
      calculation: 'Sum of sales amount per region, June 2026',
    }),
    evidence('ev_rt_products', V, {
      rows: 5,
      operation: 'breakdown.top_n',
      calculation: 'Top 5 products by sales amount',
    }),
    evidence('ev_rt_forecast', V, {
      rows: 3,
      operation: 'ml.forecast',
      calculation: '3-month forecast with 80% interval',
      model: { model_id: 'ets', model_version: 'statsmodels-0.14' },
      confidence: { level: 'medium', score: 0.6, basis: ['Backtest error within baseline'] },
    }),
    evidence('ev_rt_anomaly', V, {
      rows: 1,
      operation: 'ml.anomaly',
      calculation: 'Robust z-score on monthly revenue',
      model: { model_id: 'robust_z', model_version: '1.0' },
    }),
  ]),
  rows: {
    ev_rt_trend: months.map((month, i) => ({ month, revenue: monthly[i] })),
    ev_rt_region: [
      { region: 'North', revenue: 1320000 },
      { region: 'South', revenue: 1180000 },
      { region: 'East', revenue: 910000 },
      { region: 'West', revenue: 800000 },
    ],
    ev_rt_products: [
      { product: 'Tea 500g', revenue: 612000, units: 5100 },
      { product: 'Rice 5kg', revenue: 540000, units: 1800 },
      { product: 'Oil 1L', revenue: 431000, units: 2870 },
      { product: 'Soap bar', revenue: 220000, units: 7330 },
      { product: 'Biscuits', revenue: 198000, units: 9900 },
    ],
    // Overlay rows as GET /evidence/{id}/data serves them (forecast points; flattened anomalies).
    ev_rt_forecast: [
      { period: '2026-07', value: 4260000, lower: 3900000, upper: 4620000 },
      { period: '2026-08', value: 4300000, lower: 3800000, upper: 4800000 },
      { period: '2026-09', value: 4350000, lower: 3700000, upper: 5000000 },
    ],
    ev_rt_anomaly: [
      {
        'subject.period': '2026-04',
        metric: 'revenue',
        observed: 4480000,
        expected: 3980000,
        score: 0.82,
        direction: 'high',
      },
    ],
  },
};
