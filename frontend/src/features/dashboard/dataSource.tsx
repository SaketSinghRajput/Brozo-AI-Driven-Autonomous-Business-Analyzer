import { createContext, useContext, type ReactNode } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { Evidence } from '../../contracts/generated/evidence';
import { EVIDENCE_STALE_TIME, queryKeys } from '../../lib/queryClient';

export type { Evidence };

/** One row of a tool result, keyed by column name (backend EvidenceData.rows). */
export type DataRow = Record<string, string | number | boolean | null>;

/**
 * Where widget data comes from: the evidence record and the stored result rows behind it.
 * Implementations: the backend (features/evidence/apiSource.ts: GET /evidence/{id} and
 * GET /evidence/{id}/data) and fixtures (demo route, tests). Forecast and anomaly overlays are
 * rows of their own evidence too (forecast: period, value, lower, upper; anomalies: flattened
 * `subject.*` plus observed, expected, direction).
 */
export interface DashboardDataSource {
  getEvidence(evidenceId: string): Promise<unknown>;
  getRows(evidenceId: string): Promise<unknown>;
}

const DataSourceContext = createContext<DashboardDataSource | null>(null);

export function DashboardDataProvider({
  source,
  children,
}: {
  source: DashboardDataSource;
  children: ReactNode;
}) {
  return <DataSourceContext.Provider value={source}>{children}</DataSourceContext.Provider>;
}

function useSource(): DashboardDataSource {
  const source = useContext(DataSourceContext);
  if (!source) throw new Error('DashboardDataProvider is missing');
  return source;
}

export function useEvidence(evidenceId: string | null) {
  const source = useSource();
  return useQuery({
    queryKey: queryKeys.evidence(evidenceId ?? ''),
    queryFn: async () => {
      // The generated validators are large; the shell loads them only when evidence is read.
      const [data, { validateEvidence }] = await Promise.all([
        source.getEvidence(evidenceId ?? ''),
        import('../../contracts/generated/validators'),
      ]);
      // Everything crossing the boundary is validated against its contract before use.
      if (!validateEvidence(data)) throw new Error('Evidence does not match its contract');
      return data as Evidence;
    },
    enabled: evidenceId !== null,
    staleTime: EVIDENCE_STALE_TIME,
  });
}

function isRow(value: unknown): value is DataRow {
  return (
    typeof value === 'object' &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every((cell) => cell === null || typeof cell !== 'object')
  );
}

/** Result rows behind an evidence record (immutable like the evidence, so cached forever). */
export function useEvidenceRows(versionId: string, evidenceId: string | undefined) {
  const source = useSource();
  return useQuery({
    queryKey: queryKeys.widgetRows(versionId, evidenceId ?? ''),
    queryFn: async () => {
      const rows = await source.getRows(evidenceId ?? '');
      if (!Array.isArray(rows) || !rows.every(isRow)) {
        throw new Error('Widget rows must be an array of flat objects');
      }
      return rows;
    },
    enabled: evidenceId !== undefined,
    staleTime: EVIDENCE_STALE_TIME,
  });
}
