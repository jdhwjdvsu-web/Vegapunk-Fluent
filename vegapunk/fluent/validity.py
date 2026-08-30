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
    status: str = "PASS"
    checks: tuple[dict[str, Any], ...] = ()
    reasons: tuple[str, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)
    constraint_vector: tuple[float, ...] = ()

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
        return None, GateResult(False, status="PARAMETER", reasons=(reason,))
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
    if not reasons:
        for combination in spec.parameter_constraints:
            actual = sum(
                coefficient * normalized[name]
                for name, coefficient in combination.coefficients.items()
            )
            passed = {
                "<": actual < combination.value,
                "<=": actual <= combination.value,
                ">": actual > combination.value,
                ">=": actual >= combination.value,
                "==": actual == combination.value,
            }[combination.operator]
            checks.append(
                {
                    "name": combination.name,
                    "passed": passed,
                    "actual": actual,
                    "operator": combination.operator,
                    "limit": combination.value,
                }
            )
            if not passed:
                reasons.append(
                    f"parameter constraint {combination.name} failed: "
                    f"{actual:g} {combination.operator} {combination.value:g}"
                )
    if reasons:
        return None, GateResult(
            False,
            status="PARAMETER",
            checks=tuple(checks),
            reasons=tuple(reasons),
        )
    return normalized, GateResult(True, checks=tuple(checks))


def _constraint_violation(check: Mapping[str, Any]) -> float:
    actual = float(check["actual"])
    limit = float(check["limit"])
    operator = check["operator"]
    if operator in {"<", "<="}:
        return max(0.0, actual - limit)
    if operator in {">", ">="}:
        return max(0.0, limit - actual)
    return abs(actual - limit)


def _conservation_error(
    reports: Mapping[str, Any],
    input_names: tuple[str, ...],
    output_names: tuple[str, ...],
    epsilon: float,
) -> float | None:
    try:
        inputs = sum(abs(float(reports[name]["value"])) for name in input_names)
        outputs = sum(abs(float(reports[name]["value"])) for name in output_names)
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(inputs) or not math.isfinite(outputs):
        return None
    return abs(inputs - outputs) / max(inputs, outputs, epsilon)


def evaluate_result_gate(spec: ExperimentSpec, result: Mapping[str, Any]) -> GateResult:
    """Classify a Trial as PASS, CONSTRAINT, or DIVERGED.

    ``CONSTRAINT`` is reserved for a trustworthy objective with an engineering
    constraint violation.  Solver, numeric, convergence, mass, or component balance
    failures are ``DIVERGED`` and must not be disguised as successful Optuna Trials.
    """

    if spec.optimization is None:
        raise ValueError("an optimization block is required for Fluent result gates")
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    metrics: dict[str, float] = {}
    diverged = False

    status_ok = result.get("status") == "completed"
    checks.append({"name": "solver_status", "passed": status_ok})
    if not status_ok:
        reasons.append(f"solver status is {result.get('status')!r}")
        diverged = True

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
            diverged = True

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
            diverged = True

    convergence = result.get("convergence")
    if isinstance(convergence, Mapping) and convergence.get("required"):
        convergence_passed = convergence.get("passed") is True
        checks.append({"name": "numerical_convergence", "passed": convergence_passed})
        if not convergence_passed:
            reasons.append("configured numerical convergence checks did not pass")
            diverged = True

    monitor_history = result.get("monitor_history")
    if not isinstance(monitor_history, Mapping):
        monitor_history = {}
    for monitor in spec.optimization.numerical_monitors:
        raw_values = monitor_history.get(monitor.report)
        values = []
        if isinstance(raw_values, (list, tuple)):
            try:
                values = [float(value) for value in raw_values[-monitor.window :]]
            except (TypeError, ValueError):
                values = []
        finite = len(values) >= monitor.window and all(math.isfinite(v) for v in values)
        if finite:
            scale = max(abs(sum(values) / len(values)), 1e-12)
            relative_slope = abs(values[-1] - values[0]) / (len(values) - 1) / scale
            relative_span = (max(values) - min(values)) / scale
            passed = (
                relative_slope <= monitor.max_relative_slope
                and relative_span <= monitor.max_relative_span
            )
        else:
            relative_slope = None
            relative_span = None
            passed = False
        checks.append(
            {
                "name": f"monitor:{monitor.report}",
                "passed": passed,
                "samples": len(values),
                "relative_slope": relative_slope,
                "relative_span": relative_span,
                "max_relative_slope": monitor.max_relative_slope,
                "max_relative_span": monitor.max_relative_span,
            }
        )
        if not passed:
            reasons.append(
                f"numerical monitor {monitor.report} is missing or not stationary"
            )
            diverged = True

    reports = result.get("reports")
    if not isinstance(reports, Mapping):
        reports = {}
    for conservation in spec.optimization.conservation_checks:
        relative_error = _conservation_error(
            reports,
            conservation.input_reports,
            conservation.output_reports,
            conservation.epsilon,
        )
        passed = (
            relative_error is not None
            and relative_error <= conservation.relative_tolerance
        )
        metric_name = f"{conservation.name}_relative_error"
        if relative_error is not None:
            metrics[metric_name] = relative_error
        checks.append(
            {
                "name": conservation.name,
                "passed": passed,
                "relative_error": relative_error,
                "tolerance": conservation.relative_tolerance,
            }
        )
        if not passed:
            reasons.append(
                f"{conservation.name} conservation is missing or exceeds tolerance"
            )
            diverged = True

    constraint_checks = result.get("constraints")
    if not isinstance(constraint_checks, list):
        constraint_checks = []
    constraint_vector: list[float] = []
    for item in constraint_checks:
        if not isinstance(item, Mapping):
            continue
        violation = _constraint_violation(item)
        constraint_vector.append(violation)
        checks.append(
            {
                "name": f"constraint:{item.get('report')}",
                "passed": violation <= 0,
                "violation": violation,
            }
        )
        if violation > 0:
            reasons.append(f"engineering constraint failed: {item.get('report')}")

    constrained = any(value > 0 for value in constraint_vector)
    status = "DIVERGED" if diverged else "CONSTRAINT" if constrained else "PASS"

    return GateResult(
        status == "PASS",
        status=status,
        checks=tuple(checks),
        reasons=tuple(reasons),
        metrics=metrics,
        constraint_vector=tuple(constraint_vector),
    )
