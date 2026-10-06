#!/usr/bin/env python3
"""BENCH-008 / TASK-MLOPS-007: ingest -> profile -> understand -> metric queries, per size tier.

    cd backend && uv run python ../scripts/bench/bench_008.py                 # 10k, 100k
    cd backend && uv run python ../scripts/bench/bench_008.py --tiers 10000 100000 1000000

Each tier runs in a fresh child process (so peak RSS is per tier) on the seeded CSV from
scripts/gen_perf_data.py (generated on demand into the gitignored data/perf/). The child runs
the production path of evals/pipeline.py (FileInspector -> ingest_file -> profile_frame ->
understand_frame -> DomainDetector -> SemanticMapper), then three analytics tool calls through
the real ToolExecutor (evidence recording included), each 1 warm-up + ``--repeats`` timed runs:
  q_total  analytics.metric_query  Revenue, Aug 2026 (date-range filter)
  q_group  analytics.rank          Revenue by Region, whole range
  q_trend  analytics.trend         Revenue per month, 24 months, Mann-Kendall
Writes evals/reports/bench_008.json; numbers are measured in this run, nothing is estimated.
"""

import argparse
import asyncio
import ctypes
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "evals"))
REPORT = ROOT / "evals" / "reports" / "bench_008.json"
AUG26 = ["2026-08-01", "2026-08-31"]
QUERIES: dict[str, tuple[str, dict[str, Any]]] = {
    "q_total": (
        "analytics.metric_query",
        {
            "metric": "Revenue",
            "filters": [{"dimension": "Order Date", "op": "between", "value": AUG26}],
        },
    ),
    "q_group": ("analytics.rank", {"metric": "Revenue", "dimension": "Region"}),
    "q_trend": (
        "analytics.trend",
        {"metric": "Revenue", "grain": "month", "start": "2024-09-01", "end": "2026-08-31"},
    ),
}


def peak_rss_mb() -> float:
    """Peak resident set of this process: ru_maxrss on POSIX, PeakWorkingSetSize on Windows."""
    try:
        import resource

        kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return kb / 1024 if sys.platform != "darwin" else kb / 1024 / 1024
    except ImportError:
        pass

    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p  # pseudo-handle -1, 64-bit
    get_info = kernel32.K32GetProcessMemoryInfo
    get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not get_info(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise OSError("K32GetProcessMemoryInfo failed")
    return float(counters.PeakWorkingSetSize) / 2**20


def child(rows: int, repeats: int) -> dict[str, Any]:
    from gen_perf_data import generate, path_for
    from pipeline import SCOPE, rig, understand_csv

    from app.evidence.recorder import CallKey
    from app.tools.domain import ToolContext
    from app.tools.executor import TurnBudget
    from app.tools.permissions import Role, TurnProfile, effective_permissions

    path = path_for(rows)
    if not path.exists():
        generate(rows)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        data = understand_csv(path, "orders", Path(tmp))
    pipeline_s = time.perf_counter() - started
    model = data.model
    r = rig(data, "bench")
    ctx = ToolContext(
        workspace_id=SCOPE.workspace_id,
        principal_id=SCOPE.principal_id,
        trace_id="bench-008",
        permissions=effective_permissions(Role.ANALYST, TurnProfile.CHAT_DEFAULT),
        dataset_version_id=model.dataset_version_id,
        semantic_model_version=model.version,
    )
    queries: dict[str, Any] = {}
    for name, (tool, args) in QUERIES.items():
        times, status = [], None
        for i in range(repeats + 1):
            key = CallKey(f"bench-{name}", f"S{i}")
            t0 = time.perf_counter()
            result = asyncio.run(r.executor.run(tool, dict(args), ctx, key, TurnBudget(4)))
            elapsed = time.perf_counter() - t0
            status = result.status
            if i:  # i == 0 is the warm-up
                times.append(elapsed * 1000)
        ordered = sorted(times)
        queries[name] = {
            "tool": tool,
            "status": status,
            "runs": len(times),
            "p50_ms": round(statistics.median(ordered), 2),
            "p95_ms": round(ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))], 2),
            "max_ms": round(ordered[-1], 2),
        }
    return {
        "rows": rows,
        "csv_bytes": path.stat().st_size,
        "rows_ingested": data.frame.height,
        "metrics": [m.name for m in model.metrics],
        "domain": f"{data.domain.status.value}:{getattr(data.domain, 'primary', None)}",
        "stages_s": {k: round(v, 3) for k, v in data.stages_s.items()},
        "pipeline_s": round(pipeline_s, 3),
        "ingest_rows_per_s": round(rows / data.stages_s["ingest"]),
        "pipeline_rows_per_s": round(rows / pipeline_s),
        "queries": queries,
        "peak_rss_mb": round(peak_rss_mb(), 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="BENCH-008 performance harness.")
    parser.add_argument("--tiers", type=int, nargs="*", default=[10_000, 100_000])
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--child", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=int, default=600, help="seconds per tier")
    args = parser.parse_args()
    if args.child is not None:
        print(json.dumps(child(args.child, args.repeats)))
        return 0
    results: list[dict[str, Any]] = []
    for rows in args.tiers:
        cmd = [sys.executable, __file__, "--child", str(rows), "--repeats", str(args.repeats)]
        started = time.perf_counter()
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)  # noqa: S603 - fixed argv (this script)
        except subprocess.TimeoutExpired:
            results.append({"rows": rows, "error": f"timed out after {args.timeout}s"})
            continue
        if done.returncode != 0:
            results.append({"rows": rows, "error": done.stderr.strip().splitlines()[-3:]})
            continue
        tier = json.loads(done.stdout.strip().splitlines()[-1])
        tier["process_wall_s"] = round(time.perf_counter() - started, 2)
        results.append(tier)
        print(json.dumps(tier), flush=True)
    report = {
        "bench": "BENCH-008",
        "python": platform.python_version(),
        "platform": platform.platform(terse=True),
        "repeats": args.repeats,
        "tiers": results,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
