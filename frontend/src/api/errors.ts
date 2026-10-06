/**
 * Mirrors backend/app/core/errors.py `ErrorEnvelope` (TRD section 35). Not part of the
 * generated OpenAPI client: FastAPI's schema only documents 2xx bodies per endpoint, not the
 * shared error shape returned by the app-wide exception handlers.
 */
export interface ErrorEnvelope {
  error_code: string;
  message: string;
  details: Record<string, unknown> | null;
  retryable: boolean;
  trace_id: string | null;
  timestamp: string;
}

/** Narrows an unknown fetch-client error into an `ErrorEnvelope` when the backend sent one. */
export function isErrorEnvelope(value: unknown): value is ErrorEnvelope {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as Record<string, unknown>).error_code === 'string' &&
    typeof (value as Record<string, unknown>).message === 'string' &&
    typeof (value as Record<string, unknown>).retryable === 'boolean'
  );
}

/** A failed API call: HTTP status (0 = network failure) plus the envelope when one was sent. */
export class ApiError extends Error {
  readonly status: number;
  readonly envelope: ErrorEnvelope | null;
  /** Seconds from the `Retry-After` header (429 login throttling), when sent. */
  readonly retryAfterSeconds: number | null;

  constructor(
    status: number,
    envelope: ErrorEnvelope | null,
    retryAfterSeconds: number | null = null,
  ) {
    super(envelope?.message ?? (status === 0 ? 'Network error' : `HTTP ${status}`));
    this.name = 'ApiError';
    this.status = status;
    this.envelope = envelope;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  /** Specific cause: `details.reason` when present (ingestion errors), else `error_code`. */
  get reason(): string | null {
    const reason = this.envelope?.details?.reason;
    return typeof reason === 'string' ? reason : (this.envelope?.error_code ?? null);
  }
}

interface FieldsResult<T> {
  data?: T;
  error?: unknown;
  response?: Response;
}

/** Turns a generated-client `{data, error, response}` result into data or a thrown ApiError. */
export async function unwrap<T>(call: Promise<FieldsResult<T>>): Promise<T> {
  const { data, error, response } = await call;
  if (response?.ok && data !== undefined) return data;
  const retryAfter = Number.parseInt(response?.headers.get('Retry-After') ?? '', 10);
  throw new ApiError(
    response?.status ?? 0,
    isErrorEnvelope(error) ? error : null,
    Number.isFinite(retryAfter) && retryAfter >= 0 ? retryAfter : null,
  );
}

export interface ErrorDisplay {
  message: string;
  hint?: string;
  retryable: boolean;
  traceId?: string;
}

/** Human text for any error (UI-UX §38): never a stack trace or a bare status code. */
export function describeError(error: unknown): ErrorDisplay {
  if (!(error instanceof ApiError)) {
    return { message: 'Something went wrong. Please try again.', retryable: true };
  }
  if (error.status === 0) {
    return {
      message: "The server can't be reached. Check your connection and try again.",
      retryable: true,
    };
  }
  const hint = error.envelope?.details?.hint;
  const base = {
    traceId: error.envelope?.trace_id ?? undefined,
    hint: typeof hint === 'string' ? hint : undefined,
    retryable: error.envelope?.retryable ?? false,
  };
  if (error.status === 401) {
    return { ...base, message: 'Your session has expired. Sign in again to continue.' };
  }
  if (error.status === 403) {
    return {
      ...base,
      message: "You don't have permission for this action. Ask a workspace administrator.",
    };
  }
  if (error.status === 404) {
    return { ...base, message: "This item doesn't exist or isn't available in your workspace." };
  }
  return { ...base, message: error.envelope?.message ?? 'The request failed. Please try again.' };
}
