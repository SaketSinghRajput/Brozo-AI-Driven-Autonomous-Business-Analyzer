import { Skeleton } from '../../../components';
import { evidenceNumber, type KpiCardWidget } from '../../../contracts/spec';
import { formatCompact, formatNumber } from '../../../lib/format';
import { useOpenEvidence } from '../../evidence/EvidenceDrawer';
import { useEvidence } from '../dataSource';
import { WidgetFrame, provenanceOf } from './WidgetFrame';

/**
 * KPI card (UI-UX §20, design.md §55). The value is read from the referenced evidence record,
 * never from the spec or computed here. The comparison shows the baseline value itself plus a
 * direction arrow + text; no derived "% change" is computed in the browser (CLAUDE.md §13).
 */
export function KpiWidget({ widget }: { widget: KpiCardWidget }) {
  const openEvidence = useOpenEvidence();
  const value = useEvidence(widget.value_ref.evidence_id);
  const comparison = useEvidence(widget.comparison_ref?.evidence_id ?? null);

  const current = evidenceNumber(value.data, widget.value_ref.field);
  const spec = widget.format;
  const format = {
    unit: spec?.unit ?? value.data?.result.unit,
    currency: spec?.currency,
    locale: spec?.locale,
    precision: spec?.precision ?? (current === null || Number.isInteger(current) ? 0 : 2),
  };
  const full = current === null ? null : formatNumber(current, format);
  const claim = full === null ? widget.title : `${widget.title}: ${full}`;
  const request = {
    evidenceId: widget.value_ref.evidence_id,
    claim,
    status: widget.validation_status,
  };

  const ref = widget.comparison_ref;
  const prior = ref ? evidenceNumber(comparison.data, ref.field) : null;

  return (
    <WidgetFrame
      title={widget.title}
      status={widget.validation_status}
      provenance={provenanceOf(value.data)}
      evidence={request}
      footer={widget.validation_status === 'UNCERTAIN' ? 'Unverified: shown with caution' : null}
    >
      {value.isPending ? <Skeleton variant="text" className="h-8" /> : null}
      {value.isError ? <p className="text-body text-error">Value could not be loaded.</p> : null}
      {value.data && current === null ? (
        <p className="text-body text-text-muted">No value in this result.</p>
      ) : null}
      {current !== null && full !== null ? (
        <button
          type="button"
          onClick={() => openEvidence(request)}
          title={full}
          aria-label={`${claim}. Open evidence`}
          className="text-display rounded-sm font-semibold tabular-nums hover:underline"
        >
          {formatCompact(current, format)}
        </button>
      ) : null}
      {ref && current !== null && typeof prior === 'number' ? (
        <p className="text-caption mt-1 text-text-muted">
          <span aria-hidden="true">{current > prior ? '▲ ' : current < prior ? '▼ ' : '■ '}</span>
          {current > prior ? 'Higher than' : current < prior ? 'Lower than' : 'Same as'} {ref.label}{' '}
          (
          <button
            type="button"
            className="tabular-nums underline decoration-dotted"
            onClick={() =>
              openEvidence({
                evidenceId: ref.evidence_id,
                claim: `${widget.title}, ${ref.label}: ${formatNumber(prior, format)}`,
                status: widget.validation_status,
              })
            }
          >
            {formatCompact(prior, format)}
          </button>
          )
        </p>
      ) : null}
    </WidgetFrame>
  );
}
