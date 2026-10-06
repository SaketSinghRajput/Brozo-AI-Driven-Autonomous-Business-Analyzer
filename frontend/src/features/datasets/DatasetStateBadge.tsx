import type { DatasetState } from '../../api/generated/types.gen';
import { Badge, type BadgeVariant } from '../../components/StatusBadges';
import { FAILED_STATES, isProcessing } from './api';

const LABELS: Record<DatasetState, string> = {
  UPLOADED: 'Received',
  VALIDATING: 'Validating',
  VALIDATION_FAILED: 'Validation failed',
  INGESTING: 'Reading rows',
  INGESTION_FAILED: 'Import failed',
  INGESTED: 'Profiling',
  PROFILED: 'Profiled',
  SCHEMA_ANALYZED: 'Columns understood',
  DOMAIN_IDENTIFIED: 'Domain detected',
  SEMANTICALLY_MAPPED: 'Meaning mapped',
  ANALYZING: 'Analysing',
  READY: 'Ready',
  ANALYSIS_FAILED: 'Analysis failed',
};

/** Lifecycle state as icon + text (colour is never the only signal, UI-UX §48). */
export function DatasetStateBadge({ state }: { state: DatasetState }) {
  const failed = FAILED_STATES.has(state);
  const processing = isProcessing(state);
  const variant: BadgeVariant = failed ? 'error' : processing ? 'info' : 'success';
  const icon = failed ? '✕' : processing ? '◌' : '✓';
  return (
    <Badge variant={variant} icon={icon}>
      {LABELS[state]}
    </Badge>
  );
}
