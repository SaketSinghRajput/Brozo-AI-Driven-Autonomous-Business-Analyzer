"""Versioned capability rules + analysis thresholds (TASK-ANALYSIS-002; design 17, 66; CLAUDE.md
section 12.1: thresholds live in config, are documented in docs/ML.md and are tested).

``capability_rules.v1.json`` is a ``CapabilityRuleSet`` (format spec: app.planner.domain).
``analysis_config.v1.json`` holds the numbers the rules reference as ``config.<group>.<key>``;
a period threshold may be a map keyed by planning grain (month / quarter / year). Loading fails
when a rule references a config key that does not exist, so a typo cannot silently disable a rule.
JSON, not YAML: the stdlib parses it (same choice as the understanding config).
"""

from functools import cache
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from app.planner.domain import CapabilityRuleSet, ParamValue, Requirement

CONFIG_DIR = Path(__file__).parent / "config"
RULES_PATH = CONFIG_DIR / "capability_rules.v1.json"
CONFIG_PATH = CONFIG_DIR / "analysis_config.v1.json"
CONFIG_PREFIX = "config."

Threshold = float | dict[str, float]


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannerLimits(_Cfg):
    max_steps: Annotated[int, Field(ge=1)]
    max_kpis: Annotated[int, Field(ge=1)]
    max_dimensions: Annotated[int, Field(ge=1)]
    seed: int


class AnalysisConfig(_Cfg):
    version: Annotated[str, Field(pattern=r"^analysis-config@\d+(\.\d+)*$")]
    thresholds: dict[str, dict[str, Threshold]]
    planner: PlannerLimits

    def resolve(self, value: ParamValue) -> ParamValue | dict[str, float]:
        """A ``config.<group>.<key>`` reference -> its value; anything else unchanged."""
        if not (isinstance(value, str) and value.startswith(CONFIG_PREFIX)):
            return value
        group, _, key = value.removeprefix(CONFIG_PREFIX).partition(".")
        try:
            return self.thresholds[group][key]
        except KeyError:
            raise ValueError(f"unknown analysis config reference {value!r}") from None

    def threshold(self, group: str, key: str) -> Threshold:
        return self.thresholds[group][key]


def _references(requirements: list[Requirement]) -> list[ParamValue]:
    found: list[ParamValue] = []
    for r in requirements:
        found += list(r.params.values()) + _references(r.any_of)
    return found


def check_references(rules: CapabilityRuleSet, config: AnalysisConfig) -> None:
    for rule in rules.analyses.values():
        for value in _references(rule.requires):
            config.resolve(value)


def load_rules(path: Path = RULES_PATH) -> CapabilityRuleSet:
    return CapabilityRuleSet.model_validate_json(path.read_text(encoding="utf-8"))


def load_analysis_config(path: Path = CONFIG_PATH) -> AnalysisConfig:
    return AnalysisConfig.model_validate_json(path.read_text(encoding="utf-8"))


@cache
def default_rules() -> CapabilityRuleSet:
    rules = load_rules()
    check_references(rules, default_analysis_config())
    return rules


@cache
def default_analysis_config() -> AnalysisConfig:
    return load_analysis_config()
