import { afterEach, describe, expect, it, vi } from 'vitest';
import { setCsrfToken } from '../../api/client';
import { ApiError } from '../../api/errors';
import { StreamInterruptedError, streamTurn, type TurnEvent } from './stream';

/**
 * Real fetch-event-source parsing over a stubbed fetch: the SSE wire format as sse-starlette
 * writes it (event / id / data lines, ": ping" comments between events).
 */
function frame(seq: number, event: object): string {
  const data = JSON.stringify({ schema_version: '1.0', seq, event });
  return `event: ${(event as { type: string }).type}\nid: ${seq}\ndata: ${data}\n\n`;
}

const STATUS = { type: 'status', stage: 'CLASSIFYING' };
const FINAL = {
  type: 'final',
  message_id: 'msg_2',
  state: 'COMPLETED',
  mode: 'normal',
  validation_status: 'VALID',
  usage: { steps: 5, tool_calls: 1, llm_tokens: 900, retries: 0 },
};

function stubFetch(response: () => Response) {
  const calls: Request[] = [];
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(new Request(input, init));
      return Promise.resolve(response());
    }),
  );
  return calls;
}

function sse(body: string): Response {
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

async function collect(conversation = 'cv_1') {
  const events: TurnEvent[] = [];
  await streamTurn(conversation, 'How did it change?', {
    signal: new AbortController().signal,
    onEvent: (event) => events.push(event),
  });
  return events;
}

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrfToken(null);
});

describe('streamTurn', () => {
  it('POSTs with the CSRF header and yields events in order, skipping pings and replays', async () => {
    setCsrfToken('csrf-token-value');
    const calls = stubFetch(() =>
      sse(
        frame(0, STATUS) +
          ': ping\n\n' +
          frame(1, { type: 'status', stage: 'PLANNING' }) +
          frame(1, { type: 'status', stage: 'PLANNING' }) +
          frame(2, FINAL),
      ),
    );
    const events = await collect();

    expect(events.map((e) => e.seq)).toEqual([0, 1, 2]);
    expect(events.map((e) => e.event.type)).toEqual(['status', 'status', 'final']);
    const request = calls[0];
    expect(request.method).toBe('POST');
    expect(new URL(request.url).pathname).toBe('/api/v1/chat/conversations/cv_1/messages');
    expect(request.headers.get('X-CSRF-Token')).toBe('csrf-token-value');
    expect(await request.json()).toEqual({ content: 'How did it change?' });
  });

  it('rejects with StreamInterruptedError when the stream ends before `final`', async () => {
    stubFetch(() => sse(frame(0, STATUS)));
    await expect(collect()).rejects.toBeInstanceOf(StreamInterruptedError);
  });

  it('turns an error envelope into an ApiError and never retries', async () => {
    const calls = stubFetch(
      () =>
        new Response(
          JSON.stringify({
            error_code: 'FORBIDDEN',
            message: 'Not allowed',
            details: null,
            retryable: false,
            trace_id: 'tr_1',
            timestamp: '2026-09-28T00:00:00Z',
          }),
          { status: 403, headers: { 'Content-Type': 'application/json' } },
        ),
    );
    const failure = await collect().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ApiError);
    expect((failure as ApiError).status).toBe(403);
    expect(calls).toHaveLength(1);
  });

  it('rejects a payload that does not match the TurnEvent contract', async () => {
    stubFetch(() => sse(frame(0, { type: 'status', stage: 'DANCING' })));
    await expect(collect()).rejects.toThrow('malformed turn event');
  });

  it('resolves quietly when the caller aborts', async () => {
    stubFetch(() => sse(frame(0, STATUS)));
    const controller = new AbortController();
    const run = streamTurn('cv_1', 'q', {
      signal: controller.signal,
      onEvent: () => controller.abort(),
    });
    await expect(run).resolves.toBeUndefined();
  });
});
