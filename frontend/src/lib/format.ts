/**
 * Number formatting from the fixed FormatSpec vocabulary (unit, precision, locale, currency).
 * Never evaluates a format string from the backend (design.md §57).
 *
 * Unit vocabulary (backend app/dashboard/domain.py, TRD 25.1), shared by evidence, KPI cards,
 * charts, tables and chat number segments:
 * - `currency:<ISO>`: money; the ISO code comes from `currency` when given, else the unit.
 * - `pct`, `pp`: FRACTIONS, displayed x100 (0.142 -> "14.2%", 0.023 -> "2.3 pp").
 * - `count`: a plain number.
 * - anything else: a plain unit label appended to the number ("27.1 t/ha").
 *
 * One number locale for the whole app (U-1): `en-US` grouping and decimals (1,318,451.67), the
 * same as the backend's text (Python `{:,}` in summaries, insights and answers) and the
 * validator's number parser. A spec's `locale` is not used for grouping, so a KPI card never
 * shows 13,18,451.67 next to a sentence saying 1,318,451.67. The currency symbol still follows
 * the ISO code (₹, $, €).
 */
export interface NumberFormat {
  unit?: string | null;
  precision?: number;
  /** Ignored for grouping (see APP_LOCALE); kept because the FormatSpec contract carries it. */
  locale?: string;
  currency?: string | null;
}

export const APP_LOCALE = 'en-US';
const CURRENCY_PREFIX = 'currency:';

interface Display {
  value: number;
  currency: string | null;
  suffix: string;
}

function display(value: number, { unit, currency }: NumberFormat): Display {
  if (unit === 'pct') return { value: value * 100, currency: null, suffix: '%' };
  if (unit === 'pp') return { value: value * 100, currency: null, suffix: ' pp' };
  if (unit?.startsWith(CURRENCY_PREFIX)) {
    return { value, currency: currency ?? unit.slice(CURRENCY_PREFIX.length), suffix: '' };
  }
  // Bare "currency" (the backend emits it when the dataset names no ISO code): money of an
  // unknown currency, so a plain number unless `currency` supplies the code. Never guess one.
  if (unit === 'currency') return { value, currency: currency ?? null, suffix: '' };
  if (!unit || unit === 'count') return { value, currency: null, suffix: '' };
  return { value, currency: null, suffix: ` ${unit}` };
}

function build(value: number, format: NumberFormat, compact: boolean): string {
  const { precision = 0 } = format;
  const shown = display(value, format);
  const digits = compact
    ? { maximumFractionDigits: 1 }
    : { maximumFractionDigits: precision, minimumFractionDigits: precision };
  const notation = compact ? ('compact' as const) : ('standard' as const);
  try {
    const style = shown.currency ? { style: 'currency' as const, currency: shown.currency } : {};
    return (
      new Intl.NumberFormat(APP_LOCALE, { notation, ...style, ...digits }).format(shown.value) +
      shown.suffix
    );
  } catch {
    // Unknown locale/currency from the backend: fall back rather than crash the widget.
    return `${shown.currency ? `${shown.currency} ` : ''}${shown.value}${shown.suffix}`;
  }
}

/** Full-precision text (tables, tooltips, evidence). */
export function formatNumber(value: number, format: NumberFormat = {}): string {
  return build(value, format, false);
}

/** Abbreviated text for large headline numbers; pair with `formatNumber` in a tooltip. */
export function formatCompact(value: number, format: NumberFormat = {}): string {
  return build(value, format, Math.abs(display(value, format).value) >= 10_000);
}

/** Short label of a unit for captions and footers ("INR", "%", "pp", "t/ha"); null for none. */
export function unitLabel(format: NumberFormat): string | null {
  const { unit, currency } = format;
  if (!unit || unit === 'count') return null;
  if (unit === 'pct') return '%';
  if (unit.startsWith(CURRENCY_PREFIX)) return currency ?? unit.slice(CURRENCY_PREFIX.length);
  return unit;
}

export function formatCell(
  value: string | number | null | undefined,
  format: NumberFormat = {},
): string {
  if (value === null || value === undefined) return '—';
  return typeof value === 'number' ? formatNumber(value, format) : value;
}
