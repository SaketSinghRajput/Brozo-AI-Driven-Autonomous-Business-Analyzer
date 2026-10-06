import { Link } from 'react-router';
import { describeError } from '../api/errors';
import { EmptyState, ErrorState } from '../components/StateViews';
import { Skeleton } from '../components/Surfaces';
import { useAuth } from '../context/AuthContext';
import { useDatasets } from '../features/datasets/api';
import { dateTime, UploadAction } from '../features/datasets/DatasetsListPage';
import { FirstUseTip } from '../features/onboarding/FirstUseTip';

const RECENT = 5;
const link =
  'text-primary underline underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus';

/** First launch (UI-UX §12): one sentence, the privacy note and the two ways to start. */
function Welcome() {
  const { can } = useAuth();
  return (
    <section
      aria-labelledby="welcome-heading"
      className="flex max-w-2xl flex-col gap-4 rounded-md border border-border bg-surface p-6"
    >
      <h2 id="welcome-heading" className="text-h2 text-text">
        Welcome
      </h2>
      <p className="text-body text-text">
        Upload business data; get an evidence-backed analysis you can question.
      </p>
      <p className="text-caption text-text-muted">
        <span aria-hidden="true">🔒 </span>
        Your data is processed by this self-hosted instance. Nothing is sent to external AI
        services.
      </p>
      <div className="flex flex-wrap items-center gap-4">
        <UploadAction />
        {can('UPLOAD_DATASET') ? (
          <Link to="/datasets/upload#samples" className={`text-body ${link}`}>
            Try with sample data
          </Link>
        ) : null}
      </div>
    </section>
  );
}

/** Home (UI-UX §9, §12): welcome on first use, recent datasets afterwards. */
export function HomePage() {
  const datasets = useDatasets();
  const recent = [...(datasets.data ?? [])]
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .slice(0, RECENT);

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-h1 text-text">Home</h1>

      {datasets.isPending ? <Skeleton variant="card" /> : null}
      {datasets.isError ? (
        <ErrorState {...describeError(datasets.error)} onRetry={() => void datasets.refetch()} />
      ) : null}
      {datasets.data?.length === 0 ? <Welcome /> : null}

      {recent.length > 0 ? (
        <>
          <FirstUseTip id="home-next-steps">
            Open a dataset to see its processing state and data profile · Review flagged items in
            its Overview · Ask questions in the Assistant.
          </FirstUseTip>
          <section aria-labelledby="recent-heading" className="flex flex-col gap-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 id="recent-heading" className="text-h2 text-text">
                Recent datasets
              </h2>
              <UploadAction />
            </div>
            <ul className="flex flex-col gap-2">
              {recent.map((dataset) => (
                <li key={dataset.id} className="text-body flex flex-wrap gap-x-3">
                  <Link to={`/datasets/${dataset.id}/overview`} className={`font-medium ${link}`}>
                    {dataset.name}
                  </Link>
                  <span className="text-text-muted">
                    {dateTime.format(new Date(dataset.created_at))}
                  </span>
                </li>
              ))}
            </ul>
            <Link to="/datasets" className={`text-caption ${link}`}>
              All datasets
            </Link>
          </section>
        </>
      ) : null}

      <section className="flex flex-col gap-3">
        <h2 className="text-h2 text-text">Recent conversations</h2>
        <EmptyState message="No conversations yet. Ask a question about your data." />
      </section>
    </div>
  );
}
