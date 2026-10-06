import { useId } from 'react';
import { NavLink, Outlet, useOutletContext, useParams, useSearchParams } from 'react-router';
import { describeError } from '../../api/errors';
import type { Dataset, DatasetVersion } from '../../api/generated/types.gen';
import { ErrorState } from '../../components/StateViews';
import { Skeleton } from '../../components/Surfaces';
import { useDataset, useVersions } from './api';
import { DatasetStateBadge } from './DatasetStateBadge';

const tabs = [
  { to: 'overview', label: 'Overview' },
  { to: 'dashboard', label: 'Dashboard' },
  { to: 'understanding', label: 'Understanding' },
  { to: 'profile', label: 'Data profile' },
  { to: 'history', label: 'Versions' },
];

export interface WorkspaceContext {
  dataset: Dataset;
  versions: DatasetVersion[];
  /** The version selected via `?version=`, else the newest one; null before the first upload. */
  version: DatasetVersion | null;
}

/** Shared state for the workspace tabs. */
export function useWorkspace(): WorkspaceContext {
  return useOutletContext<WorkspaceContext>();
}

/**
 * Dataset workspace (UI-UX §9, §15): tabs share one header with name, version selector and
 * lifecycle state. Results are version-specific, so the version lives in the URL.
 */
export function DatasetWorkspacePage() {
  const { datasetId = '' } = useParams<{ datasetId: string }>();
  const [params, setParams] = useSearchParams();
  const selectId = useId();
  const dataset = useDataset(datasetId);
  const versions = useVersions(datasetId);

  const error = dataset.error ?? versions.error;
  if (error) {
    return (
      <ErrorState
        {...describeError(error)}
        onRetry={() => void Promise.all([dataset.refetch(), versions.refetch()])}
      />
    );
  }
  if (!dataset.data || !versions.data) return <Skeleton variant="card" />;

  const sorted = [...versions.data].sort((a, b) => b.version_no - a.version_no);
  const requested = params.get('version');
  const version = sorted.find((v) => v.id === requested) ?? sorted[0] ?? null;
  const search = version ? `?version=${version.id}` : '';
  const context: WorkspaceContext = { dataset: dataset.data, versions: sorted, version };

  return (
    <div className="flex flex-col gap-4">
      <header className="flex flex-col gap-2 border-b border-border pb-4">
        <nav aria-label="Breadcrumb" className="text-caption text-text-muted">
          <NavLink to="/datasets" className="underline-offset-2 hover:underline">
            Datasets
          </NavLink>{' '}
          › {dataset.data.name}
          {version ? ` › v${version.version_no}` : null}
        </nav>
        <div className="flex flex-wrap items-center gap-3">
          <h1 className="text-h1 text-text">{dataset.data.name}</h1>
          {version ? <DatasetStateBadge state={version.state} /> : null}
          {sorted.length > 0 ? (
            <label htmlFor={selectId} className="text-caption flex items-center gap-2 text-text">
              Version
              <select
                id={selectId}
                value={version?.id}
                onChange={(event) => setParams({ version: event.target.value })}
                className="text-body rounded-sm border border-border bg-surface px-2 py-1 text-text focus-visible:outline-2 focus-visible:outline-focus"
              >
                {sorted.map((v) => (
                  <option key={v.id} value={v.id}>
                    v{v.version_no}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
          {version?.row_count != null ? (
            <span className="text-caption text-text-muted">
              {version.row_count.toLocaleString()} rows
            </span>
          ) : null}
        </div>
      </header>

      <nav aria-label="Dataset workspace" className="border-b border-border">
        <ul className="flex flex-wrap gap-4">
          {tabs.map((tab) => (
            <li key={tab.to}>
              <NavLink
                to={`${tab.to}${search}`}
                className={({ isActive }) =>
                  `text-caption inline-block border-b-2 px-1 py-2 font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus ${
                    isActive
                      ? 'border-primary text-primary'
                      : 'border-transparent text-text-muted hover:text-text'
                  }`
                }
              >
                {tab.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>

      <div>
        <Outlet context={context} />
      </div>
    </div>
  );
}
