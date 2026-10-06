import { useState, type FormEvent } from 'react';
import { ApiError, describeError } from '../../api/errors';
import type { OverrideInput, SemanticMeasure, SemanticModel } from '../../api/generated/types.gen';
import { Button, Card, ErrorState, ProvenanceChip } from '../../components';
import { requiredRoleText } from '../../context/permissions';
import { usePatchSemanticModel, type VersionRef } from './api';

/** Backend Aggregation vocabulary (app/data/semantic/domain.py). */
const AGGREGATIONS: SemanticMeasure['default_agg'][] = [
  'sum',
  'avg',
  'weighted_avg',
  'min',
  'max',
  'count',
  'count_distinct',
  'median',
  'last',
];

interface Row {
  column: string;
  meaning: string;
  role: string;
  unit: string | null;
  measure?: SemanticMeasure;
}

function rows(model: SemanticModel): Row[] {
  return [
    ...model.measures.map((m) => ({
      column: m.column,
      meaning: m.name,
      role: `Measure (${m.default_agg.replace(/_/g, ' ')})`,
      unit: m.unit,
      measure: m,
    })),
    ...model.dimensions.map((d) => ({
      column: d.column,
      meaning: d.name,
      role: `Dimension (${d.type})`,
      unit: null,
    })),
    ...model.entities.map((e) => ({
      column: e.key,
      meaning: e.name,
      role: 'Identifier',
      unit: null,
    })),
  ];
}

function MeasureEditor({
  measure,
  onSave,
  onCancel,
  saving,
}: {
  measure: SemanticMeasure;
  onSave: (overrides: OverrideInput[]) => void;
  onCancel: () => void;
  saving: boolean;
}) {
  const [unit, setUnit] = useState(measure.unit ?? '');
  const [aggregation, setAggregation] = useState(measure.default_agg);

  function submit(event: FormEvent) {
    event.preventDefault();
    const changes: OverrideInput[] = [];
    if (unit !== (measure.unit ?? '')) {
      changes.push({ field: `measures.${measure.name}.unit`, value: unit });
    }
    if (aggregation !== measure.default_agg) {
      changes.push({ field: `measures.${measure.name}.default_agg`, value: aggregation });
    }
    if (changes.length > 0) onSave(changes);
    else onCancel();
  }

  return (
    <form onSubmit={submit} className="text-caption flex flex-wrap items-end gap-2">
      <label className="flex flex-col gap-1">
        Unit
        <input
          value={unit}
          onChange={(event) => setUnit(event.target.value)}
          className="rounded-sm border border-border bg-surface px-2 py-1 text-text"
        />
      </label>
      <label className="flex flex-col gap-1">
        Aggregation
        <select
          value={aggregation}
          onChange={(event) => setAggregation(event.target.value as SemanticMeasure['default_agg'])}
          className="rounded-sm border border-border bg-surface px-2 py-1 text-text"
        >
          {AGGREGATIONS.map((agg) => (
            <option key={agg} value={agg}>
              {agg.replace(/_/g, ' ')}
            </option>
          ))}
        </select>
      </label>
      <Button type="submit" size="sm" loading={saving}>
        Save
      </Button>
      <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
        Cancel
      </Button>
    </form>
  );
}

/**
 * Semantic mapping (UI-UX §18): what each column means. Analysts confirm a mapping or edit a
 * measure's unit / aggregation; every save is a PATCH against the model version on screen
 * (optimistic concurrency) and creates a new semantic model version.
 */
export function MappingTable({
  model,
  flags,
  versionRef,
  canEdit,
  onReload,
}: {
  model: SemanticModel;
  flags: string[];
  versionRef: VersionRef;
  canEdit: boolean;
  onReload: () => void;
}) {
  const patch = usePatchSemanticModel(versionRef);
  const [editing, setEditing] = useState<string | null>(null);
  const confirmed = new Set(
    (model.overrides ?? [])
      .filter((o) => o.field.startsWith('mapping.'))
      .map((o) => o.field.slice('mapping.'.length)),
  );
  const ambiguous = new Set(
    flags.filter((f) => f.startsWith('MAPPING_AMBIGUOUS:')).map((f) => f.split(':')[1]),
  );
  const stale = patch.error instanceof ApiError && patch.error.status === 409;

  function save(overrides: OverrideInput[]) {
    patch.mutate({ baseVersion: model.version, overrides }, { onSuccess: () => setEditing(null) });
  }

  return (
    <Card className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-h3 text-text">What each column means</h2>
        <span className="text-caption text-text-muted">Semantic model v{model.version}</span>
      </div>
      {!canEdit ? (
        <p className="text-caption text-text-muted">
          {requiredRoleText('EDIT_SEMANTICS')} to confirm or edit mappings.
        </p>
      ) : null}
      {stale ? (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-2 rounded-md border border-warning/40 bg-warning/10 p-3"
        >
          <p className="text-body text-text">
            This mapping was changed since you opened it. Reload to see the latest version, then
            apply your change again.
          </p>
          <Button
            size="sm"
            variant="secondary"
            onClick={() => {
              patch.reset();
              onReload();
            }}
          >
            Reload
          </Button>
        </div>
      ) : null}
      {patch.isError && !stale ? <ErrorState {...describeError(patch.error)} /> : null}
      {patch.isSuccess ? (
        <p role="status" className="text-caption text-success">
          Saved as semantic model v{patch.data.version}. Updating analyses…
        </p>
      ) : null}

      <div className="overflow-x-auto">
        <table className="text-caption w-full text-left">
          <thead className="text-text-muted">
            <tr>
              <th scope="col" className="py-1 pr-3 font-medium">
                Column
              </th>
              <th scope="col" className="py-1 pr-3 font-medium">
                Meaning
              </th>
              <th scope="col" className="py-1 pr-3 font-medium">
                Role
              </th>
              <th scope="col" className="py-1 pr-3 font-medium">
                Unit
              </th>
              <th scope="col" className="py-1 pr-3 font-medium">
                Status
              </th>
              {canEdit ? (
                <th scope="col" className="py-1 font-medium">
                  <span className="sr-only">Actions</span>
                </th>
              ) : null}
            </tr>
          </thead>
          <tbody>
            {rows(model).map((row) => (
              <tr key={`${row.role}-${row.column}`} className="border-t border-border align-top">
                <td className="py-2 pr-3 font-mono">{row.column}</td>
                <td className="py-2 pr-3 text-text">{row.meaning}</td>
                <td className="py-2 pr-3">{row.role}</td>
                <td className="py-2 pr-3">{row.unit ?? '—'}</td>
                <td className="py-2 pr-3">
                  {confirmed.has(row.column) ? (
                    <ProvenanceChip kind="USER_DECISION" />
                  ) : ambiguous.has(row.column) ? (
                    <span className="text-warning">⚑ Needs review</span>
                  ) : (
                    <span className="text-text-muted">✓ Auto</span>
                  )}
                </td>
                {canEdit ? (
                  <td className="py-2">
                    {editing === row.column && row.measure ? (
                      <MeasureEditor
                        measure={row.measure}
                        saving={patch.isPending}
                        onSave={save}
                        onCancel={() => setEditing(null)}
                      />
                    ) : (
                      <div className="flex flex-wrap gap-1">
                        {!confirmed.has(row.column) ? (
                          <Button
                            size="sm"
                            variant="secondary"
                            aria-label={`Confirm ${row.column} means ${row.meaning}`}
                            disabled={patch.isPending}
                            onClick={() =>
                              save([{ field: `mapping.${row.column}`, value: row.meaning }])
                            }
                          >
                            Confirm
                          </Button>
                        ) : null}
                        {row.measure ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            aria-label={`Edit ${row.column}`}
                            onClick={() => setEditing(row.column)}
                          >
                            Edit
                          </Button>
                        ) : null}
                      </div>
                    )}
                  </td>
                ) : null}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {model.metrics.length > 0 ? (
        <div>
          <h3 className="text-caption font-semibold text-text-muted">Metric definitions</h3>
          <ul className="text-caption mt-1 flex flex-col gap-1">
            {model.metrics.map((metric) => (
              <li key={metric.name}>
                <span className="font-medium text-text">{metric.name}</span> ={' '}
                <span className="font-mono text-text-muted">{metric.expr}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Card>
  );
}
