import { useQuery } from '@tanstack/react-query';
import type { DatasetVersion } from '../../api/generated/types.gen';
import { ApiError, describeError, unwrap } from '../../api/errors';
import {
  getDashboardApiV1DatasetsDatasetIdVersionsVersionIdDashboardGet,
  getDimensionValuesApiV1DatasetsDatasetIdVersionsVersionIdDimensionsNameValuesGet,
} from '../../api/generated/sdk.gen';
import { EmptyState, ErrorState } from '../../components/StateViews';
import { Skeleton } from '../../components/Surfaces';
import { queryKeys } from '../../lib/queryClient';
import { FAILED_STATES, POLL_MS } from '../datasets/api';
import { useWorkspace } from '../datasets/DatasetWorkspacePage';
import { activityOf } from '../datasets/ProcessingTimeline';
import { rejectionText } from '../datasets/reasons';
import { useCapabilities, useSemanticModel } from '../semantic/api';
import { DashboardScreen } from './DashboardScreen';

function isNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

function DashboardSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-3">
      <Skeleton variant="card" />
      <Skeleton variant="card" />
      <Skeleton variant="card" />
      <div className="md:col-span-3">
        <Skeleton variant="chart" />
      </div>
    </div>
  );
}

function Pending({ message }: { message: string }) {
  return (
    <div className="flex flex-col gap-4">
      <p role="status" className="text-body text-text">
        {message} This page updates automatically.
      </p>
      <DashboardSkeleton />
    </div>
  );
}

/**
 * GET …/versions/{v}/dashboard. 404 means "not built yet" (the version is not READY, or a
 * re-plan after a semantic-model change is rebuilding it): polled, shown as regenerating, never
 * as an error. The key includes the state change time so a new READY transition refetches.
 */
function useDashboard(version: DatasetVersion) {
  return useQuery({
    queryKey: [...queryKeys.dashboardSpec(version.id), version.state_changed_at],
    queryFn: () =>
      unwrap(
        getDashboardApiV1DatasetsDatasetIdVersionsVersionIdDashboardGet({
          path: { dataset_id: version.dataset_id, version_id: version.id },
        }),
      ),
    enabled: version.state === 'READY',
    retry: false,
    refetchInterval: (query) => (isNotFound(query.state.error) ? POLL_MS * 2 : false),
  });
}

function VersionDashboard({ version }: { version: DatasetVersion }) {
  const ref = { datasetId: version.dataset_id, versionId: version.id };
  const ready = version.state === 'READY';
  const dashboard = useDashboard(version);
  const model = useSemanticModel(ref, ready);
  const capabilities = useCapabilities(ref, model.data?.version);

  if (FAILED_STATES.has(version.state)) {
    return (
      <ErrorState
        message={rejectionText(
          version.failure?.code,
          version.failure?.message ?? 'Processing failed, so there is no dashboard.',
        )}
        hint={version.failure ? `Reason code: ${version.failure.code}` : undefined}
      />
    );
  }
  if (!ready) {
    const activity = activityOf(version.state);
    return (
      <Pending
        message={`Preparing the dashboard${activity ? `: ${activity.toLowerCase()}` : ''}…`}
      />
    );
  }
  if (isNotFound(dashboard.error)) {
    return <Pending message="Regenerating the dashboard for the latest changes…" />;
  }
  if (dashboard.isError) {
    return (
      <ErrorState {...describeError(dashboard.error)} onRetry={() => void dashboard.refetch()} />
    );
  }
  if (!dashboard.data) return <DashboardSkeleton />;
  return (
    <DashboardScreen
      spec={dashboard.data}
      capabilities={capabilities.data}
      semanticModelVersion={model.data?.version}
      loadValues={(name) =>
        unwrap(
          getDimensionValuesApiV1DatasetsDatasetIdVersionsVersionIdDimensionsNameValuesGet({
            path: { dataset_id: version.dataset_id, version_id: version.id, name },
            query: { limit: 200 },
          }),
        )
      }
    />
  );
}

/**
 * Dashboard tab (UI-UX §19, design.md §57): the DashboardSpec the backend built for this dataset
 * version, rendered by the spec-driven renderer with backend evidence and rows.
 */
export function DashboardTab() {
  const { version } = useWorkspace();
  if (!version) return <EmptyState message="This dataset has no versions yet." />;
  return <VersionDashboard key={version.id} version={version} />;
}
