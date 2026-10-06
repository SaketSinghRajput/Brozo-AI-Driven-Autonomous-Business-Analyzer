"""EvidenceValidator (TASK-VAL-004; design 40: existence, workspace, version consistency).

Hard issues:
- MISSING_EVIDENCE: a cited id (declared or used by a number segment) is not in the bundle. The
  bundle is loaded through the workspace-scoped repository, so fabricated ids and ids of another
  workspace both end up here.
- FOREIGN_EVIDENCE: the bundle holds a record of another workspace, or a record stored under a
  different id (defence in depth for callers that build bundles themselves).
- SEMANTIC_VERSION_MISMATCH: evidence built on a different semantic model version than the one
  the request pins.
- DATASET_VERSION_MISMATCH: one answer mixes two versions of the same dataset.
Soft issue NO_EVIDENCE: nothing grounds the subject at all.
"""

from app.validator.domain import EvidenceBundle, ValidationIssue, ValidationRequest, cited_ids


def _hard(code: str, target: str, detail: str) -> ValidationIssue:
    return ValidationIssue(code=code, severity="hard", target=target, detail=detail)


class EvidenceValidator:
    name = "evidence"
    version = "1.0.0"

    def __init__(self, workspace_id: str) -> None:
        self._workspace_id = workspace_id

    async def check(self, req: ValidationRequest, ev: EvidenceBundle) -> list[ValidationIssue]:
        ids = cited_ids(req)
        if not ids:
            detail = "the subject cites no evidence"
            return [
                ValidationIssue(
                    code="NO_EVIDENCE", severity="soft", target="subject", detail=detail
                )
            ]
        issues: list[ValidationIssue] = []
        versions: dict[str, str] = {}  # dataset_id -> first dataset_version_id seen
        for evidence_id in ids:
            e = ev.get(evidence_id)
            if e is None:
                issues.append(_hard("MISSING_EVIDENCE", evidence_id, "evidence not found"))
                continue
            if e.workspace_id != self._workspace_id or e.evidence_id != evidence_id:
                detail = "evidence does not belong to this workspace"
                issues.append(_hard("FOREIGN_EVIDENCE", evidence_id, detail))
                continue
            pinned = req.semantic_model_version
            if pinned is not None and e.semantic_model_version not in (None, pinned):
                detail = (
                    f"built on semantic model v{e.semantic_model_version}, answer pins v{pinned}"
                )
                issues.append(_hard("SEMANTIC_VERSION_MISMATCH", evidence_id, detail))
            if e.dataset_id is not None and e.dataset_version_id is not None:
                first = versions.setdefault(e.dataset_id, e.dataset_version_id)
                if first != e.dataset_version_id:
                    detail = f"mixes dataset versions {first} and {e.dataset_version_id}"
                    issues.append(_hard("DATASET_VERSION_MISMATCH", evidence_id, detail))
        return issues
