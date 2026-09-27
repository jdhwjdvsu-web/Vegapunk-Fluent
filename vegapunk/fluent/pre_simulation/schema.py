"""Versioned, side-effect-free data contracts for Pre-Simulation Intelligence.

These types validate proposals. They do not inspect a Case, call an LLM,
resolve bounds, approve a task, or execute a solver.
"""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator


PRE_SIMULATION_SCHEMA_VERSION = 1


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class EvidenceStatus(StrEnum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    UNKNOWN = "UNKNOWN"


class VariableRoute(StrEnum):
    DIRECT = "DIRECT"
    MAPPED = "MAPPED"
    GEOMETRY = "GEOMETRY"


class VariableRole(StrEnum):
    DESIGN_VARIABLE = "DESIGN_VARIABLE"
    OPERATING_CONDITION = "OPERATING_CONDITION"
    FIXED_PARAMETER = "FIXED_PARAMETER"
    GEOMETRY_CANDIDATE = "GEOMETRY_CANDIDATE"


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number, not bool or text")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


class NumericRange(StrictContract):
    lower: float
    upper: float
    unit: str = Field(min_length=1, max_length=80)

    @field_validator("lower", "upper", mode="before")
    @classmethod
    def finite_bound(cls, value: object) -> float:
        return _number(value, "range bound")

    @model_validator(mode="after")
    def ordered(self) -> NumericRange:
        if self.lower > self.upper:
            raise ValueError("range lower must not exceed upper")
        return self

    def contains(self, other: NumericRange) -> bool:
        return self.unit == other.unit and self.lower <= other.lower and other.upper <= self.upper


class EvidenceRef(StrictContract):
    source_type: str = Field(min_length=1, max_length=80)
    source_id: str = Field(min_length=1, max_length=200)
    fact: str = Field(min_length=1, max_length=2000)
    profile_version: str | None = Field(default=None, min_length=1, max_length=100)
    verification: VerificationStatus


class UserIntent(StrictContract):
    schema_version: Literal[1] = PRE_SIMULATION_SCHEMA_VERSION
    intent_id: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=1000)
    hard_bounds: dict[str, NumericRange] = Field(default_factory=dict)
    requested_variables: list[str] = Field(default_factory=list)
    fixed_conditions: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    source_text: str = Field(min_length=1)


class FeatureObservation(StrictContract):
    value: str | float | bool | list[float] | None = None
    unit: str | None = Field(default=None, min_length=1, max_length=80)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    evidence_status: EvidenceStatus = EvidenceStatus.INSUFFICIENT
    missing_information: list[str] = Field(default_factory=list)
    verification: VerificationStatus

    @field_validator("value")
    @classmethod
    def finite_feature_number(cls, value: str | float | bool | list[float] | None):
        numbers = value if isinstance(value, list) else [value]
        if any(isinstance(item, float) and not math.isfinite(item) for item in numbers):
            raise ValueError("feature value must be finite")
        return value


class ModelProfile(StrictContract):
    schema_version: Literal[1] = PRE_SIMULATION_SCHEMA_VERSION
    case_fingerprint: str = Field(min_length=1, max_length=256)
    profile_version: str = Field(min_length=1, max_length=100)
    generated_at: datetime
    dependency_versions: dict[str, str] = Field(min_length=1)
    state_digest: dict[str, str] = Field(default_factory=dict)
    readbacks: dict[str, FeatureObservation] = Field(default_factory=dict)
    dimension: int | None = Field(default=None, ge=2, le=3)
    fluent_version: str | None = None
    product_version: str | None = None
    solver_type: str | None = None
    time_regime: str | None = None
    physical_models: dict[str, object] = Field(default_factory=dict)
    materials: list[dict] = Field(default_factory=list)
    fluid_zones: list[dict] = Field(default_factory=list)
    solid_zones: list[dict] = Field(default_factory=list)
    boundary_zones: list[dict] = Field(default_factory=list)
    heat_sources: list[dict] = Field(default_factory=list)
    parameters: list[dict] = Field(default_factory=list)
    mesh_summary: dict = Field(default_factory=dict)
    mesh_quality: dict = Field(default_factory=dict)
    residual_settings: dict = Field(default_factory=dict)
    reports: dict | list[dict] = Field(default_factory=list)
    solver_settings: dict = Field(default_factory=dict)
    unknown_fields: list[str] = Field(default_factory=list)
    unverified_fields: list[str] = Field(default_factory=list)
    verification: VerificationStatus

    @field_validator("generated_at")
    @classmethod
    def timestamp_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must include a timezone")
        return value

    @field_validator("dependency_versions", "state_digest")
    @classmethod
    def nonempty_version_entries(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() or not item.strip() for key, item in value.items()):
            raise ValueError("version and state digest entries must be nonempty")
        return value


class PhysicsFeatures(StrictContract):
    schema_version: Literal[1] = PRE_SIMULATION_SCHEMA_VERSION
    profile_version: str = Field(min_length=1, max_length=100)
    features: dict[str, FeatureObservation] = Field(default_factory=dict)


class CandidateVariable(StrictContract):
    variable: str = Field(min_length=1, max_length=120)
    unit: str = Field(min_length=1, max_length=80)
    user_rationale: str = Field(min_length=1, max_length=2000)
    route_candidate: VariableRoute
    role: VariableRole = VariableRole.DESIGN_VARIABLE


class VariableRouteBinding(StrictContract):
    variable: str = Field(min_length=1, max_length=120)
    route: VariableRoute
    parameter_ids: list[str] = Field(default_factory=list)
    mapping_id: str | None = Field(default=None, min_length=1, max_length=200)
    capability_status: VerificationStatus
    evidence: list[EvidenceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def route_shape(self) -> VariableRouteBinding:
        if self.route != VariableRoute.MAPPED and self.mapping_id is not None:
            raise ValueError("mapping_id is allowed only for MAPPED routes")
        if self.route == VariableRoute.MAPPED and self.capability_status == VerificationStatus.VERIFIED:
            if not self.mapping_id or not self.parameter_ids:
                raise ValueError("verified MAPPED routes require mapping_id and parameter_ids")
        if self.route == VariableRoute.DIRECT and self.capability_status == VerificationStatus.VERIFIED:
            if not self.parameter_ids:
                raise ValueError("verified DIRECT routes require parameter_ids")
        return self


class CapabilityDescriptor(StrictContract):
    capability_id: str = Field(min_length=1, max_length=200)
    route: VariableRoute
    semantic_keys: list[str] = Field(default_factory=list)
    parameter_ids: list[str] = Field(default_factory=list)
    bound: NumericRange | None = None
    executable: bool = False
    assumptions: list[str] = Field(default_factory=list)
    verification: VerificationStatus
    evidence: list[EvidenceRef] = Field(default_factory=list)


class CapabilityRegistrySnapshot(StrictContract):
    registry_version: str = Field(min_length=1, max_length=100)
    profile_version: str = Field(min_length=1, max_length=100)
    capabilities: list[CapabilityDescriptor] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_capabilities(self) -> CapabilityRegistrySnapshot:
        ids = [item.capability_id for item in self.capabilities]
        if len(set(ids)) != len(ids):
            raise ValueError("capability IDs must be unique")
        return self


class ParameterFeasibleRange(StrictContract):
    variable: str = Field(min_length=1, max_length=120)
    feasible_range: NumericRange
    constraint_sources: list[EvidenceRef] = Field(min_length=1)
    mapping_preimage_rule: str | None = Field(default=None, min_length=1, max_length=500)
    verification: VerificationStatus
    resolved_by: Literal["deterministic"] = "deterministic"


class PriorProposal(StrictContract):
    schema_version: Literal[1] = PRE_SIMULATION_SCHEMA_VERSION
    variable: str = Field(min_length=1, max_length=120)
    route: VariableRoute
    current_value: float | None
    unit: str = Field(min_length=1, max_length=80)
    feasible_range: NumericRange  # Copy of resolver output, checked again in OptimizationPrior.
    recommended_range: NumericRange
    high_potential_range: NumericRange | None
    confidence: float = Field(ge=0, le=1)
    evidence_status: EvidenceStatus
    evidence: list[EvidenceRef]
    assumptions: list[str]
    missing_information: list[str]
    reasoning_summary: str = Field(min_length=1, max_length=4000)
    profile_version: str = Field(min_length=1, max_length=100)

    @field_validator("confidence", mode="before")
    @classmethod
    def finite_confidence(cls, value: object) -> float:
        return _number(value, "confidence")

    @field_validator("current_value", mode="before")
    @classmethod
    def finite_current_value(cls, value: object) -> float | None:
        return None if value is None else _number(value, "current_value")

    @model_validator(mode="after")
    def validate_ranges_and_evidence(self) -> PriorProposal:
        if self.unit != self.feasible_range.unit:
            raise ValueError("proposal unit must match feasible_range unit")
        if not self.feasible_range.contains(self.recommended_range):
            raise ValueError("recommended_range must be within feasible_range with the same unit")
        if self.high_potential_range is not None and not self.recommended_range.contains(self.high_potential_range):
            raise ValueError("high_potential_range must be within recommended_range with the same unit")
        if self.evidence_status == EvidenceStatus.INSUFFICIENT:
            if self.high_potential_range is not None:
                raise ValueError("INSUFFICIENT evidence requires high_potential_range=null")
            if self.recommended_range != self.feasible_range:
                raise ValueError("INSUFFICIENT evidence cannot narrow recommended_range")
            if not self.missing_information:
                raise ValueError("INSUFFICIENT evidence requires missing_information")
        if self.evidence_status == EvidenceStatus.SUPPORTED and not self.evidence:
            raise ValueError("SUPPORTED evidence requires at least one evidence reference")
        if any(item.profile_version not in (None, self.profile_version) for item in self.evidence):
            raise ValueError("proposal evidence profile_version does not match proposal")
        return self


class IterationPolicy(StrictContract):
    min_iterations: int = Field(strict=True, ge=1, validation_alias=AliasChoices("min_iterations", "minimum"))
    initial_budget: int = Field(strict=True, ge=1)
    hard_limit: int = Field(strict=True, ge=1, validation_alias=AliasChoices("hard_limit", "maximum"))
    check_interval: int = Field(strict=True, ge=1)
    early_stop_enabled: bool
    convergence_window: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode="before")
    @classmethod
    def legacy_budget_alias(cls, value):
        if isinstance(value, dict) and "initial_budget" not in value:
            value = {**value, "initial_budget": value.get("hard_limit", value.get("maximum"))}
        return value

    @property
    def minimum(self) -> int:
        return self.min_iterations

    @property
    def maximum(self) -> int:
        return self.hard_limit

    @model_validator(mode="after")
    def valid_budget(self) -> IterationPolicy:
        if self.min_iterations > self.initial_budget or self.initial_budget > self.hard_limit:
            raise ValueError("iteration minimum must not exceed maximum")
        if self.check_interval > self.hard_limit:
            raise ValueError("check_interval must not exceed maximum")
        if self.early_stop_enabled and self.convergence_window is None:
            raise ValueError("early stop requires convergence_window")
        if self.convergence_window is not None and self.convergence_window > self.hard_limit:
            raise ValueError("convergence_window must not exceed maximum")
        return self


class ConvergencePolicy(StrictContract):
    monitor_ids: list[str] = Field(default_factory=list)
    residual_thresholds: dict[str, float] = Field(default_factory=dict)

    @field_validator("residual_thresholds", mode="before")
    @classmethod
    def positive_thresholds(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        for name, threshold in value.items():
            if not isinstance(name, str) or not name.strip() or _number(threshold, "residual threshold") <= 0:
                raise ValueError("residual thresholds require names and positive finite numbers")
        return value


class SolverPreset(StrictContract):
    schema_version: Literal[1] = PRE_SIMULATION_SCHEMA_VERSION
    preset_id: str = Field(min_length=1, max_length=120)
    profile_version: str = Field(min_length=1, max_length=100)
    iteration_policy: IterationPolicy
    convergence_policy: ConvergencePolicy
    confidence: float = Field(ge=0, le=1)
    preset_basis: list[EvidenceRef] = Field(min_length=1)
    assumptions: list[str]
    evidence_status: EvidenceStatus
    verification: VerificationStatus

    @field_validator("confidence", mode="before")
    @classmethod
    def finite_confidence(cls, value: object) -> float:
        return _number(value, "confidence")

    @model_validator(mode="after")
    def early_stop_has_monitor(self) -> SolverPreset:
        if self.iteration_policy.early_stop_enabled and not (
            self.convergence_policy.monitor_ids or self.convergence_policy.residual_thresholds
        ):
            raise ValueError("early stop requires a convergence monitor or residual threshold")
        if any(item.profile_version not in (None, self.profile_version) for item in self.preset_basis):
            raise ValueError("preset_basis profile_version does not match preset")
        return self


class OptimizationPrior(StrictContract):
    schema_version: Literal[1] = PRE_SIMULATION_SCHEMA_VERSION
    prior_id: str = Field(min_length=1, max_length=120)
    profile_version: str = Field(min_length=1, max_length=100)
    feasible_ranges: list[ParameterFeasibleRange] = Field(min_length=1)
    proposals: list[PriorProposal] = Field(min_length=1)
    fusion_policy_version: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def authoritative_ranges_and_versions(self) -> OptimizationPrior:
        ranges = {entry.variable: entry.feasible_range for entry in self.feasible_ranges}
        if len(ranges) != len(self.feasible_ranges):
            raise ValueError("feasible_ranges must have unique variables")
        variables = [proposal.variable for proposal in self.proposals]
        if len(set(variables)) != len(variables) or set(variables) != set(ranges):
            raise ValueError("proposals must match feasible_ranges one-to-one")
        for proposal in self.proposals:
            if proposal.profile_version != self.profile_version:
                raise ValueError("proposal profile_version does not match prior")
            if proposal.feasible_range != ranges[proposal.variable]:
                raise ValueError("proposal feasible_range differs from deterministic range")
        return self


class PriorValidation(StrictContract):
    valid: bool
    errors: list[str]
    warnings: list[str]
    profile_version: str = Field(min_length=1, max_length=100)
    validator_version: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def result_consistent(self) -> PriorValidation:
        if self.valid and self.errors:
            raise ValueError("valid prior validation cannot contain errors")
        if not self.valid and not self.errors:
            raise ValueError("invalid prior validation requires errors")
        return self


class OptimizationTask(StrictContract):
    schema_version: Literal[1] = PRE_SIMULATION_SCHEMA_VERSION
    task_id: str = Field(min_length=1, max_length=120)
    approval_id: str = Field(min_length=1, max_length=120)
    case_fingerprint: str = Field(min_length=1, max_length=256)
    profile_version: str = Field(min_length=1, max_length=100)
    prior: OptimizationPrior
    solver_preset: SolverPreset
    prior_validation: PriorValidation
    runner_contract_version: str = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def approved_contract(self) -> OptimizationTask:
        if not self.prior_validation.valid:
            raise ValueError("OptimizationTask requires successful PriorValidation")
        if self.prior.profile_version != self.profile_version:
            raise ValueError("prior profile_version does not match task")
        if self.solver_preset.profile_version != self.profile_version:
            raise ValueError("solver preset profile_version does not match task")
        if self.prior_validation.profile_version != self.profile_version:
            raise ValueError("validation profile_version does not match task")
        return self


class ProviderPrior(StrictContract):
    """Future provider output; unverified sources cannot carry range suggestions."""

    provider_id: str = Field(min_length=1, max_length=120)
    source_kind: Literal["knowledge", "historical", "surrogate"]
    variable: str = Field(min_length=1, max_length=120)
    profile_version: str = Field(min_length=1, max_length=100)
    verification: VerificationStatus
    recommended_range: NumericRange | None = None
    high_potential_range: NumericRange | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def no_unverified_range(self) -> ProviderPrior:
        if self.verification != VerificationStatus.VERIFIED and (
            self.recommended_range is not None or self.high_potential_range is not None
        ):
            raise ValueError("unverified provider cannot suggest parameter ranges")
        if self.high_potential_range is not None and (
            self.recommended_range is None or not self.recommended_range.contains(self.high_potential_range)
        ):
            raise ValueError("provider high_potential_range must be within recommended_range")
        if self.recommended_range is not None and (
            not self.evidence or any(item.verification != VerificationStatus.VERIFIED for item in self.evidence)
        ):
            raise ValueError("provider range suggestions require verified evidence")
        return self


class PriorReasoningRequest(StrictContract):
    schema_version: Literal[1] = PRE_SIMULATION_SCHEMA_VERSION
    user_intent: UserIntent
    model_profile: ModelProfile
    physics_features: PhysicsFeatures
    candidate_variables: list[CandidateVariable] = Field(min_length=1)
    variable_routes: list[VariableRouteBinding] = Field(min_length=1)
    capability_registry: CapabilityRegistrySnapshot
    feasible_ranges: list[ParameterFeasibleRange] = Field(min_length=1)
    knowledge_prior: list[ProviderPrior] | None = None
    historical_prior: list[ProviderPrior] | None = None
    surrogate_prior: list[ProviderPrior] | None = None

    @model_validator(mode="after")
    def shared_profile_and_variables(self) -> PriorReasoningRequest:
        version = self.model_profile.profile_version
        if self.physics_features.profile_version != version:
            raise ValueError("physics_features must use current ModelProfile version")
        if self.capability_registry.profile_version != version:
            raise ValueError("capability_registry must use current ModelProfile version")
        candidate_ids = [item.variable for item in self.candidate_variables]
        route_ids = [item.variable for item in self.variable_routes]
        range_ids = [item.variable for item in self.feasible_ranges]
        if any(len(set(ids)) != len(ids) for ids in (candidate_ids, route_ids, range_ids)):
            raise ValueError("candidate, route, and feasible variables must be unique")
        if set(candidate_ids) != set(route_ids) or set(candidate_ids) != set(range_ids):
            raise ValueError("candidate, route, and feasible variables must match")
        for candidate in self.candidate_variables:
            actual_range = next(item.feasible_range for item in self.feasible_ranges if item.variable == candidate.variable)
            if candidate.unit != actual_range.unit:
                raise ValueError("candidate unit must match feasible range unit")
        evidence_groups = (
            *(item.evidence for item in self.variable_routes),
            *(item.constraint_sources for item in self.feasible_ranges),
            *(item.evidence for item in self.capability_registry.capabilities),
            *(item.evidence for item in self.physics_features.features.values()),
        )
        if any(ref.profile_version not in (None, version) for group in evidence_groups for ref in group):
            raise ValueError("request evidence must use current ModelProfile version")
        for group, kind in (
            (self.knowledge_prior, "knowledge"),
            (self.historical_prior, "historical"),
            (self.surrogate_prior, "surrogate"),
        ):
            for item in group or []:
                if item.source_kind != kind or item.profile_version != version or item.variable not in candidate_ids:
                    raise ValueError("provider prior has mismatched kind, profile, or variable")
                authoritative = next(entry.feasible_range for entry in self.feasible_ranges if entry.variable == item.variable)
                if item.recommended_range is not None and not authoritative.contains(item.recommended_range):
                    raise ValueError("provider prior range exceeds deterministic feasible range")
        return self


class PriorProviderRequest(StrictContract):
    model_profile: ModelProfile
    variable: CandidateVariable
    feasible_range: ParameterFeasibleRange

    @model_validator(mode="after")
    def matching_variable(self) -> PriorProviderRequest:
        if self.variable.variable != self.feasible_range.variable:
            raise ValueError("provider request variable does not match feasible range")
        return self


class KnowledgePriorProvider(Protocol):
    async def provide(self, request: PriorProviderRequest) -> list[ProviderPrior]: ...


class HistoricalPriorProvider(Protocol):
    async def provide(self, request: PriorProviderRequest) -> list[ProviderPrior]: ...


class SurrogatePriorProvider(Protocol):
    async def provide(self, request: PriorProviderRequest) -> list[ProviderPrior]: ...


class PriorReasoner(Protocol):
    async def propose(self, request: PriorReasoningRequest) -> list[PriorProposal]: ...
