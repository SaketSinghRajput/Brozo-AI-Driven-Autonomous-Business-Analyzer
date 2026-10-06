import { Link } from 'react-router';
import type { DatasetVersion } from '../../api/generated/types.gen';
import { EmptyState, ErrorState } from '../../components/StateViews';
import { Card, Skeleton } from '../../components/Surfaces';
import { describeError } from '../../api/errors';
import { ConfidenceLabel } from '../../components/StatusBadges';
import { CapabilityReportView } from '../dashboard/CapabilityReportView';
import { MAPPED, useCapabilities, useSemanticModel, useUnderstanding } from '../semantic/api';
import { domainLabel } from '../semantic/DomainPanel';
import { FAILED_STATES, isProcessing, isProfiled, useQuarantine } from './api';
import { dateTime } from './DatasetsListPage';
import { useWorkspace } from './DatasetWorkspacePage';
import { explainFlag, rejectionText } from './reasons';

/** Review flags with plain-language explanations (UI-UX §15 "Needs your review"). */
function ReviewFlags({ flags }: { flags: string[] }) {
  if (flags.length === 0) {
    return <p className="text-body text-text-muted">Nothing needs your review.</p>;
  }
  return (
    <ul className="flex flex-col gap-2">
      {flags.map((flag) => {
        const { code, text } = explainFlag(flag);
        return (
          <li key={flag} className="text-body flex gap-2">
            <span aria-hidden="true" className="text-warning">
              ⚑
            </span>
            <span>
              <span className="text-text">{text}</span>{' '}
              <span className="text-caption font-mono text-text-muted">{code}</span>
            </span>
          </li>
        );
      })}
    </ul>
  );
}

/** Malformed-row sample: row numbers and reasons only, never row content (backend contract). */
function RejectedRows({ version }: { version: DatasetVersion }) {
  const quarantine = useQuarantine(version);
  if (!quarantine.data || quarantine.data.rows_rejected === 0) return null;
  const { rows_seen, rows_rejected, rejected_sample } = quarantine.data;
  return (
    <Card className="flex flex-col gap-2">
      <h2 className="text-h3 text-text">Rows set aside</h2>
      <p className="text-body text-text">
        {rows_rejected.toLocaleString()} of {rows_seen.toLocaleString()} rows couldn&apos;t be read
        and were not imported.
      </p>
      {rejected_sample.length > 0 ? (
        <table className="text-caption w-full max-w-xl text-left">
          <caption className="sr-only">Sample of rows that were set aside</caption>
          <thead>
            <tr className="text-text-muted">
              <th scope="col" className="pr-4 font-medium">
                Row
              </th>
              <th scope="col" className="font-medium">
                Reason
              </th>
            </tr>
          </thead>
          <tbody>
            {rejected_sample.map((row) => (
              <tr key={row.row_number}>
                <td className="pr-4 tabular-nums">{row.row_number}</td>
                <td>{row.reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </Card>
  );
}

/** Which analyses this version supports, for its current semantic model (PRD-PLAN-002). */
function Capabilities({ datasetId, version }: { datasetId: string; version: DatasetVersion }) {
  const ref = { datasetId, versionId: version.id };
  const model = useSemanticModel(ref);
  const report = useCapabilities(ref, model.data?.version);
  if (report.isError) {
    return <ErrorState {...describeError(report.error)} onRetry={() => void report.refetch()} />;
  }
  if (!report.data) return model.isError ? null : <Skeleton variant="card" />;
  return <CapabilityReportView report={report.data} />;
}

/** Detected domain + confidence level (UX-AC-02, U-3); details stay on the Understanding tab. */
function DomainSummary({ datasetId, version }: { datasetId: string; version: DatasetVersion }) {
  const understanding = useUnderstanding({ datasetId, versionId: version.id });
  const view = understanding.data;
  if (!view?.domain) return null;
  const { domain, domain_confidence_level: level } = view;
  const [first, second] = domain.candidates;
  const name =
    domain.status === 'KNOWN' && domain.primary
      ? domainLabel(domain.primary)
      : domain.status === 'MULTI_DOMAIN' && first && second
        ? `${domainLabel(first.domain)} + ${domainLabel(second.domain)}`
        : 'Unknown — generic analysis is used';
  return (
    <Card>
      <section aria-labelledby="domain-heading" className="flex flex-col gap-2">
        <h2 id="domain-heading" className="text-h3 text-text">
          Domain
        </h2>
        <p className="text-body flex flex-wrap items-center gap-2 text-text">
          <strong>{name}</strong>
          {level ? (
            <ConfidenceLabel confidence={{ level, score: domain.confidence, basis: [] }} />
          ) : null}
        </p>
        <Link
          to={`/datasets/${datasetId}/understanding`}
          className="text-body text-primary underline focus-visible:outline-2 focus-visible:outline-focus"
        >
          Why this domain?
        </Link>
      </section>
    </Card>
  );
}

/** Dataset Overview tab (UI-UX §15, TASK-UI-005): lifecycle, domain, review flags, rejected rows. */
export function OverviewTab() {
  const { dataset, version } = useWorkspace();
  if (!version) return <EmptyState message="This dataset has no versions yet." />;

  const failed = FAILED_STATES.has(version.state);
  const flags = version.review_flags ?? [];
  const profileLink = `/datasets/${dataset.id}/profile?version=${version.id}`;

  return (
    <div className="flex max-w-4xl flex-col gap-4">
      {failed ? (
        <ErrorState
          message={rejectionText(
            version.failure?.code,
            version.failure?.message ?? 'Processing failed.',
          )}
          hint={version.failure ? `Reason code: ${version.failure.code}` : undefined}
        />
      ) : null}
      {isProcessing(version.state) ? (
        <p role="status" className="text-body text-text">
          This version is still being processed. This page updates automatically.
        </p>
      ) : null}

      <Card className="flex flex-col gap-2">
        <h2 className="text-h3 text-text">Version {version.version_no}</h2>
        <dl className="text-body grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1">
          <dt className="text-text-muted">Uploaded</dt>
          <dd className="text-text">{dateTime.format(new Date(version.created_at))}</dd>
          <dt className="text-text-muted">State since</dt>
          <dd className="text-text">{dateTime.format(new Date(version.state_changed_at))}</dd>
          <dt className="text-text-muted">Rows</dt>
          <dd className="text-text">
            {version.row_count != null ? version.row_count.toLocaleString() : 'Not known yet'}
          </dd>
          <dt className="text-text-muted">File fingerprint</dt>
          <dd className="font-mono text-text-muted" title={version.source_sha256}>
            {version.source_sha256.slice(0, 19)}…
          </dd>
        </dl>
        {isProfiled(version.state) ? (
          <Link
            to={profileLink}
            className="text-body text-primary underline focus-visible:outline-2 focus-visible:outline-focus"
          >
            View the data profile
          </Link>
        ) : null}
      </Card>

      {isProfiled(version.state) ? (
        <DomainSummary datasetId={dataset.id} version={version} />
      ) : null}

      <Card className="flex flex-col gap-2">
        <h2 className="text-h3 text-text">
          Needs your review{flags.length > 0 ? ` (${flags.length})` : ''}
        </h2>
        <ReviewFlags flags={flags} />
      </Card>

      <RejectedRows version={version} />
      {MAPPED.has(version.state) ? <Capabilities datasetId={dataset.id} version={version} /> : null}
    </div>
  );
}
