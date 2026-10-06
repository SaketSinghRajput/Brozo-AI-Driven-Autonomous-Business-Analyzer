#!/usr/bin/env python3
"""Synthetic performance-tier datasets (TASK-TEST-007): retail-like orders, 10k / 100k / 1M rows.

    python scripts/gen_perf_data.py                 # all tiers -> data/perf/orders_<rows>.csv
    python scripts/gen_perf_data.py --rows 10000    # one tier
    python scripts/gen_perf_data.py --check         # regenerate in memory, compare SHA-256

Stdlib only, seeded (``random.Random(seed)``, default 20260928), so a tier is byte-identical on
every run and platform. Output goes to ``data/perf/`` (``/data/`` is gitignored): never commit it.
Columns match evals/fixtures/retail/orders.csv so the production SemanticMapper maps the same
semantic model: order_id, customer_id, product_id, order_date (YYYY-MM-DD, 2024-09-01 ..
2026-08-31), quantity, unit_price, discount_pct, region, channel.
"""

import argparse
import csv
import hashlib
import io
import random
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "perf"
TIERS = (10_000, 100_000, 1_000_000)
SEED = 20260928
START, DAYS = date(2024, 9, 1), 730
REGIONS = ("North", "South", "East", "West")
CHANNELS = ("Online", "Retail Store", "Marketplace")
HEADER = [
    "order_id",
    "customer_id",
    "product_id",
    "order_date",
    "quantity",
    "unit_price",
    "discount_pct",
    "region",
    "channel",
]


def rows(n: int, seed: int = SEED) -> io.StringIO:
    rng = random.Random(seed)  # noqa: S311 - seeded test data, not crypto
    prices = [round(rng.uniform(5, 500), 2) for _ in range(200)]
    customers = max(50, n // 20)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    for i in range(n):
        day = START + timedelta(days=i * DAYS // n)  # sorted by date, evenly spread
        product = rng.randrange(200)
        writer.writerow(
            [
                f"O{i + 1:07d}",
                f"C{rng.randrange(customers) + 1:06d}",
                f"P{product + 1:04d}",
                day.isoformat(),
                rng.choices((1, 2, 3, 4, 5), (40, 25, 15, 12, 8))[0],
                f"{prices[product]:.2f}",
                rng.choices((0, 5, 10, 20), (70, 15, 10, 5))[0],
                rng.choice(REGIONS),
                rng.choices(CHANNELS, (50, 30, 20))[0],
            ]
        )
    return buf


def path_for(n: int) -> Path:
    return OUT / f"orders_{n}.csv"


def generate(n: int) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    path = path_for(n)
    path.write_text(rows(n).getvalue(), encoding="utf-8", newline="")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate seeded performance-tier CSVs.")
    parser.add_argument("--rows", type=int, nargs="*", default=list(TIERS))
    parser.add_argument("--check", action="store_true", help="verify existing files match")
    args = parser.parse_args()
    status = 0
    for n in args.rows:
        if args.check:
            want = hashlib.sha256(rows(n).getvalue().encode()).hexdigest()
            path = path_for(n)
            have = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing"
            print(n, "ok" if have == want else f"MISMATCH ({have[:12]} != {want[:12]})")
            status |= have != want
        else:
            path = generate(n)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            print(f"{path.relative_to(ROOT)} {path.stat().st_size} bytes sha256:{digest}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
