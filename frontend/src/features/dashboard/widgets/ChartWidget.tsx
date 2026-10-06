import { useId, useState } from 'react';
import { ProvenanceChip, Skeleton } from '../../../components';
import type { ChartWidget as ChartWidgetSpec } from '../../../contracts/spec';
import type { FieldRef } from '../../../contracts/generated/dashboard_spec';
import { formatNumber, unitLabel, type NumberFormat } from '../../../lib/format';
import { useEvidence, useEvidenceRows, type DataRow } from '../dataSource';
import { ChartCanvas, type PlotKind } from './ChartCanvas';
import {
  BAND_KEY,
  FORECAST_KEY,
  anomalyMarkers,
  toChartData,
  withForecast,
  type AnomalyMarker,
  type ChartData,
} from './chartData';
import { DataTable, HeatmapTable, type Column } from './DataTable';
import { WidgetFrame, provenanceOf } from './WidgetFrame';

const PLOT_KIND: Record<ChartWidgetSpec['widget_type'], PlotKind | 'table' | 'heatmap'> = {
  line_chart: 'line',
  bar_chart: 'bar',
  stacked_bar: 'stacked_bar',
  area_chart: 'area',
  scatter: 'scatter',
  table: 'table',
  heatmap: 'heatmap',
};

const KIND_LABEL: Record<PlotKind, string> = {
  line: 'Line chart',
  bar: 'Bar chart',
  stacked_bar: 'Stacked bar chart',
  area: 'Area chart',
  scatter: 'Scatter plot',
};

function summarize(kind: PlotKind, data: ChartData, format: NumberFormat): string {
  const yLabels = data.series.map((series) => series.label).join(', ');
  const by = data.xKey ? ` by ${data.xKey}` : '';
  const valueKey = data.series[0]?.key;
  const describe = (row: Record<string, unknown> | undefined) => {
    const value = valueKey ? row?.[valueKey] : undefined;
    const x = data.xKey ? String(row?.[data.xKey]) : '';
    return typeof value === 'number' ? `${formatNumber(value, format)} at ${x}` : x;
  };
  const range =
    data.rows.length > 1 && kind !== 'scatter'
      ? `, from ${describe(data.rows[0])} to ${describe(data.rows.at(-1))}`
      : '';
  return `${KIND_LABEL[kind]} of ${yLabels}${by}: ${data.rows.length} data points${range}.`;
}

function tableColumns(data: ChartData, hasForecast: boolean, y: FieldRef[]): Column[] {
  const columns: Column[] = data.xKey ? [{ key: data.xKey, label: data.xKey }] : [];
  // Several y fields (tables): each column carries its own unit; one y: the chart's format.
  const units = new Map(y.length > 1 ? y.map((ref) => [ref.field, ref.unit ?? null]) : []);
  columns.push(
    ...data.series.map((series) => ({
      key: series.key,
      label: series.label,
      unit: units.get(series.key),
    })),
  );
  if (hasForecast) {
    columns.push(
      { key: FORECAST_KEY, label: 'Forecast (estimate)' },
      { key: '__lower', label: 'Estimate lower' },
      { key: '__upper', label: 'Estimate upper' },
    );
  }
  return columns;
}

function flattenBand(rows: ChartData['rows']): ChartData['rows'] {
  return rows.map((row) => {
    const band = row[BAND_KEY];
    return Array.isArray(band) ? { ...row, __lower: band[0], __upper: band[1] } : row;
  });
}

/**
 * Chart card rendered from a ChartSpec (design.md §27, UI-UX §21). With `onDrill`, clicking a
 * bar or point (or choosing a value in the drill-down picker) filters the dashboard by that x.
 */
export function ChartWidget({
  widget,
  versionId,
  onDrill,
}: {
  widget: ChartWidgetSpec;
  versionId: string;
  onDrill?: (value: string | number) => void;
}) {
  const { chart } = widget;
  const kind = PLOT_KIND[widget.widget_type];
  const forecastRef = chart.overlays?.find((overlay) => overlay.type === 'forecast_band');
  const anomalyRef = chart.overlays?.find((overlay) => overlay.type === 'anomaly_markers');

  const rows = useEvidenceRows(versionId, chart.evidence_id);
  const evidence = useEvidence(chart.evidence_id);
  const forecast = useEvidenceRows(versionId, forecastRef?.evidence_id);
  const anomalies = useEvidenceRows(versionId, anomalyRef?.evidence_id);

  const format: NumberFormat = {
    unit: chart.format?.unit ?? chart.y[0].unit,
    precision: chart.format?.precision,
    currency: chart.format?.currency,
    locale: chart.format?.locale,
  };
  const filters = (chart.filters ?? [])
    .map(
      (filter) =>
        `${filter.dimension} ${filter.op === 'eq' ? '=' : filter.op} ${
          Array.isArray(filter.value) ? filter.value.join(', ') : String(filter.value)
        }`,
    )
    .join(' · ');

  return (
    <WidgetFrame
      title={widget.title}
      status={widget.validation_status}
      provenance={provenanceOf(evidence.data)}
      evidence={{
        evidenceId: chart.evidence_id,
        claim: widget.title,
        status: widget.validation_status,
      }}
      footer={
        <>
          Data version {versionId} · {chart.aggregation}
          {unitLabel(format) ? ` · ${unitLabel(format)}` : ''}
          {filters ? ` · ${filters}` : ''}
        </>
      }
    >
      {forecastRef ? (
        <p className="text-caption mb-2 flex flex-wrap items-center gap-2 text-text-muted">
          <ProvenanceChip kind="MODEL_OUTPUT" />
          <span>
            <strong className="font-semibold text-text">Estimate</strong>: the dashed line and
            shaded band are a forecast, not observed data.
          </span>
        </p>
      ) : null}
      {rows.isPending ? <Skeleton variant="chart" /> : null}
      {rows.isError ? (
        <p className="text-body text-error">Chart data could not be loaded.</p>
      ) : null}
      {rows.data ? (
        <ChartBody
          kind={kind}
          title={widget.title}
          observed={toChartData(chart, rows.data)}
          forecast={forecast.data}
          seriesField={chart.series?.field}
          valueField={chart.y[0].field}
          y={chart.y}
          format={format}
          markers={anomalyMarkers(chart.x, anomalies.data)}
          rawRows={rows.data}
          onDrill={onDrill}
        />
      ) : null}
    </WidgetFrame>
  );
}

interface ChartBodyProps {
  kind: PlotKind | 'table' | 'heatmap';
  title: string;
  observed: ChartData;
  forecast: DataRow[] | undefined;
  seriesField: string | undefined;
  valueField: string;
  y: FieldRef[];
  format: NumberFormat;
  markers: AnomalyMarker[];
  rawRows: Array<Record<string, unknown>>;
  onDrill?: (value: string | number) => void;
}

/**
 * Keyboard-operable drill-down (the chart click is pointer-only): pick one x value to filter the
 * whole dashboard by it.
 */
function DrillPicker({
  data,
  onDrill,
}: {
  data: ChartData;
  onDrill: (value: string | number) => void;
}) {
  const xKey = data.xKey;
  if (!xKey) return null;
  const values = [
    ...new Set(
      data.rows
        .map((row) => row[xKey])
        .filter((v): v is string | number => typeof v === 'string' || typeof v === 'number'),
    ),
  ];
  return (
    <label className="text-caption flex flex-wrap items-center gap-2 text-text-muted">
      Drill down: filter the dashboard by {xKey}
      <select
        value=""
        onChange={(event) => {
          const picked = values.find((v) => String(v) === event.target.value);
          if (picked !== undefined) onDrill(picked);
        }}
        className="text-body rounded-sm border border-border bg-surface px-2 py-1 text-text focus-visible:outline-2 focus-visible:outline-focus"
      >
        <option value="">Choose a value…</option>
        {values.map((v) => (
          <option key={String(v)} value={String(v)}>
            {String(v)}
          </option>
        ))}
      </select>
    </label>
  );
}

function ChartBody({
  kind,
  title,
  observed,
  forecast,
  seriesField,
  valueField,
  y,
  format,
  markers,
  rawRows,
  onDrill,
}: ChartBodyProps) {
  const [showTable, setShowTable] = useState(false);
  const tableId = useId();
  const data = withForecast(observed, forecast);
  const hasForecast = data !== observed;
  const columns = tableColumns(data, hasForecast, y);
  const tableRows = flattenBand(data.rows);

  if (kind === 'table') {
    return <DataTable caption={title} columns={columns} rows={tableRows} format={format} />;
  }
  if (kind === 'heatmap') {
    if (!data.xKey || !seriesField) {
      return <p className="text-body text-text-muted">This heatmap needs two dimensions.</p>;
    }
    return (
      <HeatmapTable
        caption={title}
        xKey={data.xKey}
        seriesKey={seriesField}
        valueKey={valueField}
        rows={rawRows}
        format={format}
      />
    );
  }

  const summary = summarize(kind, observed, format);
  const drill = kind === 'scatter' ? undefined : onDrill;
  return (
    <div className="flex flex-col gap-2">
      <figure role="img" aria-label={`${title}. ${summary}`}>
        <ChartCanvas
          kind={kind}
          data={data}
          format={format}
          markers={markers}
          hasForecast={hasForecast}
          onSelect={drill}
        />
      </figure>
      <p className="text-caption text-text-muted">{summary}</p>
      {drill ? <DrillPicker data={observed} onDrill={drill} /> : null}
      {markers.length > 0 ? (
        <div>
          <h4 className="text-caption font-semibold">Unusual values detected</h4>
          <ul className="text-caption">
            {markers.map((marker) => (
              <li key={marker.x}>
                <span aria-hidden="true">◆ </span>
                {marker.x}: {formatNumber(marker.observed, format)}
                {marker.expected !== null
                  ? ` (expected about ${formatNumber(marker.expected, format)})`
                  : ''}
                {marker.direction === 'high' ? ', higher than usual' : ', lower than usual'}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div>
        <button
          type="button"
          aria-expanded={showTable}
          aria-controls={tableId}
          onClick={() => setShowTable((value) => !value)}
          className="text-caption rounded-sm font-medium text-primary hover:underline"
        >
          {showTable ? 'Hide data table' : 'Show data table'}
        </button>
        {showTable ? (
          <DataTable
            id={tableId}
            caption={`${title} (data)`}
            columns={columns}
            rows={tableRows}
            format={format}
          />
        ) : null}
      </div>
    </div>
  );
}
