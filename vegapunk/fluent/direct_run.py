"""Bounded single-point Fluent run with a temperature-contour artifact."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codegen import (
    build_evaluate_point_code,
    extract_result_payload,
    normalize_computed_reports,
    parse_last_residuals,
)
from .runner import FluentExperimentError
from .spec import DesignPointSpec, ExperimentSpec


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _local_case_path(value: str) -> Path:
    """Resolve a Windows case path to its WSL mount for read-only verification."""

    direct = Path(value)
    if direct.is_file():
        return direct
    if os.name != "nt":
        match = re.fullmatch(r"([A-Za-z]):[\\/](.+)", value)
        if match:
            relative = match.group(2).replace("\\", "/")
            mounted = Path("/mnt") / match.group(1).lower() / relative
            if mounted.is_file():
                return mounted
    raise FluentExperimentError(f"baseline case is not readable: {value}")


def _verify_baseline_for_direct_run(spec: ExperimentSpec) -> None:
    expected = spec.connection.baseline_sha256
    if expected is None:
        return
    value = spec.connection.connect_kwargs.get("case_file_name")
    if not isinstance(value, str) or not value:
        raise FluentExperimentError(
            "case_file_name is required when baseline_sha256 is configured"
        )
    path = _local_case_path(value)
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    actual = hasher.hexdigest()
    if actual != expected:
        raise FluentExperimentError(
            f"baseline case SHA-256 mismatch: expected {expected}, got {actual}"
        )


def _structured(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


async def _call(client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(
        name,
        arguments,
        raise_on_error=False,
    )
    data = _structured(result)
    if getattr(result, "is_error", False) or data.get("status") == "error":
        raise FluentExperimentError(
            f"{name} failed: {data.get('message') or data.get('error_code') or data}"
        )
    return data


def build_direct_temperature_code(
    spec: ExperimentSpec,
    *,
    parameter_values: dict[str, float],
    iterations: int,
    surface: str,
) -> str:
    """Build fixed-shape solver and contour code from validated inputs."""

    point = DesignPointSpec(
        name="direct-parameter-audit",
        values=parameter_values,
    )
    solver_code = build_evaluate_point_code(spec, point).rstrip()
    contour_name = "temperature-contour-direct-run"
    contour_code = [
        "__vp_contours = solver.settings.results.graphics.contour",
        f"__vp_contours[{contour_name!r}] = {{}}",
        f"__vp_contour = __vp_contours[{contour_name!r}]",
        "__vp_contour.field = 'temperature'",
        f"__vp_contour.surfaces_list = [{surface!r}]",
        "__vp_contour.filled = True",
        "__vp_contour.node_values = True",
        "__vp_contour.display()",
        "solver.settings.results.graphics.views.auto_scale()",
        "print('VEGAPUNK_CONTOUR=ready')",
    ]
    return solver_code.replace(
        f"iterate(iter_count={spec.solver.iterations})",
        f"iterate(iter_count={iterations})",
    ) + "\n" + "\n".join(contour_code) + "\n"


async def run_direct_temperature_case(
    spec: ExperimentSpec,
    output_dir: str | Path,
    *,
    parameter_values: dict[str, float],
    iterations: int,
    classification: str = "controlled_parameter",
    parameter_metadata: list[dict[str, Any]] | None = None,
    surface: str = "symmetry-xyplane",
) -> dict[str, Any]:
    """Run one exact point through PyFluent-MCP and persist its contour."""

    if not spec.parameters:
        raise FluentExperimentError("direct run requires at least one parameter")
    if spec.optimization is None:
        raise FluentExperimentError("direct run requires mass-balance configuration")
    expected = {parameter.name: parameter for parameter in spec.parameters}
    if set(parameter_values) != set(expected):
        raise FluentExperimentError("direct run parameter values do not match the spec")
    validated_values = {
        name: expected[name].validate_value(value, name)
        for name, value in parameter_values.items()
    }
    if iterations < 1 or iterations > 1_000_000:
        raise FluentExperimentError("iterations must be between 1 and 1000000")
    _verify_baseline_for_direct_run(spec)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = Path(output_dir) / "direct_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    code = build_direct_temperature_code(
        spec,
        parameter_values=validated_values,
        iterations=iterations,
        surface=surface,
    )
    code_path = run_dir / "generated_code.py"
    stdout_path = run_dir / "solver_stdout.log"
    contour_path = run_dir / "temperature_contour.png"
    summary_path = run_dir / "summary.json"
    code_path.write_text(code, encoding="utf-8")

    try:
        from fastmcp import Client
    except ImportError as exc:
        raise FluentExperimentError("fastmcp is required for direct Fluent runs") from exc

    started_at = _utc_now()
    started = time.perf_counter()
    connected_by_run = False
    execution: dict[str, Any] = {}
    async with Client(
        spec.connection.endpoint,
        timeout=spec.connection.client_timeout_seconds,
    ) as client:
        status = await _call(client, "session_status", {})
        if status.get("connected"):
            raise FluentExperimentError(
                "PyFluent-MCP already has an active session; wait for it to finish"
            )
        try:
            await _call(
                client,
                "connect",
                {"connect_kwargs": spec.connection.connect_kwargs},
            )
            connected_by_run = True
            validation = await _call(client, "validate_code", {"code": code})
            if validation.get("status") != "ok":
                raise FluentExperimentError(
                    f"validate_code rejected direct run: {validation}"
                )
            execution = await _call(client, "run_code", {"code": code})
            stdout = str(execution.get("stdout", ""))
            stdout_path.write_text(stdout, encoding="utf-8")
            if "VEGAPUNK_CONTOUR=ready" not in stdout:
                raise FluentExperimentError("Fluent did not display the contour")
            screenshot = await _call(client, "screenshot", {})
            encoded = screenshot.get("data")
            if not isinstance(encoded, str) or not encoded:
                raise FluentExperimentError("MCP screenshot did not contain PNG data")
            try:
                image = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise FluentExperimentError("MCP screenshot was not valid base64") from exc
            if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                raise FluentExperimentError("MCP screenshot was not a PNG image")
            contour_path.write_bytes(image)
        finally:
            if connected_by_run:
                await _call(client, "disconnect", {})

    stdout = str(execution.get("stdout", ""))
    payload = extract_result_payload(stdout)
    reports = normalize_computed_reports(
        payload.get("computed_reports"),
        {report.name for report in spec.reports},
    )
    inlet = abs(float(reports[spec.optimization.mass_flow_in_report]["value"]))
    outlet = abs(float(reports[spec.optimization.mass_flow_out_report]["value"]))
    relative_error = abs(outlet - inlet) / max(
        inlet, spec.optimization.mass_balance_epsilon
    )
    objective = reports[spec.objective.report]
    elapsed_seconds = time.perf_counter() - started
    result = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "SUCCEEDED",
        "classification": classification,
        "started_at": started_at,
        "completed_at": _utc_now(),
        "elapsed_seconds": elapsed_seconds,
        "parameters": validated_values,
        "parameter_details": parameter_metadata
        or [
            {
                "key": parameter.name,
                "label": parameter.name,
                "unit": parameter.unit,
                "display_value": validated_values[parameter.name],
            }
            for parameter in spec.parameters
        ],
        "iterations_requested": iterations,
        "residuals": parse_last_residuals(stdout),
        "reports": reports,
        "objective_value": float(objective["value"]),
        "objective_unit": objective.get("unit"),
        "mass_balance_relative_error": relative_error,
        "mass_balance_gate_limit": (
            spec.optimization.mass_balance_relative_tolerance
        ),
        "mass_balance_gate_passed": (
            math.isfinite(relative_error)
            and relative_error
            <= spec.optimization.mass_balance_relative_tolerance
        ),
        "surface": surface,
        "image_url": f"/api/direct-runs/{run_id}/temperature-contour.png",
        "artifacts": {
            "summary": str(summary_path),
            "temperature_contour": str(contour_path),
            "generated_code": str(code_path),
            "solver_stdout": str(stdout_path),
        },
    }
    summary_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return result
