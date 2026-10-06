import type { ChartWidget } from '../../../contracts/spec';
import type { DataRow } from '../dataSource';

/** Keys for overlay values merged into chart rows (prefixed so they cannot clash with fields). */
export const FORECAST_KEY = '__forecast';
export const BAND_KEY = '__band';

export interface Series {
  key: string;
  label: string;
}

export interface ChartData {
  xKey: string | null;
  rows: Array<Record<string, unknown>>;
  series: Series[];
}

/**
 * Reshape tool rows for plotting. Pure presentation reshaping - no values are computed.
 * With a `series` field the long rows (x, series, y) are pivoted to one column per series value.
 */
export function toChartData(chart: ChartWidget['chart'], rows: DataRow[]): ChartData {
  const xKey = chart.x?.field ?? null;
  const seriesField = chart.series?.field;
  const yField = chart.y[0].field;

  if (!seriesField || !xKey) {
    return {
      xKey,
      rows: rows.map((row) => ({ ...row })),
      series: chart.y.map((y) => ({ key: y.field, label: y.field })),
    };
  }

  const byX = new Map<string, Record<string, unknown>>();
  const seriesKeys: string[] = [];
  for (const row of rows) {
    const x = String(row[xKey]);
    const seriesValue = String(row[seriesField]);
    if (!seriesKeys.includes(seriesValue)) seriesKeys.push(seriesValue);
    const target = byX.get(x) ?? { [xKey]: row[xKey] };
    target[seriesValue] = row[yField];
    byX.set(x, target);
  }
  return {
    xKey,
    rows: [...byX.values()],
    series: seriesKeys.map((key) => ({ key, label: key })),
  };
}

function isNumber(value: unknown): value is number {
  return typeof value === 'number';
}

/**
 * Append forecast rows (period, value, lower, upper) after the observed rows. The last observed
 * point also gets the forecast key so the dashed estimate line starts where the observed line
 * ends. Rows without a numeric value are skipped.
 */
export function withForecast(data: ChartData, forecast: DataRow[] | undefined): ChartData {
  if (!forecast?.length || !data.xKey || data.series.length !== 1) return data;
  const xKey = data.xKey;
  const valueKey = data.series[0].key;
  const rows = data.rows.map((row) => ({ ...row }));
  const last = rows.at(-1);
  if (last) last[FORECAST_KEY] = last[valueKey];
  for (const point of forecast) {
    if (!isNumber(point.value) || point.period === undefined || point.period === null) continue;
    rows.push({
      [xKey]: point.period,
      [FORECAST_KEY]: point.value,
      [BAND_KEY]:
        isNumber(point.lower) && isNumber(point.upper) ? [point.lower, point.upper] : undefined,
    });
  }
  return { ...data, rows };
}

export interface AnomalyMarker {
  x: string;
  observed: number;
  expected: number | null;
  direction: 'high' | 'low';
}

/**
 * Anomaly markers positioned on the x axis via the anomaly's flattened subject: `subject.<x>`,
 * or `subject.period` on a time axis (time-series detectors name their subject "period").
 */
export function anomalyMarkers(
  x: { field: string; type?: string | null } | null | undefined,
  rows: DataRow[] | undefined,
): AnomalyMarker[] {
  if (!rows || !x) return [];
  const markers: AnomalyMarker[] = [];
  for (const row of rows) {
    const at = row[`subject.${x.field}`] ?? (x.type === 'time' ? row['subject.period'] : null);
    if (at === undefined || at === null || !isNumber(row.observed)) continue;
    markers.push({
      x: String(at),
      observed: row.observed,
      expected: isNumber(row.expected) ? row.expected : null,
      direction: row.direction === 'low' ? 'low' : 'high',
    });
  }
  return markers;
}
