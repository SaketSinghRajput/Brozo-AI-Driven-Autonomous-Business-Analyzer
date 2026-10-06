import type { DashboardDataSource, DataRow, Evidence } from '../dataSource';

/** A complete demo payload: what the backend will serve for one dataset version. */
export interface FixtureBundle {
  label: string;
  spec: unknown;
  capabilities: unknown;
  evidence: Record<string, unknown>;
  rows: Record<string, DataRow[]>;
  /** Pick-list members per filter dimension (GET …/dimensions/{name}/values). */
  dimensionValues?: Record<string, string[]>;
}

const HASH = `sha256:${'0123456789abcdef'.repeat(4)}`;

interface EvidenceInput {
  value?: number;
  unit?: string;
  rows?: number;
  operation: string;
  calculation: string;
  args?: Record<string, unknown>;
  model?: { model_id: string; model_version: string };
  confidence?: Evidence['confidence'];
  sql?: string;
}

/** Builds a contract-valid Evidence record (shared/schemas/evidence.schema.json). */
export function evidence(id: string, datasetVersionId: string, input: EvidenceInput): Evidence {
  return {
    schema_version: '1.0',
    evidence_id: id,
    workspace_id: 'ws_demo',
    kind: 'tool_result',
    dataset_id: datasetVersionId.replace(/^dsv_/, 'ds_'),
    dataset_version_id: datasetVersionId,
    dataset_content_hash: HASH,
    semantic_model_version: 1,
    operation: input.operation,
    tool_version: '1.0.0',
    arguments: input.args ?? {},
    calculation: input.calculation,
    result: {
      result_hash: HASH,
      value: input.value ?? null,
      unit: input.unit ?? null,
      rows: input.rows ?? null,
    },
    model: input.model ?? null,
    confidence: input.confidence ?? {
      level: 'high',
      score: 0.9,
      basis: ['Deterministic calculation'],
    },
    query: input.sql ? { engine: 'duckdb', sql: input.sql, params: {} } : null,
    created_at: '2026-09-28T10:00:00Z',
    software_version: '0.1.0',
  };
}

/** Evidence map keyed by id. */
export function byId(records: Evidence[]): Record<string, Evidence> {
  return Object.fromEntries(records.map((record) => [record.evidence_id, record]));
}

function lookup(map: Record<string, unknown>, id: string, what: string): Promise<unknown> {
  return id in map ? Promise.resolve(map[id]) : Promise.reject(new Error(`No ${what} for ${id}`));
}

/** DashboardDataSource backed by a fixture bundle (demo route + tests). */
export function fixtureSource(bundle: FixtureBundle): DashboardDataSource {
  return {
    getEvidence: (id) => lookup(bundle.evidence, id, 'evidence'),
    getRows: (id) => lookup(bundle.rows, id, 'rows'),
  };
}

export function layoutItem(id: string, x: number, y: number, w: number, h = 1) {
  return { id, x, y, w, h };
}
