import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ReferenceDot,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { formatCompact, formatNumber, type NumberFormat } from '../../../lib/format';
import { BAND_KEY, FORECAST_KEY, type AnomalyMarker, type ChartData } from './chartData';

export type PlotKind = 'line' | 'bar' | 'stacked_bar' | 'area' | 'scatter';

const PALETTE = [1, 2, 3, 4, 5, 6].map((n) => `var(--color-chart-${n})`);
const AXIS = { fill: 'var(--color-text-muted)', fontSize: 12 };

function Diamond({ cx = 0, cy = 0 }: { cx?: number; cy?: number }) {
  return (
    <path
      d={`M${cx},${cy - 7} L${cx + 7},${cy} L${cx},${cy + 7} L${cx - 7},${cy} Z`}
      fill="var(--color-chart-6)"
      stroke="var(--color-text)"
      strokeWidth={1}
    />
  );
}

/**
 * The SVG chart itself (Recharts, ADR-0012). The parent gives it an accessible name + summary
 * and a data-table alternative (UI-UX §53).
 */
export function ChartCanvas({
  kind,
  data,
  format,
  markers,
  hasForecast,
  onSelect,
}: {
  kind: PlotKind;
  data: ChartData;
  format: NumberFormat;
  markers: AnomalyMarker[];
  hasForecast: boolean;
  /** Drill-down: called with the x value of the clicked bar/point. */
  onSelect?: (x: string | number) => void;
}) {
  const tick = (value: unknown): string =>
    typeof value === 'number' ? formatCompact(value, format) : String(value);
  const tooltipValue = (value: unknown): string =>
    typeof value === 'number'
      ? formatNumber(value, format)
      : Array.isArray(value)
        ? value.map(tick).join(' – ')
        : String(value);

  if (kind === 'scatter') {
    const xField = data.xKey ?? data.series[0].key;
    const yField = (data.series.find((series) => series.key !== xField) ?? data.series[0]).key;
    return (
      <ResponsiveContainer width="100%" height={260}>
        <ScatterChart margin={{ top: 8, right: 16, bottom: 8, left: 8 }}>
          <CartesianGrid stroke="var(--color-border)" strokeOpacity={0.4} />
          <XAxis type="number" dataKey={xField} name={xField} tick={AXIS} tickFormatter={tick} />
          <YAxis type="number" dataKey={yField} name={yField} tick={AXIS} tickFormatter={tick} />
          <Tooltip formatter={tooltipValue} />
          <Scatter data={data.rows} fill={PALETTE[0]} isAnimationActive={false} />
        </ScatterChart>
      </ResponsiveContainer>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={260}>
      <ComposedChart
        data={data.rows}
        margin={{ top: 16, right: 16, bottom: 8, left: 8 }}
        onClick={
          onSelect
            ? (state) => {
                if (state.activeLabel !== undefined) onSelect(state.activeLabel);
              }
            : undefined
        }
        style={onSelect ? { cursor: 'pointer' } : undefined}
      >
        <CartesianGrid stroke="var(--color-border)" strokeOpacity={0.4} vertical={false} />
        <XAxis dataKey={data.xKey ?? undefined} tick={AXIS} />
        <YAxis tick={AXIS} tickFormatter={tick} width={64} />
        <Tooltip formatter={tooltipValue} />
        {data.series.length > 1 || hasForecast ? (
          // Legend text in the text token: series colours alone fail text contrast (UI-UX §46).
          <Legend formatter={(value: string) => <span className="text-text">{value}</span>} />
        ) : null}
        {hasForecast ? (
          <Area
            dataKey={BAND_KEY}
            name="Estimate interval"
            stroke="none"
            fill={PALETTE[0]}
            fillOpacity={0.18}
            isAnimationActive={false}
          />
        ) : null}
        {data.series.map((series, index) => {
          const color = PALETTE[index % PALETTE.length];
          if (kind === 'bar' || kind === 'stacked_bar') {
            return (
              <Bar
                key={series.key}
                dataKey={series.key}
                name={series.label}
                fill={color}
                stackId={kind === 'stacked_bar' ? 'stack' : undefined}
                isAnimationActive={false}
              />
            );
          }
          if (kind === 'area') {
            return (
              <Area
                key={series.key}
                dataKey={series.key}
                name={series.label}
                stroke={color}
                fill={color}
                fillOpacity={0.25}
                isAnimationActive={false}
              />
            );
          }
          return (
            <Line
              key={series.key}
              dataKey={series.key}
              name={series.label}
              stroke={color}
              strokeWidth={2}
              dot={false}
              isAnimationActive={false}
            />
          );
        })}
        {hasForecast ? (
          <Line
            dataKey={FORECAST_KEY}
            name="Forecast (estimate)"
            stroke={PALETTE[0]}
            strokeWidth={2}
            strokeDasharray="6 4"
            dot={false}
            isAnimationActive={false}
          />
        ) : null}
        {markers.map((marker) => (
          <ReferenceDot
            key={marker.x}
            x={marker.x}
            y={marker.observed}
            shape={<Diamond />}
            label={{ value: 'Unusual', position: 'top', fill: 'var(--color-text)', fontSize: 11 }}
          />
        ))}
      </ComposedChart>
    </ResponsiveContainer>
  );
}
