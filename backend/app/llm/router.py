"""ModelRouter (TASK-LLM-012, design.md section 30).

MVP policy: one `default` profile serves every task (HW-001: one 7B model fits the 6 GB GPU at a
time; BENCH-009). The policy table exists so a `fast`/`strong` profile can be added later by
config without touching callers. The router also enforces context fit:
`est_context_tokens <= max_context - reserve_tokens`, else CONTEXT_TOO_LARGE.
"""

from pydantic import BaseModel, ConfigDict, Field

from app.llm.errors import LLMFailure, LLMFailureReason
from app.llm.ports import LLMCapabilities, LLMTask

DEFAULT_PROFILE = "default"


class ModelProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    provider: str
    model_version: str
    max_context: int = Field(gt=0)
    reserve_tokens: int = Field(default=1024, ge=0)  # room left for the completion
    capabilities: LLMCapabilities


class ModelRouter:
    def __init__(
        self,
        profiles: dict[str, ModelProfile],
        policy: dict[LLMTask, str] | None = None,
    ) -> None:
        self._profiles = profiles
        self._policy = policy or dict.fromkeys(LLMTask, DEFAULT_PROFILE)
        missing = set(self._policy.values()) - set(profiles)
        if missing:
            raise ValueError(f"routing policy references unknown profiles: {sorted(missing)}")

    def select(self, task: LLMTask, est_context_tokens: int) -> ModelProfile:
        profile = self._profiles[self._policy[task]]
        limit = profile.max_context - profile.reserve_tokens
        if est_context_tokens > limit:
            raise LLMFailure(
                LLMFailureReason.CONTEXT_TOO_LARGE,
                f"context of {est_context_tokens} tokens exceeds {limit} for profile "
                f"{profile.name!r}",
                retryable=False,
            )
        return profile
