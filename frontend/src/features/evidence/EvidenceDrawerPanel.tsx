import { useEffect, useRef } from 'react';
import { ConfidenceLabel, Skeleton, ValidationBadge } from '../../components';
import { useEvidence, type Evidence } from '../dashboard/dataSource';
import { formatNumber } from '../../lib/format';
import type { EvidenceRequest } from './EvidenceDrawer';

/**
 * Evidence drawer (UI-UX §27, O-01). Native modal <dialog>: Esc closes, the background is
 * inert (focus stays inside), focus returns to the opener on close. Loaded on first open
 * (EvidenceDrawerProvider), so it and the evidence validator stay out of the shell bundle.
 */
export function EvidenceDrawerPanel({
  request,
  onClose,
}: {
  request: EvidenceRequest;
  onClose: () => void;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const evidence = useEvidence(request.evidenceId);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
  }, []);

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby="evidence-title"
      onClose={onClose}
      className="fixed inset-y-0 right-0 left-auto m-0 h-dvh max-h-dvh w-full max-w-lg overflow-y-auto border-l border-border bg-surface-raised p-6 text-text shadow-lg backdrop:bg-text/30"
    >
      <div className="flex items-start justify-between gap-4">
        <h2 id="evidence-title" className="text-h2 font-semibold">
          Evidence
        </h2>
        <button
          type="button"
          onClick={() => dialogRef.current?.close()}
          className="text-caption rounded-md border border-border px-2 py-1 hover:bg-surface"
        >
          Close
        </button>
      </div>

      <dl className="mt-4 grid grid-cols-[8rem_1fr] gap-x-3 gap-y-2">
        <dt className="text-caption text-text-muted">Claim</dt>
        <dd>{request.claim}</dd>
        <dt className="text-caption text-text-muted">Validation</dt>
        <dd>
          <ValidationBadge status={request.status} />
        </dd>
        {evidence.data ? <EvidenceFields evidence={evidence.data} /> : null}
      </dl>

      {evidence.isPending ? <Skeleton variant="table" rows={6} className="mt-4" /> : null}
      {evidence.isError ? (
        <p role="alert" className="mt-4 text-error">
          Evidence not available for {request.evidenceId}.
        </p>
      ) : null}
      {evidence.data ? <Technical evidence={evidence.data} /> : null}
    </dialog>
  );
}

function modelText(evidence: Evidence): string {
  const model = evidence.model;
  if (!model?.model_id && !model?.model_version) return 'None (deterministic calculation)';
  return [
    model.model_id,
    model.model_version && `version ${model.model_version}`,
    model.model_run_id && `run ${model.model_run_id}`,
  ]
    .filter(Boolean)
    .join(' · ');
}

const PROVENANCE_STEPS = ['Claim', 'Dataset', 'Query / tool', 'Calculation', 'Model', 'Confidence'];

function EvidenceFields({ evidence }: { evidence: Evidence }) {
  const { result } = evidence;
  const args = Object.entries(evidence.arguments);
  return (
    <>
      <dt className="text-caption text-text-muted">Source</dt>
      <dd>
        Dataset {evidence.dataset_id ?? '—'} · version {evidence.dataset_version_id ?? '—'}
      </dd>
      <dt className="text-caption text-text-muted">Query / tool</dt>
      <dd>
        {evidence.operation} (tool {evidence.tool_version})
      </dd>
      <dt className="text-caption text-text-muted">Period &amp; filters</dt>
      <dd>
        {args.length === 0 ? (
          'None'
        ) : (
          <ul>
            {args.map(([key, value]) => (
              <li key={key}>
                {key}: {typeof value === 'string' ? value : JSON.stringify(value)}
              </li>
            ))}
          </ul>
        )}
      </dd>
      <dt className="text-caption text-text-muted">Calculation</dt>
      <dd>{evidence.calculation ?? '—'}</dd>
      <dt className="text-caption text-text-muted">Result</dt>
      <dd className="tabular-nums">
        {typeof result.value === 'number'
          ? formatNumber(result.value, { unit: result.unit, precision: 2 })
          : '—'}
        {typeof result.rows === 'number' ? ` · ${result.rows} rows` : ''}
      </dd>
      <dt className="text-caption text-text-muted">Model</dt>
      <dd>{modelText(evidence)}</dd>
      <dt className="text-caption text-text-muted">Confidence</dt>
      <dd>
        {evidence.confidence ? (
          <ConfidenceLabel confidence={evidence.confidence} />
        ) : (
          'Not assessed'
        )}
      </dd>
      <dt className="text-caption text-text-muted">Provenance</dt>
      <dd>
        <ol
          aria-label="Provenance chain"
          className="text-caption flex flex-wrap items-center gap-1"
        >
          {PROVENANCE_STEPS.map((step, index) => (
            <li key={step} className="flex items-center gap-1">
              {index > 0 ? <span aria-hidden="true">→</span> : null}
              <span className="rounded-full border border-border px-2 py-0.5">{step}</span>
            </li>
          ))}
        </ol>
      </dd>
    </>
  );
}

/**
 * Level 3 detail (UI-UX §7.3), collapsed by default.
 * ponytail: shown to everyone until RBAC exists; gate on VIEW_QUERY (design §59) then.
 */
function Technical({ evidence }: { evidence: Evidence }) {
  return (
    <details className="mt-6 border-t border-border pt-4">
      <summary className="cursor-pointer font-medium">Technical details</summary>
      <dl className="text-caption mt-2 grid grid-cols-[8rem_1fr] gap-x-3 gap-y-1 break-all">
        <dt className="text-text-muted">Evidence ID</dt>
        <dd className="font-mono">{evidence.evidence_id}</dd>
        {evidence.query ? (
          <>
            <dt className="text-text-muted">Engine</dt>
            <dd>{evidence.query.engine}</dd>
            <dt className="text-text-muted">Query</dt>
            <dd>
              <pre className="font-mono whitespace-pre-wrap">{evidence.query.sql}</pre>
            </dd>
          </>
        ) : null}
        <dt className="text-text-muted">Result hash</dt>
        <dd className="font-mono">{evidence.result.result_hash}</dd>
        <dt className="text-text-muted">Semantic model</dt>
        <dd>{evidence.semantic_model_version ?? '—'}</dd>
        <dt className="text-text-muted">Software</dt>
        <dd>{evidence.software_version}</dd>
        <dt className="text-text-muted">Created</dt>
        <dd>{evidence.created_at}</dd>
      </dl>
    </details>
  );
}
