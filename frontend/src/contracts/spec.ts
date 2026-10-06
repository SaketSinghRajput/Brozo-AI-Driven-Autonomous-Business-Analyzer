import type {
  ChartWidget,
  DashboardSpec,
  Insight,
  KpiCardWidget,
  SummarySentence,
  UnavailableWidget,
} from './generated/dashboard_spec';
import {
  validateChartWidget,
  validateDashboardSpec,
  validateKpiCardWidget,
  validateUnavailableWidget,
  type ValidateFunction,
} from './generated/validators';

export type {
  ChartWidget,
  DashboardSpec,
  Insight,
  KpiCardWidget,
  SummarySentence,
  UnavailableWidget,
};

/** The contract major version this build renders (dashboard_spec.schema.json `schema_version`). */
const SUPPORTED_MAJOR = '1';

/**
 * One entry per widget in the spec, in spec order. Unknown / malformed widgets do not fail the
 * whole dashboard (UI-UX S-09: "Failed widgets show error cards; the others render").
 */
export type ResolvedWidget =
  | { kind: 'kpi_card'; id: string; widget: KpiCardWidget }
  | { kind: 'chart'; id: string; widget: ChartWidget }
  | { kind: 'unavailable'; id: string; widget: UnavailableWidget }
  | { kind: 'unknown'; id: string; widgetType: string }
  | { kind: 'invalid'; id: string; issues: string[] };

export type ParsedSpec =
  | { status: 'ok'; spec: DashboardSpec; widgets: ResolvedWidget[] }
  | { status: 'invalid'; issues: string[] }
  | { status: 'upgrade_required'; version: string };

const CHART_TYPES = new Set([
  'line_chart',
  'bar_chart',
  'stacked_bar',
  'area_chart',
  'scatter',
  'table',
  'heatmap',
]);

function issuesOf(validate: ValidateFunction): string[] {
  return (validate.errors ?? []).map(
    (error) => `${error.instancePath || '(root)'} ${error.message ?? 'is invalid'}`,
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function resolveWidget(raw: unknown, index: number): ResolvedWidget {
  const record = isRecord(raw) ? raw : {};
  const id = typeof record.id === 'string' ? record.id : `widget-${index}`;
  const widgetType = typeof record.widget_type === 'string' ? record.widget_type : '';

  const check = (validate: ValidateFunction): string[] | null =>
    validate(raw) ? null : issuesOf(validate);

  if (widgetType === 'kpi_card') {
    const issues = check(validateKpiCardWidget);
    return issues
      ? { kind: 'invalid', id, issues }
      : { kind: 'kpi_card', id, widget: raw as KpiCardWidget };
  }
  if (widgetType === 'unavailable') {
    const issues = check(validateUnavailableWidget);
    return issues
      ? { kind: 'invalid', id, issues }
      : { kind: 'unavailable', id, widget: raw as UnavailableWidget };
  }
  if (CHART_TYPES.has(widgetType)) {
    const issues = check(validateChartWidget);
    return issues
      ? { kind: 'invalid', id, issues }
      : { kind: 'chart', id, widget: raw as ChartWidget };
  }
  return { kind: 'unknown', id, widgetType: widgetType || '(missing)' };
}

/**
 * Runtime validation of an untrusted DashboardSpec (design.md §57). The envelope is validated
 * strictly against the generated schema validator; widgets are validated one by one so an
 * unknown widget type becomes a placeholder instead of rejecting the whole dashboard.
 */
export function parseDashboardSpec(input: unknown): ParsedSpec {
  if (!isRecord(input)) return { status: 'invalid', issues: ['(root) must be an object'] };

  const version = input.schema_version;
  if (typeof version === 'string' && version.split('.')[0] !== SUPPORTED_MAJOR) {
    return { status: 'upgrade_required', version };
  }
  if (!Array.isArray(input.widgets)) {
    return { status: 'invalid', issues: ['/widgets must be an array'] };
  }

  if (!validateDashboardSpec({ ...input, widgets: [] })) {
    return { status: 'invalid', issues: issuesOf(validateDashboardSpec) };
  }

  const widgets = input.widgets.map(resolveWidget);
  return { status: 'ok', spec: input as unknown as DashboardSpec, widgets };
}

/**
 * Resolves an `EvidenceRef.field` (dashboard_spec 1.1): a dotted path into the Evidence record,
 * dict keys and list indexes as digits, as the backend validator resolves it
 * (app.validator.numerical.resolve), e.g. "result.value". The number there, else null.
 */
export function evidenceNumber(record: unknown, field: string): number | null {
  let node: unknown = record;
  for (const part of field.split('.')) {
    if (Array.isArray(node) && /^\d+$/.test(part)) node = node[Number(part)];
    else if (isRecord(node) && part in node) node = node[part];
    else return null;
  }
  return typeof node === 'number' ? node : null;
}
