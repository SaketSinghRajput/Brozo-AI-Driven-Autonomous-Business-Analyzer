import { useId, useState } from 'react';
import { describeError } from '../../api/errors';
import { Button, EmptyState, ErrorState, Skeleton } from '../../components';
import { useDatasets, useVersions } from '../datasets/api';
import { useSemanticModel } from '../semantic/api';
import { useConversations, useCreateConversation } from './api';
import { ChatThread } from './ChatThread';

const selectClass =
  'text-body rounded-sm border border-border bg-surface px-2 py-1 text-text focus-visible:outline-2 focus-visible:outline-focus';

/** Start a conversation bound to a dataset's newest version and its current semantic model. */
function NewConversation({ onCreated }: { onCreated: (id: string) => void }) {
  const datasets = useDatasets();
  const [datasetId, setDatasetId] = useState('');
  const selectId = useId();
  const versions = useVersions(datasetId);
  const newest = [...(versions.data ?? [])].sort((a, b) => b.version_no - a.version_no)[0];
  const model = useSemanticModel(
    { datasetId, versionId: newest?.id ?? '' },
    datasetId !== '' && newest !== undefined,
  );
  const create = useCreateConversation();
  const dataset = datasets.data?.find((d) => d.id === datasetId);

  if (datasets.isError) return <ErrorState {...describeError(datasets.error)} />;
  if (datasets.data?.length === 0) {
    return <EmptyState message="Upload a dataset first, then ask questions about it." />;
  }

  function start() {
    if (!newest || !model.data || !dataset) return;
    create.mutate(
      {
        dataset_version_id: newest.id,
        semantic_model_version: model.data.version,
        title: `Questions about ${dataset.name}`,
      },
      { onSuccess: (conversation) => onCreated(conversation.id) },
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <label htmlFor={selectId} className="text-caption font-medium text-text">
        Dataset
      </label>
      <select
        id={selectId}
        value={datasetId}
        onChange={(event) => setDatasetId(event.target.value)}
        className={selectClass}
      >
        <option value="">Choose a dataset…</option>
        {(datasets.data ?? []).map((d) => (
          <option key={d.id} value={d.id}>
            {d.name}
          </option>
        ))}
      </select>
      {datasetId && model.isError ? (
        <p className="text-caption text-text-muted">
          This dataset isn&apos;t ready for questions yet: its meaning has not been mapped.
        </p>
      ) : null}
      {newest && model.data ? (
        <p className="text-caption text-text-muted">
          Uses version {newest.version_no} · semantic model v{model.data.version}
        </p>
      ) : null}
      {create.isError ? <ErrorState {...describeError(create.error)} /> : null}
      <div>
        <Button
          size="sm"
          onClick={start}
          loading={create.isPending}
          disabled={!model.data}
          disabledReason="Choose a dataset that has finished processing"
        >
          Start conversation
        </Button>
      </div>
    </div>
  );
}

export interface ChatWorkspaceProps {
  conversationId: string | null;
  onSelect: (conversationId: string) => void;
}

/** Conversation picker + new conversation + the selected thread (full page and docked panel). */
export function ChatWorkspace({ conversationId, onSelect }: ChatWorkspaceProps) {
  const conversations = useConversations();
  const [creating, setCreating] = useState(false);
  const selectId = useId();

  if (conversations.isPending) return <Skeleton variant="text" rows={3} />;
  if (conversations.isError) {
    return (
      <ErrorState
        {...describeError(conversations.error)}
        onRetry={() => void conversations.refetch()}
      />
    );
  }
  const selected = conversations.data.find((c) => c.id === conversationId) ?? null;
  const showNew = creating || conversations.data.length === 0;

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex flex-wrap items-end gap-2">
        {conversations.data.length > 0 ? (
          <label htmlFor={selectId} className="text-caption flex flex-col gap-1 text-text">
            Conversation
            <select
              id={selectId}
              value={selected?.id ?? ''}
              onChange={(event) => {
                setCreating(false);
                onSelect(event.target.value);
              }}
              className={selectClass}
            >
              <option value="" disabled>
                Choose a conversation…
              </option>
              {conversations.data.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.title}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {!showNew ? (
          <Button size="sm" variant="secondary" onClick={() => setCreating(true)}>
            New conversation
          </Button>
        ) : null}
      </div>
      {showNew ? (
        <NewConversation
          onCreated={(id) => {
            setCreating(false);
            onSelect(id);
          }}
        />
      ) : null}
      {selected && !showNew ? <ChatThread key={selected.id} conversation={selected} /> : null}
      {!selected && !showNew ? (
        <EmptyState message="Choose a conversation or start a new one." />
      ) : null}
    </div>
  );
}
