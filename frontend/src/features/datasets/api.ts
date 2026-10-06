import { useQuery } from '@tanstack/react-query';
import { csrfHeaders, notifyUnauthorized } from '../../api/client';
import { ApiError, isErrorEnvelope, unwrap } from '../../api/errors';
import {
  cancelJobApiV1JobsJobIdCancelPost,
  getDatasetApiV1DatasetsDatasetIdGet,
  getJobApiV1JobsJobIdGet,
  getProfileApiV1DatasetsDatasetIdVersionsVersionIdProfileGet,
  getQuarantineApiV1DatasetsDatasetIdVersionsVersionIdQuarantineGet,
  getVersionApiV1DatasetsDatasetIdVersionsVersionIdGet,
  listDatasetsApiV1DatasetsGet,
  listVersionsApiV1DatasetsDatasetIdVersionsGet,
} from '../../api/generated/sdk.gen';
import type {
  DatasetState,
  DatasetVersion,
  Job,
  UploadResponse,
} from '../../api/generated/types.gen';

/** Keys: anything version-derived starts with ["version", id] (lib/queryClient.ts rule). */
export const datasetKeys = {
  list: ['datasets'] as const,
  dataset: (datasetId: string) => ['datasets', datasetId] as const,
  versions: (datasetId: string) => ['datasets', datasetId, 'versions'] as const,
  version: (versionId: string) => ['version', versionId, 'meta'] as const,
  profile: (versionId: string) => ['version', versionId, 'profile'] as const,
  quarantine: (versionId: string) => ['version', versionId, 'quarantine'] as const,
  job: (jobId: string) => ['job', jobId] as const,
};

export const FAILED_STATES: ReadonlySet<DatasetState> = new Set([
  'VALIDATION_FAILED',
  'INGESTION_FAILED',
  'ANALYSIS_FAILED',
]);
// backend/app/data/jobs.py: ingest -> process_dataset (PROFILED) -> process_dataset
// (understanding, -> SEMANTICALLY_MAPPED) -> run_plan (plan, execute, dashboard -> READY).
const SETTLED_STATES: ReadonlySet<DatasetState> = new Set(['READY']);

// A profile exists from PROFILED on, including when a later (analysis) stage failed.
const PROFILED_STATES: ReadonlySet<DatasetState> = new Set([
  'PROFILED',
  'SCHEMA_ANALYZED',
  'DOMAIN_IDENTIFIED',
  'SEMANTICALLY_MAPPED',
  'ANALYZING',
  'READY',
  'ANALYSIS_FAILED',
]);

export function isProcessing(state: DatasetState): boolean {
  return !FAILED_STATES.has(state) && !SETTLED_STATES.has(state);
}

export function isProfiled(state: DatasetState): boolean {
  return PROFILED_STATES.has(state);
}

const TERMINAL_JOB: ReadonlySet<Job['status']> = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);
export const POLL_MS = 1500;

export function useDatasets() {
  return useQuery({
    queryKey: datasetKeys.list,
    queryFn: () => unwrap(listDatasetsApiV1DatasetsGet({ query: { limit: 100 } })),
  });
}

export function useDataset(datasetId: string) {
  return useQuery({
    queryKey: datasetKeys.dataset(datasetId),
    queryFn: () => unwrap(getDatasetApiV1DatasetsDatasetIdGet({ path: { dataset_id: datasetId } })),
  });
}

export function useVersions(datasetId: string) {
  return useQuery({
    queryKey: datasetKeys.versions(datasetId),
    queryFn: () =>
      unwrap(listVersionsApiV1DatasetsDatasetIdVersionsGet({ path: { dataset_id: datasetId } })),
    enabled: datasetId !== '',
    refetchInterval: (query) =>
      query.state.data?.some((v) => isProcessing(v.state)) ? POLL_MS : false,
  });
}

/** One version's lifecycle state; polls while the backend is still processing it. */
export function useVersion(datasetId: string, versionId: string) {
  return useQuery({
    queryKey: datasetKeys.version(versionId),
    queryFn: () =>
      unwrap(
        getVersionApiV1DatasetsDatasetIdVersionsVersionIdGet({
          path: { dataset_id: datasetId, version_id: versionId },
        }),
      ),
    refetchInterval: (query) =>
      query.state.data && !isProcessing(query.state.data.state) ? false : POLL_MS,
  });
}

export function useJob(jobId: string) {
  return useQuery({
    queryKey: datasetKeys.job(jobId),
    queryFn: () => unwrap(getJobApiV1JobsJobIdGet({ path: { job_id: jobId } })),
    refetchInterval: (query) =>
      query.state.data && TERMINAL_JOB.has(query.state.data.status) ? false : POLL_MS,
  });
}

export function cancelJob(jobId: string): Promise<Job> {
  return unwrap(cancelJobApiV1JobsJobIdCancelPost({ path: { job_id: jobId } }));
}

export function useProfile(version: DatasetVersion) {
  return useQuery({
    queryKey: datasetKeys.profile(version.id),
    queryFn: () =>
      unwrap(
        getProfileApiV1DatasetsDatasetIdVersionsVersionIdProfileGet({
          path: { dataset_id: version.dataset_id, version_id: version.id },
        }),
      ),
    enabled: isProfiled(version.state),
    staleTime: Infinity, // a version's profile never changes (keyed by version id)
  });
}

export function useQuarantine(version: DatasetVersion) {
  return useQuery({
    queryKey: datasetKeys.quarantine(version.id),
    queryFn: () =>
      unwrap(
        getQuarantineApiV1DatasetsDatasetIdVersionsVersionIdQuarantineGet({
          path: { dataset_id: version.dataset_id, version_id: version.id },
        }),
      ),
    // The ingest report exists once rows were stored (row_count set).
    enabled: version.row_count !== null && version.row_count !== undefined,
    retry: false,
  });
}

export interface UploadResult {
  /** false = the backend matched an existing version (Idempotency-Key replay or same content). */
  created: boolean;
  response: UploadResponse;
}

export interface UploadOptions {
  name?: string;
  idempotencyKey: string;
  onProgress?: (loaded: number, total: number) => void;
  signal?: AbortSignal;
}

/**
 * POST /api/v1/datasets (multipart). XMLHttpRequest instead of the generated fetch client only
 * because fetch cannot report upload progress (UI-UX §13: determinate bytes). Same contract:
 * same-origin cookie, CSRF header, Idempotency-Key, error envelope -> ApiError.
 */
export function uploadDataset(file: Blob, options: UploadOptions): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const url = '/api/v1/datasets';
    const xhr = new XMLHttpRequest();
    xhr.open('POST', url);
    for (const [header, value] of Object.entries(csrfHeaders())) {
      xhr.setRequestHeader(header, value);
    }
    xhr.setRequestHeader('Idempotency-Key', options.idempotencyKey);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) options.onProgress?.(event.loaded, event.total);
    };
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText) as unknown;
      } catch {
        // Non-JSON body (e.g. a proxy error page): reported as a status-only ApiError below.
      }
      if (xhr.status === 200 || xhr.status === 201) {
        resolve({ created: xhr.status === 201, response: body as UploadResponse });
        return;
      }
      if (xhr.status === 401) notifyUnauthorized(url);
      reject(new ApiError(xhr.status, isErrorEnvelope(body) ? body : null));
    };
    xhr.onerror = () => reject(new ApiError(0, null));
    xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'));
    options.signal?.addEventListener('abort', () => xhr.abort());

    const form = new FormData();
    form.append('file', file, file instanceof File ? file.name : 'upload.csv');
    if (options.name) form.append('name', options.name);
    xhr.send(form);
  });
}
