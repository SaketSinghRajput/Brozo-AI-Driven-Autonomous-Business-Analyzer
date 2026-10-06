import { byId, evidence, layoutItem, type FixtureBundle } from './build';

const V = 'dsv_farm_v1';
const districts = ['Pune', 'Nashik', 'Satara'];
const crops: Array<[string, number[]]> = [
  ['Wheat', [3.1, 2.8, 3.4]],
  ['Soybean', [1.9, 2.2, 1.7]],
  ['Onion', [24.5, 27.1, 22.0]],
];
const weeks = ['2026-W18', '2026-W19', '2026-W20', '2026-W21', '2026-W22', '2026-W23'];
const prices = [2150, 2180, 2165, 2710, 2240, 2260];

export const agricultureFixture: FixtureBundle = {
  label: 'Farm production (sample)',
  spec: {
    schema_version: '1.1',
    dashboard_id: 'db_farm',
    dataset_version_id: V,
    plan_id: 'pl_farm',
    generated_at: '2026-09-28T10:00:00Z',
    widgets: [
      {
        id: 'w_yield',
        widget_type: 'kpi_card',
        title: 'Average wheat yield',
        metric: 'yield',
        value_ref: { evidence_id: 'ev_ag_yield', field: 'result.value' },
        validation_status: 'VALID',
        operation_id: 'C1-S1',
      },
      {
        id: 'w_cost',
        widget_type: 'kpi_card',
        title: 'Input cost per hectare',
        metric: 'input_cost',
        value_ref: { evidence_id: 'ev_ag_cost', field: 'result.value' },
        comparison_ref: {
          evidence_id: 'ev_ag_cost_prev',
          field: 'result.value',
          label: 'last season',
        },
        validation_status: 'VALID',
        operation_id: 'C1-S2',
      },
      {
        id: 'w_heat',
        widget_type: 'heatmap',
        title: 'Yield by district and crop (t/ha)',
        validation_status: 'VALID',
        operation_id: 'C2-S1',
        chart: {
          type: 'heatmap',
          x: { field: 'district', type: 'category' },
          y: [{ field: 'yield_t_ha', type: 'numeric', unit: 't/ha' }],
          series: { field: 'crop', type: 'category' },
          aggregation: 'avg',
          source_operation: 'C2-S1',
          evidence_id: 'ev_ag_heat',
          format: { unit: 't/ha', precision: 1, locale: 'en-IN' },
        },
      },
      {
        id: 'w_price',
        widget_type: 'line_chart',
        title: 'Market price of wheat by week',
        validation_status: 'VALID',
        operation_id: 'C3-S1',
        chart: {
          type: 'line',
          x: { field: 'week', type: 'time' },
          y: [{ field: 'price', type: 'numeric', unit: 'currency:INR' }],
          aggregation: 'avg',
          source_operation: 'C3-S1',
          evidence_id: 'ev_ag_price',
          format: { unit: 'currency:INR', currency: 'INR', precision: 0, locale: 'en-IN' },
          overlays: [{ type: 'anomaly_markers', evidence_id: 'ev_ag_price_anomaly' }],
        },
      },
      {
        id: 'w_prod',
        widget_type: 'bar_chart',
        title: 'Production by crop (tonnes)',
        validation_status: 'VALID',
        operation_id: 'C4-S1',
        chart: {
          type: 'bar',
          x: { field: 'crop', type: 'category' },
          y: [{ field: 'tonnes', type: 'numeric', unit: 't' }],
          aggregation: 'sum',
          source_operation: 'C4-S1',
          evidence_id: 'ev_ag_prod',
          format: { unit: 't', precision: 0, locale: 'en-IN' },
        },
      },
      {
        id: 'w_segments',
        widget_type: 'unavailable',
        title: 'Farm segments',
        reason_code: 'INSUFFICIENT_ENTITIES',
        message: 'Grouping farms needs at least 30 farms; this data has 12.',
        would_enable: 'Records for at least 18 more farms.',
      },
    ],
    summary: [
      {
        text: 'Nashik had the highest onion yield of the three districts.',
        evidence_ids: ['ev_ag_heat'],
        validation_status: 'VALID',
      },
    ],
    insights: [
      {
        id: 'in_ag_1',
        validation_status: 'VALID',
        observed: {
          text: 'Wheat prices were unusually high in week 21 of 2026.',
          evidence_ids: ['ev_ag_price_anomaly'],
        },
        possible_explanations: [
          { text: 'Lower wheat yield in Nashik this season', evidence_ids: ['ev_ag_heat'] },
        ],
      },
    ],
    layout: {
      columns: 12,
      items: [
        layoutItem('w_yield', 0, 0, 6),
        layoutItem('w_cost', 6, 0, 6),
        layoutItem('w_heat', 0, 1, 12),
        layoutItem('w_price', 0, 2, 7, 2),
        layoutItem('w_prod', 7, 2, 5, 2),
        layoutItem('w_segments', 0, 4, 12),
      ],
    },
  },
  capabilities: {
    schema_version: '1.0',
    dataset_version_id: V,
    semantic_model_version: 1,
    rules_version: '1.0',
    available: ['kpi_summary', 'breakdown', 'anomaly_detection'],
    unavailable: [
      {
        analysis: 'segmentation',
        reason_code: 'INSUFFICIENT_ENTITIES',
        message: 'Needs at least 30 farms; this data has 12.',
        would_enable: 'Records for at least 18 more farms.',
      },
      {
        analysis: 'forecast',
        reason_code: 'IRREGULAR_TIME',
        message: 'Harvest dates are irregular, so no regular time grain exists.',
        would_enable: 'Consistent weekly or monthly records.',
      },
    ],
  },
  evidence: byId([
    evidence('ev_ag_yield', V, {
      value: 3.1,
      unit: 't/ha',
      operation: 'kpi.avg',
      calculation: 'Average of wheat yield per hectare across farms',
      args: { crop: 'Wheat', season: 'Rabi 2026' },
    }),
    evidence('ev_ag_cost', V, {
      value: 41250,
      unit: 'currency:INR',
      operation: 'kpi.ratio',
      calculation: 'Total input cost divided by hectares sown, this season',
    }),
    evidence('ev_ag_cost_prev', V, {
      value: 43800,
      unit: 'currency:INR',
      operation: 'kpi.ratio',
      calculation: 'Same, last season',
    }),
    evidence('ev_ag_heat', V, {
      rows: 9,
      operation: 'breakdown.by_two_dimensions',
      calculation: 'Average yield per district and crop',
    }),
    evidence('ev_ag_price', V, {
      rows: 6,
      operation: 'trend.by_period',
      calculation: 'Average weekly market price of wheat per quintal',
    }),
    evidence('ev_ag_price_anomaly', V, {
      rows: 1,
      operation: 'ml.anomaly',
      calculation: 'Interquartile-range rule on weekly price',
      model: { model_id: 'iqr', model_version: '1.0' },
    }),
    evidence('ev_ag_prod', V, {
      rows: 3,
      operation: 'breakdown.by_dimension',
      calculation: 'Sum of harvested tonnes per crop',
    }),
  ]),
  rows: {
    ev_ag_heat: crops.flatMap(([crop, values]) =>
      districts.map((district, i) => ({ district, crop, yield_t_ha: values[i] })),
    ),
    ev_ag_price: weeks.map((week, i) => ({ week, price: prices[i] })),
    ev_ag_prod: [
      { crop: 'Wheat', tonnes: 1840 },
      { crop: 'Soybean', tonnes: 960 },
      { crop: 'Onion', tonnes: 12400 },
    ],
    ev_ag_price_anomaly: [
      {
        'subject.week': '2026-W21',
        metric: 'price',
        observed: 2710,
        expected: 2190,
        score: 0.9,
        direction: 'high',
      },
    ],
  },
};
