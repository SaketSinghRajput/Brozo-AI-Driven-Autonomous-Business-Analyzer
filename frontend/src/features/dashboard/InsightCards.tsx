import { Card, ProvenanceChip, ValidationBadge, type ValidationStatus } from '../../components';
import type { Insight, SummarySentence } from '../../contracts/spec';
import type { EvidencedText } from '../../contracts/generated/dashboard_spec';
import { EvidenceLink } from '../evidence/EvidenceDrawer';

/** Insight + executive-summary shapes come from the DashboardSpec 1.1 contract. */
export type { Insight, SummarySentence };

function EvidenceLinks({ item, status }: { item: EvidencedText; status: ValidationStatus }) {
  return (
    <>
      {item.evidence_ids.map((evidenceId, index) => (
        <EvidenceLink
          key={evidenceId}
          request={{ evidenceId, claim: item.text, status }}
          label={item.evidence_ids.length > 1 ? `Evidence ${index + 1} →` : 'Evidence →'}
        />
      ))}
    </>
  );
}

/**
 * Executive summary (UI-UX §19): 1-3 validated sentences marked as AI interpretation. Sentences
 * needing review are withheld; invalid ones are never shown (UI-UX §28).
 */
export function ExecutiveSummary({ sentences }: { sentences: SummarySentence[] }) {
  const shown = sentences.filter(
    (sentence) =>
      sentence.validation_status === 'VALID' || sentence.validation_status === 'UNCERTAIN',
  );
  const held = sentences.filter(
    (sentence) => sentence.validation_status === 'REQUIRES_REVIEW',
  ).length;
  if (shown.length === 0 && held === 0) return null;
  return (
    <Card>
      <section aria-labelledby="executive-summary" className="flex flex-col gap-2">
        <header className="flex flex-wrap items-center gap-2">
          <h2 id="executive-summary" className="text-h3 font-semibold">
            Executive summary
          </h2>
          <ProvenanceChip kind="INTERPRETATION" />
        </header>
        <ul className="flex flex-col gap-2">
          {shown.slice(0, 3).map((sentence) => (
            <li key={sentence.text} className="flex flex-wrap items-center gap-2">
              <span>{sentence.text}</span>
              <ValidationBadge status={sentence.validation_status} />
              <EvidenceLinks item={sentence} status={sentence.validation_status} />
            </li>
          ))}
        </ul>
        {held > 0 ? (
          <p className="text-caption text-text-muted">
            {held === 1 ? '1 statement is' : `${held} statements are`} held for review and not
            shown.
          </p>
        ) : null}
      </section>
    </Card>
  );
}

/**
 * Insight cards (UI-UX §22). Only VALID insights are shown (PRD-DASH-006). "Observed" and
 * "Possible explanation" are always visually separated; explanations are labelled as not
 * proven causes.
 */
export function InsightCards({ insights }: { insights: Insight[] }) {
  const valid = insights.filter((insight) => insight.validation_status === 'VALID');
  if (valid.length === 0) return null;
  return (
    <section aria-labelledby="insights-heading" className="flex flex-col gap-3">
      <h2 id="insights-heading" className="text-h3 font-semibold">
        Insights
      </h2>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {valid.map((insight) => (
          <Card key={insight.id}>
            <article aria-label="Insight" className="flex flex-col gap-3">
              <div className="flex flex-wrap items-center justify-end gap-1">
                <ProvenanceChip kind="INTERPRETATION" />
                <ValidationBadge status={insight.validation_status} />
              </div>
              <div>
                <h3 className="text-label font-semibold tracking-wide text-text-muted uppercase">
                  Observed
                </h3>
                <p className="text-body mt-1 font-medium">{insight.observed.text}</p>
                <div className="mt-1 flex gap-3">
                  <ProvenanceChip kind="CALCULATION" />
                  <EvidenceLinks item={insight.observed} status={insight.validation_status} />
                </div>
              </div>
              {(insight.possible_explanations ?? []).length > 0 ? (
                <div className="border-t border-dashed border-border pt-3">
                  <h3 className="text-label font-medium text-text-muted">
                    Possible explanation (largest contributors, not proven causes)
                  </h3>
                  <ul className="mt-1 flex flex-col gap-1">
                    {(insight.possible_explanations ?? []).map((explanation) => (
                      <li
                        key={explanation.text}
                        className="text-caption flex flex-wrap items-center gap-2 text-text-muted"
                      >
                        <span>{explanation.text}</span>
                        <EvidenceLinks item={explanation} status={insight.validation_status} />
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </article>
          </Card>
        ))}
      </div>
    </section>
  );
}
