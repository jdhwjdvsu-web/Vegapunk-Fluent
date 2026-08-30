"""Generate fixed-shape PyFluent code from a validated experiment spec."""

from __future__ import annotations

import ast
import re
from typing import Any

from .spec import DesignPointSpec, ExperimentSpec, ParameterSpec

RESULT_MARKER = "VEGAPUNK_FLUENT_RESULT="
SETUP_MARKER = "VEGAPUNK_FLUENT_SETUP=ok"


def _settings_expression(parameter: ParameterSpec) -> str:
    return (
        f"solver.settings.{parameter.collection_path}[{parameter.object_name!r}]"
        f".{parameter.property_path}"
    )


def _report_setup_lines(spec: ExperimentSpec) -> list[str]:
    lines = ["__vp_report_defs = solver.settings.solution.report_definitions"]
    for report in spec.reports:
        lines.append(f"__vp_collection = __vp_report_defs.{report.kind}")
        lines.append(f"__vp_collection[{report.name!r}] = {{}}")
        lines.append(f"__vp_report = __vp_collection[{report.name!r}]")
        if report.kind == "surface":
            lines.append(f"__vp_report.report_type = {report.report_type!r}")
            lines.append(f"__vp_report.field = {report.field_name!r}")
            lines.append(f"__vp_report.surface_names = {list(report.locations)!r}")
        else:
            lines.append(f"__vp_report.boundaries = {list(report.locations)!r}")
    return lines


def build_report_setup_code(spec: ExperimentSpec) -> str:
    """Create report definitions using only validated, declarative fields."""

    lines = _report_setup_lines(spec)
    lines.append(f"print({SETUP_MARKER!r})")
    return "\n".join(lines) + "\n"


def build_design_point_code(spec: ExperimentSpec, point: DesignPointSpec) -> str:
    """Create one deterministic parameter-update and solve transaction."""

    parameter_by_name = {parameter.name: parameter for parameter in spec.parameters}
    lines = [f"__vp_point_name = {point.name!r}", "__vp_applied = {}"]
    for name, value in point.values.items():
        target = _settings_expression(parameter_by_name[name])
        lines.append(f"{target} = {value!r}")
        lines.append(f"__vp_applied[{name!r}] = {value!r}")
    if spec.solver.initialization == "hybrid":
        lines.append("solver.settings.solution.initialization.hybrid_initialize()")
    elif spec.solver.initialization == "standard":
        lines.append("solver.settings.solution.initialization.standard_initialize()")
    monitored_reports = []
    if spec.optimization is not None:
        monitored_reports = sorted(
            {monitor.report for monitor in spec.optimization.numerical_monitors}
        )
    if monitored_reports:
        monitor_count = max(
            monitor.window for monitor in spec.optimization.numerical_monitors
        )
        base, remainder = divmod(spec.solver.iterations, monitor_count)
        chunks = [base + (1 if index < remainder else 0) for index in range(monitor_count)]
        lines.append("__vp_monitor_history_raw = []")
        lines.append(f"for __vp_chunk_size in {chunks!r}:")
        lines.append(
            "    solver.settings.solution.run_calculation.iterate"
            "(iter_count=__vp_chunk_size)"
        )
        lines.append(
            "    __vp_monitor_history_raw.append("
            "solver.settings.solution.report_definitions.compute"
            f"(report_defs={monitored_reports!r}))"
        )
    else:
        lines.append(
            "solver.settings.solution.run_calculation.iterate"
            f"(iter_count={spec.solver.iterations})"
        )
    report_names = [report.name for report in spec.reports]
    lines.append(
        "__vp_computed = solver.settings.solution.report_definitions.compute"
        f"(report_defs={report_names!r})"
    )
    monitor_expression = "__vp_monitor_history_raw" if monitored_reports else "[]"
    lines.append(
        "__vp_payload = {"
        "'name': __vp_point_name, 'parameters': __vp_applied, "
        "'iterations_requested': "
        f"{spec.solver.iterations}, 'computed_reports': __vp_computed, "
        f"'monitor_history_raw': {monitor_expression}"
        "}"
    )
    lines.append(f"print({RESULT_MARKER!r} + repr(__vp_payload))")
    return "\n".join(lines) + "\n"


def build_evaluate_point_code(spec: ExperimentSpec, point: DesignPointSpec) -> str:
    """Build an isolated trial that reloads the immutable baseline case first."""

    case_file = spec.connection.connect_kwargs.get("case_file_name")
    if not isinstance(case_file, str) or not case_file.strip():
        raise ValueError(
            "connection.connect_kwargs.case_file_name is required for isolated trials"
        )
    transaction = build_design_point_code(spec, point).splitlines()
    lines = [
        f"__vp_baseline_case = {case_file!r}",
        "solver.settings.file.read_case(file_name=__vp_baseline_case)",
    ]
    lines.extend(_report_setup_lines(spec))
    lines.extend(transaction)
    return "\n".join(lines) + "\n"


def extract_result_payload(stdout: str) -> dict[str, Any]:
    """Extract the final literal payload printed by generated solver code."""

    marker_lines = [
        line[len(RESULT_MARKER) :]
        for line in stdout.splitlines()
        if line.startswith(RESULT_MARKER)
    ]
    if not marker_lines:
        raise ValueError("Fluent output did not contain a Vegapunk result marker")
    try:
        payload = ast.literal_eval(marker_lines[-1])
    except (SyntaxError, ValueError) as exc:
        raise ValueError("Fluent result marker was not a safe Python literal") from exc
    if not isinstance(payload, dict):
        raise TypeError("Fluent result payload must be an object")
    return payload


def normalize_computed_reports(
    raw: Any, expected_names: set[str]
) -> dict[str, dict[str, Any]]:
    """Normalize PyFluent's report compute response to numeric values and units."""

    merged: dict[str, Any] = {}
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if isinstance(item, dict):
            merged.update(item)
    normalized: dict[str, dict[str, Any]] = {}
    for name in expected_names:
        if name not in merged:
            raise ValueError(f"Fluent did not return expected report: {name}")
        value = merged[name]
        unit = None
        if isinstance(value, (list, tuple)) and value:
            unit = value[1] if len(value) > 1 else None
            value = value[0]
        if isinstance(value, bool):
            raise TypeError(f"Fluent report {name} was not numeric")
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Fluent report {name} was not numeric") from exc
        normalized[name] = {"value": numeric, "unit": str(unit) if unit else None}
    return normalized


_ITERATION_ROW = re.compile(r"^\s*(\d+)\s+(.*)$")


def parse_last_residuals(stdout: str) -> dict[str, Any] | None:
    """Parse the last Fluent residual row from a solver transcript."""

    columns: list[str] | None = None
    last: dict[str, Any] | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("iter") and "time/iter" in stripped:
            header = stripped.split()
            time_index = header.index("time/iter")
            columns = header[1:time_index]
            continue
        if not columns:
            continue
        match = _ITERATION_ROW.match(line)
        if not match:
            continue
        tokens = match.group(2).split()
        if len(tokens) < len(columns):
            continue
        try:
            values = [float(token) for token in tokens[: len(columns)]]
        except ValueError:
            continue
        last = {
            "iteration": int(match.group(1)),
            "values": dict(zip(columns, values)),
        }
    return last
