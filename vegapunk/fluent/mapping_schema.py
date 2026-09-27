"""Typed classification, mapping and resolved-task contracts."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .mapping_contract import MAPPING_DSL_SCHEMA_VERSION, MAPPING_VERSION, MappingOperator
from .task_schema import SimulationTaskObject, StrictModel, VariableIntent


class VariableClassification(StrEnum):
    DIRECT = "DIRECT"
    MAPPED_PROXY = "MAPPED_PROXY"
    GEOMETRY_UNSUPPORTED = "GEOMETRY_UNSUPPORTED"


class VerificationRoute(StrEnum):
    NONE = "NONE"
    MANUAL_GEOMETRY = "MANUAL_GEOMETRY"
    FUTURE_WORKBENCH_MCP = "FUTURE_WORKBENCH_MCP"


class VariableBinding(StrictModel):
    variable_id: str = Field(min_length=1, max_length=120)
    classification: VariableClassification
    classification_reason: str = Field(min_length=1, max_length=2000)
    selected_capability_id: str | None = Field(default=None, max_length=200)
    candidate_parameter_ids: list[str] = Field(default_factory=list, max_length=10)
    candidate_metric_ids: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)
    missing_information: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("confidence", mode="before")
    @classmethod
    def finite_confidence(cls, value):
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("confidence must be finite")
        return value


class ClassificationEnvelope(StrictModel):
    bindings: list[VariableBinding] = Field(min_length=1, max_length=5)


class MappingExpression(StrictModel):
    op: MappingOperator | None = None
    args: list["MappingExpression"] = Field(default_factory=list, max_length=20)
    ref: str | None = Field(default=None, max_length=200)
    value: float | None = None
    minimum: float | None = None
    maximum: float | None = None

    @field_validator("value", "minimum", "maximum", mode="before")
    @classmethod
    def finite_numbers(cls, value):
        if value is not None and (isinstance(value, bool) or not math.isfinite(float(value))):
            raise ValueError("DSL constants must be finite and not bool")
        return value

    @model_validator(mode="after")
    def exactly_one_form(self):
        forms = int(self.ref is not None) + int(self.value is not None) + int(self.op is not None)
        if forms != 1:
            raise ValueError("mapping expression requires exactly one of ref, value, or op")
        if self.ref is not None and self.args:
            raise ValueError("reference expressions cannot have args")
        if self.value is not None and self.args:
            raise ValueError("constant expressions cannot have args")
        return self


class MappingSpec(StrictModel):
    mapping_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")
    schema_version: Literal[1] = MAPPING_DSL_SCHEMA_VERSION
    mapping_version: str = Field(default=MAPPING_VERSION, min_length=1, max_length=40)
    source_variables: list[str] = Field(min_length=1, max_length=5)
    context_variables: list[str] = Field(default_factory=list, max_length=10)
    proxy_variables: list[str] = Field(min_length=1, max_length=10)
    selected_capability_id: str = Field(min_length=1, max_length=200)
    selected_fluent_parameter_ids: list[str] = Field(min_length=1, max_length=10)
    forward_expression: dict[str, MappingExpression]
    reverse_expression: dict[str, MappingExpression]
    source_units: dict[str, str]
    context_values: dict[str, float] = Field(default_factory=dict)
    output_units: dict[str, str]
    coordinate_convention: str = Field(min_length=1, max_length=1000)
    angle_convention: str = Field(min_length=1, max_length=1000)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    validity_conditions: list[str] = Field(default_factory=list, max_length=30)
    verification_route: VerificationRoute
    confidence: float = Field(ge=0, le=1)
    automatic_geometry_execution: Literal[False] = False

    @field_validator("confidence", mode="before")
    @classmethod
    def finite_confidence(cls, value):
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("confidence must be finite")
        return value

    @field_validator("context_values", mode="before")
    @classmethod
    def finite_context(cls, value):
        for name, number in value.items():
            if isinstance(number, bool) or not math.isfinite(float(number)):
                raise ValueError(f"context value {name} must be finite and not bool")
        return value


class MappingEnvelope(StrictModel):
    mappings: list[MappingSpec] = Field(default_factory=list, max_length=5)


class ResolvedVariable(StrictModel):
    intent: VariableIntent
    binding: VariableBinding
    mapping_id: str | None = None
    executable: bool


class ResolvedTaskObject(StrictModel):
    schema_version: Literal[3] = 3
    task: SimulationTaskObject
    variables: list[ResolvedVariable]
    mappings: list[MappingSpec] = Field(default_factory=list)
    executable: bool
    geometry_unsupported: list[str] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    capability_registry_version: str
    mapping_dsl_schema_version: Literal[1] = MAPPING_DSL_SCHEMA_VERSION
    automatic_geometry_execution: Literal[False] = False


class FutureGeometryHandoffRequest(StrictModel):
    delta: float = Field(default=1.0, gt=0)

    @field_validator("delta", mode="before")
    @classmethod
    def finite_delta(cls, value):
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("geometry handoff delta must be finite and not bool")
        return value


class GeometryRecommendation(StrictModel):
    result_class: Literal["geometry_recommendation"] = "geometry_recommendation"
    source_campaign_id: str = Field(min_length=1, max_length=240)
    source_trial_id: str = Field(min_length=1, max_length=240)
    capability_id: str = Field(min_length=1, max_length=200)
    mapping_id: str = Field(min_length=1, max_length=160)
    mapping_version: str = Field(min_length=1, max_length=40)
    proxy_variable: str = Field(min_length=1, max_length=160)
    optimal_proxy_value: float
    recommended_geometry_parameter: str = Field(min_length=1, max_length=160)
    recommended_geometry_value: float
    unit: str = Field(min_length=1, max_length=80)
    reference_conditions: dict[str, float]
    assumptions: list[str] = Field(max_length=30)
    confidence: float = Field(ge=0, le=1)
    verification_required: Literal[True] = True
    verification_route: VerificationRoute
    automatic_execution: Literal[False] = False
    proxy_result_notice: str = "当前结果来自固定几何下的代理优化，尚未通过真实几何重建和重网格验证。"
    future_handoff_request: FutureGeometryHandoffRequest

    @field_validator(
        "optimal_proxy_value",
        "recommended_geometry_value",
        "confidence",
        mode="before",
    )
    @classmethod
    def finite_numbers(cls, value):
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("geometry recommendation values must be finite")
        return value

    @field_validator("reference_conditions", mode="before")
    @classmethod
    def finite_reference_conditions(cls, value):
        if not isinstance(value, dict):
            raise ValueError("reference_conditions must be an object")
        for name, number in value.items():
            if not str(name).strip() or isinstance(number, bool) or not math.isfinite(float(number)):
                raise ValueError("reference conditions must have names and finite numeric values")
        return value


class GeometryRecommendationEnvelope(StrictModel):
    message: str = Field(min_length=1, max_length=4000)
    recommendation: GeometryRecommendation


MappingExpression.model_rebuild()
