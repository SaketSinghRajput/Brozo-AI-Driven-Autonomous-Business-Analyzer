import { QueryClient } from '@tanstack/react-query';
import { ApiError } from '../api/errors';

/**
 * Server-state cache (design.md §56, ADR-0012: TanStack Query 5).
 *
 * Key rules:
 * - Anything derived from a dataset version has the version id in its key, so a new dataset
 *   version (or semantic model version) drops exactly those entries (`invalidateVersion`).
 * - Evidence records are immutable (CLAUDE.md §15) -> cached forever, keyed by evidence id only.
 * - Login / logout / workspace switch -> `queryClient.clear()` (see AuthContext).
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      // One retry for network/5xx only: a 4xx (auth, permission, not found, validation) won't
      // change on retry and must reach the screen immediately.
      queries: {
        retry: (failureCount, error) =>
          failureCount < 1 &&
          !(error instanceof ApiError && error.status >= 400 && error.status < 500),
        refetchOnWindowFocus: false,
      },
    },
  });
}

export const queryClient = createQueryClient();

export const queryKeys = {
  evidence: (evidenceId: string) => ['evidence', evidenceId] as const,
  dashboardSpec: (versionId: string) => ['version', versionId, 'dashboard-spec'] as const,
  widgetRows: (versionId: string, evidenceId: string) =>
    ['version', versionId, 'rows', evidenceId] as const,
  capabilities: (versionId: string, semanticModelVersion: number) =>
    ['version', versionId, 'capabilities', semanticModelVersion] as const,
};

export const EVIDENCE_STALE_TIME = Infinity;

/** Drop every cached entry belonging to a superseded dataset version (evidence is kept). */
export function invalidateVersion(client: QueryClient, oldVersionId: string): void {
  client.removeQueries({ queryKey: ['version', oldVersionId] });
}
