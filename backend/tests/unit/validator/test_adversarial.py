"""TASK-VAL-008: every adversarial case gets its exact state; controls stay VALID."""

from collections.abc import Callable

import pytest

from app.validator.domain import AnswerSegment, ValidationStatus

from .adversarial_cases import ADVERSARIAL, ALL, CONTROLS, INJECTION, Case
from .factories import caveat, run, text


@pytest.mark.parametrize("case", ALL, ids=lambda c: c.name)
def test_case_gets_expected_status(case: Case) -> None:
    result = run(case.req, case.ev, case.models, **case.options)
    assert result.status is case.expected, [i.code for i in result.issues]
    assert case.codes <= {i.code for i in result.issues}


@pytest.mark.parametrize("case", ADVERSARIAL, ids=lambda c: c.name)
def test_blocked_cases_never_show_as_fact(case: Case) -> None:
    result = run(case.req, case.ev, case.models, **case.options)
    assert result.status is not ValidationStatus.VALID
    if case.req.impact == "high":
        assert result.status is ValidationStatus.REQUIRES_REVIEW


@pytest.mark.parametrize("case", CONTROLS + ADVERSARIAL[:6], ids=lambda c: c.name)
@pytest.mark.parametrize("segment", [text, caveat])
def test_injected_instructions_never_change_the_outcome(
    case: Case, segment: Callable[[str], AnswerSegment]
) -> None:
    before = run(case.req, case.ev, case.models, **case.options)
    segments = [*(case.req.segments or []), segment(" " + INJECTION)]
    injected = case.req.model_copy(update={"segments": segments})
    after = run(injected, case.ev, case.models, **case.options)
    assert after.status is before.status
    assert [i.code for i in after.issues] == [i.code for i in before.issues]


def test_suite_covers_every_attack_category() -> None:
    categories = {c.category for c in ADVERSARIAL}
    required = {
        "wrong_number",
        "rounding",
        "pct_pp",
        "fabricated_evidence",
        "cross_workspace",
        "schema",
        "causal",
        "injection",
    }
    assert required <= categories
