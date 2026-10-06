import type { CSSProperties, ReactNode } from 'react';
import { ErrorState } from '../../components';
import { parseDashboardSpec, type ResolvedWidget } from '../../contracts/spec';
import type { LayoutItem } from '../../contracts/generated/dashboard_spec';
import { bindQuery, type Filter } from './FilterBar';
import { ChartWidget } from './widgets/ChartWidget';
import { FilteredChart, FilteredKpi, type FilterBackend } from './widgets/FilteredWidgets';
import { KpiWidget } from './widgets/KpiWidget';
import { InvalidWidgetCard, UnavailableWidgetCard, UnknownWidgetCard } from './widgets/WidgetFrame';

/** Active dashboard filters and what widgets may do with them (TASK-UI-013). */
export interface DashboardFiltering {
  /** Active clauses in FilterBinder order. */
  filters: Filter[];
  /** Re-query target; absent for fixture payloads (nothing to re-query). */
  backend?: FilterBackend;
  /** Dimensions with a pick-list filter: a chart over one of them offers drill-down. */
  drillDimensions: ReadonlySet<string>;
  onDrill: (dimension: string, value: string | number) => void;
}

function NotFilterable({ children }: { children: ReactNode }) {
  return (
    <div className="flex h-full flex-col gap-1">
      {children}
      <p className="text-caption text-text-muted">Not filterable: shows all data.</p>
    </div>
  );
}

/**
 * Frontend widget registry (design.md §57): resolved widget kind -> fixed component. Nothing
 * from the spec is ever evaluated as code, HTML or a format string. Under active filters a
 * widget with a `query` is re-queried (new evidence); one without is marked "not filterable".
 */
function renderWidget(
  resolved: ResolvedWidget,
  versionId: string,
  filtering?: DashboardFiltering,
): ReactNode {
  const active = filtering && filtering.filters.length > 0 ? filtering : undefined;
  switch (resolved.kind) {
    case 'kpi_card': {
      const { widget } = resolved;
      const plain = <KpiWidget widget={widget} />;
      if (!active) return plain;
      if (!widget.query) return <NotFilterable>{plain}</NotFilterable>;
      if (!active.backend) return plain;
      const request = bindQuery(widget.query, active.filters);
      return <FilteredKpi widget={widget} request={request} backend={active.backend} />;
    }
    case 'chart': {
      const { widget } = resolved;
      const x = widget.chart.x?.field;
      const onDrill =
        filtering && x && filtering.drillDimensions.has(x)
          ? (value: string | number) => filtering.onDrill(x, value)
          : undefined;
      const plain = <ChartWidget widget={widget} versionId={versionId} onDrill={onDrill} />;
      if (!active) return plain;
      if (!widget.query) return <NotFilterable>{plain}</NotFilterable>;
      if (!active.backend) return plain;
      const request = bindQuery(widget.query, active.filters);
      return (
        <FilteredChart
          widget={widget}
          request={request}
          backend={active.backend}
          onDrill={onDrill}
        />
      );
    }
    case 'unavailable':
      return <UnavailableWidgetCard widget={resolved.widget} />;
    case 'invalid':
      return <InvalidWidgetCard issues={resolved.issues} />;
    case 'unknown':
      return <UnknownWidgetCard widgetType={resolved.widgetType} />;
  }
}

/** Grid placement from the spec's 12-column layout; single column below the lg breakpoint. */
function placement(item: LayoutItem | undefined): CSSProperties | undefined {
  if (!item) return undefined;
  const x = Math.min(item.x, 11);
  return {
    '--grid-col': `${x + 1} / span ${Math.min(item.w, 12 - x)}`,
    '--grid-row': `${item.y + 1} / span ${item.h}`,
  } as CSSProperties;
}

/**
 * Renders an untrusted DashboardSpec (UI-UX §19, design.md §57). Invalid specs show a safe
 * error state; unknown or malformed widgets show placeholders while the others render.
 */
export function DashboardRenderer({
  spec,
  filtering,
}: {
  spec: unknown;
  /** Active dashboard filters (TASK-UI-013); omitted = the spec's results. */
  filtering?: DashboardFiltering;
}) {
  const parsed = parseDashboardSpec(spec);

  if (parsed.status === 'upgrade_required') {
    return (
      <ErrorState
        message="This dashboard was created by a newer version of the app."
        hint={`Dashboard format ${parsed.version} is not supported here. Update the app to view it.`}
      />
    );
  }
  if (parsed.status === 'invalid') {
    return (
      <div className="flex flex-col gap-2">
        <ErrorState
          message="This dashboard couldn't be displayed."
          hint="Its definition did not pass validation, so nothing from it is shown."
        />
        <details className="text-caption text-text-muted">
          <summary className="cursor-pointer">Technical details</summary>
          <ul className="mt-1 list-disc pl-5 font-mono">
            {parsed.issues.map((issue) => (
              <li key={issue}>{issue}</li>
            ))}
          </ul>
        </details>
      </div>
    );
  }

  const { spec: dashboard, widgets } = parsed;
  if (widgets.length === 0) {
    return (
      <p className="text-body text-text-muted">
        This data doesn&apos;t support automatic analyses yet.
      </p>
    );
  }

  const layout = new Map(dashboard.layout.items.map((item) => [item.id, item]));
  // Reading order follows the visual order (row, then column); unplaced widgets go last.
  const ordered = [...widgets].sort((a, b) => {
    const pa = layout.get(a.id);
    const pb = layout.get(b.id);
    if (!pa || !pb) return pa ? -1 : pb ? 1 : 0;
    return pa.y - pb.y || pa.x - pb.x;
  });

  return (
    <div role="region" aria-label="Dashboard" className="grid grid-cols-1 gap-4 lg:grid-cols-12">
      {ordered.map((resolved) => (
        <div
          key={resolved.id}
          style={placement(layout.get(resolved.id))}
          className="min-w-0 lg:[grid-column:var(--grid-col,1/-1)] lg:[grid-row:var(--grid-row,auto)]"
        >
          {renderWidget(resolved, dashboard.dataset_version_id, filtering)}
        </div>
      ))}
    </div>
  );
}
