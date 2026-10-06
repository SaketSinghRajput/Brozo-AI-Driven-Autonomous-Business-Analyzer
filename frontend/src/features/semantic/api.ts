import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { unwrap } from '../../api/errors';
import {
  getCapabilitiesApiV1DatasetsDatasetIdVersionsVersionIdCapabilitiesGet,
  getSemanticModelApiV1DatasetsDatasetIdVersionsVersionIdSemanticModelGet,
  getUnderstandingApiV1DatasetsDatasetIdVersionsVersionIdUnderstandingGet,
  overrideDomainApiV1DatasetsDatasetIdDomainPut,
  patchSemanticModelApiV1DatasetsDatasetIdVersionsVersionIdSemanticModelPatch,
} from '../../api/generated/sdk.gen';
import type { DatasetState, OverrideInput } from '../../api/generated/types.gen';
import { queryKeys } from '../../lib/queryClient';

/** States in which a semantic model exists (backend override_service `_MAPPED`, plus later). */
export const MAPPED: ReadonlySet<DatasetState> = new Set([
  'SEMANTICALLY_MAPPED',
  'ANALYZING',
  'READY',
  'ANALYSIS_FAILED',
]);

export interface VersionRef {
  datasetId: string;
  versionId: string;
}

/** Keys: version-derived, so a superseded version drops them (lib/queryClient.ts rule). */
export const semanticKeys = {
  understanding: (versionId: string) => ['version', versionId, 'understanding'] as const,
  model: (versionId: string) => ['version', versionId, 'semantic-model'] as const,
};

function path({ datasetId, versionId }: VersionRef) {
  return { dataset_id: datasetId, version_id: versionId };
}

export function useUnderstanding(ref: VersionRef, enabled = true) {
  return useQuery({
    queryKey: semanticKeys.understanding(ref.versionId),
    queryFn: () =>
      unwrap(
        getUnderstandingApiV1DatasetsDatasetIdVersionsVersionIdUnderstandingGet({
          path: path(ref),
        }),
      ),
    enabled,
  });
}

/** The current semantic model of a version (its `version` is the PATCH base_version). */
export function useSemanticModel(ref: VersionRef, enabled = true) {
  return useQuery({
    queryKey: semanticKeys.model(ref.versionId),
    queryFn: () =>
      unwrap(
        getSemanticModelApiV1DatasetsDatasetIdVersionsVersionIdSemanticModelGet({
          path: path(ref),
        }),
      ),
    enabled,
  });
}

export function useCapabilities(ref: VersionRef, semanticModelVersion: number | undefined) {
  return useQuery({
    queryKey: queryKeys.capabilities(ref.versionId, semanticModelVersion ?? 0),
    queryFn: () =>
      unwrap(
        getCapabilitiesApiV1DatasetsDatasetIdVersionsVersionIdCapabilitiesGet({
          path: path(ref),
          query: { semantic_model_version: semanticModelVersion },
        }),
      ),
    enabled: semanticModelVersion !== undefined,
    staleTime: Infinity, // keyed by the semantic model version, which never changes in place
  });
}

/**
 * PATCH the semantic model with optimistic concurrency: `baseVersion` must be the current model
 * version, else the backend answers 409 and the caller asks the user to reload.
 */
export function usePatchSemanticModel(ref: VersionRef) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ baseVersion, overrides }: { baseVersion: number; overrides: OverrideInput[] }) =>
      unwrap(
        patchSemanticModelApiV1DatasetsDatasetIdVersionsVersionIdSemanticModelPatch({
          path: path(ref),
          body: { base_version: baseVersion, overrides },
        }),
      ),
    onSuccess: (model) => {
      client.setQueryData(semanticKeys.model(ref.versionId), model);
      void client.invalidateQueries({ queryKey: semanticKeys.understanding(ref.versionId) });
    },
  });
}

/** PUT the dataset's domain (applies to its latest version; re-maps and re-plans). */
export function useOverrideDomain(ref: VersionRef) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (domain: string) =>
      unwrap(
        overrideDomainApiV1DatasetsDatasetIdDomainPut({
          path: { dataset_id: ref.datasetId },
          body: { domain },
        }),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ['version', ref.versionId] }),
  });
}
