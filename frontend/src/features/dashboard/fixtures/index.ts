import { agricultureFixture } from './agriculture';
import { byId, evidence, type FixtureBundle } from './build';
import { retailFixture } from './retail';
import { saasFixture } from './saas';

const V = 'dsv_edge_v1';

/** Spec that fails the envelope contract: no dashboard_id, layout is the wrong type. */
export const invalidSpecFixture: FixtureBundle = {
  label: 'Invalid spec (safe error state)',
  spec: {
    schema_version: '1.0',
    dataset_version_id: V,
    plan_id: 'pl_edge',
    generated_at: '2026-09-28T10:00:00Z',
    widgets: [],
    layout: 'not-a-layout',
  },
  capabilities: { broken: true },
  evidence: {},
  rows: {},
};

/** Valid envelope with an unknown widget type and a malformed KPI next to a valid one. */
export const unknownWidgetFixture: FixtureBundle = {
  label: 'Unknown + malformed widgets',
  spec: {
    schema_version: '1.0',
    dashboard_id: 'db_edge',
    dataset_version_id: V,
    plan_id: 'pl_edge',
    generated_at: '2026-09-28T10:00:00Z',
    widgets: [
      {
        id: 'w_ok',
        widget_type: 'kpi_card',
        title: 'Row count',
        metric: 'row_count',
        value_ref: { evidence_id: 'ev_edge_rows', field: 'result.value' },
        validation_status: 'VALID',
        operation_id: 'E1-S1',
      },
      { id: 'w_gauge', widget_type: 'gauge', title: 'A future widget', value: 3 },
      {
        id: 'w_bad',
        widget_type: 'kpi_card',
        title: 'Missing its evidence reference',
        metric: 'x',
        validation_status: 'VALID',
        operation_id: 'E1-S2',
      },
    ],
    layout: { columns: 12, items: [] },
  },
  capabilities: {
    schema_version: '1.0',
    dataset_version_id: V,
    semantic_model_version: 1,
    rules_version: '1.0',
    available: ['kpi_summary'],
    unavailable: [
      {
        analysis: 'trend',
        reason_code: 'NO_TIME_DIMENSION',
        message: 'No date or time column was found.',
        would_enable: 'A date column.',
      },
    ],
  },
  evidence: byId([
    evidence('ev_edge_rows', V, { value: 1200, operation: 'kpi.count', calculation: 'Row count' }),
  ]),
  rows: {},
};

export const demoFixtures: Record<string, FixtureBundle> = {
  retail: retailFixture,
  saas: saasFixture,
  agriculture: agricultureFixture,
  unknown: unknownWidgetFixture,
  invalid: invalidSpecFixture,
};
