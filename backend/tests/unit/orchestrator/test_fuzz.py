"""TASK-AGENT-009: random FakeLLM scripts never loop forever and never exceed the turn limits.

Each seed builds a random script from valid/invalid intents, tool-call bursts (offered,
unoffered, duplicated, malformed), free text, compose answers (good, lying, causal, broken),
LLM failures and crashes; the script may also run out. Every turn must end with exactly one
final event in a terminal state, within the tool, retry, step and LLM-call bounds.
"""

import random
from collections.abc import Callable

from app.llm.errors import LLMFailure, LLMFailureReason
from app.llm.ports import ChatResponse
from app.orchestrator.domain import FinalEvent, TurnLimits, TurnState
from tests.unit.orchestrator.fakes import AUG, NORTH, compose, harness, intent, num, reply, text

LIMITS = TurnLimits(
    max_steps=24, max_tool_calls=3, max_retries=1, max_llm_tokens=8000, max_loop_rounds=3
)
TERMINAL = {
    TurnState.COMPLETED,
    TurnState.REQUIRES_REVIEW,
    TurnState.REFUSED,
    TurnState.NEEDS_CLARIFICATION,
    TurnState.RECOVERED,
}
TOOLS = ["analytics.metric_query", "analytics.rank", "analytics.growth", "repair.run_code", "x.y"]
OBJECTIVES = ["metric_value", "rank", "compare", "trend", "explain_change", "forecast"]
# intent: 2 (one repair) + loop rounds + compose: 2 attempts x 2 (one repair each)
MAX_LLM_CALLS = 2 + LIMITS.max_loop_rounds + 2 * (LIMITS.max_retries + 1)


def _step(rng: random.Random) -> ChatResponse | BaseException:
    choices: list[Callable[[], ChatResponse | BaseException]] = [
        lambda: intent(
            objective=rng.choice(OBJECTIVES),
            metrics=[rng.choice(["Revenue", "Orders", "Nope"])],
            dimensions=rng.choice([[], ["Region"], ["Category"]]),
            filters=rng.choice([[], NORTH]),
            **rng.choice([{}, AUG]),
        ),
        lambda: intent(route=rng.choice(["clarify", "unsupported", "document", "mixed"])),
        lambda: reply("{not json"),
        lambda: reply(rng.choice(["done", "", "I will call tools now."])),
        lambda: reply(
            calls=[
                (rng.choice(TOOLS), rng.choice([{"metric": "Revenue"}, {}, {"metric": 1}]))
                for _ in range(rng.randint(1, 6))
            ]
        ),
        lambda: compose(text("Revenue was "), num("n1"), text(".")),
        lambda: compose(text("Revenue rose 12345% due to magic.")),
        lambda: compose(num("n9")),
        lambda: LLMFailure(rng.choice(list(LLMFailureReason)), "fuzz failure"),
        lambda: RuntimeError("fuzz crash"),
    ]
    return rng.choice(choices)()


def test_random_scripts_always_terminate_within_limits() -> None:
    seen: set[TurnState] = set()
    for seed in range(100):  # ~0.4 s each, mostly DuckDB connection setup
        rng = random.Random(seed)  # noqa: S311 - reproducible fuzzing, not crypto
        script = [_step(rng) for _ in range(rng.randint(0, 14))]
        start = [intent(metrics=["Revenue"], filters=NORTH, **AUG)]
        if rng.random() < 0.2:  # a model that repeats the same bad answer every time
            start += [reply("done"), *[compose(text("It rose 99% due to magic."))] * 4]
        if rng.random() < 0.6:  # bias towards turns that reach tools and composing
            script = [*start, *script]
        question = rng.choice(["Why did revenue drop?", "What was revenue?", "Rank regions"])
        h = harness(script, question, limits=LIMITS)
        events = h.run()  # asyncio.wait_for(..., 30 s) inside: a hang fails the test
        finals = [e for e in events if isinstance(e, FinalEvent)]
        assert len(finals) == 1, seed
        assert events[-1] is finals[0], seed
        final = finals[0]
        assert final.state in TERMINAL, seed
        assert final.usage.tool_calls <= LIMITS.max_tool_calls, seed
        assert len(h.executor.runs) <= LIMITS.max_tool_calls * 2, seed  # + one retry each
        assert final.usage.retries <= LIMITS.max_retries, seed
        assert final.usage.steps <= LIMITS.max_steps + 3, seed  # + FAILED, RECOVERED
        assert len(h.provider.requests) <= MAX_LLM_CALLS, seed
        assert len(h.recorder.records) == 1, seed
        seen.add(final.state)
    assert seen == TERMINAL  # the fuzz reaches every terminal state
