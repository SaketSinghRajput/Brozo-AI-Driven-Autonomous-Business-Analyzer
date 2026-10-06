import type { ColumnProfile } from '../../api/generated/types.gen';
import { Card } from '../../components/Surfaces';

const num = new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 });
const pctFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
export const pct = (ratio: number) => `${pctFormat.format(ratio * 100)}%`;

const TYPE_LABELS: Record<ColumnProfile['physical_type'], string> = {
  int: 'Whole number',
  float: 'Decimal number',
  decimal: 'Decimal number',
  bool: 'Yes / no',
  datetime: 'Date and time',
  date: 'Date',
  string: 'Text',
  mixed: 'Mixed types',
};

export function typeLabel(column: ColumnProfile): string {
  return TYPE_LABELS[column.physical_type];
}

/** Human issue list for one column (shown in the table and in the detail panel). */
export function columnIssues(column: ColumnProfile): string[] {
  const issues: string[] = [];
  if (column.null_ratio === 1) issues.push('Empty in every row');
  else if (column.null_ratio > 0) issues.push(`${pct(column.null_ratio)} missing`);
  if (column.parse_success_ratio < 1) {
    issues.push(`${pct(1 - column.parse_success_ratio)} of values don't match the column type`);
  }
  if (column.outliers && column.outliers.count > 0) {
    issues.push(`${column.outliers.count.toLocaleString()} outliers`);
  }
  if (column.temporal?.tz_mixed) issues.push('Mixed time zones');
  if (column.temporal && column.temporal.gaps > 0) {
    issues.push(`${column.temporal.gaps.toLocaleString()} gaps in the time series`);
  }
  return issues;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-text-muted">{label}</dt>
      <dd className="text-text tabular-nums">{value}</dd>
    </>
  );
}

/** Top values as labelled bars; the text carries the numbers (bars are decorative). */
function TopValues({ column }: { column: ColumnProfile }) {
  const top = column.categorical;
  if (!top || top.values.length === 0) return null;
  return (
    <section className="flex flex-col gap-2">
      <h4 className="text-label font-medium text-text">Most frequent values</h4>
      {top.masked ? (
        <p className="text-caption rounded-sm bg-info/10 p-2 text-text">
          <span aria-hidden="true">🔒 </span>
          These values look like personal data, so they are shown masked.
        </p>
      ) : null}
      <ul className="flex flex-col gap-1">
        {top.values.map((entry) => (
          <li key={entry.value} className="text-caption grid grid-cols-[10rem_1fr_7rem] gap-2">
            <span className="truncate font-mono text-text" title={entry.value}>
              {entry.value}
              {top.masked ? <span className="sr-only"> (masked)</span> : null}
            </span>
            <span aria-hidden="true" className="h-3 self-center rounded-sm bg-border/30">
              <span
                className="block h-3 rounded-sm bg-primary"
                style={{ width: `${Math.max(entry.frequency * 100, 1)}%` }}
              />
            </span>
            <span className="text-right text-text-muted tabular-nums">
              {entry.count.toLocaleString()} ({pct(entry.frequency)})
            </span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function TechnicalDetails({ column }: { column: ColumnProfile }) {
  const { numeric, text } = column;
  if (!numeric && !text) return null;
  return (
    <details className="text-caption">
      <summary className="cursor-pointer text-primary">Technical details</summary>
      <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
        {numeric ? (
          <>
            <Stat label="Standard deviation" value={num.format(numeric.std)} />
            <Stat label="Variance" value={num.format(numeric.var)} />
            <Stat label="Skewness" value={numeric.skew == null ? '—' : num.format(numeric.skew)} />
            {Object.entries(numeric.quantiles).map(([q, value]) => (
              <Stat key={q} label={`Quantile ${q}`} value={num.format(value)} />
            ))}
          </>
        ) : null}
        {text ? (
          <>
            <Stat
              label="Text length"
              value={`${text.min_length}–${text.max_length} (mean ${num.format(text.mean_length)})`}
            />
            {Object.entries(text.regex_hit_ratios).map(([pattern, share]) => (
              <Stat key={pattern} label={`Matches ${pattern}`} value={pct(share)} />
            ))}
          </>
        ) : null}
      </dl>
    </details>
  );
}

/** Column detail panel (UI-UX §16): distribution, outliers, patterns, temporal coverage. */
export function ColumnDetail({ column, onClose }: { column: ColumnProfile; onClose: () => void }) {
  const { numeric, outliers, temporal, text } = column;
  const issues = columnIssues(column);
  return (
    <Card
      role="region"
      aria-label={`Column detail: ${column.name}`}
      className="flex flex-col gap-4"
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-h3 font-mono text-text">{column.name}</h3>
          <p className="text-caption text-text-muted">{typeLabel(column)}</p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="text-caption rounded-sm border border-border px-2 py-1 text-text hover:bg-surface-raised focus-visible:outline-2 focus-visible:outline-focus"
        >
          Close
        </button>
      </div>

      <dl className="text-caption grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
        <Stat label="Completeness" value={pct(1 - column.null_ratio)} />
        <Stat label="Distinct values" value={column.cardinality.toLocaleString()} />
        <Stat label="Unique share" value={pct(column.unique_ratio)} />
        <Stat label="Values matching type" value={pct(column.parse_success_ratio)} />
      </dl>

      {issues.length > 0 ? (
        <ul className="text-caption flex list-disc flex-col gap-0.5 pl-5 text-text">
          {issues.map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      ) : null}

      {numeric ? (
        <section className="flex flex-col gap-2">
          <h4 className="text-label font-medium text-text">Value range</h4>
          <dl className="text-caption grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
            <Stat label="Minimum" value={num.format(numeric.min)} />
            <Stat label="Median" value={num.format(numeric.median)} />
            <Stat label="Mean" value={num.format(numeric.mean)} />
            <Stat label="Maximum" value={num.format(numeric.max)} />
            <Stat label="Zero values" value={pct(numeric.zero_ratio)} />
            <Stat label="Negative values" value={pct(numeric.neg_ratio)} />
          </dl>
        </section>
      ) : null}

      {outliers ? (
        <p className="text-caption text-text">
          {outliers.count.toLocaleString()} outliers found (
          {outliers.method === 'iqr' ? 'interquartile range' : 'median absolute deviation'} rule)
          {outliers.sample_indices.length > 0
            ? `; sample rows: ${outliers.sample_indices.slice(0, 10).join(', ')}`
            : ''}
          .
        </p>
      ) : null}

      {temporal ? (
        <section className="flex flex-col gap-2">
          <h4 className="text-label font-medium text-text">Time coverage</h4>
          <dl className="text-caption grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
            <Stat label="From" value={temporal.min} />
            <Stat label="To" value={temporal.max} />
            <Stat label="Granularity" value={temporal.granularity ?? 'Irregular / unknown'} />
            <Stat label="Regularity" value={pct(temporal.regularity)} />
            <Stat label="Gaps" value={temporal.gaps.toLocaleString()} />
          </dl>
        </section>
      ) : null}

      <TopValues column={column} />

      {text && Object.keys(text.pattern_classes).length > 0 ? (
        <section className="flex flex-col gap-1">
          <h4 className="text-label font-medium text-text">Text patterns</h4>
          <ul className="text-caption flex flex-col gap-0.5 text-text">
            {Object.entries(text.pattern_classes).map(([pattern, share]) => (
              <li key={pattern}>
                <span className="font-mono">{pattern}</span>: {pct(share)}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <TechnicalDetails column={column} />
    </Card>
  );
}
