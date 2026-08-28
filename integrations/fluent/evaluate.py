"""Windows-side direct PyFluent check for deterministic baseline reloads."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from pathlib import Path

from vegapunk.fluent.codegen import (
    build_evaluate_point_code,
    extract_result_payload,
    normalize_computed_reports,
    parse_last_residuals,
)
from vegapunk.fluent.spec import DesignPointSpec, load_experiment_spec
from vegapunk.fluent.validity import evaluate_result_gate


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate two points through direct PyFluent"
    )
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--values", nargs="+", type=float, default=[0.55, 0.85])
    args = parser.parse_args()
    spec = load_experiment_spec(args.spec)
    if len(spec.parameters) != 1:
        raise ValueError(
            "the direct demo evaluator currently supports exactly one parameter"
        )

    import ansys.fluent.core as pyfluent

    solver = pyfluent.launch_fluent(**spec.connection.connect_kwargs)
    results = []
    try:
        parameter = spec.parameters[0]
        for index, value in enumerate(args.values):
            normalized = parameter.validate_value(value, f"values[{index}]")
            point = DesignPointSpec(
                name=f"direct-{index + 1}", values={parameter.name: normalized}
            )
            code = build_evaluate_point_code(spec, point)
            transcript = io.StringIO()
            started = time.perf_counter()
            with contextlib.redirect_stdout(transcript):
                exec(  # noqa: S102 - code is generated from the validated fixed schema
                    compile(code, "<vegapunk-fluent-direct>", "exec"),
                    {"solver": solver},
                )
            elapsed = time.perf_counter() - started
            stdout = transcript.getvalue()
            payload = extract_result_payload(stdout)
            reports = normalize_computed_reports(
                payload["computed_reports"], {report.name for report in spec.reports}
            )
            result = {
                "status": "completed",
                "parameters": point.values,
                "objective_value": reports[spec.objective.report]["value"],
                "objective_unit": reports[spec.objective.report].get("unit"),
                "reports": reports,
                "residuals": parse_last_residuals(stdout),
                "elapsed_seconds": elapsed,
                "baseline_reloaded": True,
            }
            if spec.optimization is not None:
                result["mass_flow_in"] = abs(
                    reports[spec.optimization.mass_flow_in_report]["value"]
                )
                result["mass_flow_out"] = abs(
                    reports[spec.optimization.mass_flow_out_report]["value"]
                )
                result["gates"] = evaluate_result_gate(spec, result).to_dict()
            results.append(result)
    finally:
        solver.exit()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"results": results}, indent=2), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
