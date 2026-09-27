"""Strict business contracts for the Fluent planning graph."""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TASK_SCHEMA_VERSION = 3
MAX_VARIABLES = 5
MAX_TRIALS = 1000
MAX_ITERATIONS = 1_000_000
MAX_WALL_TIME_SECONDS = 7 * 24 * 3600
MAX_FAILED_TRIALS = 1000


def _finite(value: float | int | None, label: str) -> float | int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number and not bool")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class VariableIntent(StrictModel):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    semantic_key: str = Field(min_length=1, max_length=160)
    minimum: float | None = None
    maximum: float | None = None
    initial_value: float | None = None
    unit: str | None = Field(default=None, max_length=80)
    description: str = Field(default="", max_length=1000)

    @field_validator("minimum", "maximum", "initial_value", mode="before")
    @classmethod
    def finite_values(cls, value, info):
        return _finite(value, info.field_name)

    @model_validator(mode="after")
    def valid_range(self):
        if self.minimum is not None and self.maximum is not None and self.minimum >= self.maximum:
            raise ValueError("variable maximum must be greater than minimum")
        if self.initial_value is not None and self.minimum is not None and self.initial_value < self.minimum:
            raise ValueError("initial_value must not be below minimum")
        if self.initial_value is not None and self.maximum is not None and self.initial_value > self.maximum:
            raise ValueError("initial_value must not exceed maximum")
        return self


class ObjectiveIntent(StrictModel):
    semantic_metric: str = Field(min_length=1, max_length=200)
    metric_key: str | None = Field(default=None, max_length=200)
    direction: Literal["minimize", "maximize"]
    aggregation: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=240)
    unit: str | None = Field(default=None, max_length=80)


class ConstraintIntent(StrictModel):
    semantic_metric: str = Field(min_length=1, max_length=200)
    metric_key: str | None = Field(default=None, max_length=200)
    operator: Literal["<", "<=", ">", ">=", "=="]
    value: float
    unit: str | None = Field(default=None, max_length=80)

    @field_validator("value", mode="before")
    @classmethod
    def finite_value(cls, value):
        return float(_finite(value, "constraint value"))


class FixedCondition(StrictModel):
    id: str = Field(min_length=1, max_length=120, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=200)
    value: float
    unit: str = Field(min_length=1, max_length=80)
    source: Literal["user", "case_scan", "derived"]
    parameter_id: str | None = Field(default=None, max_length=200)
    report_id: str | None = Field(default=None, max_length=200)
    evidence_key: str | None = Field(default=None, max_length=200)
    verification: Literal["case_parameter", "run_readback", "documented_assumption"]

    @field_validator("value", mode="before")
    @classmethod
    def finite_value(cls, value):
        return float(_finite(value, "fixed condition value"))

    @model_validator(mode="after")
    def parameter_backed_verification(self):
        if self.verification == "case_parameter" and not self.parameter_id:
            raise ValueError("case_parameter fixed conditions require parameter_id")
        bindings = [self.parameter_id, self.report_id, self.evidence_key]
        if self.verification == "run_readback" and not any(bindings):
            raise ValueError("run_readback fixed conditions require parameter_id, report_id, or evidence_key")
        if sum(value is not None for value in bindings) > 1:
            raise ValueError("fixed conditions cannot use more than one readback binding")
        return self


class ThermalGuardRequirements(StrictModel):
    data_file: Literal['paired-case-data'] = Field(description='Safe artifact reference: data file paired with the selected .cas.h5; never emit a filesystem path.')
    data_sha256: str
    heat_source_W: float
    heat_net_report: str
    mass_in_report: str
    mass_out_report: str
    temperature_reports: list[str]
    window: int
    temperature_span_K: float
    heat_relative_tolerance: float
    mass_relative_tolerance: float
    pseudo_courant: float


class SolverRequirements(StrictModel):
    thermal_guard: ThermalGuardRequirements | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator('thermal_guard')
    @classmethod
    def validate_thermal_guard(cls, value):
        from .thermal_guard import validate_thermal_guard
        if value is not None:
            raw = value.model_dump()
            raw['data_file'] = 'paired.dat.h5'
            validate_thermal_guard(raw)
        return value

    iterations: int = Field(default=100, ge=1, le=MAX_ITERATIONS)
    initialization: Literal["hybrid", "standard", "none"] = "hybrid"
    residual_thresholds: dict[str, float] = Field(default_factory=dict)
    mass_balance_required: bool = True
    mass_balance_relative_tolerance: float = Field(default=0.001, gt=0, lt=1)
    verification_relative_tolerance: float = Field(default=0.002, ge=0, lt=1)
    verification_absolute_tolerance: float = Field(default=1e-6, gt=0)
    verification_tolerance_mode: Literal["auto", "absolute", "reference_scale"] = "auto"
    verification_reference_scale: float | None = Field(default=None, gt=0)
    verification_policy_basis: str = Field(default="Configured repeatability tolerance; not proof of improvement", min_length=1)

    @field_validator(
        "mass_balance_relative_tolerance",
        "verification_relative_tolerance",
        "verification_absolute_tolerance",
        "verification_reference_scale",
        mode="before",
    )
    @classmethod
    def finite_tolerances(cls, value, info):
        return None if value is None else float(_finite(value, info.field_name))

    @model_validator(mode="after")
    def explicit_verification_scale(self):
        if self.verification_tolerance_mode == "reference_scale" and self.verification_reference_scale is None:
            raise ValueError("reference_scale verification requires an explicit positive scale")
        if not self.verification_policy_basis.strip():
            raise ValueError("verification policy basis must not be blank")
        return self

    @field_validator("iterations", mode="before")
    @classmethod
    def iterations_not_bool(cls, value):
        if isinstance(value, bool):
            raise ValueError("iterations must not be bool")
        return value

    @field_validator("residual_thresholds")
    @classmethod
    def finite_thresholds(cls, value):
        clean: dict[str, float] = {}
        for name, threshold in value.items():
            if not str(name).strip() or isinstance(threshold, bool):
                raise ValueError("residual threshold names and values must be valid")
            number = float(_finite(threshold, f"residual {name}"))
            if number <= 0:
                raise ValueError("residual thresholds must be positive")
            clean[str(name)] = number
        return clean


class TerminationCondition(StrictModel):
    max_trials: int = Field(default=20, ge=1, le=MAX_TRIALS)
    max_wall_time_seconds: float = Field(default=3600, gt=0, le=MAX_WALL_TIME_SECONDS)
    no_improvement_trials: int | None = Field(default=None, ge=1, le=MAX_TRIALS)
    target_objective: float | None = None
    maximum_failed_trials: int = Field(default=10, ge=0, le=MAX_FAILED_TRIALS)

    @field_validator(
        "max_trials",
        "max_wall_time_seconds",
        "no_improvement_trials",
        "target_objective",
        "maximum_failed_trials",
        mode="before",
    )
    @classmethod
    def finite_and_not_bool(cls, value, info):
        return _finite(value, info.field_name)

    @model_validator(mode="after")
    def bounded_patience(self):
        if self.no_improvement_trials is not None and self.no_improvement_trials > self.max_trials:
            raise ValueError("no_improvement_trials cannot exceed max_trials")
        return self


class SimulationTaskObject(StrictModel):
    schema_version: Literal[3] = TASK_SCHEMA_VERSION
    research_question: str = Field(min_length=1, max_length=4000)
    variables: list[VariableIntent] = Field(min_length=1, max_length=MAX_VARIABLES)
    objective: ObjectiveIntent
    constraints: list[ConstraintIntent] = Field(default_factory=list, max_length=50)
    fixed_conditions: list[FixedCondition] = Field(default_factory=list, max_length=30)
    solver_requirements: SolverRequirements = Field(default_factory=SolverRequirements)
    termination: TerminationCondition = Field(default_factory=TerminationCondition)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    questions: list[str] = Field(default_factory=list, max_length=20)
    needs_information: bool = False

    @model_validator(mode="after")
    def unique_variables(self):
        ids = [item.id for item in self.variables]
        if len(ids) != len(set(ids)):
            raise ValueError("variable ids must be unique")
        fixed_ids = [item.id for item in self.fixed_conditions]
        if len(fixed_ids) != len(set(fixed_ids)):
            raise ValueError("fixed condition ids must be unique")
        overlap = set(ids) & set(fixed_ids)
        if overlap:
            raise ValueError(f"fixed condition ids cannot overlap variables: {sorted(overlap)}")
        return self

    def information_gaps(self) -> list[str]:
        gaps = [question.strip() for question in self.questions if question.strip()]
        for variable in self.variables:
            if variable.minimum is None or variable.maximum is None:
                gaps.append(f"请提供变量“{variable.name}”的最小值和最大值。")
            if not variable.unit or not variable.unit.strip():
                gaps.append(f"请提供变量“{variable.name}”的单位。")
        if not self.objective.unit or not self.objective.unit.strip():
            gaps.append("请提供目标函数的单位。")
        for constraint in self.constraints:
            if not constraint.unit or not constraint.unit.strip():
                gaps.append(f"请提供约束“{constraint.semantic_metric}”的单位。")
        fixed_pattern = re.compile(
            r"(?:保持|固定|fixed|constant).{0,40}\d|\d.{0,40}(?:保持|固定|fixed|constant)",
            re.IGNORECASE,
        )
        if not self.fixed_conditions and any(
            fixed_pattern.search(segment)
            for segment in [self.research_question, *self.assumptions]
        ):
            gaps.append("检测到固定数值条件；请将其写入 fixed_conditions，并声明单位、来源和验证方式。")
        if any(item.semantic_key == "fan_installation_angle" for item in self.variables):
            text = " ".join([self.research_question, *self.assumptions]).lower()
            if "wind_direction" not in text and "风向" not in text:
                gaps.append("请提供 wind_direction（风向角）及其单位。")
            if not any("坐标" in item or "coordinate" in item.lower() for item in self.assumptions):
                gaps.append("请明确坐标系、角度零点和角度正方向。")
        return list(dict.fromkeys(gaps))


class TaskParseEnvelope(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    task: SimulationTaskObject


def schema_for(model: type[BaseModel]) -> dict:
    """JSON schema accepted by UnifiedModelRuntime.generate_json()."""
    return model.model_json_schema()
