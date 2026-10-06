import type { ReactNode } from "react";

export type BadgeVariant = "neutral" | "success" | "warning" | "error" | "info";

const badgeVariantClasses: Record<BadgeVariant, string> = {
  neutral: "bg-border/20 text-text",
  success: "bg-success/10 text-success",
  warning: "bg-warning/10 text-warning",
  error: "bg-error/10 text-error",
  info: "bg-info/10 text-info",
};

export interface BadgeProps {
  variant?: BadgeVariant;
  icon?: ReactNode;
  children: ReactNode;
}

/** Generic status/label badge (UI-UX §48). Icon + text; colour is never the only signal. */
export function Badge({ variant = "neutral", icon, children }: BadgeProps) {
  return (
    <span
      className={`text-label inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-medium ${badgeVariantClasses[variant]}`}
    >
      {icon ? (
        <span aria-hidden="true" className="leading-none">
          {icon}
        </span>
      ) : null}
      {children}
    </span>
  );
}

export type ValidationStatus = "VALID" | "UNCERTAIN" | "REQUIRES_REVIEW" | "INVALID";

const validationConfig: Record<
  ValidationStatus,
  { icon: string; label: string; variant: BadgeVariant; defaultExplanation: string }
> = {
  VALID: {
    icon: "✓",
    label: "Validated",
    variant: "success",
    defaultExplanation: "Numbers independently checked against the calculations.",
  },
  UNCERTAIN: {
    icon: "◐",
    label: "Unverified",
    variant: "warning",
    defaultExplanation: "Could not confirm the document supports this statement.",
  },
  REQUIRES_REVIEW: {
    icon: "⚑",
    label: "Needs review",
    variant: "warning",
    defaultExplanation: "The available data does not fully support this conclusion.",
  },
  INVALID: {
    icon: "✕",
    label: "Not shown",
    variant: "error",
    defaultExplanation: "Rejected answer - only visible in history/technical views.",
  },
};

export interface ValidationBadgeProps {
  status: ValidationStatus;
  /** Overrides the default explanation text (UI-UX §28). */
  explanation?: string;
}

/** VALID / UNCERTAIN / REQUIRES_REVIEW / INVALID badge (UI-UX §28). Icon + text, never colour alone. */
export function ValidationBadge({ status, explanation }: ValidationBadgeProps) {
  const config = validationConfig[status];
  return (
    <span
      className="text-label inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-medium"
      title={explanation ?? config.defaultExplanation}
    >
      <Badge variant={config.variant} icon={config.icon}>
        {config.label}
      </Badge>
    </span>
  );
}

export interface Confidence {
  level: "low" | "medium" | "high";
  score: number;
  basis: string[];
}

const confidenceLabels: Record<Confidence["level"], string> = {
  high: "High confidence",
  medium: "Moderate confidence",
  low: "Low confidence",
};

/**
 * Confidence display (UI-UX §29). Level text only - the numeric score is ordinal and
 * uncalibrated (TRD §25.1), so it is never shown as a percentage. Low confidence always
 * exposes its "Why?" basis.
 */
export function ConfidenceLabel({ confidence }: { confidence: Confidence }) {
  const { level, basis } = confidence;
  const style =
    level === "high" ? "bg-secondary/15 text-text" : "border border-border text-text-muted";
  const label = (
    <span className={`text-label inline-flex rounded-full px-2 py-0.5 font-medium ${style}`}>
      {confidenceLabels[level]}
    </span>
  );
  if (level !== "low" || basis.length === 0) return label;
  return (
    <span className="inline-flex items-center gap-1">
      {label}
      <details className="text-caption relative">
        <summary className="cursor-pointer text-primary underline">Why?</summary>
        <ul className="absolute z-10 mt-1 w-56 list-disc rounded-md border border-border bg-surface-raised p-2 pl-5 shadow-md">
          {basis.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      </details>
    </span>
  );
}

export type ProvenanceKind =
  | "FACT"
  | "CALCULATION"
  | "MODEL_OUTPUT"
  | "INTERPRETATION"
  | "SIMULATION"
  | "USER_DECISION";

const provenanceConfig: Record<ProvenanceKind, { icon: string; label: string }> = {
  FACT: { icon: "▦", label: "Source data" },
  CALCULATION: { icon: "Σ", label: "Calculated" },
  MODEL_OUTPUT: { icon: "⌇", label: "Model estimate" },
  INTERPRETATION: { icon: "❝", label: "AI interpretation" },
  SIMULATION: { icon: "⑂", label: "Simulation" },
  USER_DECISION: { icon: "◉", label: "Your decision" },
};

export interface ProvenanceChipProps {
  kind: ProvenanceKind;
}

/**
 * Trust-hierarchy provenance chip (UI-UX §7.2). Neutral background - colour is
 * not used to distinguish provenance kinds, only the icon + text pairing.
 */
export function ProvenanceChip({ kind }: ProvenanceChipProps) {
  const config = provenanceConfig[kind];
  return (
    <span className="text-label inline-flex items-center gap-1 rounded-full border border-border bg-surface-raised px-2 py-0.5 font-medium text-text-muted">
      <span aria-hidden="true" className="leading-none">
        {config.icon}
      </span>
      {config.label}
    </span>
  );
}
