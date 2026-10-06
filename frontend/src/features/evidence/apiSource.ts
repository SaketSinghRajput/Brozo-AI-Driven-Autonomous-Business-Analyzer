import { unwrap } from '../../api/errors';
import {
  getEvidenceApiV1EvidenceEvidenceIdGet,
  getEvidenceDataApiV1EvidenceEvidenceIdDataGet,
} from '../../api/generated/sdk.gen';
import type { DashboardDataSource } from '../dashboard/dataSource';

/**
 * Backend-backed data source (dashboard tab, chat answers, semantic screens):
 * GET /api/v1/evidence/{id} for the record and GET /api/v1/evidence/{id}/data for the result
 * rows behind it (<= 500; chart, table, forecast and anomaly overlays alike).
 */
export const apiDataSource: DashboardDataSource = {
  getEvidence: (evidenceId) =>
    unwrap(getEvidenceApiV1EvidenceEvidenceIdGet({ path: { evidence_id: evidenceId } })),
  getRows: async (evidenceId) => {
    const data = await unwrap(
      getEvidenceDataApiV1EvidenceEvidenceIdDataGet({ path: { evidence_id: evidenceId } }),
    );
    return data.rows;
  },
};
