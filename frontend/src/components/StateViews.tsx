import { useState, type ReactNode } from "react";
import { Button } from "./Button";

export interface EmptyStateProps {
  icon?: ReactNode;
  message: string;
  actionLabel?: string;
  onAction?: () => void;
}

/** No-content state (UI-UX §57): icon, one explanatory sentence, one primary action. */
export function EmptyState({ icon, message, actionLabel, onAction }: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-md border border-dashed border-border p-6 text-center">
      {icon ? (
        <span aria-hidden="true" className="text-2xl text-text-muted">
          {icon}
        </span>
      ) : null}
      <p className="text-body text-text-muted">{message}</p>
      {actionLabel && onAction ? (
        <Button variant="secondary" size="sm" onClick={onAction}>
          {actionLabel}
        </Button>
      ) : null}
    </div>
  );
}

export interface ErrorStateProps {
  /** TRD §35 error envelope. */
  message: string;
  hint?: string;
  retryable?: boolean;
  onRetry?: () => void;
  traceId?: string;
}

/** Failure state rendering the standard error envelope (TRD §35, UI-UX §38). */
export function ErrorState({ message, hint, retryable, onRetry, traceId }: ErrorStateProps) {
  const [copied, setCopied] = useState(false);

  async function copyTraceId() {
    if (!traceId) return;
    try {
      await navigator.clipboard.writeText(traceId);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API unavailable - the trace ID is still visible as text.
    }
  }

  return (
    <div
      role="alert"
      className="flex flex-col gap-2 rounded-md border border-error/40 bg-error/5 p-4"
    >
      <p className="text-body font-medium text-error">{message}</p>
      {hint ? <p className="text-caption text-text-muted">{hint}</p> : null}
      <div className="flex items-center gap-3">
        {retryable && onRetry ? (
          <Button variant="secondary" size="sm" onClick={onRetry}>
            Retry
          </Button>
        ) : null}
        {traceId ? (
          <button
            type="button"
            onClick={() => {
              void copyTraceId();
            }}
            className="text-caption font-mono text-text-muted underline decoration-dotted hover:text-text focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            {copied ? "Copied!" : `Trace ID: ${traceId}`}
          </button>
        ) : null}
      </div>
    </div>
  );
}

export interface InsufficientDataAction {
  label: string;
  actionLabel: string;
  onAction: () => void;
}

export interface InsufficientDataStateProps {
  reason: string;
  actions?: InsufficientDataAction[];
}

/**
 * "Cannot answer" state (UI-UX §30). Styled as a helpful answer, never as an
 * error: neutral surface, info icon.
 */
export function InsufficientDataState({ reason, actions = [] }: InsufficientDataStateProps) {
  return (
    <div className="flex flex-col gap-3 rounded-md border border-info/30 bg-info/5 p-4">
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className="text-info">
          {"ⓘ"}
        </span>
        <div>
          <p className="text-body font-medium text-text">
            I can&apos;t reliably answer this from the available data.
          </p>
          <p className="text-caption mt-1 text-text-muted">{reason}</p>
        </div>
      </div>
      {actions.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {actions.map((action) => (
            <li key={action.label} className="flex items-center justify-between gap-3">
              <span className="text-caption text-text">{action.label}</span>
              <Button variant="ghost" size="sm" onClick={action.onAction}>
                {action.actionLabel}
              </Button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
