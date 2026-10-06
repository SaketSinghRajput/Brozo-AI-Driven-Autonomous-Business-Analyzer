import { fetchEventSource } from '@microsoft/fetch-event-source';
import { csrfHeaders, notifyUnauthorized } from '../../api/client';
import { ApiError, isErrorEnvelope } from '../../api/errors';
import type { TurnEvent } from '../../contracts/generated/turn_event';

export type { TurnEvent };

/** The stream ended (or broke) before the turn's `final` event arrived. */
export class StreamInterruptedError extends Error {
  constructor() {
    super('The connection closed before the answer finished.');
    this.name = 'StreamInterruptedError';
  }
}

export interface StreamOptions {
  signal: AbortSignal;
  onEvent: (event: TurnEvent) => void;
}

async function openError(response: Response): Promise<ApiError> {
  if (response.status === 401) notifyUnauthorized(response.url);
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Non-JSON error body (e.g. a proxy page): reported as a status-only ApiError.
  }
  return new ApiError(response.status, isErrorEnvelope(body) ? body : null);
}

/**
 * POST a question and stream the turn (ADR-0020: fetch-event-source, because the request needs
 * a body, the session cookie and the CSRF header, which native EventSource cannot send).
 *
 * - Every `data` payload is checked against shared/schemas/turn_event.schema.json.
 * - Never retried or reopened automatically: a re-POST would ask the question twice. A broken
 *   stream rejects with StreamInterruptedError and the UI offers "Ask again".
 * - Events are de-duplicated by `seq`, so a replayed event is never rendered twice.
 * - Resolves after `final`; resolves silently when `signal` aborts (the backend cancels the
 *   turn on disconnect).
 */
export async function streamTurn(
  conversationId: string,
  content: string,
  { signal, onEvent }: StreamOptions,
): Promise<void> {
  let lastSeq = -1;
  let finished = false;
  // The generated validators are large: loaded on the first question, not with the shell.
  const { validateTurnEvent } = await import('../../contracts/generated/validators');
  const url = `${window.location.origin}/api/v1/chat/conversations/${encodeURIComponent(
    conversationId,
  )}/messages`;
  await fetchEventSource(url, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...csrfHeaders() },
    body: JSON.stringify({ content }),
    signal,
    openWhenHidden: true, // hiding the tab must not abort and re-POST the question
    async onopen(response) {
      const type = response.headers.get('content-type') ?? '';
      if (response.ok && type.startsWith('text/event-stream')) return;
      throw await openError(response);
    },
    onmessage(message) {
      if (!message.data) return; // keep-alive ping
      const data: unknown = JSON.parse(message.data);
      if (!validateTurnEvent(data)) throw new Error('The server sent a malformed turn event.');
      const event = data as TurnEvent;
      if (event.seq <= lastSeq) return;
      lastSeq = event.seq;
      if (event.event.type === 'final') finished = true;
      onEvent(event);
    },
    onerror(error: unknown) {
      throw error; // no automatic retry (see above)
    },
  });
  if (!finished && !signal.aborted) throw new StreamInterruptedError();
}
