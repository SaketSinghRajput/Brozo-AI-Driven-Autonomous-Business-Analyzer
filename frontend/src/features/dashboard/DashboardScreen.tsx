import { useRef, useState } from 'react';
import { parseDashboardSpec } from '../../contracts/spec';
import { CapabilityReportView } from './CapabilityReportView';
import { DashboardRenderer, type DashboardFiltering } from './DashboardRenderer';
import { FilterBar, clauseFor, withClause, type Filter, type LoadValues } from './FilterBar';
import { ExecutiveSummary, InsightCards } from './InsightCards';

/**
 * The whole dashboard screen in UI-UX §19 order, for one DashboardSpec: executive summary,
 * filters, widgets, insights, the analyses report. Used by the dataset workspace tab (backend
 * data) and the fixture preview; the data source comes from the surrounding provider.
 * Filters re-query widgets only when `semanticModelVersion` is known (a backend dataset).
 */
export function DashboardScreen({
  spec,
  capabilities,
  semanticModelVersion,
  loadValues,
}: {
  spec: unknown;
  capabilities?: unknown;
  semanticModelVersion?: number;
  /** Pick-list values of a filter dimension. */
  loadValues: LoadValues;
}) {
  const [filters, setFilters] = useState<Filter[]>([]);
  const filtersRef = useRef<HTMLElement>(null);
  const parsed = parseDashboardSpec(spec);
  const dashboard = parsed.status === 'ok' ? parsed.spec : null;
  const definitions = dashboard?.filters ?? [];

  const backend =
    dashboard && semanticModelVersion !== undefined
      ? { datasetVersionId: dashboard.dataset_version_id, semanticModelVersion }
      : undefined;

  function drill(dimension: string, value: string | number) {
    const definition = definitions.find((d) => d.dimension === dimension);
    if (!definition) return;
    setFilters((current) =>
      withClause(current, definitions, dimension, clauseFor(definition, [value])),
    );
    // The drilled chart re-renders (re-query), so its picker goes away: move focus to the
    // filter region, which now lists the new filter.
    filtersRef.current?.focus();
  }

  const filtering: DashboardFiltering = {
    filters,
    backend,
    drillDimensions: new Set(
      definitions.filter((d) => d.type !== 'date_range').map((d) => d.dimension),
    ),
    onDrill: drill,
  };

  return (
    <div className="flex flex-col gap-6">
      <ExecutiveSummary sentences={dashboard?.summary ?? []} />
      <FilterBar
        definitions={definitions}
        value={filters}
        onChange={setFilters}
        loadValues={loadValues}
        cacheKey={dashboard?.dataset_version_id ?? ''}
        sectionRef={filtersRef}
      />
      {filters.length > 0 && !backend ? (
        <p role="status" className="text-caption text-text-muted">
          Sample data: filtered values are recomputed by the backend for real datasets; the cards
          below show the unfiltered sample.
        </p>
      ) : null}
      <DashboardRenderer spec={spec} filtering={filtering} />
      <InsightCards insights={dashboard?.insights ?? []} />
      {capabilities !== undefined ? <CapabilityReportView report={capabilities} /> : null}
    </div>
  );
}
