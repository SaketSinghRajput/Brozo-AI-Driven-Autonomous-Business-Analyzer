import { useState } from 'react';
import { Link } from 'react-router';
import type { DatasetState } from '../../api/generated/types.gen';
import { describeError } from '../../api/errors';
import { Button } from '../../components/Button';
import { ErrorState } from '../../components/StateViews';
import { cancelJob, FAILED_STATES, isProcessing, useJob, useVersion } from './api';
import { rejectionText } from './reasons';

/**
 * Lifecycle order (backend app/data/domain.py DatasetState). A state names the last stage
 * reached; while a version sits in it, `activity` describes what the worker is doing next
 * (VALIDATING / INGESTING / ANALYZING are themselves in-progress states).
 */
const STEPS: { state: DatasetState; label: string; activity: string }[] = [
  { state: 'UPLOADED', label: 'Received', activity: 'Waiting for a worker' },
  { state: 'VALIDATING', label: 'Validated', activity: 'Checking the file is safe to read' },
  { state: 'INGESTING', label: 'Rows read', activity: 'Reading and storing rows' },
  { state: 'INGESTED', label: 'Rows stored', activity: 'Checking completeness and value ranges' },
  { state: 'PROFILED', label: 'Profiled', activity: 'Understanding columns' },
  { state: 'SCHEMA_ANALYZED', label: 'Columns understood', activity: 'Detecting business domain' },
  { state: 'DOMAIN_IDENTIFIED', label: 'Domain detected', activity: 'Mapping business meaning' },
  { state: 'SEMANTICALLY_MAPPED', label: 'Meaning mapped', activity: 'Planning analyses' },
  { state: 'ANALYZING', label: 'Analysed', activity: 'Running analyses' },
  { state: 'READY', label: 'Ready', activity: 'Building dashboard' },
];

/** Where each failure state stops the timeline. */
const FAILED_AT: Partial<Record<DatasetState, DatasetState>> = {
  VALIDATION_FAILED: 'VALIDATING',
  INGESTION_FAILED: 'INGESTING',
  ANALYSIS_FAILED: 'ANALYZING',
};

// Every stage now runs automatically (run_plan drives the version to READY).
const AUTO_LAST_INDEX = STEPS.findIndex((s) => s.state === 'READY');

/** What the worker does next for a version in `state` (e.g. "Running analyses"). */
export function activityOf(state: DatasetState): string | null {
  return STEPS.find((s) => s.state === state)?.activity ?? null;
}
const ACTIVE_JOB = new Set(['QUEUED', 'RUNNING', 'RETRYING']);

export interface ProcessingTimelineProps {
  datasetId: string;
  versionId: string;
  jobId: string;
}

/**
 * Processing timeline (UI-UX §14): each line is a real DatasetVersion state from the API; no
 * fake percentages. Steps after "Profiled" are shown only once the backend reaches them.
 */
export function ProcessingTimeline({ datasetId, versionId, jobId }: ProcessingTimelineProps) {
  const version = useVersion(datasetId, versionId);
  const job = useJob(jobId);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<unknown>(null);

  if (version.isError) {
    return <ErrorState {...describeError(version.error)} onRetry={() => void version.refetch()} />;
  }
  if (!version.data) return <p className="text-caption text-text-muted">Loading status…</p>;

  const data = version.data;
  const failed = FAILED_STATES.has(data.state);
  const current = STEPS.findIndex((s) => s.state === (FAILED_AT[data.state] ?? data.state));
  const visible = STEPS.slice(0, Math.max(current + 1, AUTO_LAST_INDEX + 1));
  const processing = isProcessing(data.state);
  const jobActive = job.data !== undefined && ACTIVE_JOB.has(job.data.status);
  const progress = job.data?.progress ?? 0;
  const flagCount = data.review_flags?.length ?? 0;

  async function handleCancel() {
    setCancelling(true);
    setCancelError(null);
    try {
      await cancelJob(jobId);
      await job.refetch();
    } catch (caught) {
      setCancelError(caught);
    } finally {
      setCancelling(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <ol aria-label="Processing steps" aria-live="polite" className="flex flex-col gap-2">
        {visible.map((step, index) => {
          const stepFailed = index === current && failed;
          const active = index === current && processing;
          const done = !stepFailed && !active && index <= current;
          const icon = stepFailed ? '✕' : done ? '✓' : active ? '●' : '○';
          const status = stepFailed ? 'failed' : done ? 'done' : active ? 'in progress' : 'pending';
          const tone = stepFailed
            ? 'text-error'
            : done
              ? 'text-success'
              : active
                ? 'animate-pulse text-primary motion-reduce:animate-none'
                : 'text-text-muted';
          return (
            <li key={step.state} className="text-body flex items-baseline gap-2">
              <span aria-hidden="true" className={tone}>
                {icon}
              </span>
              <span className={done || active || stepFailed ? 'text-text' : 'text-text-muted'}>
                {active ? `${step.activity}…` : step.label}
              </span>
              <span className="sr-only">({status})</span>
              {active && progress > 0 && progress < 1 ? (
                <span className="text-caption text-text-muted">{Math.round(progress * 100)}%</span>
              ) : null}
              {step.state === 'INGESTING' && done && data.row_count != null ? (
                <span className="text-caption text-text-muted">
                  {data.row_count.toLocaleString()} rows
                </span>
              ) : null}
            </li>
          );
        })}
      </ol>

      {failed ? (
        <ErrorState
          message={rejectionText(data.failure?.code, data.failure?.message ?? 'Processing failed.')}
          hint={data.failure ? `Reason code: ${data.failure.code}` : undefined}
        />
      ) : null}

      {processing && jobActive ? (
        <div className="flex flex-wrap items-center gap-3">
          <Button
            variant="secondary"
            size="sm"
            loading={cancelling}
            disabled={job.data?.cancel_requested}
            onClick={() => void handleCancel()}
          >
            {job.data?.cancel_requested ? 'Cancelling…' : 'Cancel processing'}
          </Button>
          <p className="text-caption text-text-muted">
            You can leave this page; we&apos;ll notify you when it finishes.
          </p>
        </div>
      ) : null}
      {cancelError ? <ErrorState {...describeError(cancelError)} /> : null}

      {!processing && !failed ? (
        <p className="text-body text-text">
          <span aria-hidden="true">✓ </span>
          Processing finished.{' '}
          <Link
            to={`/datasets/${datasetId}/profile?version=${versionId}`}
            className="text-primary underline focus-visible:outline-2 focus-visible:outline-focus"
          >
            View the data profile
          </Link>
          {flagCount > 0 ? (
            <>
              {' · '}
              <Link
                to={`/datasets/${datasetId}/overview?version=${versionId}`}
                className="text-primary underline focus-visible:outline-2 focus-visible:outline-focus"
              >
                {flagCount} {flagCount === 1 ? 'item needs' : 'items need'} your review
              </Link>
            </>
          ) : null}
        </p>
      ) : null}
    </div>
  );
}
