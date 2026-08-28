"""Parameter, numeric, and conservation gates for Fluent optimization trials."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from .spec import ExperimentSpec, SpecError


@dataclass(frozen=True)
class GateResult:
    passed: bool
    checks: tuple[dict[str, Any], ...] = ()
    reasons: tuple[str, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_parameter_values(
    spec: ExperimentSpec, values: Mapping[str, Any]
) -> tuple[dict[str, float] | None, GateResult]:
    """Validate the exact parameter allowlist and numeric bounds before MCP use."""

    by_name = {parameter.name: parameter for parameter in spec.parameters}
    missing = sorted(set(by_name) - set(values))
    extra = sorted(set(values) - set(by_name))
    if missing or extra:
        reason = f"parameter mismatch; missing={missing}, extra={extra}"
        return None, GateResult(False, reasons=(reason,))
    normalized: dict[str, float] = {}
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    for name, parameter in by_name.items():
        try:
            normalized[name] = parameter.validate_value(
                values[name], f"parameters.{name}"
            )
            checks.append({"name": name, "passed": True, "value": normalized[name]})
        except SpecError as exc:
            checks.append({"name": name, "passed": False, "value": values.get(name)})
            reasons.append(str(exc))
    if reasons:
        return None, GateResult(False, tuple(checks), tuple(reasons))
    return normalized, GateResult(True, tuple(checks))


def evaluate_result_gate(spec: ExperimentSpec, result: Mapping[str, Any]) -> GateResult:
    """Reject non-finite objectives and trials that violate mass conservation."""

    if spec.optimization is None:
        raise ValueError("an optimization block is required for Fluent result gates")
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    metrics: dict[str, float] = {}

    status_ok = result.get("status") == "completed"
    checks.append({"name": "solver_status", "passed": status_ok})
    if not status_ok:
        reasons.append(f"solver status is {result.get('status')!r}")

    numeric_values: dict[str, float] = {}
    for key in ("objective_value", "mass_flow_in", "mass_flow_out"):
        try:
            value = float(result[key])
            passed = math.isfinite(value)
        except (KeyError, TypeError, ValueError):
            value = float("nan")
            passed = False
        checks.append({"name": f"finite_{key}", "passed": passed, "value": value})
        if passed:
            numeric_values[key] = value
        else:
            reasons.append(f"{key} is missing or non-finite")

    if {"mass_flow_in", "mass_flow_out"} <= set(numeric_values):
        inlet = abs(numeric_values["mass_flow_in"])
        outlet = abs(numeric_values["mass_flow_out"])
        denominator = max(inlet, outlet, spec.optimization.mass_balance_epsilon)
        relative_error = abs(inlet - outlet) / denominator
        tolerance = spec.optimization.mass_balance_relative_tolerance
        passed = relative_error <= tolerance
        metrics["mass_balance_relative_error"] = relative_error
        checks.append(
            {
                "name": "mass_balance",
                "passed": passed,
                "relative_error": relative_error,
                "tolerance": tolerance,
            }
        )
        if not passed:
            reasons.append(
                f"mass balance relative error {relative_error:.6g} exceeds {tolerance:.6g}"
            )

    return GateResult(not reasons, tuple(checks), tuple(reasons), metrics)
