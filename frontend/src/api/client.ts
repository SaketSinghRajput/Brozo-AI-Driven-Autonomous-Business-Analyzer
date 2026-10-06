import { client } from './generated/client.gen';

// Same-origin only. `baseUrl` is set explicitly (not left relative) because the underlying
// fetch/Request implementation requires an absolute URL with no ambient base (true in jsdom
// tests, and generally per the Fetch spec) - window.location.origin keeps it same-origin in
// the browser, behind the Vite dev proxy, and behind the nginx proxy in prod (infra/docker/
// frontend.Dockerfile). Session auth is an HttpOnly cookie (ADR-0013) sent via `credentials`;
// JS never reads it. Never a token in localStorage.
client.setConfig({
  baseUrl: window.location.origin,
  credentials: 'same-origin',
});

/** backend/app/security/auth.py CSRF_HEADER. */
export const CSRF_HEADER = 'X-CSRF-Token';
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

// The synchronizer CSRF token comes from the login / me response body (SessionInfo.csrf_token)
// and lives only in memory: a reload re-fetches it from GET /auth/me.
let csrfToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

/** Headers every unsafe request must carry (also used by the XHR upload path). */
export function csrfHeaders(): Record<string, string> {
  return csrfToken ? { [CSRF_HEADER]: csrfToken } : {};
}

/** Called on any 401 except a failed login attempt (the session is gone or expired). */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export function notifyUnauthorized(url: string): void {
  if (!url.endsWith('/api/v1/auth/login')) unauthorizedHandler?.();
}

client.interceptors.request.use((request) => {
  if (!SAFE_METHODS.has(request.method.toUpperCase()) && csrfToken) {
    request.headers.set(CSRF_HEADER, csrfToken);
  }
  return request;
});

client.interceptors.response.use((response, request) => {
  if (response.status === 401) notifyUnauthorized(request.url);
  return response;
});

export { client };
