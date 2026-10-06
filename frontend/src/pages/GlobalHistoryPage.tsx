import { Link, useNavigate } from 'react-router';
import { describeError } from '../api/errors';
import { Card, Skeleton } from '../components/Surfaces';
import { EmptyState, ErrorState } from '../components/StateViews';
import { dateTime } from '../features/datasets/DatasetsListPage';
import { useConversations } from '../features/chat/api';

/**
 * Global history (UI-UX §36-37, TASK-UI-028): conversations, newest first; opening one restores
 * it in the Assistant (GET …/messages). The analysis-runs list waits for its backend endpoint.
 */
export function GlobalHistoryPage() {
  const conversations = useConversations();
  const navigate = useNavigate();

  return (
    <div className="flex max-w-4xl flex-col gap-4">
      <h1 className="text-h1 text-text">History</h1>
      <section aria-labelledby="conversations-heading" className="flex flex-col gap-2">
        <h2 id="conversations-heading" className="text-h3 text-text">
          Conversations
        </h2>
        {conversations.isPending ? <Skeleton variant="table" rows={3} /> : null}
        {conversations.isError ? (
          <ErrorState
            {...describeError(conversations.error)}
            onRetry={() => void conversations.refetch()}
          />
        ) : null}
        {conversations.data?.length === 0 ? (
          <EmptyState
            message="No conversations yet. Ask a question about your data."
            actionLabel="Open the assistant"
            onAction={() => void navigate('/assistant')}
          />
        ) : null}
        {conversations.data && conversations.data.length > 0 ? (
          <ul className="flex flex-col gap-2">
            {conversations.data.map((conversation) => (
              <li key={conversation.id}>
                <Card className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <Link
                      to={`/assistant/${encodeURIComponent(conversation.id)}`}
                      className="text-body font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-focus"
                    >
                      {conversation.title}
                    </Link>
                    <p className="text-caption text-text-muted">
                      Dataset version {conversation.dataset_version_id} · semantic model v
                      {conversation.semantic_model_version}
                    </p>
                  </div>
                  <span className="text-caption text-text-muted">
                    {dateTime.format(new Date(conversation.created_at))}
                  </span>
                </Card>
              </li>
            ))}
          </ul>
        ) : null}
      </section>
      <section aria-labelledby="analyses-heading" className="flex flex-col gap-2">
        <h2 id="analyses-heading" className="text-h3 text-text">
          Analyses
        </h2>
        <p className="text-caption text-text-muted">
          A history of analysis runs will appear here once the backend provides it.
        </p>
      </section>
    </div>
  );
}
