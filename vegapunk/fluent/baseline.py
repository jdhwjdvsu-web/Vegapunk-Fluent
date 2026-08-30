"""Analyze the Stage-0 cold-start and sequential-repeatability evidence."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any

from .history import atomic_json, utc_now


def analyze_repeatability(
    records: list[dict[str, Any]],
    tolerances: dict[str, float],
    *,
    minimum_cold_runs: int = 3,
    minimum_sequence_runs: int = 10,
    maximum_memory_growth_fraction: float = 0.10,
) -> dict[str, Any]:
    """Evaluate configured metric spread without assuming a universal 0.1%."""

    cold = [record for record in records if record.get("phase") == "cold"]
    sequence = [record for record in records if record.get("phase") == "sequence"]
    checks = []
    for metric, tolerance in tolerances.items():
        values = []
        for record in records:
            try:
                value = float(record["metrics"][metric])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
        if not values:
            checks.append(
                {"metric": metric, "passed": False, "reason": "no finite values"}
            )
            continue
        mean = statistics.fmean(values)
        relative_spread = (max(values) - min(values)) / max(abs(mean), 1e-12)
        checks.append(
            {
                "metric": metric,
                "samples": len(values),
                "mean": mean,
                "minimum": min(values),
                "maximum": max(values),
                "relative_spread": relative_spread,
                "tolerance": float(tolerance),
                "passed": relative_spread <= float(tolerance),
            }
        )

    memory = [
        float(record["process_memory_mb"])
        for record in sequence
        if record.get("process_memory_mb") is not None
    ]
    memory_growth = None
    memory_passed = False
    if len(memory) >= 2:
        memory_growth = (memory[-1] - memory[0]) / max(memory[0], 1e-12)
        memory_passed = memory_growth <= maximum_memory_growth_fraction
    readiness = {
        "cold_runs": len(cold),
        "sequence_runs": len(sequence),
        "cold_run_count_passed": len(cold) >= minimum_cold_runs,
        "sequence_run_count_passed": len(sequence) >= minimum_sequence_runs,
        "memory_growth_fraction": memory_growth,
        "memory_growth_tolerance": maximum_memory_growth_fraction,
        "memory_growth_passed": memory_passed,
    }
    passed = (
        readiness["cold_run_count_passed"]
        and readiness["sequence_run_count_passed"]
        and memory_passed
        and all(check["passed"] for check in checks)
    )
    return {
        "schema_version": 1,
        "passed": passed,
        "metric_checks": checks,
        "readiness": readiness,
        "generated_at": utc_now(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyze Fluent repeatability runs")
    parser.add_argument("--input", required=True, help="JSON file with records/tolerances")
    parser.add_argument("--output", default="baseline_assessment.json")
    args = parser.parse_args(argv)
    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    result = analyze_repeatability(raw["records"], raw["tolerances"])
    atomic_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
