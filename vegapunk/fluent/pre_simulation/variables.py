"""Deterministic variable planning, routing, mapping and feasible bounds."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..capability_registry import CapabilityRegistry
from ..mapping_schema import MappingSpec
from ..parameter_rules import resolve_parameters
from .schema import (
    CandidateVariable,
    CapabilityDescriptor,
    CapabilityRegistrySnapshot,
    EvidenceRef,
    ModelProfile,
    NumericRange,
    ParameterFeasibleRange,
    UserIntent,
    VariableRole,
    VariableRoute,
    VariableRouteBinding,
    VerificationStatus,
)


class PlanningContractError(ValueError):
    pass


MAPPING_REGISTRY_VERSION = "1"


@dataclass(frozen=True)
class RegisteredMapping:
    mapping_id: str
    version: str
    input_variables: tuple[str, ...]
    output_rule_ids: tuple[str, ...]
    input_unit: str
    output_unit: str
    domain: NumericRange
    formula: str
    validity_conditions: tuple[str, ...]
    supported_case_constraints: tuple[str, ...]

    def forward(self, angle_deg: float) -> tuple[float, float, float]:
        if self.mapping_id != "inlet_direction_angle_xy.v1":
            raise PlanningContractError("Unimplemented registered mapping")
        if isinstance(angle_deg, bool) or not isinstance(angle_deg, (float, int)) or not math.isfinite(angle_deg):
            raise PlanningContractError("Angle must be finite")
        if not self.domain.contains(NumericRange(lower=angle_deg, upper=angle_deg, unit="deg")):
            raise PlanningContractError("Angle exceeds registered mapping domain")
        radians = math.radians(angle_deg)
        return math.cos(radians), math.sin(radians), 0.0


class MappingRegistry:
    """Server-owned, versioned formulas; models may select IDs, never author code."""

    version = MAPPING_REGISTRY_VERSION

    def __init__(self) -> None:
        angle = RegisteredMapping(
            mapping_id="inlet_direction_angle_xy.v1",
            version="1",
            input_variables=("inlet_angle",),
            output_rule_ids=("flow_direction_x", "flow_direction_y", "flow_direction_z"),
            input_unit="deg", output_unit="dimensionless",
            domain=NumericRange(lower=-180, upper=180, unit="deg"),
            formula="absolute XY inlet direction from +X: (cos_deg(angle), sin_deg(angle), 0)",
            validity_conditions=("one or more synchronized inlet vectors", "unit direction", "XY plane", "Z=0"),
            supported_case_constraints=("already-active direction-vector mode", "complete XYZ per inlet"),
        )
        self._items = {angle.mapping_id: angle}

    def get(self, mapping_id: str) -> RegisteredMapping | None:
        return self._items.get(mapping_id)

    def require(self, mapping_id: str) -> RegisteredMapping:
        item = self.get(mapping_id)
        if item is None:
            raise PlanningContractError(f"Mapping is not registered: {mapping_id}")
        return item


def build_registered_mapping_spec(mapping_id: str, parameter_ids: list[str], parameters: dict) -> MappingSpec:
    """Construct the only V1 mapped execution formula from server-owned code."""
    mapping = MappingRegistry().require(mapping_id)
    if mapping.mapping_id != "inlet_direction_angle_xy.v1":
        raise PlanningContractError("No executable registered mapping builder")
    groups = {}
    for key in parameter_ids:
        if key not in parameters:
            raise PlanningContractError("Registered angle mapping references an unknown parameter")
        parameter = parameters[key]
        group = groups.setdefault(parameter.zone, {})
        if parameter.rule_id in group:
            raise PlanningContractError("Registered angle mapping has duplicate direction components")
        group[parameter.rule_id] = key
    if not groups or any(set(group) != set(mapping.output_rule_ids) for group in groups.values()):
        raise PlanningContractError("Registered angle mapping requires complete XYZ per inlet")
    ordered_ids = [group[f"flow_direction_{axis}"] for _, group in sorted(groups.items()) for axis in "xyz"]
    proxy = "inlet_angle_proxy"
    expressions = {proxy: {"ref": "inlet_angle"}}
    output_units = {"inlet_angle": "deg", proxy: "deg"}
    for _, group in sorted(groups.items()):
        x, y, z = (group[f"flow_direction_{axis}"] for axis in "xyz")
        expressions.update({
            x: {"op": "cos_deg", "args": [{"ref": proxy}]},
            y: {"op": "sin_deg", "args": [{"ref": proxy}]},
            z: {"value": 0.0},
        })
        output_units.update({x: "dimensionless", y: "dimensionless", z: "dimensionless"})
    return MappingSpec.model_validate({
        "mapping_id": mapping.mapping_id,
        "mapping_version": mapping.version,
        "source_variables": ["inlet_angle"],
        "context_variables": [],
        "proxy_variables": [proxy],
        "selected_capability_id": "mapped.fan_incidence_2d.v1",
        "selected_fluent_parameter_ids": ordered_ids,
        "forward_expression": expressions,
        "reverse_expression": {"inlet_angle": {"ref": proxy}},
        "source_units": {"inlet_angle": "deg"},
        "output_units": output_units,
        "coordinate_convention": "XY Cartesian; +X is zero; Z is zero",
        "angle_convention": "absolute inlet direction in degrees; counter-clockwise positive",
        "assumptions": ["Existing inlet vector mode is active; geometry remains fixed"],
        "validity_conditions": list(mapping.validity_conditions),
        "verification_route": "NONE",
        "confidence": 1.0,
        "automatic_geometry_execution": False,
    })


_VARIABLES = {
    "inlet_velocity": ("velocity", "m/s", VariableRoute.DIRECT),
    "inlet_angle": ("wind_direction", "deg", VariableRoute.MAPPED),
    "heat_source_power": ("heat_source_power", "W", VariableRoute.DIRECT),
    "fan_physical_position": ("fan_physical_position", "m", VariableRoute.GEOMETRY),
}


class VariablePlanner:
    def plan(self, intent: UserIntent, profile: ModelProfile, registry: CapabilityRegistrySnapshot) -> list[CandidateVariable]:
        if registry.profile_version != profile.profile_version:
            raise PlanningContractError("Capability Registry is not bound to current ModelProfile")
        requested = list(dict.fromkeys([*intent.requested_variables, *intent.hard_bounds]))
        candidates = []
        for name in requested:
            if name not in _VARIABLES:
                raise PlanningContractError(f"Unknown structured variable: {name}")
            semantic, unit, route = _VARIABLES[name]
            hard_bound = intent.hard_bounds.get(name)
            if hard_bound is not None and hard_bound.unit != unit:
                raise PlanningContractError(f"Variable {name} hard-bound unit must be {unit}")
            if name == "heat_source_power":
                supported = any(
                    item.route == VariableRoute.DIRECT and item.executable
                    and item.verification == VerificationStatus.VERIFIED and semantic in item.semantic_keys
                    for item in registry.capabilities
                )
                if not supported or name not in intent.requested_variables:
                    raise PlanningContractError("heat_source_power requires explicit user selection and verified capability")
            role = VariableRole.GEOMETRY_CANDIDATE if route == VariableRoute.GEOMETRY else VariableRole.DESIGN_VARIABLE
            candidates.append(CandidateVariable(
                variable=name, unit=unit, user_rationale="Structured UserIntent selection",
                route_candidate=route, role=role,
            ))
        return candidates


def registry_snapshot(existing: CapabilityRegistry, profile: ModelProfile) -> CapabilityRegistrySnapshot:
    """Adapt the existing Case-scoped Registry; do not rebuild its capabilities."""
    descriptors = []
    for raw in existing.available():
        kind = VariableRoute.MAPPED if str(raw["capability_id"]).startswith("mapped.") else VariableRoute.DIRECT
        hard = raw.get("hard_range")
        bound = NumericRange(lower=hard[0], upper=hard[1], unit=raw["source_unit"]) if hard else None
        executable = bool(raw.get("executable"))
        descriptors.append(CapabilityDescriptor(
            capability_id=raw["capability_id"], route=kind,
            semantic_keys=list(raw["semantic_keys"]),
            parameter_ids=list(raw["available_parameter_ids"]),
            bound=bound, executable=executable,
            assumptions=list(raw.get("assumptions") or []),
            verification=VerificationStatus.VERIFIED if executable else VerificationStatus.UNVERIFIED,
            evidence=[EvidenceRef(
                source_type="registry", source_id=raw["capability_id"],
                fact=f"Case-scoped capability version {raw['version']}",
                profile_version=profile.profile_version,
                verification=VerificationStatus.VERIFIED if executable else VerificationStatus.UNVERIFIED,
            )],
        ))
    return CapabilityRegistrySnapshot(
        registry_version=existing.version, profile_version=profile.profile_version,
        capabilities=descriptors,
    )


def _catalog(profile: ModelProfile):
    return {item.key: item for item in resolve_parameters({
        "observations": profile.parameters, "physics": profile.physical_models,
    })}


class VariableRouter:
    def __init__(self, mappings: MappingRegistry | None = None) -> None:
        self.mappings = mappings or MappingRegistry()

    def route(
        self, candidate: CandidateVariable, profile: ModelProfile, registry: CapabilityRegistrySnapshot
    ) -> VariableRouteBinding:
        if registry.profile_version != profile.profile_version:
            raise PlanningContractError("Capability Registry is stale")
        if candidate.route_candidate == VariableRoute.GEOMETRY:
            return VariableRouteBinding(
                variable=candidate.variable, route=VariableRoute.GEOMETRY,
                capability_status=VerificationStatus.UNVERIFIED,
                evidence=[EvidenceRef(
                    source_type="registry", source_id="geometry-required",
                    fact="GEOMETRY_REQUIRED; V1 does not execute CAD or mesh changes",
                    profile_version=profile.profile_version, verification=VerificationStatus.UNVERIFIED,
                )],
            )
        semantic = _VARIABLES[candidate.variable][0]
        matches = [item for item in registry.capabilities if item.route == candidate.route_candidate
                   and semantic in item.semantic_keys and item.executable
                   and item.verification == VerificationStatus.VERIFIED]
        if len(matches) != 1:
            return VariableRouteBinding(
                variable=candidate.variable, route=candidate.route_candidate,
                capability_status=VerificationStatus.UNVERIFIED,
                evidence=[EvidenceRef(
                    source_type="registry", source_id=candidate.variable,
                    fact="No unique verified executable capability",
                    profile_version=profile.profile_version, verification=VerificationStatus.UNVERIFIED,
                )],
            )
        capability = matches[0]
        available = _catalog(profile)
        ids = [key for key in capability.parameter_ids if key in available]
        if candidate.route_candidate == VariableRoute.DIRECT:
            verified = len(ids) == 1
            return VariableRouteBinding(
                variable=candidate.variable, route=VariableRoute.DIRECT,
                parameter_ids=ids if verified else [],
                capability_status=VerificationStatus.VERIFIED if verified else VerificationStatus.UNVERIFIED,
                evidence=capability.evidence,
            )
        mapping = self.mappings.require("inlet_direction_angle_xy.v1")
        rules = {available[key].rule_id for key in ids}
        zones = {available[key].zone for key in ids}
        groups = {zone: {available[key].rule_id for key in ids if available[key].zone == zone} for zone in zones}
        verified = bool(groups) and all(group == set(mapping.output_rule_ids) for group in groups.values())
        verified = verified and len(ids) == 3 * len(groups)
        return VariableRouteBinding(
            variable=candidate.variable, route=VariableRoute.MAPPED,
            parameter_ids=ids if verified else [],
            mapping_id=mapping.mapping_id if verified else None,
            capability_status=VerificationStatus.VERIFIED if verified else VerificationStatus.UNVERIFIED,
            evidence=capability.evidence,
        )


class FeasibleRangeResolver:
    def __init__(self, mappings: MappingRegistry | None = None) -> None:
        self.mappings = mappings or MappingRegistry()

    def resolve(
        self, intent: UserIntent, candidate: CandidateVariable, route: VariableRouteBinding,
        profile: ModelProfile, registry: CapabilityRegistrySnapshot,
    ) -> ParameterFeasibleRange:
        if route.variable != candidate.variable or route.capability_status != VerificationStatus.VERIFIED:
            raise PlanningContractError("Cannot resolve an unverified variable route")
        if route.route == VariableRoute.GEOMETRY:
            raise PlanningContractError("GEOMETRY_REQUIRED cannot have an executable V1 range")
        if registry.profile_version != profile.profile_version:
            raise PlanningContractError("Capability Registry is stale")
        semantic = _VARIABLES[candidate.variable][0]
        matches = [item for item in registry.capabilities if item.route == route.route
                   and semantic in item.semantic_keys and item.executable]
        if len(matches) != 1:
            raise PlanningContractError("No unique executable capability for feasible range")
        capability = matches[0]
        ranges: list[NumericRange] = []
        sources: list[EvidenceRef] = []
        hard = intent.hard_bounds.get(candidate.variable)
        if hard:
            ranges.append(hard)
            sources.append(EvidenceRef(
                source_type="user", source_id=intent.intent_id,
                fact=f"User hard bound for {candidate.variable}",
                profile_version=None, verification=VerificationStatus.VERIFIED,
            ))
        if capability.bound:
            ranges.append(capability.bound)
            sources.extend(capability.evidence)
        catalog = _catalog(profile)
        if route.route == VariableRoute.DIRECT:
            if len(route.parameter_ids) != 1 or route.parameter_ids[0] not in catalog:
                raise PlanningContractError("DIRECT route requires exactly one exposed parameter")
            parameter = catalog[route.parameter_ids[0]]
            ranges.append(NumericRange(lower=parameter.hard_min, upper=parameter.hard_max, unit=parameter.unit))
            sources.append(EvidenceRef(
                source_type="model_readback", source_id=parameter.key,
                fact="Fluent parameter bound intersected with engineering rule",
                profile_version=profile.profile_version, verification=VerificationStatus.VERIFIED,
            ))
            mapping_rule = None
        else:
            mapping = self.mappings.require(route.mapping_id or "")
            if candidate.variable not in mapping.input_variables or candidate.unit != mapping.input_unit:
                raise PlanningContractError("Registered mapping does not match variable or unit")
            selected = [catalog.get(key) for key in route.parameter_ids]
            if not selected or any(item is None for item in selected):
                raise PlanningContractError("Mapping requires exposed direction parameters")
            groups = {item.zone: {entry.rule_id for entry in selected if entry.zone == item.zone} for item in selected}
            if len(selected) != 3 * len(groups) or any(group != set(mapping.output_rule_ids) for group in groups.values()):
                raise PlanningContractError("Mapping requires complete XYZ components for each inlet")
            if any(item.hard_min > -1 or item.hard_max < 1 for item in selected):
                raise PlanningContractError("Cannot prove the mapping preimage for restricted component bounds")
            ranges.append(mapping.domain)
            sources.append(EvidenceRef(
                source_type="registry", source_id=mapping.mapping_id,
                fact=f"Registered mapping domain and preimage rule version {mapping.version}",
                profile_version=profile.profile_version, verification=VerificationStatus.VERIFIED,
            ))
            mapping_rule = f"{mapping.mapping_id}@{mapping.version}: full [-1,1] XYZ component domain"
        if not ranges:
            raise PlanningContractError("No proven feasible bounds")
        if any(item.unit != candidate.unit for item in ranges):
            raise PlanningContractError("Feasible range units do not match variable unit")
        lower = max(item.lower for item in ranges)
        upper = min(item.upper for item in ranges)
        if lower > upper:
            raise PlanningContractError("Conflicting hard bounds leave an empty feasible range")
        return ParameterFeasibleRange(
            variable=candidate.variable,
            feasible_range=NumericRange(lower=lower, upper=upper, unit=candidate.unit),
            constraint_sources=sources,
            mapping_preimage_rule=mapping_rule,
            verification=VerificationStatus.VERIFIED,
        )
