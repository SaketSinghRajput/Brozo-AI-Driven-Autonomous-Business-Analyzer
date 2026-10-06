import { ApiError, describeError } from '../../api/errors';
import { EmptyState, ErrorState, Skeleton } from '../../components';
import { useAuth } from '../../context/AuthContext';
import { isProfiled } from '../datasets/api';
import { useWorkspace } from '../datasets/DatasetWorkspacePage';
import { MAPPED, useSemanticModel, useUnderstanding } from './api';
import { DomainPanel } from './DomainPanel';
import { MappingTable } from './MappingTable';

function notYet(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

/**
 * Understanding tab (UI-UX §17-18, TASK-UI-007): detected domain with its signals and the
 * semantic mapping. Edit controls only for EDIT_SEMANTICS (Analyst+); the backend enforces it.
 */
export function UnderstandingTab() {
  const { dataset, version } = useWorkspace();
  const { can } = useAuth();
  const versionRef = { datasetId: dataset.id, versionId: version?.id ?? '' };
  const understanding = useUnderstanding(versionRef, version !== null && isProfiled(version.state));
  const model = useSemanticModel(versionRef, version !== null && MAPPED.has(version.state));
  const canEdit = can('EDIT_SEMANTICS');

  if (!version) return <EmptyState message="This dataset has no versions yet." />;
  if (!isProfiled(version.state) || notYet(understanding.error)) {
    return (
      <EmptyState message="This version hasn't been analysed yet. This page updates when it is." />
    );
  }
  if (understanding.isError) {
    return (
      <ErrorState
        {...describeError(understanding.error)}
        onRetry={() => void understanding.refetch()}
      />
    );
  }
  if (!understanding.data) return <Skeleton variant="table" rows={4} />;

  return (
    <div className="flex max-w-5xl flex-col gap-4">
      <DomainPanel view={understanding.data} versionRef={versionRef} canEdit={canEdit} />
      {model.data ? (
        <MappingTable
          model={model.data}
          flags={understanding.data.review_flags}
          versionRef={versionRef}
          canEdit={canEdit}
          onReload={() => {
            void model.refetch();
            void understanding.refetch();
          }}
        />
      ) : model.isError && !notYet(model.error) ? (
        <ErrorState {...describeError(model.error)} onRetry={() => void model.refetch()} />
      ) : model.isPending && MAPPED.has(version.state) ? (
        <Skeleton variant="table" rows={4} />
      ) : (
        <p className="text-body text-text-muted">
          Column meanings are mapped after the domain is identified.
        </p>
      )}
    </div>
  );
}
