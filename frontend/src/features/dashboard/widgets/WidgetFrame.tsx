import { useId, type ReactNode } from 'react';
import {
  Card,
  ProvenanceChip,
  ValidationBadge,
  type ProvenanceKind,
  type ValidationStatus,
} from '../../../components';
import { EvidenceLink, type EvidenceRequest } from '../../evidence/EvidenceDrawer';
import type { UnavailableWidget } from '../../../contracts/spec';
import type { Evidence } from '../dataSource';

export interface WidgetFrameProps {
  title: string;
  status: ValidationStatus;
  provenance?: ProvenanceKind;
  evidence: EvidenceRequest;
  footer?: ReactNode;
  children: ReactNode;
}

/** Trust-hierarchy kind of a tool result (UI-UX §7.2). */
export function provenanceOf(evidence: Evidence | undefined): ProvenanceKind {
  if (evidence?.kind === 'simulation') return 'SIMULATION';
  if (evidence?.model?.model_id || evidence?.model?.model_version) return 'MODEL_OUTPUT';
  return 'CALCULATION';
}

/**
 * Card shell for every data widget: a labelled region with title, provenance chip, validation
 * badge and an evidence link. Enforces the validation display rules (UI-UX §28): results that
 * need review or failed validation never show their numbers.
 */
export function WidgetFrame({
  title,
  status,
  provenance,
  evidence,
  footer,
  children,
}: WidgetFrameProps) {
  const headingId = useId();
  const withheld = status === 'REQUIRES_REVIEW' || status === 'INVALID';
  return (
    <Card className="h-full">
      <section aria-labelledby={headingId} className="flex h-full flex-col gap-3">
        <header className="flex flex-wrap items-start justify-between gap-2">
          <h3 id={headingId} className="text-h3 font-semibold">
            {title}
          </h3>
          <div className="flex flex-wrap items-center gap-1">
            {provenance && !withheld ? <ProvenanceChip kind={provenance} /> : null}
            <ValidationBadge status={status} />
          </div>
        </header>
        <div className="flex-1">
          {status === 'REQUIRES_REVIEW' ? (
            <p className="text-body text-text-muted">
              Held for review: the available data does not fully support this result, so its numbers
              are not shown.
            </p>
          ) : status === 'INVALID' ? (
            <p className="text-body text-text-muted">Not shown: this result failed validation.</p>
          ) : (
            children
          )}
        </div>
        <footer className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-caption text-text-muted">{withheld ? null : footer}</div>
          <EvidenceLink request={evidence} label={withheld ? 'View details →' : 'Evidence →'} />
        </footer>
      </section>
    </Card>
  );
}

function PlaceholderCard({ title, children }: { title: string; children: ReactNode }) {
  const headingId = useId();
  return (
    <Card className="h-full border-dashed">
      <section aria-labelledby={headingId} className="flex flex-col gap-2">
        <h3 id={headingId} className="text-h3 font-semibold">
          {title}
        </h3>
        {children}
      </section>
    </Card>
  );
}

/** A widget type this build does not know (forward compatibility, design.md §57). */
export function UnknownWidgetCard({ widgetType }: { widgetType: string }) {
  return (
    <PlaceholderCard title="Widget not supported">
      <p className="text-body text-text-muted">
        This dashboard contains a widget of type &ldquo;{widgetType}&rdquo; that this version of the
        app cannot display. The rest of the dashboard is unaffected.
      </p>
    </PlaceholderCard>
  );
}

/** A known widget type whose content failed contract validation. */
export function InvalidWidgetCard({ issues }: { issues: string[] }) {
  return (
    <PlaceholderCard title="Widget could not be displayed">
      <p role="alert" className="text-body text-error">
        This widget&apos;s definition is malformed, so it is not shown.
      </p>
      <details className="text-caption text-text-muted">
        <summary className="cursor-pointer">Technical details</summary>
        <ul className="mt-1 list-disc pl-5 font-mono">
          {issues.map((issue) => (
            <li key={issue}>{issue}</li>
          ))}
        </ul>
      </details>
    </PlaceholderCard>
  );
}

/** "Not available" card (UI-UX §20, §30): helpful, never an error, never "0" or "N/A". */
export function UnavailableWidgetCard({ widget }: { widget: UnavailableWidget }) {
  return (
    <PlaceholderCard title={widget.title}>
      <p className="text-body flex items-center gap-2 font-medium">
        <span aria-hidden="true" className="text-info">
          ⓘ
        </span>
        Not available: insufficient data
      </p>
      <p className="text-body text-text-muted">{widget.message}</p>
      {widget.would_enable ? (
        <p className="text-caption text-text">
          <span className="font-medium">What would enable it: </span>
          {widget.would_enable}
        </p>
      ) : null}
      <p className="text-caption font-mono text-text-muted">{widget.reason_code}</p>
    </PlaceholderCard>
  );
}
