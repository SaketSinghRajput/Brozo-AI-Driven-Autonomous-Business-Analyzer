import { useId, useState } from 'react';
import { describeError } from '../../api/errors';
import type { DatasetVersion, ProfileResult } from '../../api/generated/types.gen';
import { EmptyState, ErrorState } from '../../components/StateViews';
import { Card, Skeleton } from '../../components/Surfaces';
import { FAILED_STATES, isProfiled, useProfile } from './api';
import { ColumnDetail, columnIssues, pct, typeLabel } from './ColumnDetail';
import { useWorkspace } from './DatasetWorkspacePage';
import { FirstUseTip } from '../onboarding/FirstUseTip';

const coefficient = new Intl.NumberFormat(undefined, {
  maximumFractionDigits: 2,
  minimumFractionDigits: 2,
});
const MAX_CORRELATIONS = 10;

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <Card className="flex flex-col gap-1 p-3">
      <span className="text-caption text-text-muted">{label}</span>
      <span className="text-h2 text-text tabular-nums">{value}</span>
    </Card>
  );
}

/** Summary strip (UI-UX §16): completeness, duplicates, columns with issues, time coverage. */
function Summary({ profile }: { profile: ProfileResult }) {
  const columns = profile.columns;
  const completeness =
    columns.length === 0 ? 1 : 1 - columns.reduce((s, c) => s + c.null_ratio, 0) / columns.length;
  const withIssues = columns.filter((c) => columnIssues(c).length > 0).length;
  const temporal = columns.flatMap((c) => (c.temporal ? [c.temporal] : []));
  const from = temporal.map((t) => t.min).sort()[0];
  const to = temporal
    .map((t) => t.max)
    .sort()
    .at(-1);
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
      <Tile label="Rows" value={profile.row_count.toLocaleString()} />
      <Tile label="Completeness" value={pct(completeness)} />
      <Tile label="Duplicate rows" value={profile.duplicate_rows.toLocaleString()} />
      <Tile label="Columns with issues" value={`${withIssues} of ${columns.length}`} />
      <Tile
        label="Time coverage"
        value={from && to ? `${from.slice(0, 10)} – ${to.slice(0, 10)}` : 'No date column'}
      />
    </div>
  );
}

function Correlations({ profile }: { profile: ProfileResult }) {
  const pairs = [...profile.correlations]
    .sort((a, b) => Math.abs(b.coefficient) - Math.abs(a.coefficient))
    .slice(0, MAX_CORRELATIONS);
  if (pairs.length === 0) return null;
  return (
    <section className="flex flex-col gap-2">
      <h2 className="text-h3 text-text">Strongest relationships between numeric columns</h2>
      <p className="text-caption text-text-muted">
        Correlation shows columns that move together; it does not show that one causes the other.
      </p>
      <table className="text-body w-full max-w-3xl border-collapse text-left">
        <caption className="sr-only">Correlation between column pairs, strongest first</caption>
        <thead>
          <tr className="border-b border-border text-text-muted">
            <th scope="col" className="py-1 pr-4 font-medium">
              Columns
            </th>
            <th scope="col" className="py-1 pr-4 font-medium">
              Coefficient
            </th>
            <th scope="col" className="py-1 pr-4 font-medium">
              Method
            </th>
            <th scope="col" className="py-1 font-medium">
              Rows used (n)
            </th>
          </tr>
        </thead>
        <tbody>
          {pairs.map((pair) => (
            <tr key={`${pair.a}|${pair.b}|${pair.method}`} className="border-b border-border/40">
              <td className="py-1 pr-4 font-mono text-text">
                {pair.a} ↔ {pair.b}
              </td>
              <td className="py-1 pr-4 text-text tabular-nums">
                {coefficient.format(pair.coefficient)}
              </td>
              <td className="py-1 pr-4 text-text-muted capitalize">{pair.method}</td>
              <td className="py-1 text-text tabular-nums">{pair.n.toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function ColumnTable({
  profile,
  selected,
  onSelect,
}: {
  profile: ProfileResult;
  selected: string | null;
  onSelect: (name: string) => void;
}) {
  const filterId = useId();
  const [onlyIssues, setOnlyIssues] = useState(false);
  const rows = profile.columns.filter((c) => !onlyIssues || columnIssues(c).length > 0);
  return (
    <section className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-h3 text-text">Columns</h2>
        <label htmlFor={filterId} className="text-caption flex items-center gap-2 text-text">
          <input
            id={filterId}
            type="checkbox"
            checked={onlyIssues}
            onChange={(event) => setOnlyIssues(event.target.checked)}
            className="size-4 accent-primary"
          />
          Only columns with issues
        </label>
      </div>
      {rows.length === 0 ? (
        <EmptyState message="No columns have issues." />
      ) : (
        <table className="text-body w-full border-collapse text-left">
          <caption className="sr-only">
            Column profile. Select a column name for its details.
          </caption>
          <thead className="sticky top-0 bg-bg">
            <tr className="border-b border-border text-text-muted">
              <th scope="col" className="py-2 pr-4 font-medium">
                Column
              </th>
              <th scope="col" className="py-2 pr-4 font-medium">
                Type
              </th>
              <th scope="col" className="py-2 pr-4 font-medium">
                Completeness
              </th>
              <th scope="col" className="py-2 pr-4 font-medium">
                Distinct
              </th>
              <th scope="col" className="py-2 font-medium">
                Issues
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((column) => {
              const issues = columnIssues(column);
              const complete = 1 - column.null_ratio;
              const isSelected = column.name === selected;
              return (
                <tr
                  key={column.name}
                  className={`border-b border-border/40 ${isSelected ? 'bg-primary/5' : ''}`}
                >
                  <th scope="row" className="py-2 pr-4 font-normal">
                    <button
                      type="button"
                      aria-pressed={isSelected}
                      onClick={() => onSelect(column.name)}
                      className="font-mono text-primary underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-focus"
                    >
                      {column.name}
                    </button>
                  </th>
                  <td className="py-2 pr-4 text-text-muted">{typeLabel(column)}</td>
                  <td className="py-2 pr-4">
                    <span className="flex items-center gap-2">
                      <span aria-hidden="true" className="h-2 w-20 rounded-sm bg-border/30">
                        <span
                          className={`block h-2 rounded-sm ${complete < 1 ? 'bg-warning' : 'bg-success'}`}
                          style={{ width: `${complete * 100}%` }}
                        />
                      </span>
                      <span className="text-caption text-text tabular-nums">{pct(complete)}</span>
                    </span>
                  </td>
                  <td className="py-2 pr-4 text-text tabular-nums">
                    {column.cardinality.toLocaleString()}
                  </td>
                  <td className="text-caption py-2 text-text">
                    {issues.length === 0 ? (
                      <span className="text-text-muted">None</span>
                    ) : (
                      <>
                        <span aria-hidden="true" className="text-warning">
                          ⚠{' '}
                        </span>
                        {issues.join(' · ')}
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function ProfileView({ version }: { version: DatasetVersion }) {
  const profile = useProfile(version);
  const [selected, setSelected] = useState<string | null>(null);

  if (FAILED_STATES.has(version.state) && !isProfiled(version.state)) {
    return <EmptyState message="This version failed to process, so it has no profile." />;
  }
  if (!isProfiled(version.state)) {
    return (
      <p role="status" className="text-body text-text">
        The profile appears here once processing reaches the profiling step.
      </p>
    );
  }
  if (profile.isError) {
    return <ErrorState {...describeError(profile.error)} onRetry={() => void profile.refetch()} />;
  }
  if (!profile.data) return <Skeleton variant="table" rows={8} />;

  const data = profile.data;
  const column = data.columns.find((c) => c.name === selected) ?? null;
  return (
    <div className="flex flex-col gap-6">
      <FirstUseTip id="profile-column-detail">
        Select a column name to see its value distribution, outliers and time coverage.
      </FirstUseTip>
      <Summary profile={data} />
      {data.sampled ? (
        <p className="text-caption text-text-muted">
          Statistics were computed on a sample of {data.sample_size?.toLocaleString() ?? 'the'} rows
          out of {data.row_count.toLocaleString()}.
        </p>
      ) : null}
      <div className="grid gap-6 xl:grid-cols-[1fr_24rem]">
        <ColumnTable profile={data} selected={selected} onSelect={setSelected} />
        {column ? <ColumnDetail column={column} onClose={() => setSelected(null)} /> : null}
      </div>
      <Correlations profile={data} />
    </div>
  );
}

/** Data profile tab (UI-UX §16, TASK-UI-006): summary first, then columns, then details. */
export function ProfileTab() {
  const { version } = useWorkspace();
  if (!version) return <EmptyState message="This dataset has no versions yet." />;
  return <ProfileView key={version.id} version={version} />;
}
