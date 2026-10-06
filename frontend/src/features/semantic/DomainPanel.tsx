import { useId, useState, type FormEvent } from 'react';
import { describeError } from '../../api/errors';
import type { DomainClassification, UnderstandingView } from '../../api/generated/types.gen';
import { Button, Card, ErrorState, ProvenanceChip } from '../../components';
import { requiredRoleText } from '../../context/permissions';
import { useOverrideDomain, type VersionRef } from './api';

const DOMAIN_ID = /^[a-z][a-z0-9_]*$/; // backend DomainOverrideRequest.domain pattern

/** Domain ids come from the backend's domain packs; shown as words, never hardcoded here. */
export function domainLabel(domain: string): string {
  const text = domain.replace(/_+/g, ' ').trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function Headline({ domain }: { domain: DomainClassification }) {
  const [first, second] = domain.candidates;
  if (domain.status === 'KNOWN' && domain.primary) {
    return (
      <p className="text-body text-text">
        Detected domain: <strong>{domainLabel(domain.primary)}</strong>
      </p>
    );
  }
  if (domain.status === 'MULTI_DOMAIN' && first && second) {
    return (
      <p className="text-body text-text">
        This data looks like <strong>{domainLabel(first.domain)}</strong> +{' '}
        <strong>{domainLabel(second.domain)}</strong>
      </p>
    );
  }
  return (
    <div className="flex items-start gap-2 rounded-md border border-info/30 bg-info/5 p-3">
      <span aria-hidden="true" className="text-info">
        ⓘ
      </span>
      <p className="text-body text-text">
        {domain.status === 'AMBIGUOUS' ? 'Domain: ambiguous. ' : 'Domain: unknown. '}
        We could not confidently identify the domain — generic analysis is used. General analyses
        are still available.
      </p>
    </div>
  );
}

function Signals({ domain }: { domain: DomainClassification }) {
  const top = domain.candidates[0];
  const others = domain.candidates.slice(1);
  return (
    <>
      {top && top.evidence.length > 0 ? (
        <div>
          <h3 className="text-caption font-semibold text-text-muted">
            Why{domain.status === 'KNOWN' ? '' : ` (${domainLabel(top.domain)})`}
          </h3>
          <ul className="text-caption mt-1 list-disc pl-5 text-text">
            {top.evidence.map((signal, index) => (
              <li key={index}>{signal.detail}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {others.length > 0 ? (
        <p className="text-caption text-text-muted">
          {domain.status === 'KNOWN' ? 'Other possibilities: ' : 'Candidates: '}
          {others.map((c) => domainLabel(c.domain)).join(', ')}
        </p>
      ) : null}
      {/* ponytail: the classification confidence is an uncalibrated number with no backend
          level mapping, so it is not shown as a level or percentage (UI-UX §29). */}
    </>
  );
}

function OverrideForm({ view, versionRef }: { view: UnderstandingView; versionRef: VersionRef }) {
  const override = useOverrideDomain(versionRef);
  const [other, setOther] = useState('');
  const inputId = useId();
  const current = view.domain?.primary ?? null;
  const choices = (view.domain?.candidates ?? []).map((c) => c.domain).filter((d) => d !== current);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (DOMAIN_ID.test(other)) override.mutate(other);
  }

  return (
    <div className="flex flex-col gap-2 border-t border-border pt-3">
      {choices.length > 0 ? (
        <div role="group" aria-label="Choose the best fit" className="flex flex-wrap gap-2">
          {choices.map((domain) => (
            <Button
              key={domain}
              size="sm"
              variant="secondary"
              loading={override.isPending && override.variables === domain}
              onClick={() => override.mutate(domain)}
            >
              Use {domainLabel(domain)}
            </Button>
          ))}
        </div>
      ) : null}
      <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
        <label htmlFor={inputId} className="text-caption flex flex-col gap-1 text-text">
          Other domain id
          <input
            id={inputId}
            value={other}
            onChange={(event) => setOther(event.target.value.trim())}
            pattern="[a-z][a-z0-9_]*"
            className="text-body rounded-sm border border-border bg-surface px-2 py-1 text-text"
          />
        </label>
        <Button type="submit" size="sm" variant="secondary" disabled={!DOMAIN_ID.test(other)}>
          Set domain
        </Button>
      </form>
      {override.isSuccess ? (
        <p role="status" className="text-caption text-success">
          Saved. Updating analyses…
        </p>
      ) : null}
      {override.isError ? <ErrorState {...describeError(override.error)} /> : null}
    </div>
  );
}

/** Domain detection (UI-UX §17): result, signals, candidates and (Analyst+) the override. */
export function DomainPanel({
  view,
  versionRef,
  canEdit,
}: {
  view: UnderstandingView;
  versionRef: VersionRef;
  canEdit: boolean;
}) {
  return (
    <Card className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-h3 text-text">Business domain</h2>
        {view.domain_source === 'user_override' ? <ProvenanceChip kind="USER_DECISION" /> : null}
      </div>
      {view.domain ? (
        <>
          <Headline domain={view.domain} />
          <Signals domain={view.domain} />
        </>
      ) : (
        <p className="text-body text-text-muted">The domain has not been identified yet.</p>
      )}
      {canEdit ? (
        <OverrideForm view={view} versionRef={versionRef} />
      ) : (
        <p className="text-caption text-text-muted">
          {requiredRoleText('EDIT_SEMANTICS')} to change the domain.
        </p>
      )}
    </Card>
  );
}
