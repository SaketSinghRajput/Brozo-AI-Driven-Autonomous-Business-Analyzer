import { Link, useNavigate } from 'react-router';
import { describeError } from '../../api/errors';
import { Button } from '../../components/Button';
import { EmptyState, ErrorState } from '../../components/StateViews';
import { Skeleton } from '../../components/Surfaces';
import { useAuth } from '../../context/AuthContext';
import { requiredRoleText } from '../../context/permissions';
import { useDatasets } from './api';

export const dateTime = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'short',
});

const primaryLink =
  'text-body inline-flex h-10 items-center rounded-md bg-primary px-4 font-medium text-surface hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2';

/** "Upload dataset" page action; visible but disabled with the reason for roles without it. */
export function UploadAction() {
  const { can } = useAuth();
  if (can('UPLOAD_DATASET')) {
    return (
      <Link to="/datasets/upload" className={primaryLink}>
        Upload dataset
      </Link>
    );
  }
  const reason = requiredRoleText('UPLOAD_DATASET');
  return (
    <span className="inline-flex items-center gap-2">
      <Button disabled disabledReason={reason}>
        Upload dataset
      </Button>
      <span className="text-caption text-text-muted">{reason}</span>
    </span>
  );
}

const SOURCE_LABELS: Record<string, string> = {
  file_upload: 'File upload',
  postgres_snapshot: 'Database snapshot',
};

/** Dataset list (UI-UX §9, TASK-UI-005). */
export function DatasetsListPage() {
  const { can } = useAuth();
  const navigate = useNavigate();
  const datasets = useDatasets();

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-h1 text-text">Datasets</h1>
        <UploadAction />
      </div>

      {datasets.isPending ? <Skeleton variant="table" rows={5} /> : null}
      {datasets.isError ? (
        <ErrorState {...describeError(datasets.error)} onRetry={() => void datasets.refetch()} />
      ) : null}
      {datasets.data?.length === 0 ? (
        <EmptyState
          message="No datasets yet. Upload your first business dataset to begin automatic analysis."
          actionLabel={can('UPLOAD_DATASET') ? 'Upload dataset' : undefined}
          onAction={() => void navigate('/datasets/upload')}
        />
      ) : null}
      {datasets.data && datasets.data.length > 0 ? (
        <table className="text-body w-full border-collapse text-left">
          <caption className="sr-only">Datasets in this workspace</caption>
          <thead>
            <tr className="border-b border-border text-text-muted">
              <th scope="col" className="py-2 pr-4 font-medium">
                Name
              </th>
              <th scope="col" className="py-2 pr-4 font-medium">
                Source
              </th>
              <th scope="col" className="py-2 font-medium">
                Created
              </th>
            </tr>
          </thead>
          <tbody>
            {datasets.data.map((dataset) => (
              <tr key={dataset.id} className="border-b border-border/40">
                <td className="py-2 pr-4">
                  <Link
                    to={`/datasets/${dataset.id}/overview`}
                    className="font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-focus"
                  >
                    {dataset.name}
                  </Link>
                </td>
                <td className="py-2 pr-4 text-text-muted">
                  {SOURCE_LABELS[dataset.source_type] ?? dataset.source_type}
                </td>
                <td className="py-2 text-text-muted">
                  {dateTime.format(new Date(dataset.created_at))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}
