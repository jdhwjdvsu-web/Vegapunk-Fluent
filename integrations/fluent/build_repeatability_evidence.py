"""Build Stage-0 repeatability evidence from direct PyFluent evaluations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from vegapunk.fluent.history import atomic_json


def _record(result: dict[str, Any]) -> dict[str, Any]:
    mass_in = float(result["mass_flow_in"])
    mass_out = float(result["mass_flow_out"])
    return {
        "phase": result["phase"],
        "run_index": int(result["run_index"]),
        "metrics": {
            "outlet_temperature": float(result["objective_value"]),
            "mass_flow_in": mass_in,
            "mass_flow_out": mass_out,
            "mass_balance_relative_error": abs(mass_in - mass_out)
            / max(abs(mass_in), 1e-12),
        },
        "elapsed_seconds": float(result["elapsed_seconds"]),
        "process_memory_mb": result.get("process_memory_mb"),
        "process_ids": result.get("process_ids", {}),
        "started_at": result.get("started_at"),
        "completed_at": result.get("completed_at"),
        "baseline_reloaded": bool(result.get("baseline_reloaded")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cold", nargs="+", required=True)
    parser.add_argument("--sequence", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    records: list[dict[str, Any]] = []
    for value in args.cold:
        document = json.loads(Path(value).read_text(encoding="utf-8"))
        records.extend(_record(result) for result in document["results"])
    sequence = json.loads(Path(args.sequence).read_text(encoding="utf-8"))
    records.extend(_record(result) for result in sequence["results"])
    records.sort(key=lambda item: (item["phase"] != "cold", item["run_index"]))

    atomic_json(
        Path(args.output),
        {
            "schema_version": 1,
            "tolerances": {
                "outlet_temperature": 0.001,
                "mass_flow_in": 0.001,
                "mass_flow_out": 0.001,
                "mass_balance_relative_error": 0.01,
            },
            "records": records,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
