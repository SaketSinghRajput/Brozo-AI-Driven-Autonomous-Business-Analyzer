import { vi } from 'vitest';
import type { SessionInfo } from '../api/generated/types.gen';

/**
 * Test-only fetch stub for the generated client: routes "METHOD /path" to a canned response.
 * Unmatched requests get a 404 envelope so a missing route fails loudly in assertions.
 */
export type Route = { status?: number; body?: unknown } | ((request: Request) => Response);

export function json(body: unknown, status = 200): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

export function envelope(status: number, errorCode: string, message: string, details?: object) {
  return {
    status,
    body: {
      error_code: errorCode,
      message,
      details: details ?? null,
      retryable: false,
      trace_id: 'trace-test',
      timestamp: '2026-09-28T00:00:00Z',
    },
  };
}

export function session(role: SessionInfo['role'] = 'analyst'): SessionInfo {
  return {
    user_id: 'usr_1',
    email: 'person@example.test',
    workspace_id: 'ws_abc123',
    role,
    csrf_token: 'csrf-token-value',
  };
}

export function mockApi(routes: Record<string, Route>) {
  const calls: Request[] = [];
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    // Relative URLs (plain fetch of /samples/...) resolve against the page, as in a browser.
    const request =
      input instanceof Request
        ? input
        : new Request(new URL(String(input), window.location.origin), init);
    calls.push(request);
    const url = new URL(request.url);
    const route = routes[`${request.method} ${url.pathname}`];
    if (route === undefined) {
      const miss = envelope(404, 'NOT_FOUND', `no mock for ${request.method} ${url.pathname}`);
      return Promise.resolve(json(miss.body, miss.status));
    }
    if (typeof route === 'function') return Promise.resolve(route(request));
    return Promise.resolve(
      route.status === 204 ? new Response(null, { status: 204 }) : json(route.body, route.status),
    );
  });
  vi.stubGlobal('fetch', fetchMock);
  return { calls, fetchMock };
}

export const HEALTH_OK: Route = { body: { status: 'ok', version: '0.1.0' } };

/** Synthetic DatasetVersion (ids ds_1 / dsv_1) in the given lifecycle state. */
export function versionBody(state: string, extra: object = {}) {
  return {
    id: 'dsv_1',
    dataset_id: 'ds_1',
    workspace_id: 'ws_abc123',
    version_no: 1,
    state,
    state_changed_at: '2026-09-28T10:00:00Z',
    created_at: '2026-09-28T10:00:00Z',
    source_sha256: `sha256:${'0'.repeat(64)}`,
    review_flags: [],
    ...extra,
  };
}
