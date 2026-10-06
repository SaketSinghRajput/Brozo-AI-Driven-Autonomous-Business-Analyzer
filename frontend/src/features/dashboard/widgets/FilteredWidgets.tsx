import type { ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import { describeError, unwrap } from '../../../api/errors';
import { analyticsQueryApiV1AnalyticsQueryPost } from '../../../api/generated/sdk.gen';
import type { MetricRequest } from '../../../api/generated/types.gen';
import { ErrorState, Skeleton } from '../../../components';
import type { ChartWidget as ChartWidgetSpec, KpiCardWidget } from '../../../contracts/spec';
import { filterText } from '../FilterBar';
import { ChartWidget } from './ChartWidget';
import { KpiWidget } from './KpiWidget';

/** Where filtered widgets are re-queried: a backend dataset version and its semantic model. */
export interface FilterBackend {
  datasetVersionId: string;
  semanticModelVersion: number;
}

/**
 * POST /api/v1/analytics/query with a widget's bound query (FilterBar.bindQuery). The backend
 * runs it as analytics.metric_query and returns a NEW evidence record, so the widget still reads
 * its numbers from evidence and its rows from GET /evidence/{id}/data.
 */
function useRequery(request: MetricRequest, backend: FilterBackend) {
  const { datasetVersionId, semanticModelVersion } = backend;
  return useQuery({
    queryKey: ['version', datasetVersionId, 'filtered', semanticModelVersion, request],
    queryFn: () =>
      unwrap(
        analyticsQueryApiV1AnalyticsQueryPost({
          body: {
            dataset_version_id: datasetVersionId,
            semantic_model_version: semanticModelVersion,
            request,
          },
        }),
      ),
    staleTime: Infinity, // same request on the same version + model = same result
  });
}

function Requeried({
  request,
  backend,
  skeleton,
  children,
}: {
  request: MetricRequest;
  backend: FilterBackend;
  skeleton: 'card' | 'chart';
  children: (evidenceId: string) => ReactNode;
}) {
  const result = useRequery(request, backend);
  if (result.isPending) return <Skeleton variant={skeleton} />;
  if (result.isError) {
    return <ErrorState {...describeError(result.error)} onRetry={() => void result.refetch()} />;
  }
  const label = (request.filters ?? []).map(filterText).join(' · ');
  return (
    <div className="flex h-full flex-col gap-1">
      {children(result.data.evidence_id)}
      <p className="text-caption text-text-muted">Filtered: {label}</p>
    </div>
  );
}

/** A KPI card under dashboard filters (UI-UX §19); the unfiltered comparison no longer applies. */
export function FilteredKpi({
  widget,
  request,
  backend,
}: {
  widget: KpiCardWidget;
  request: MetricRequest;
  backend: FilterBackend;
}) {
  return (
    <Requeried request={request} backend={backend} skeleton="card">
      {(evidenceId) => (
        <KpiWidget
          widget={{
            ...widget,
            value_ref: { evidence_id: evidenceId, field: 'result.value' },
            comparison_ref: undefined,
          }}
        />
      )}
    </Requeried>
  );
}

/**
 * A chart under dashboard filters: new evidence, its rows via /data. Overlays (forecast,
 * anomalies) were computed on the unfiltered data, so they are not drawn over filtered rows.
 */
export function FilteredChart({
  widget,
  request,
  backend,
  onDrill,
}: {
  widget: ChartWidgetSpec;
  request: MetricRequest;
  backend: FilterBackend;
  onDrill?: (value: string | number) => void;
}) {
  return (
    <Requeried request={request} backend={backend} skeleton="chart">
      {(evidenceId) => (
        <ChartWidget
          widget={{
            ...widget,
            chart: {
              ...widget.chart,
              evidence_id: evidenceId,
              overlays: [],
              filters: request.filters,
            },
          }}
          versionId={backend.datasetVersionId}
          onDrill={onDrill}
        />
      )}
    </Requeried>
  );
}
