import { describe, expect, it } from 'vitest';
import type { ChartWidget } from '../../../contracts/spec';
import type { DataRow } from '../dataSource';
import { BAND_KEY, FORECAST_KEY, anomalyMarkers, toChartData, withForecast } from './chartData';

const chart: ChartWidget['chart'] = {
  type: 'line',
  x: { field: 'm' },
  y: [{ field: 'v' }],
  aggregation: 'sum',
  source_operation: 'op',
  evidence_id: 'ev_1',
};

describe('chart data reshaping', () => {
  it('pivots long rows to one column per series value', () => {
    const data = toChartData({ ...chart, series: { field: 's' } }, [
      { m: '2026-01', s: 'A', v: 1 },
      { m: '2026-01', s: 'B', v: 2 },
      { m: '2026-02', s: 'A', v: 3 },
    ]);
    expect(data.series.map((series) => series.key)).toEqual(['A', 'B']);
    expect(data.rows).toEqual([
      { m: '2026-01', A: 1, B: 2 },
      { m: '2026-02', A: 3 },
    ]);
  });

  it('appends forecast rows with bands and joins the estimate line to the last observation', () => {
    const forecast = [
      { period: '2026-03', value: 5, lower: 4, upper: 6 },
      { period: '2026-04', value: null, lower: null, upper: null },
    ];
    const data = withForecast(toChartData(chart, [{ m: '2026-02', v: 3 }]), forecast);
    expect(data.rows).toEqual([
      { m: '2026-02', v: 3, [FORECAST_KEY]: 3 },
      { m: '2026-03', [FORECAST_KEY]: 5, [BAND_KEY]: [4, 6] },
    ]);
  });

  it("places anomaly markers by the flattened subject's x-field value", () => {
    const rows: DataRow[] = [
      { 'subject.m': '2026-02', observed: 9, expected: 3, direction: 'high' },
      { 'subject.other': 1, observed: 1, expected: null, direction: 'low' },
    ];
    expect(anomalyMarkers({ field: 'm' }, rows)).toEqual([
      { x: '2026-02', observed: 9, expected: 3, direction: 'high' },
    ]);
  });

  it('falls back to subject.period on a time axis', () => {
    const rows = [{ 'subject.period': '2026-03-01', observed: 2, expected: 5, direction: 'low' }];
    expect(anomalyMarkers({ field: 'order_date', type: 'time' }, rows)).toEqual([
      { x: '2026-03-01', observed: 2, expected: 5, direction: 'low' },
    ]);
    expect(anomalyMarkers({ field: 'region', type: 'category' }, rows)).toEqual([]);
  });
});
