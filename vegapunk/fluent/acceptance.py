"""Stage-4 fixed 20-point unattended fault-tolerance admission check."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .history import atomic_json, utc_now


EXPECTED_SCENARIOS = tuple(
    [f"normal-{index:02d}" for index in range(1, 14)]
    + [
        "parameter-out-of-range",
        "mass-balance-failure",
        "numerical-divergence",
        "trial-timeout",
        "fluent-terminated",
        "mcp-restart",
        "controller-restart",
    ]
)


def validate_acceptance_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate evidence produced by a real or simulated 20-point Campaign."""

    ids = [str(record.get("scenario_id")) for record in records]
    counts = Counter(ids)
    missing = sorted(set(EXPECTED_SCENARIOS) - set(ids))
    duplicate_scenarios = sorted(name for name, count in counts.items() if count != 1)
    duplicate_computations = sorted(
        record["scenario_id"]
        for record in records
        if int(record.get("fluent_execution_count", 0)) > 1
    )
    nonterminal = sorted(
        record["scenario_id"]
        for record in records
        if record.get("state")
        not in {"TOLD", "FAILED", "CANCELLED", "ORPHANED"}
    )
    invalid_best = sorted(
        record["scenario_id"]
        for record in records
        if record.get("selected_as_best") and record.get("gate_status") != "PASS"
    )
    inconsistent = sorted(
        record["scenario_id"]
        for record in records
        if not record.get("ledger_consistent", False)
        or not record.get("result_files_consistent", False)
    )
    passed = not any(
        (missing, duplicate_scenarios, duplicate_computations, nonterminal, invalid_best, inconsistent)
    )
    return {
        "schema_version": 1,
        "passed": passed,
        "expected_points": 20,
        "observed_points": len(records),
        "missing": missing,
        "duplicate_scenarios": duplicate_scenarios,
        "duplicate_computations": duplicate_computations,
        "nonterminal": nonterminal,
        "invalid_selected_as_best": invalid_best,
        "inconsistent_artifacts": inconsistent,
        "generated_at": utc_now(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the fixed 20-point gate")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", default="acceptance_summary.json")
    args = parser.parse_args(argv)
    records = json.loads(Path(args.input).read_text(encoding="utf-8"))
    result = validate_acceptance_records(records)
    atomic_json(Path(args.output), result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
