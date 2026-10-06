import type { ButtonHTMLAttributes, ReactNode } from 'react';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner and disables the button; label stays visible. */
  loading?: boolean;
  /** Shown as a tooltip when the button is disabled (UI-UX §48: "disabled with reason tooltip"). */
  disabledReason?: string;
  children: ReactNode;
}

const variantClasses: Record<ButtonVariant, string> = {
  primary: 'bg-primary text-surface border-transparent hover:bg-primary/90',
  secondary: 'bg-surface text-text border-border hover:bg-surface-raised',
  ghost: 'bg-transparent text-text border-transparent hover:bg-surface-raised',
  danger: 'bg-error text-surface border-transparent hover:bg-error/90',
};

const sizeClasses: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-body gap-1.5',
  md: 'h-10 px-4 text-body gap-2',
};

/** Base Button (UI-UX §48). States: default, hover, focus-visible, disabled (with reason), loading. */
export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  disabled,
  disabledReason,
  className = '',
  children,
  title,
  ...rest
}: ButtonProps) {
  const isDisabled = Boolean(disabled) || loading;

  return (
    <button
      type="button"
      {...rest}
      disabled={isDisabled}
      title={isDisabled && disabledReason ? disabledReason : title}
      aria-busy={loading || undefined}
      className={[
        'inline-flex items-center justify-center rounded-md border font-medium',
        'transition-colors duration-150 ease-standard',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-2 focus-visible:ring-offset-bg',
        'disabled:cursor-not-allowed disabled:opacity-50',
        variantClasses[variant],
        sizeClasses[size],
        className,
      ].join(' ')}
    >
      {loading ? (
        <span
          aria-hidden="true"
          className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent"
        />
      ) : null}
      <span>{children}</span>
    </button>
  );
}
