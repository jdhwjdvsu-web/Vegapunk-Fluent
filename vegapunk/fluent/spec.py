"""Validated configuration contract for Fluent parameter experiments."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class SpecError(ValueError):
    """Raised when an experiment specification is unsafe or incomplete."""


_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ALLOWED_CONNECT_KWARGS = {
    "precision",
    "processor_count",
    "ui_mode",
    "product_version",
    "dimension",
    "mode",
    "gpu",
    "case_file_name",
    "case_data_file_name",
    "graphics_driver",
    "scheduler_options",
    "start_timeout",
    "cleanup_on_exit",
}
_OPERATORS = {"<", "<=", ">", ">=", "=="}


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecError(f"{label} must be an object")
    return value


def _non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{label} must be a non-empty string")
    return value.strip()


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise SpecError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SpecError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise SpecError(f"{label} must be finite")
    return result


def _path(value: Any, label: str) -> str:
    path = _non_empty_string(value, label)
    tokens = path.split(".")
    if any(not _TOKEN.fullmatch(token) for token in tokens):
        raise SpecError(
            f"{label} must be a dot-separated Fluent settings path containing only identifiers"
        )
    return path


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise SpecError(
                    f"environment variable {name} is required by the Fluent spec"
                )
            return os.environ[name]

        return _ENV.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _expand_env(item) for key, item in value.items()}
    return value


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ConnectionSpec:
    endpoint: str = "http://127.0.0.1:18000/mcp"
    job_endpoint: str | None = None
    baseline_sha256: str | None = None
    connect_kwargs: dict[str, Any] = field(default_factory=dict)
    client_timeout_seconds: int = 300
    allow_remote_endpoint: bool = False
    reuse_existing_session: bool = False
    disconnect_on_exit: bool = True

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ConnectionSpec:
        endpoint = _non_empty_string(
            _expand_env(raw.get("endpoint", cls.endpoint)), "connection.endpoint"
        )
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise SpecError("connection.endpoint must be an HTTP(S) MCP URL")
        allow_remote = bool(raw.get("allow_remote_endpoint", False))
        if not allow_remote and not _is_loopback(parsed.hostname):
            raise SpecError(
                "non-loopback Fluent MCP endpoints require allow_remote_endpoint=true"
            )
        job_endpoint_raw = _expand_env(raw.get("job_endpoint"))
        job_endpoint = None
        if job_endpoint_raw is not None:
            job_endpoint = _non_empty_string(
                job_endpoint_raw, "connection.job_endpoint"
            )
            parsed_job = urlparse(job_endpoint)
            if parsed_job.scheme not in {"http", "https"} or not parsed_job.hostname:
                raise SpecError("connection.job_endpoint must be an HTTP(S) MCP URL")
            if not allow_remote and not _is_loopback(parsed_job.hostname):
                raise SpecError(
                    "non-loopback Fluent Job MCP endpoints require "
                    "allow_remote_endpoint=true"
                )
        baseline_sha256 = raw.get("baseline_sha256")
        if baseline_sha256 is not None:
            baseline_sha256 = _non_empty_string(
                baseline_sha256, "connection.baseline_sha256"
            ).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", baseline_sha256):
                raise SpecError(
                    "connection.baseline_sha256 must contain 64 hexadecimal characters"
                )
        kwargs = dict(
            _mapping(raw.get("connect_kwargs", {}), "connection.connect_kwargs")
        )
        unknown = sorted(set(kwargs) - _ALLOWED_CONNECT_KWARGS)
        if unknown:
            raise SpecError(f"unsupported Fluent connect kwargs: {', '.join(unknown)}")
        kwargs = _expand_env(kwargs)
        timeout = int(raw.get("client_timeout_seconds", 300))
        if not 1 <= timeout <= 86400:
            raise SpecError(
                "connection.client_timeout_seconds must be between 1 and 86400"
            )
        return cls(
            endpoint=endpoint,
            job_endpoint=job_endpoint,
            baseline_sha256=baseline_sha256,
            connect_kwargs=kwargs,
            client_timeout_seconds=timeout,
            allow_remote_endpoint=allow_remote,
            reuse_existing_session=bool(raw.get("reuse_existing_session", False)),
            disconnect_on_exit=bool(raw.get("disconnect_on_exit", True)),
        )


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    collection_path: str
    object_name: str
    property_path: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    log: bool = False

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> ParameterSpec:
        prefix = f"parameters[{index}]"
        minimum = raw.get("minimum")
        maximum = raw.get("maximum")
        step = raw.get("step")
        result = cls(
            name=_non_empty_string(raw.get("name"), f"{prefix}.name"),
            collection_path=_path(
                raw.get("collection_path"), f"{prefix}.collection_path"
            ),
            object_name=_non_empty_string(
                raw.get("object_name"), f"{prefix}.object_name"
            ),
            property_path=_path(raw.get("property_path"), f"{prefix}.property_path"),
            unit=(
                _non_empty_string(raw.get("unit"), f"{prefix}.unit")
                if raw.get("unit") is not None
                else None
            ),
            minimum=_number(minimum, f"{prefix}.minimum")
            if minimum is not None
            else None,
            maximum=_number(maximum, f"{prefix}.maximum")
            if maximum is not None
            else None,
            step=_number(step, f"{prefix}.step") if step is not None else None,
            log=bool(raw.get("log", False)),
        )
        if (
            result.minimum is not None
            and result.maximum is not None
            and result.minimum > result.maximum
        ):
            raise SpecError(f"{prefix}.minimum cannot exceed maximum")
        if result.step is not None and result.step <= 0:
            raise SpecError(f"{prefix}.step must be greater than zero")
        if result.log and result.step is not None:
            raise SpecError(f"{prefix}.step and log=true cannot be combined")
        if result.log and result.minimum is not None and result.minimum <= 0:
            raise SpecError(f"{prefix}.minimum must be positive when log=true")
        return result

    def validate_value(self, value: Any, label: str) -> float:
        numeric = _number(value, label)
        if self.minimum is not None and numeric < self.minimum:
            raise SpecError(f"{label}={numeric} is below minimum {self.minimum}")
        if self.maximum is not None and numeric > self.maximum:
            raise SpecError(f"{label}={numeric} exceeds maximum {self.maximum}")
        return numeric


@dataclass(frozen=True)
class ParameterConstraintSpec:
    """Safe linear combination constraint; no arbitrary expression evaluation."""

    name: str
    coefficients: dict[str, float]
    operator: str
    value: float

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any], index: int
    ) -> ParameterConstraintSpec:
        prefix = f"parameter_constraints[{index}]"
        coefficients_raw = _mapping(
            raw.get("coefficients"), f"{prefix}.coefficients"
        )
        if not coefficients_raw:
            raise SpecError(f"{prefix}.coefficients must not be empty")
        operator = _non_empty_string(raw.get("operator"), f"{prefix}.operator")
        if operator not in _OPERATORS:
            raise SpecError(f"{prefix}.operator is not supported")
        return cls(
            name=_non_empty_string(raw.get("name"), f"{prefix}.name"),
            coefficients={
                _non_empty_string(name, f"{prefix}.coefficient name"): _number(
                    coefficient, f"{prefix}.coefficients.{name}"
                )
                for name, coefficient in coefficients_raw.items()
            },
            operator=operator,
            value=_number(raw.get("value"), f"{prefix}.value"),
        )


@dataclass(frozen=True)
class ReportSpec:
    name: str
    kind: str
    locations: tuple[str, ...]
    report_type: str | None = None
    field_name: str | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> ReportSpec:
        prefix = f"reports[{index}]"
        kind = _non_empty_string(raw.get("kind"), f"{prefix}.kind").lower()
        if kind not in {"surface", "flux"}:
            raise SpecError(f"{prefix}.kind must be 'surface' or 'flux'")
        locations_raw = raw.get("locations")
        if not isinstance(locations_raw, list) or not locations_raw:
            raise SpecError(f"{prefix}.locations must be a non-empty list")
        locations = tuple(
            _non_empty_string(item, f"{prefix}.locations") for item in locations_raw
        )
        report_type = raw.get("report_type")
        # ``field`` is the public JSON key; ``field_name`` keeps dataclass
        # round-trips compatible with ``ExperimentSpec.to_dict()``.
        field_name = raw.get("field", raw.get("field_name"))
        if kind == "surface" and (report_type is None or field_name is None):
            raise SpecError(f"{prefix} surface reports require report_type and field")
        return cls(
            name=_non_empty_string(raw.get("name"), f"{prefix}.name"),
            kind=kind,
            locations=locations,
            report_type=(
                _non_empty_string(report_type, f"{prefix}.report_type")
                if report_type is not None
                else None
            ),
            field_name=(
                _non_empty_string(field_name, f"{prefix}.field")
                if field_name is not None
                else None
            ),
        )


@dataclass(frozen=True)
class DesignPointSpec:
    name: str
    values: dict[str, float]


@dataclass(frozen=True)
class ObjectiveSpec:
    report: str
    direction: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ObjectiveSpec:
        direction = _non_empty_string(
            raw.get("direction"), "objective.direction"
        ).lower()
        if direction not in {"minimize", "maximize"}:
            raise SpecError("objective.direction must be minimize or maximize")
        return cls(
            report=_non_empty_string(raw.get("report"), "objective.report"),
            direction=direction,
        )


@dataclass(frozen=True)
class ConstraintSpec:
    report: str
    operator: str
    value: float

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> ConstraintSpec:
        operator = _non_empty_string(
            raw.get("operator"), f"constraints[{index}].operator"
        )
        if operator not in _OPERATORS:
            raise SpecError(f"constraints[{index}].operator is not supported")
        return cls(
            report=_non_empty_string(raw.get("report"), f"constraints[{index}].report"),
            operator=operator,
            value=_number(raw.get("value"), f"constraints[{index}].value"),
        )


@dataclass(frozen=True)
class SolverSpec:
    initialization: str = "hybrid"
    iterations: int = 100
    residual_thresholds: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SolverSpec:
        initialization = _non_empty_string(
            raw.get("initialization", "hybrid"), "solver.initialization"
        ).lower()
        if initialization not in {"hybrid", "standard", "none"}:
            raise SpecError("solver.initialization must be hybrid, standard, or none")
        iterations = int(raw.get("iterations", 100))
        if not 1 <= iterations <= 1_000_000:
            raise SpecError("solver.iterations must be between 1 and 1000000")
        thresholds_raw = _mapping(
            raw.get("residual_thresholds", {}), "solver.residual_thresholds"
        )
        thresholds = {
            _non_empty_string(name, "residual name"): _number(
                value, f"solver.residual_thresholds.{name}"
            )
            for name, value in thresholds_raw.items()
        }
        if any(value <= 0 for value in thresholds.values()):
            raise SpecError("residual thresholds must be greater than zero")
        return cls(
            initialization=initialization,
            iterations=iterations,
            residual_thresholds=thresholds,
        )


@dataclass(frozen=True)
class ConservationSpec:
    """One configurable mass, salt, or component conservation equation."""

    name: str
    input_reports: tuple[str, ...]
    output_reports: tuple[str, ...]
    relative_tolerance: float
    epsilon: float = 1e-12

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> ConservationSpec:
        prefix = f"optimization.conservation_checks[{index}]"
        inputs = raw.get("input_reports")
        outputs = raw.get("output_reports")
        if not isinstance(inputs, list) or not inputs:
            raise SpecError(f"{prefix}.input_reports must be a non-empty list")
        if not isinstance(outputs, list) or not outputs:
            raise SpecError(f"{prefix}.output_reports must be a non-empty list")
        tolerance = _number(
            raw.get("relative_tolerance"), f"{prefix}.relative_tolerance"
        )
        epsilon = _number(raw.get("epsilon", 1e-12), f"{prefix}.epsilon")
        if not 0 < tolerance < 1:
            raise SpecError(f"{prefix}.relative_tolerance must be between zero and one")
        if epsilon <= 0:
            raise SpecError(f"{prefix}.epsilon must be positive")
        return cls(
            name=_non_empty_string(raw.get("name"), f"{prefix}.name"),
            input_reports=tuple(
                _non_empty_string(value, f"{prefix}.input_reports")
                for value in inputs
            ),
            output_reports=tuple(
                _non_empty_string(value, f"{prefix}.output_reports")
                for value in outputs
            ),
            relative_tolerance=tolerance,
            epsilon=epsilon,
        )


@dataclass(frozen=True)
class NumericalMonitorSpec:
    report: str
    window: int = 10
    max_relative_slope: float = 1e-4
    max_relative_span: float = 1e-3

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], index: int) -> NumericalMonitorSpec:
        prefix = f"optimization.numerical_monitors[{index}]"
        window = int(raw.get("window", 10))
        slope = _number(
            raw.get("max_relative_slope", 1e-4), f"{prefix}.max_relative_slope"
        )
        span = _number(
            raw.get("max_relative_span", 1e-3), f"{prefix}.max_relative_span"
        )
        if window < 3:
            raise SpecError(f"{prefix}.window must be at least three")
        if slope < 0 or span < 0:
            raise SpecError(f"{prefix} thresholds cannot be negative")
        return cls(
            report=_non_empty_string(raw.get("report"), f"{prefix}.report"),
            window=window,
            max_relative_slope=slope,
            max_relative_span=span,
        )


@dataclass(frozen=True)
class OptimizationSpec:
    """Small, explicit Optuna contract for the Fluent walking skeleton."""

    study_name: str = "fluent_demo_v01"
    target_trials: int = 6
    sampler_seed: int = 42
    n_startup_trials: int = 2
    mass_flow_in_report: str = "mass-flow-in"
    mass_flow_out_report: str = "mass-flow-out"
    mass_balance_relative_tolerance: float = 0.001
    mass_balance_epsilon: float = 1e-12
    verify_invalid_parameter_gate: bool = True
    max_attempts_per_trial: int = 2
    heartbeat_seconds: float = 2.0
    trial_timeout_seconds: float = 3600.0
    conservation_checks: tuple[ConservationSpec, ...] = ()
    numerical_monitors: tuple[NumericalMonitorSpec, ...] = ()

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> OptimizationSpec:
        target_trials = int(raw.get("target_trials", cls.target_trials))
        startup_trials = int(raw.get("n_startup_trials", cls.n_startup_trials))
        tolerance = _number(
            raw.get(
                "mass_balance_relative_tolerance",
                cls.mass_balance_relative_tolerance,
            ),
            "optimization.mass_balance_relative_tolerance",
        )
        epsilon = _number(
            raw.get("mass_balance_epsilon", cls.mass_balance_epsilon),
            "optimization.mass_balance_epsilon",
        )
        if target_trials < 1:
            raise SpecError("optimization.target_trials must be at least one")
        if not 0 <= startup_trials <= target_trials:
            raise SpecError(
                "optimization.n_startup_trials must be between zero and target_trials"
            )
        if not 0 < tolerance < 1:
            raise SpecError(
                "optimization.mass_balance_relative_tolerance must be between zero and one"
            )
        if epsilon <= 0:
            raise SpecError("optimization.mass_balance_epsilon must be positive")
        max_attempts = int(
            raw.get("max_attempts_per_trial", cls.max_attempts_per_trial)
        )
        heartbeat = _number(
            raw.get("heartbeat_seconds", cls.heartbeat_seconds),
            "optimization.heartbeat_seconds",
        )
        trial_timeout = _number(
            raw.get("trial_timeout_seconds", cls.trial_timeout_seconds),
            "optimization.trial_timeout_seconds",
        )
        if not 1 <= max_attempts <= 10:
            raise SpecError(
                "optimization.max_attempts_per_trial must be between one and ten"
            )
        if heartbeat <= 0 or trial_timeout <= 0 or heartbeat >= trial_timeout:
            raise SpecError(
                "optimization heartbeat must be positive and shorter than Trial timeout"
            )
        checks_raw = raw.get("conservation_checks", [])
        if not isinstance(checks_raw, list):
            raise SpecError("optimization.conservation_checks must be a list")
        conservation_checks = tuple(
            ConservationSpec.from_dict(
                _mapping(value, f"optimization.conservation_checks[{index}]"), index
            )
            for index, value in enumerate(checks_raw)
        )
        check_names = [check.name for check in conservation_checks]
        if len(check_names) != len(set(check_names)):
            raise SpecError("optimization conservation check names must be unique")
        monitors_raw = raw.get("numerical_monitors", [])
        if not isinstance(monitors_raw, list):
            raise SpecError("optimization.numerical_monitors must be a list")
        numerical_monitors = tuple(
            NumericalMonitorSpec.from_dict(
                _mapping(value, f"optimization.numerical_monitors[{index}]"), index
            )
            for index, value in enumerate(monitors_raw)
        )
        return cls(
            study_name=_non_empty_string(
                raw.get("study_name", cls.study_name), "optimization.study_name"
            ),
            target_trials=target_trials,
            sampler_seed=int(raw.get("sampler_seed", cls.sampler_seed)),
            n_startup_trials=startup_trials,
            mass_flow_in_report=_non_empty_string(
                raw.get("mass_flow_in_report", cls.mass_flow_in_report),
                "optimization.mass_flow_in_report",
            ),
            mass_flow_out_report=_non_empty_string(
                raw.get("mass_flow_out_report", cls.mass_flow_out_report),
                "optimization.mass_flow_out_report",
            ),
            mass_balance_relative_tolerance=tolerance,
            mass_balance_epsilon=epsilon,
            verify_invalid_parameter_gate=bool(
                raw.get("verify_invalid_parameter_gate", True)
            ),
            max_attempts_per_trial=max_attempts,
            heartbeat_seconds=heartbeat,
            trial_timeout_seconds=trial_timeout,
            conservation_checks=conservation_checks,
            numerical_monitors=numerical_monitors,
        )


@dataclass(frozen=True)
class ExperimentSpec:
    schema_version: int
    task_name: str
    connection: ConnectionSpec
    solver: SolverSpec
    parameters: tuple[ParameterSpec, ...]
    reports: tuple[ReportSpec, ...]
    design_points: tuple[DesignPointSpec, ...]
    objective: ObjectiveSpec
    parameter_constraints: tuple[ParameterConstraintSpec, ...] = ()
    constraints: tuple[ConstraintSpec, ...] = ()
    optimization: OptimizationSpec | None = None

    @classmethod
    def from_dict(cls, raw_value: Mapping[str, Any]) -> ExperimentSpec:
        raw = _mapping(raw_value, "experiment spec")
        schema_version = int(raw.get("schema_version", 1))
        if schema_version != 1:
            raise SpecError(
                f"unsupported Fluent experiment schema version: {schema_version}"
            )
        parameters_raw = raw.get("parameters")
        reports_raw = raw.get("reports")
        points_raw = raw.get("design_points")
        if not isinstance(parameters_raw, list) or not parameters_raw:
            raise SpecError("parameters must be a non-empty list")
        if not isinstance(reports_raw, list) or not reports_raw:
            raise SpecError("reports must be a non-empty list")
        if not isinstance(points_raw, list) or not points_raw:
            raise SpecError("design_points must be a non-empty list")

        parameters = tuple(
            ParameterSpec.from_dict(_mapping(item, f"parameters[{index}]"), index)
            for index, item in enumerate(parameters_raw)
        )
        parameter_by_name = {parameter.name: parameter for parameter in parameters}
        if len(parameter_by_name) != len(parameters):
            raise SpecError("parameter names must be unique")
        parameter_constraints_raw = raw.get("parameter_constraints", [])
        if not isinstance(parameter_constraints_raw, list):
            raise SpecError("parameter_constraints must be a list")
        parameter_constraints = tuple(
            ParameterConstraintSpec.from_dict(
                _mapping(item, f"parameter_constraints[{index}]"), index
            )
            for index, item in enumerate(parameter_constraints_raw)
        )
        for combination in parameter_constraints:
            unknown = sorted(set(combination.coefficients) - set(parameter_by_name))
            if unknown:
                raise SpecError(
                    f"parameter constraint {combination.name} references unknown "
                    f"parameters: {', '.join(unknown)}"
                )

        reports = tuple(
            ReportSpec.from_dict(_mapping(item, f"reports[{index}]"), index)
            for index, item in enumerate(reports_raw)
        )
        report_names = {report.name for report in reports}
        if len(report_names) != len(reports):
            raise SpecError("report names must be unique")

        design_points: list[DesignPointSpec] = []
        point_names: set[str] = set()
        for index, item in enumerate(points_raw):
            point_raw = _mapping(item, f"design_points[{index}]")
            name = _non_empty_string(
                point_raw.get("name"), f"design_points[{index}].name"
            )
            if name in point_names:
                raise SpecError("design point names must be unique")
            point_names.add(name)
            values_raw = _mapping(
                point_raw.get("values"), f"design_points[{index}].values"
            )
            missing = sorted(set(parameter_by_name) - set(values_raw))
            extra = sorted(set(values_raw) - set(parameter_by_name))
            if missing or extra:
                raise SpecError(
                    f"design point {name} parameter mismatch; missing={missing}, extra={extra}"
                )
            values = {
                parameter_name: parameter_by_name[parameter_name].validate_value(
                    value, f"design_points[{index}].values.{parameter_name}"
                )
                for parameter_name, value in values_raw.items()
            }
            design_points.append(DesignPointSpec(name=name, values=values))

        objective = ObjectiveSpec.from_dict(_mapping(raw.get("objective"), "objective"))
        if objective.report not in report_names:
            raise SpecError(f"objective report does not exist: {objective.report}")
        constraints_raw = raw.get("constraints", [])
        if not isinstance(constraints_raw, list):
            raise SpecError("constraints must be a list")
        constraints = tuple(
            ConstraintSpec.from_dict(_mapping(item, f"constraints[{index}]"), index)
            for index, item in enumerate(constraints_raw)
        )
        for constraint in constraints:
            if constraint.report not in report_names:
                raise SpecError(
                    f"constraint report does not exist: {constraint.report}"
                )

        solver = SolverSpec.from_dict(_mapping(raw.get("solver", {}), "solver"))

        optimization_raw = raw.get("optimization")
        optimization = (
            OptimizationSpec.from_dict(_mapping(optimization_raw, "optimization"))
            if optimization_raw is not None
            else None
        )
        if optimization is not None:
            for parameter in parameters:
                if parameter.minimum is None or parameter.maximum is None:
                    raise SpecError(
                        f"optimized parameter {parameter.name} requires minimum and maximum"
                    )
                if parameter.minimum == parameter.maximum:
                    raise SpecError(
                        f"optimized parameter {parameter.name} requires a non-zero range"
                    )
            reports_by_name = {report.name: report for report in reports}
            for label, report_name in (
                ("mass_flow_in_report", optimization.mass_flow_in_report),
                ("mass_flow_out_report", optimization.mass_flow_out_report),
            ):
                report = reports_by_name.get(report_name)
                if report is None:
                    raise SpecError(
                        f"optimization.{label} does not exist: {report_name}"
                    )
                if report.kind != "flux":
                    raise SpecError(
                        f"optimization.{label} must reference a flux report"
                    )
            for check in optimization.conservation_checks:
                for report_name in (*check.input_reports, *check.output_reports):
                    if report_name not in reports_by_name:
                        raise SpecError(
                            f"conservation check {check.name} references unknown "
                            f"report: {report_name}"
                        )
            for monitor in optimization.numerical_monitors:
                if monitor.report not in reports_by_name:
                    raise SpecError(
                        "optimization numerical monitor references unknown report: "
                        f"{monitor.report}"
                    )
                if monitor.window > solver.iterations:
                    raise SpecError(
                        f"numerical monitor window {monitor.window} exceeds "
                        f"solver iterations {solver.iterations}"
                    )

        return cls(
            schema_version=schema_version,
            task_name=_non_empty_string(raw.get("task_name"), "task_name"),
            connection=ConnectionSpec.from_dict(
                _mapping(raw.get("connection", {}), "connection")
            ),
            solver=solver,
            parameters=parameters,
            reports=reports,
            design_points=tuple(design_points),
            objective=objective,
            parameter_constraints=parameter_constraints,
            constraints=constraints,
            optimization=optimization,
        )

    def to_dict(self) -> dict[str, Any]:
        # ``asdict`` preserves tuples, while specs cross JSON/MCP boundaries and
        # need a canonical JSON-native representation.
        return json.loads(json.dumps(asdict(self), ensure_ascii=False))


def load_experiment_spec(path: str | os.PathLike[str]) -> ExperimentSpec:
    """Load a JSON or YAML Fluent experiment specification."""

    spec_path = Path(path)
    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpecError(f"cannot read Fluent experiment spec: {spec_path}") from exc
    suffix = spec_path.suffix.lower()
    try:
        if suffix in {".yaml", ".yml"}:
            try:
                import yaml
            except ImportError as exc:
                raise SpecError("PyYAML is required to load YAML Fluent specs") from exc
            raw = yaml.safe_load(text)
        else:
            raw = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SpecError(f"invalid Fluent experiment spec: {exc}") from exc
    return ExperimentSpec.from_dict(_mapping(raw, "experiment spec"))
