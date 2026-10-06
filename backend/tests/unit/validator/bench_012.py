"""BENCH-012: validator false-positive / false-negative rates. Run from backend/:

    uv run python -m tests.unit.validator.bench_012

Two sets:
- ``suite``: the TASK-VAL-008 adversarial cases + controls. It was written together with the
  validator, so it is a regression floor, not an unbiased estimate.
- ``probes``: phrasings chosen to find the limits of v1 (word numbers, paraphrased causality,
  incidental counts). Labels state what a correct validator should do; the validator was NOT
  tuned to them.
Positive = must not be shown as verified fact (expected != VALID). FN = positive returned VALID.
FP = legitimate answer returned anything but VALID.
"""

import time

from app.validator.domain import ValidationStatus

from .adversarial_cases import ALL, REV, Case, _t
from .factories import bundle, run

S = ValidationStatus


def _p(name: str, sentence: str, block: bool) -> Case:
    return Case(name, "probe", _t(sentence), bundle(REV), S.INVALID if block else S.VALID)


PROBES = [
    _p("word_number", "Revenue fell by fifteen percent.", True),
    _p("wrong_metric_right_number", "Profit fell 14.2%.", True),
    _p("causal_paraphrase_why", "The price change is why revenue fell 14.2%.", True),
    _p("causal_paraphrase_thanks", "Thanks to the new price, revenue fell 14.2%.", True),
    _p("guarantee_paraphrase", "Revenue is sure to recover next quarter.", True),
    _p("unquantified_fabrication", "Revenue halved last month.", True),
    _p("ordinal_ok", "Revenue fell 14.2%, the 2nd decline in a row.", False),
    _p("rows_as_periods", "Revenue fell 14.2% over 2 months.", False),
    _p("incidental_count", "Revenue fell 14.2% in 3 of 4 regions.", False),
    _p("due_to_rounding", "Revenue fell 14.2%. Due to rounding, totals may differ.", False),
    _p("single_anomaly", "Revenue fell 14.2%; the West had 1 anomaly.", False),
    _p("top_n_driver", "Revenue fell 14.2%; the top 3 regions account for most of it.", False),
]


def main() -> None:
    for name, cases in (("suite", ALL), ("probes", PROBES)):
        started = time.perf_counter()
        fn: list[str] = []
        fp: list[str] = []
        exact = 0
        for c in cases:
            status = run(c.req, c.ev, c.models, **c.options).status
            exact += status is c.expected
            if c.should_block and status is S.VALID:
                fn.append(c.name)
            if not c.should_block and status is not S.VALID:
                fp.append(c.name)
        ms = (time.perf_counter() - started) * 1000 / len(cases)
        pos = sum(c.should_block for c in cases)
        neg = len(cases) - pos
        print(
            f"{name}: cases={len(cases)} pos={pos} neg={neg} "
            f"FN={len(fn)} ({len(fn) / pos:.3f}) FP={len(fp)} ({len(fp) / neg:.3f}) "
            f"exact={exact}/{len(cases)} ms_per_case={ms:.2f}"
        )
        print(f"  FN: {fn}\n  FP: {fp}")


if __name__ == "__main__":
    main()
