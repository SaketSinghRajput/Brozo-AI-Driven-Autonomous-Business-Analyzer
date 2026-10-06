import { formatCell, formatNumber, type NumberFormat } from '../../../lib/format';

export interface Column {
  key: string;
  label: string;
  /** This column's own unit (table y fields differ: money, counts); undefined = the table's. */
  unit?: string | null;
}

function cellValue(value: unknown): string | number | null {
  return typeof value === 'number' || typeof value === 'string' ? value : null;
}

/** Native, captioned data table (UI-UX §50): numbers right-aligned with tabular numerals. */
export function DataTable({
  caption,
  columns,
  rows,
  format,
  id,
}: {
  caption: string;
  columns: Column[];
  rows: Array<Record<string, unknown>>;
  format: NumberFormat;
  id?: string;
}) {
  return (
    <div id={id} className="max-h-80 overflow-auto">
      <table className="text-body w-full border-collapse">
        <caption className="text-caption pb-2 text-left text-text-muted">{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className="sticky top-0 border-b border-border bg-surface px-2 py-1 text-left font-medium"
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index} className="border-b border-border/40">
              {columns.map((column) => {
                const value = cellValue(row[column.key]);
                return (
                  <td
                    key={column.key}
                    className={`px-2 py-1 ${typeof value === 'number' ? 'text-right tabular-nums' : ''}`}
                  >
                    {formatCell(
                      value,
                      column.unit === undefined
                        ? format
                        : { ...format, unit: column.unit, currency: null },
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Heatmap as a table: rows = series values, columns = x values, cell shade = single-hue
 * sequential scale of `primary` (UI-UX §21, §46.2). The value is always printed in the cell,
 * so the shade is never the only signal.
 */
export function HeatmapTable({
  caption,
  xKey,
  seriesKey,
  valueKey,
  rows,
  format,
}: {
  caption: string;
  xKey: string;
  seriesKey: string;
  valueKey: string;
  rows: Array<Record<string, unknown>>;
  format: NumberFormat;
}) {
  const label = (value: unknown) => String(cellValue(value));
  const xs = [...new Set(rows.map((row) => label(row[xKey])))];
  const ys = [...new Set(rows.map((row) => label(row[seriesKey])))];
  const cells = new Map(
    rows.map((row) => [`${label(row[seriesKey])}|${label(row[xKey])}`, row[valueKey]]),
  );
  const numbers = [...cells.values()].filter((value): value is number => typeof value === 'number');
  const min = Math.min(...numbers);
  const span = Math.max(...numbers) - min || 1;

  return (
    <div className="overflow-auto">
      <table className="text-body w-full border-collapse">
        <caption className="text-caption pb-2 text-left text-text-muted">{caption}</caption>
        <thead>
          <tr>
            <th scope="col" className="px-2 py-1 text-left font-medium">
              {seriesKey} / {xKey}
            </th>
            {xs.map((x) => (
              <th key={x} scope="col" className="px-2 py-1 text-right font-medium">
                {x}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ys.map((y) => (
            <tr key={y}>
              <th scope="row" className="px-2 py-1 text-left font-medium">
                {y}
              </th>
              {xs.map((x) => {
                const value = cells.get(`${y}|${x}`);
                const shade = typeof value === 'number' ? 8 + ((value - min) / span) * 42 : 0;
                return (
                  <td
                    key={x}
                    className="border border-border/40 px-2 py-1 text-right tabular-nums"
                    style={{
                      backgroundColor: `color-mix(in srgb, var(--color-primary) ${shade}%, transparent)`,
                    }}
                  >
                    {typeof value === 'number' ? formatNumber(value, format) : '—'}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
