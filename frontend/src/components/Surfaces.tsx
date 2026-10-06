import type { HTMLAttributes, ReactNode } from "react";

export interface CardProps extends HTMLAttributes<HTMLDivElement> {
  /** Whole-card is a single click target (UI-UX §48: "only if a single action"). */
  interactive?: boolean;
  children: ReactNode;
}

/** Container primitive (UI-UX §48). Components consume tokens only. */
export function Card({ interactive = false, className = "", children, ...rest }: CardProps) {
  if (interactive) {
    return (
      <div
        {...rest}
        role="button"
        tabIndex={0}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            event.currentTarget.click();
          }
          rest.onKeyDown?.(event);
        }}
        className={`rounded-md border border-border bg-surface p-4 transition-shadow duration-(--motion-duration-fast) hover:shadow-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus ${className}`}
      >
        {children}
      </div>
    );
  }

  return (
    <div
      {...rest}
      className={`rounded-md border border-border bg-surface p-4 ${className}`}
    >
      {children}
    </div>
  );
}

export type SkeletonVariant = "text" | "card" | "chart" | "table";

export interface SkeletonProps {
  variant?: SkeletonVariant;
  /** Number of rows for the "table" variant. */
  rows?: number;
  className?: string;
}

const variantClasses: Record<SkeletonVariant, string> = {
  text: "h-4 w-full rounded-sm",
  card: "h-32 w-full rounded-md",
  chart: "h-64 w-full rounded-md",
  table: "h-4 w-full rounded-sm",
};

/** Loading placeholder matching the final layout (UI-UX §48, §56). Decorative only. */
export function Skeleton({ variant = "text", rows = 3, className = "" }: SkeletonProps) {
  if (variant === "table") {
    return (
      <div className={`flex flex-col gap-2 ${className}`} aria-hidden="true">
        {Array.from({ length: rows }, (_, index) => (
          <div key={index} className="h-4 w-full animate-pulse rounded-sm bg-border/40" />
        ))}
      </div>
    );
  }

  return (
    <div
      aria-hidden="true"
      className={`animate-pulse bg-border/40 ${variantClasses[variant]} ${className}`}
    />
  );
}
