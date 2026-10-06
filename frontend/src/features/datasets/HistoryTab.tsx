import { Link } from 'react-router';
import { EmptyState } from '../../components/StateViews';
import { dateTime } from './DatasetsListPage';
import { DatasetStateBadge } from './DatasetStateBadge';
import { useWorkspace } from './DatasetWorkspacePage';

/**
 * Dataset-scoped history (UI-UX §9, TASK-UI-005): every version with its lifecycle state and
 * review-flag count. Analyses and conversations join this list once those APIs exist.
 */
export function HistoryTab() {
  const { dataset, versions, version: selected } = useWorkspace();
  if (versions.length === 0) return <EmptyState message="This dataset has no versions yet." />;

  return (
    <table className="text-body w-full max-w-4xl border-collapse text-left">
      <caption className="sr-only">Versions of this dataset, newest first</caption>
      <thead>
        <tr className="border-b border-border text-text-muted">
          <th scope="col" className="py-2 pr-4 font-medium">
            Version
          </th>
          <th scope="col" className="py-2 pr-4 font-medium">
            State
          </th>
          <th scope="col" className="py-2 pr-4 font-medium">
            Rows
          </th>
          <th scope="col" className="py-2 pr-4 font-medium">
            Needs review
          </th>
          <th scope="col" className="py-2 font-medium">
            Uploaded
          </th>
        </tr>
      </thead>
      <tbody>
        {versions.map((v) => (
          <tr
            key={v.id}
            aria-current={v.id === selected?.id ? 'true' : undefined}
            className="border-b border-border/40"
          >
            <td className="py-2 pr-4">
              <Link
                to={`/datasets/${dataset.id}/overview?version=${v.id}`}
                className="font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-focus"
              >
                v{v.version_no}
              </Link>
              {v.id === selected?.id ? (
                <span className="text-caption ml-2 text-text-muted">(selected)</span>
              ) : null}
            </td>
            <td className="py-2 pr-4">
              <DatasetStateBadge state={v.state} />
            </td>
            <td className="py-2 pr-4 text-text tabular-nums">
              {v.row_count != null ? v.row_count.toLocaleString() : '—'}
            </td>
            <td className="py-2 pr-4 text-text tabular-nums">{v.review_flags?.length ?? 0}</td>
            <td className="py-2 text-text-muted">{dateTime.format(new Date(v.created_at))}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
