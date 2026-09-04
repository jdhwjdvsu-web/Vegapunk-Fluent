"""Export one audited Mixing Elbow temperature contour through PyFluent-MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from vegapunk.fluent.runner import FluentExperimentError
from vegapunk.fluent.spec import ConnectionSpec, load_experiment_spec


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
    result = await client.call_tool(name, arguments, raise_on_error=False)
    data = _structured(result)
    if getattr(result, "is_error", False) or data.get("status") == "error":
        raise FluentExperimentError(
            f"{name} failed: {data.get('message') or data.get('error_code') or data}"
        )
    return data


def _build_code(
    *, case_file: Path, velocity: float, iterations: int, surface: str, output: Path
) -> str:
    contour_name = "temperature-contour-cold-inlet-10ms"
    return "\n".join(
        [
            f"solver.settings.file.read_case(file_name={str(case_file)!r})",
            "__vp_inlet = solver.settings.setup.boundary_conditions.velocity_inlet['cold-inlet']",
            f"__vp_inlet.momentum.velocity_magnitude.value = {velocity!r}",
            "solver.settings.solution.initialization.hybrid_initialize()",
            f"solver.settings.solution.run_calculation.iterate(iter_count={iterations})",
            "__vp_contours = solver.settings.results.graphics.contour",
            f"__vp_contours[{contour_name!r}] = {{}}",
            f"__vp_contour = __vp_contours[{contour_name!r}]",
            "__vp_contour.field = 'temperature'",
            f"__vp_contour.surfaces_list = [{surface!r}]",
            "__vp_contour.filled = True",
            "__vp_contour.node_values = True",
            "__vp_contour.display()",
            "solver.settings.results.graphics.views.auto_scale()",
            "__vp_picture = solver.settings.results.graphics.picture",
            "__vp_picture.use_window_resolution = False",
            "__vp_picture.x_resolution = 1600",
            "__vp_picture.y_resolution = 1000",
            f"__vp_picture.save_picture(file_name={str(output)!r})",
            "print('VEGAPUNK_CONTOUR=ok')",
            "",
        ]
    )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    spec = load_experiment_spec(args.spec)
    if len(spec.parameters) != 1:
        raise ValueError("temperature contour export requires exactly one parameter")
    velocity = spec.parameters[0].validate_value(args.velocity, "velocity")
    case_file = Path(args.case).resolve()
    output = Path(args.output).resolve()
    if not case_file.is_file():
        raise FileNotFoundError(case_file)
    output.parent.mkdir(parents=True, exist_ok=True)

    connection_data = asdict(spec.connection)
    kwargs = dict(connection_data["connect_kwargs"])
    kwargs.update(
        {
            "case_file_name": str(case_file),
            "ui_mode": "hidden_gui",
            "graphics_driver": "msw",
            "cleanup_on_exit": True,
        }
    )
    connection_data["connect_kwargs"] = kwargs
    spec = replace(spec, connection=ConnectionSpec.from_dict(connection_data))
    code = _build_code(
        case_file=case_file,
        velocity=velocity,
        iterations=args.iterations,
        surface=args.surface,
        output=output,
    )
    code_path = output.with_suffix(".py")
    code_path.write_text(code, encoding="utf-8")

    try:
        from fastmcp import Client
    except ImportError as exc:
        raise FluentExperimentError("fastmcp is required") from exc

    connected_by_script = False
    async with Client(
        spec.connection.endpoint,
        timeout=spec.connection.client_timeout_seconds,
    ) as client:
        status = await _call(client, "session_status", {})
        if status.get("connected"):
            raise FluentExperimentError("PyFluent-MCP already has an active session")
        try:
            await _call(
                client,
                "connect",
                {"connect_kwargs": spec.connection.connect_kwargs},
            )
            connected_by_script = True
            validation = await _call(client, "validate_code", {"code": code})
            if validation.get("status") != "ok":
                raise FluentExperimentError(f"validate_code rejected contour: {validation}")
            execution = await _call(client, "run_code", {"code": code})
            stdout = str(execution.get("stdout", ""))
            if "VEGAPUNK_CONTOUR=ok" not in stdout:
                raise FluentExperimentError("contour completion marker was not returned")
        finally:
            if connected_by_script:
                await _call(client, "disconnect", {})

    if not output.is_file() or output.stat().st_size == 0:
        raise FluentExperimentError("Fluent did not create the contour image")
    return {
        "status": "ok",
        "output": str(output),
        "bytes": output.stat().st_size,
        "velocity_m_per_s": velocity,
        "surface": args.surface,
        "iterations_requested": args.iterations,
        "generated_code": str(code_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--case", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--velocity", type=float, required=True)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--surface", default="symmetry-xyplane")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
