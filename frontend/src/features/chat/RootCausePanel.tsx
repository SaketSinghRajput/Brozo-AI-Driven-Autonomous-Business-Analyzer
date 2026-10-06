import { useId } from 'react';
import { formatNumber } from '../../lib/format';
import { EvidenceLink, type EvidenceRequest } from '../evidence/EvidenceDrawer';

/** Mirrors backend/app/analytics/services/root_cause.py FactorFinding (fields used here). */
export interface DriverFinding {
  factor: string;
  relation_type: 'contribution' | 'association' | 'correlation';
  magnitude: number;
  unit: string | null;
  share_pct: number | null;
}

/** Fixed visual labels (UI-UX §23, design §43). Causation is refused by the backend contract. */
const RELATION_LABELS: Record<DriverFinding['relation_type'], string> = {
  contribution: 'Contribution',
  association: 'Association',
  correlation: 'Correlation',
};

export const CAUSAL_CERTAINTY =
  'Causal certainty: not established — these are the largest contributors';

function DriverChart({ findings }: { findings: DriverFinding[] }) {
  const max = Math.max(...findings.map((f) => Math.abs(f.magnitude)), 0) || 1;
  return (
    <table className="text-caption w-full">
      <caption className="sr-only">Largest contributors to the change</caption>
      <thead className="sr-only">
        <tr>
          <th scope="col">Factor</th>
          <th scope="col">Relation</th>
          <th scope="col">Change carried</th>
        </tr>
      </thead>
      <tbody>
        {findings.map((finding) => {
          const width = `${(Math.abs(finding.magnitude) / max) * 50}%`;
          const negative = finding.magnitude < 0;
          const share =
            finding.share_pct === null
              ? ''
              : // share_pct is a fraction of the total change (backend root_cause.py)
                ` (${formatNumber(finding.share_pct * 100, { precision: 1 })}% of change)`;
          return (
            <tr key={finding.factor}>
              <th scope="row" className="w-1/3 py-1 pr-2 text-left font-normal text-text">
                {finding.factor}
              </th>
              <td className="py-1 pr-2 text-text-muted">
                {RELATION_LABELS[finding.relation_type]}
              </td>
              <td className="w-1/2 py-1">
                <div className="relative h-4" aria-hidden="true">
                  <span className="absolute inset-y-0 left-1/2 w-px bg-border" />
                  <span
                    className={`absolute inset-y-0 ${
                      negative ? 'right-1/2 bg-warning/60' : 'left-1/2 bg-primary/60'
                    }`}
                    style={{ width }}
                  />
                </div>
                <span className="tabular-nums">
                  {formatNumber(finding.magnitude, { unit: finding.unit, precision: 2 })}
                  {share}
                </span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/**
 * Root-cause panel (UI-UX §23, TASK-UI-029): contributions to a change, never causes. The
 * factor list is the explain_change tool result; the chat stream carries only its evidence id
 * (no tool-output read endpoint yet), so without findings the panel links to the evidence.
 */
export function RootCausePanel({
  evidence,
  findings,
}: {
  evidence: EvidenceRequest;
  findings?: DriverFinding[];
}) {
  const headingId = useId();
  return (
    <section
      aria-labelledby={headingId}
      className="flex flex-col gap-2 rounded-md border border-border bg-surface-raised p-3"
    >
      <h4 id={headingId} className="text-caption font-semibold text-text">
        Contributing factors
      </h4>
      {findings && findings.length > 0 ? (
        <DriverChart findings={findings} />
      ) : (
        <p className="text-caption text-text-muted">
          The ranked factors are recorded with this result&apos;s evidence.
        </p>
      )}
      <p className="text-caption flex gap-1 text-text">
        <span aria-hidden="true">ⓘ</span>
        {CAUSAL_CERTAINTY}
      </p>
      <div>
        <EvidenceLink request={evidence} />
      </div>
    </section>
  );
}
