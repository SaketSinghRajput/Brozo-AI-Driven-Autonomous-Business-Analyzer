import { useId, useState, type FormEvent, type RefObject } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { Filter, MetricRequest } from '../../api/generated/types.gen';
import type {
  DashboardFilter,
  MetricRequest as SpecMetricRequest,
} from '../../contracts/generated/dashboard_spec';
import { Button } from '../../components';

export type { Filter };

type Member = string | number | boolean;

/** Distinct values of a dimension for a picker (GET …/dimensions/{name}/values). */
export interface ValueList {
  values: Array<Member | null>;
  truncated: boolean;
}
export type LoadValues = (dimension: string) => Promise<ValueList>;

/** "group in A, B" / "day between 2026-01-01 and 2026-03-31": shown on every filtered card. */
export function filterText(filter: Filter): string {
  const { value } = filter;
  if (filter.op === 'between' && Array.isArray(value)) {
    return `${filter.dimension} between ${String(value[0])} and ${String(value[1])}`;
  }
  const shown = Array.isArray(value) ? value.join(', ') : String(value);
  return `${filter.dimension} ${filter.op === 'in' ? 'in' : '='} ${shown}`;
}

/** The clause of one dashboard filter, or null for "no filter" (backend FilterBinder._clause). */
export function clauseFor(definition: DashboardFilter, values: Member[]): Filter | null {
  if (values.length === 0) return null;
  const { dimension } = definition;
  if (definition.type === 'date_range') {
    return values.length === 2 ? { dimension, op: 'between', value: values } : null;
  }
  if (definition.type === 'single_select') return { dimension, op: 'eq', value: values[0] };
  return { dimension, op: 'in', value: values };
}

/**
 * A widget's `query` with the active dashboard filters applied, exactly as the backend
 * FilterBinder.bind does: each active clause replaces any filter the widget already had on the
 * same dimension; kept filters first, then the clauses (in filter-id order, see `orderClauses`).
 */
export function bindQuery(query: SpecMetricRequest, active: Filter[]): MetricRequest {
  const bound = new Set(active.map((clause) => clause.dimension));
  const kept = (query.filters ?? []).filter((filter) => !bound.has(filter.dimension));
  return { ...query, filters: [...kept, ...active] };
}

/** Clauses sorted by their filter id, as FilterBinder sorts `active.items()`. */
function orderClauses(clauses: Filter[], definitions: DashboardFilter[]): Filter[] {
  const idOf = new Map(definitions.map((d) => [d.dimension, d.id]));
  return [...clauses].sort((a, b) => {
    const x = idOf.get(a.dimension) ?? '';
    const y = idOf.get(b.dimension) ?? '';
    return x < y ? -1 : x > y ? 1 : 0;
  });
}

/** Replaces (or, with null, removes) the clause on `dimension`, keeping FilterBinder order. */
export function withClause(
  active: Filter[],
  definitions: DashboardFilter[],
  dimension: string,
  clause: Filter | null,
): Filter[] {
  const others = active.filter((filter) => filter.dimension !== dimension);
  return orderClauses(clause ? [...others, clause] : others, definitions);
}

function selected(active: Filter | undefined): Member[] {
  if (!active) return [];
  return Array.isArray(active.value) ? active.value : [active.value];
}

/**
 * A control's pending selection. It follows external changes of the active clause (drill-down,
 * chip removal, clear) without re-mounting the control, so keyboard focus is kept on Apply.
 */
function useSelection(active: Filter | undefined) {
  const [chosen, setChosen] = useState<Member[]>(selected(active));
  const [source, setSource] = useState(active);
  if (source !== active) {
    setSource(active);
    setChosen(selected(active));
  }
  return [chosen, setChosen] as const;
}

const inputClass =
  'text-body rounded-sm border border-border bg-surface px-2 py-1 text-text focus-visible:outline-2 focus-visible:outline-focus';

function DateRangeControl({
  definition,
  active,
  onApply,
}: {
  definition: DashboardFilter;
  active: Filter | undefined;
  onApply: (filter: Filter | null) => void;
}) {
  const [range, setRange] = useSelection(active);
  const from = String(range[0] ?? '');
  const to = String(range[1] ?? '');
  const setFrom = (next: string) => setRange([next, to]);
  const setTo = (next: string) => setRange([from, next]);
  function submit(event: FormEvent) {
    event.preventDefault();
    onApply(from && to ? clauseFor(definition, [from, to]) : null);
  }
  return (
    <form onSubmit={submit} className="text-caption flex flex-wrap items-end gap-2">
      <label className="flex flex-col gap-1">
        {definition.dimension} from
        <input
          type="date"
          value={from}
          max={to || undefined}
          onChange={(e) => setFrom(e.target.value)}
          className={inputClass}
        />
      </label>
      <label className="flex flex-col gap-1">
        to
        <input
          type="date"
          value={to}
          min={from || undefined}
          onChange={(e) => setTo(e.target.value)}
          className={inputClass}
        />
      </label>
      <Button type="submit" size="sm" variant="secondary">
        Apply
      </Button>
    </form>
  );
}

function PickListControl({
  definition,
  active,
  onApply,
  loadValues,
  cacheKey,
}: {
  definition: DashboardFilter;
  active: Filter | undefined;
  onApply: (filter: Filter | null) => void;
  loadValues: LoadValues;
  cacheKey: string;
}) {
  const legendId = useId();
  const values = useQuery({
    queryKey: ['version', cacheKey, 'dimension-values', definition.dimension],
    queryFn: () => loadValues(definition.dimension),
    staleTime: Infinity, // a version's members never change
  });
  const [chosen, setChosen] = useSelection(active);
  const members = (values.data?.values ?? []).filter((v): v is Member => v !== null);
  const multi = definition.type === 'multi_select';

  function submit(event: FormEvent) {
    event.preventDefault();
    onApply(clauseFor(definition, chosen));
  }
  function toggle(member: Member, on: boolean) {
    setChosen((current) => (on ? [...current, member] : current.filter((m) => m !== member)));
  }

  return (
    <form onSubmit={submit} className="text-caption flex flex-wrap items-end gap-2">
      <fieldset aria-labelledby={legendId} className="flex min-w-48 flex-col gap-1">
        <legend id={legendId} className="font-medium">
          {definition.dimension}
        </legend>
        {values.isPending ? <p className="text-text-muted">Loading values…</p> : null}
        {values.isError ? (
          <p className="text-error">Values could not be loaded.</p>
        ) : multi ? (
          <ul className="max-h-40 overflow-auto rounded-sm border border-border p-1">
            {members.map((member) => (
              <li key={String(member)}>
                <label className="flex items-center gap-2 px-1">
                  <input
                    type="checkbox"
                    checked={chosen.includes(member)}
                    onChange={(e) => toggle(member, e.target.checked)}
                  />
                  {String(member)}
                </label>
              </li>
            ))}
          </ul>
        ) : (
          <select
            aria-labelledby={legendId}
            value={chosen.length ? String(chosen[0]) : ''}
            onChange={(e) =>
              setChosen(
                e.target.value === ''
                  ? []
                  : members.filter((member) => String(member) === e.target.value),
              )
            }
            className={inputClass}
          >
            <option value="">All</option>
            {members.map((member) => (
              <option key={String(member)} value={String(member)}>
                {String(member)}
              </option>
            ))}
          </select>
        )}
        {values.data?.truncated ? (
          <p className="text-text-muted">Showing the first {members.length} values.</p>
        ) : null}
      </fieldset>
      <Button type="submit" size="sm" variant="secondary">
        Apply
      </Button>
    </form>
  );
}

/**
 * Dashboard filter bar (UI-UX §19, TASK-UI-013) built from the spec's filter definitions: pick
 * lists for categorical dimensions (values from the backend, <= 200), a date range for the time
 * dimension (the backend serves no pick list for it). The applied clauses follow FilterBinder.
 */
export function FilterBar({
  definitions,
  value,
  onChange,
  loadValues,
  cacheKey,
  sectionRef,
}: {
  definitions: DashboardFilter[];
  value: Filter[];
  onChange: (filters: Filter[]) => void;
  loadValues: LoadValues;
  /** Scopes cached pick lists (the dataset version id). */
  cacheKey: string;
  /** The filter region; focused when the control that had focus goes away (remove, clear). */
  sectionRef: RefObject<HTMLElement | null>;
}) {
  if (definitions.length === 0) return null;

  function apply(dimension: string, filter: Filter | null) {
    onChange(withClause(value, definitions, dimension, filter));
  }
  function removeAndRefocus(next: Filter[]) {
    onChange(next);
    sectionRef.current?.focus();
  }

  return (
    <section
      ref={sectionRef}
      tabIndex={-1}
      aria-label="Filters"
      className="flex flex-col gap-2 rounded-md border border-border p-3 focus-visible:outline-2 focus-visible:outline-focus"
    >
      <div className="flex flex-wrap gap-4">
        {definitions.map((definition) => {
          const active = value.find((filter) => filter.dimension === definition.dimension);
          const onApply = (filter: Filter | null) => apply(definition.dimension, filter);
          return definition.type === 'date_range' ? (
            <DateRangeControl
              key={definition.id}
              definition={definition}
              active={active}
              onApply={onApply}
            />
          ) : (
            <PickListControl
              key={definition.id}
              definition={definition}
              active={active}
              onApply={onApply}
              loadValues={loadValues}
              cacheKey={cacheKey}
            />
          );
        })}
      </div>
      {value.length > 0 ? (
        <div className="flex flex-wrap items-center gap-2">
          <ul aria-label="Active filters" className="flex flex-wrap gap-2">
            {value.map((filter) => (
              <li
                key={filter.dimension}
                className="text-caption inline-flex items-center gap-1 rounded-full border border-border px-2 py-0.5"
              >
                {filterText(filter)}
                <button
                  type="button"
                  aria-label={`Remove filter ${filterText(filter)}`}
                  onClick={() =>
                    removeAndRefocus(withClause(value, definitions, filter.dimension, null))
                  }
                  className="rounded-full px-1 hover:bg-surface-raised"
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
          <Button type="button" size="sm" variant="secondary" onClick={() => removeAndRefocus([])}>
            Clear filters
          </Button>
        </div>
      ) : null}
    </section>
  );
}
