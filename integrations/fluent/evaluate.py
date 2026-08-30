"""Windows-side direct PyFluent check for deterministic baseline reloads."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from vegapunk.fluent.codegen import (
    build_evaluate_point_code,
    extract_result_payload,
    normalize_computed_reports,
    parse_last_residuals,
)
from vegapunk.fluent.spec import DesignPointSpec, load_experiment_spec
from vegapunk.fluent.validity import evaluate_result_gate


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _windows_working_set_mb(process_id: int | None) -> float | None:
    """Read one Windows process working set without an extra dependency."""

    if os.name != "nt" or not process_id:
        return None

    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("page_fault_count", ctypes.c_ulong),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCounters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int

    handle = kernel32.OpenProcess(0x0400 | 0x0010, 0, int(process_id))
    if not handle:
        return None
    try:
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return None
        return counters.working_set_size / (1024 * 1024)
    finally:
        kernel32.CloseHandle(handle)


def _solver_process_snapshot(solver) -> dict[str, object]:
    properties = solver.connection_properties
    process_ids = {
        "fluent_host_pid": getattr(properties, "fluent_host_pid", None),
        "cortex_pid": getattr(properties, "cortex_pid", None),
    }
    working_sets = {
        name: _windows_working_set_mb(process_id)
        for name, process_id in process_ids.items()
    }
    finite_values = [value for value in working_sets.values() if value is not None]
    return {
        "process_ids": process_ids,
        "process_working_set_mb": working_sets,
        "process_memory_mb": sum(finite_values) if finite_values else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate two points through direct PyFluent"
    )
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--values", nargs="+", type=float, default=[0.55, 0.85])
    parser.add_argument("--phase", choices=("cold", "sequence"), default="sequence")
    parser.add_argument("--run-index-start", type=int, default=1)
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
            started_at = _utc_now()
            started = time.perf_counter()
            with contextlib.redirect_stdout(transcript):
                exec(  # noqa: S102 - code is generated from the validated fixed schema
                    compile(code, "<vegapunk-fluent-direct>", "exec"),
                    {"solver": solver},
                )
            elapsed = time.perf_counter() - started
            completed_at = _utc_now()
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
                "phase": args.phase,
                "run_index": args.run_index_start + index,
                "started_at": started_at,
                "completed_at": completed_at,
                **_solver_process_snapshot(solver),
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
