import { Fragment, type ReactNode } from 'react';
import {
  ConfidenceLabel,
  InsufficientDataState,
  ProvenanceChip,
  ValidationBadge,
  type ProvenanceKind,
  type ValidationStatus,
} from '../../components';
import type {
  NumberView,
  Segment,
  ToolCallEvent,
  TurnState,
} from '../../contracts/generated/turn_event';
import { useAuth } from '../../context/AuthContext';
import { formatNumber } from '../../lib/format';
import { useOpenEvidence, type EvidenceRequest } from '../evidence/EvidenceDrawer';
import { RootCausePanel } from './RootCausePanel';
import { validationStatus, type TurnView } from './turn';

/** Business wording for the streamed stages (UI-UX §24); no chain-of-thought is shown. */
const STAGE_LABELS: Partial<Record<TurnState, string>> = {
  CLASSIFYING: 'Understanding your question',
  PLANNING: 'Planning the analysis',
  EXECUTING: 'Checking data',
  COMPOSING: 'Writing the answer',
  VALIDATING: 'Checking results',
  RETRYING: 'Re-checking the wording',
};

/** "analytics.explain_change" -> "Explain change" (UI-UX §26 business wording). */
export function toolLabel(name: string): string {
  const text = (name.split('.').pop() ?? name).replace(/_+/g, ' ').trim();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function toolKind(name: string): ProvenanceKind {
  if (name.startsWith('ml.')) return 'MODEL_OUTPUT';
  if (name.startsWith('simulation.')) return 'SIMULATION';
  return 'CALCULATION';
}

/**
 * Evidence-bound value as text, in the backend's unit vocabulary (lib/format.ts). Precision
 * matches app/orchestrator/composer.py display_format: pct/pp 1 decimal of the x100 display
 * (the value is a FRACTION), count 0, money and plain numbers 2. `absolute` (B-5): a direction
 * word ("fell by", "a … decrease") states the sign, so the size is shown without it, as in the
 * backend's claim text (app/orchestrator/composer.py `_directed`).
 */
export function numberText(segment: NumberView): string | null {
  const { value, unit } = segment;
  if (value === null) return null;
  const precision = unit === 'pct' || unit === 'pp' ? 1 : unit === 'count' ? 0 : 2;
  return formatNumber(segment.absolute ? Math.abs(value) : value, { unit, precision });
}

export function StageChips({ stages, streaming }: { stages: TurnState[]; streaming: boolean }) {
  const shown = stages.filter((stage) => STAGE_LABELS[stage]);
  if (shown.length === 0) return null;
  return (
    <ol aria-label="Progress" className="flex flex-wrap gap-1">
      {shown.map((stage, index) => {
        const current = streaming && index === shown.length - 1;
        return (
          <li
            key={`${stage}-${index}`}
            aria-current={current ? 'step' : undefined}
            className={`text-label inline-flex items-center gap-1 rounded-full border px-2 py-0.5 ${
              current ? 'border-primary text-primary' : 'border-border text-text-muted'
            }`}
          >
            <span aria-hidden="true">{current ? '●' : '✓'}</span>
            {STAGE_LABELS[stage]}
          </li>
        );
      })}
    </ol>
  );
}

const TOOL_STATUS: Record<ToolCallEvent['status'], { icon: string; text: string }> = {
  ok: { icon: '✓', text: '' },
  unavailable: { icon: '–', text: 'not available for this data' },
  error: { icon: '✕', text: 'failed' },
  rejected: { icon: '✕', text: 'not allowed' },
};

/** "Checked" list (UI-UX §26); tool names and argument names are Level 3 (VIEW_QUERY). */
function ExecutionSummary({ tools, status }: { tools: ToolCallEvent[]; status: ValidationStatus }) {
  const { can } = useAuth();
  if (tools.length === 0) return null;
  return (
    <details className="text-caption">
      <summary className="cursor-pointer text-text-muted">Checked ({tools.length})</summary>
      <ul className="mt-1 flex flex-col gap-1 pl-4">
        {tools.map((tool, index) => {
          const { icon, text } = TOOL_STATUS[tool.status];
          const label = toolLabel(tool.name);
          return (
            <li key={`${tool.name}-${index}`} className="flex flex-wrap items-center gap-2">
              <span aria-hidden="true">{icon}</span>
              <span>
                {label}
                {text ? ` — ${text}` : ''}
              </span>
              {tool.status === 'ok' && tool.evidence_id ? (
                <EvidenceButton request={{ evidenceId: tool.evidence_id, claim: label, status }}>
                  Evidence →
                </EvidenceButton>
              ) : null}
              {can('VIEW_QUERY') ? (
                <span className="font-mono text-text-muted">
                  {tool.name}({tool.args_summary}){tool.error_code ? ` ${tool.error_code}` : ''}
                </span>
              ) : null}
            </li>
          );
        })}
      </ul>
    </details>
  );
}

function EvidenceButton({
  request,
  children,
  className = 'text-primary underline-offset-2 hover:underline',
}: {
  request: EvidenceRequest;
  children: ReactNode;
  className?: string;
}) {
  const open = useOpenEvidence();
  return (
    <button
      type="button"
      onClick={() => open(request)}
      aria-label={`Evidence for ${request.claim}`}
      className={`rounded-sm font-medium focus-visible:outline-2 focus-visible:outline-focus ${className}`}
    >
      {children}
    </button>
  );
}

/** Numbers render from evidence (design §42) and open the evidence drawer in one click. */
function AnswerText({ segments, status }: { segments: Segment[]; status: ValidationStatus }) {
  const body = segments.filter((s) => s.type !== 'caveat');
  if (body.length === 0) return null;
  const shown = body.map((s) =>
    s.type === 'number' ? (numberText(s) ?? 'View value') : 'text' in s ? s.text : '',
  );
  return (
    <p className="text-body text-text">
      {body.map((segment, index) => {
        const gap =
          index > 0 &&
          (segment.type === 'number' || body[index - 1].type === 'number') &&
          needsGap(shown[index - 1], shown[index])
            ? ' '
            : '';
        if (segment.type === 'number') {
          const number = segment;
          const text = numberText(number);
          return (
            <Fragment key={index}>
              {gap}
              <EvidenceButton
                request={{ evidenceId: number.evidence_id, claim: text ?? 'this value', status }}
                className="font-semibold text-primary tabular-nums underline decoration-dotted underline-offset-2"
              >
                {shown[index]}
              </EvidenceButton>
            </Fragment>
          );
        }
        return (
          <Fragment key={index}>
            {gap}
            {shown[index]}
          </Fragment>
        );
      })}
    </p>
  );
}

/**
 * True when a word and a number would touch ("fell by" + "-6,978.90"): the local model sometimes
 * drops the space at a segment boundary. Punctuation that hugs a number ("(", ".") is left alone.
 */
export function needsGap(left: string, right: string): boolean {
  return /[\p{L}\p{N}%,:;!?]$/u.test(left) && /^[\p{L}\p{N}+\-−₹$€£]/u.test(right);
}

function Caveats({ segments }: { segments: Segment[] }) {
  const caveats = segments.filter((s) => s.type === 'caveat');
  if (caveats.length === 0) return null;
  return (
    <ul aria-label="Limitations" className="text-caption flex flex-col gap-1 text-text-muted">
      {caveats.map((caveat, index) => (
        <li key={index} className="flex gap-1">
          <span aria-hidden="true">ⓘ</span>
          {'text' in caveat ? caveat.text : ''}
        </li>
      ))}
    </ul>
  );
}

/** "How this was produced" (UI-UX §59): what was deterministic and what the AI did. */
function HowProduced({
  view,
  status,
  degraded,
}: {
  view: TurnView;
  status: ValidationStatus | null;
  degraded: boolean;
}) {
  const ok = view.tools.filter((t) => t.status === 'ok');
  if (ok.length === 0) return null;
  const model = ok.some((t) => toolKind(t.name) === 'MODEL_OUTPUT');
  const usage = view.final?.usage;
  return (
    <details className="text-caption text-text-muted">
      <summary className="cursor-pointer">
        How this was produced · Method:{' '}
        {model ? 'Model estimate' : 'Statistical aggregation (deterministic)'} · AI assistance:{' '}
        {degraded ? 'none (verified results only)' : 'wording and interpretation'} · Evidence:{' '}
        {ok.length} {ok.length === 1 ? 'calculation' : 'calculations'}
        {status ? ` · Validation: ${status === 'VALID' ? 'passed' : 'not passed'}` : ''}
      </summary>
      {usage ? (
        <p className="mt-1 pl-4">
          {usage.tool_calls} tool calls · {usage.steps} steps · {usage.retries} re-checks · about{' '}
          {usage.llm_tokens.toLocaleString()} AI tokens (estimated)
        </p>
      ) : null}
    </details>
  );
}

function ProvenanceChips({ view }: { view: TurnView }) {
  const kinds = new Set<ProvenanceKind>();
  for (const tool of view.tools) if (tool.status === 'ok') kinds.add(toolKind(tool.name));
  if (view.segments.some((s) => s.type === 'text')) kinds.add('INTERPRETATION');
  return (
    <>
      {[...kinds].map((kind) => (
        <ProvenanceChip key={kind} kind={kind} />
      ))}
    </>
  );
}

function issueText(view: TurnView): string | undefined {
  const details = view.validation?.issues.map((issue) => issue.detail) ?? [];
  return details.length > 0 ? details.join(' ') : undefined;
}

export interface AnswerViewProps {
  view: TurnView;
  /** Terminal turn state (from `final`, or the stored message). */
  state: TurnState | null;
  streaming?: boolean;
  degraded?: boolean;
  /** Validation status is not stored with a message (backend gap), so history has none. */
  fromHistory?: boolean;
  /** Stored answer written by code from the analysis results (`answer_source: 'template'`). */
  templated?: boolean;
}

export const TEMPLATED_NOTE =
  "Answer written from the analysis results (the assistant's wording did not pass verification).";

/** One assistant answer (UI-UX §25, §28): numbers only after validation, held when unverified. */
export function AnswerView({
  view,
  state,
  streaming = false,
  degraded = false,
  fromHistory = false,
  templated = false,
}: AnswerViewProps) {
  const status = validationStatus(view);
  const fromTemplate = templated || view.final?.answer_source === 'template';
  const withheld =
    status === 'REQUIRES_REVIEW' || status === 'INVALID' || state === 'REQUIRES_REVIEW';
  // Evidence opened from an answer whose status is unknown (history) says "Unverified".
  const evidenceStatus: ValidationStatus = status ?? 'UNCERTAIN';
  const refusalText = view.segments
    .map((s) => ('text' in s ? s.text : ''))
    .join(' ')
    .trim();
  const rootCause = view.tools.find((t) => t.status === 'ok' && t.name.endsWith('explain_change'));

  return (
    <div className="flex flex-col gap-2">
      {!fromHistory ? <StageChips stages={view.stages} streaming={streaming} /> : null}
      <ExecutionSummary tools={view.tools} status={evidenceStatus} />

      {state === 'REFUSED' ? (
        <InsufficientDataState
          reason={refusalText || 'No analysis could be run for this question.'}
          actions={[]}
        />
      ) : null}
      {state === 'REFUSED' ? (
        <p className="text-caption text-text-muted">
          What you can do: rephrase using a measure or column listed on the dataset&apos;s
          Understanding tab, or check which analyses this data supports on its Overview tab.
        </p>
      ) : null}

      {state === 'NEEDS_CLARIFICATION' ? (
        <div className="flex flex-col gap-1 rounded-md border border-info/30 bg-info/5 p-3">
          <p className="text-caption font-medium text-text">The assistant needs one more detail</p>
          <p className="text-body text-text">{refusalText}</p>
          <p className="text-caption text-text-muted">Reply below to continue.</p>
        </div>
      ) : null}

      {state !== 'REFUSED' && state !== 'NEEDS_CLARIFICATION' ? (
        <>
          {(status || view.segments.length > 0) && !streaming ? (
            <div className="flex flex-wrap items-center gap-1">
              {status ? (
                <ValidationBadge
                  status={status}
                  explanation={status === 'UNCERTAIN' ? issueText(view) : undefined}
                />
              ) : null}
              {!withheld ? <ProvenanceChips view={view} /> : null}
              {view.validation && !withheld ? (
                <ConfidenceLabel confidence={view.validation.confidence} />
              ) : null}
            </div>
          ) : null}
          {fromTemplate && !withheld ? (
            <p className="text-caption text-text-muted">{TEMPLATED_NOTE}</p>
          ) : null}
          {status === 'UNCERTAIN' ? (
            <p className="text-caption text-warning">
              Unverified: {issueText(view) ?? 'some statements could not be confirmed.'}
            </p>
          ) : null}
          {withheld ? (
            <div className="flex flex-col gap-1">
              <p className="text-body text-text-muted">
                Held for review: this answer could not be verified, so its numbers are not shown.
              </p>
              {view.validation && view.validation.issues.length > 0 ? (
                <details className="text-caption text-text-muted">
                  <summary className="cursor-pointer">View details</summary>
                  <ul className="mt-1 list-disc pl-5">
                    {view.validation.issues.map((issue, index) => (
                      <li key={index}>{issue.detail}</li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </div>
          ) : (
            <AnswerText segments={view.segments} status={evidenceStatus} />
          )}
          <Caveats segments={withheld ? [] : view.segments} />
          {rootCause?.evidence_id && !withheld ? (
            <RootCausePanel
              evidence={{
                evidenceId: rootCause.evidence_id,
                claim: toolLabel(rootCause.name),
                status: evidenceStatus,
              }}
              findings={(rootCause.findings ?? []).flatMap((f) =>
                f.relation_type === 'causation'
                  ? [] // never shown as a driver; the backend contract refuses causal claims
                  : [
                      {
                        factor: f.factor,
                        relation_type: f.relation_type,
                        magnitude: f.change,
                        unit: f.unit,
                        share_pct: f.share,
                      },
                    ],
              )}
            />
          ) : null}
          {!streaming && !withheld ? (
            <HowProduced view={view} status={status} degraded={degraded} />
          ) : null}
        </>
      ) : null}
    </div>
  );
}
