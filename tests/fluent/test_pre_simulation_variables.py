"""Phase 3 deterministic feature, routing, mapping and bound tests."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest

from vegapunk.fluent.capability_registry import CapabilityRegistry
from vegapunk.fluent.parameter_rules import resolve_parameters
from vegapunk.fluent.pre_simulation.physics import PhysicsFeatureExtractor
from vegapunk.fluent.pre_simulation.schema import (
    EvidenceStatus, FeatureObservation, ModelProfile, NumericRange, UserIntent,
    VariableRole, VariableRoute, VerificationStatus,
)
from vegapunk.fluent.pre_simulation.variables import (
    FeasibleRangeResolver, MappingRegistry, PlanningContractError,
    VariablePlanner, VariableRouter, registry_snapshot,
    build_registered_mapping_spec,
)


def observation(rule_id, value, collection="velocity_inlet", name="inlet"):
    return {
        "rule_id": rule_id, "collection": collection, "object_name": name,
        "value": value, "native_min": -1 if rule_id.startswith("flow_direction") else 0,
        "native_max": 1 if rule_id.startswith("flow_direction") else 100,
        "editable": True,
    }


def profile(with_direction=True):
    observations = [
        observation("velocity", 2.0),
        observation("hydraulic_diameter", 0.02),
        observation("density", 1.2, "fluid", "air"),
        observation("viscosity", 1.8e-5, "fluid", "air"),
    ]
    if with_direction:
        observations += [observation(f"flow_direction_{axis}", value) for axis, value in zip("xyz", (1.0, 0.0, 0.0))]
    return ModelProfile(
        case_fingerprint="sha256:case-a", profile_version="ps-a",
        generated_at=datetime.now(timezone.utc), dependency_versions={"scanner": "1.4"},
        verification="VERIFIED", physical_models={"energy": True}, parameters=observations,
        readbacks={
            "inlet_area_m2": FeatureObservation(value=0.01, unit="m²", verification="VERIFIED"),
            "heat_transfer_area_m2": FeatureObservation(value=0.2, unit="m²", verification="VERIFIED"),
            "thermal_load_W": FeatureObservation(value=100.0, unit="W", verification="VERIFIED"),
        },
    )


def registry(model):
    signature = {"observations": model.parameters, "physics": model.physical_models}
    parameters = {item.key: item for item in resolve_parameters(signature)}
    existing = CapabilityRegistry({"signature": signature}, parameters, {})
    return registry_snapshot(existing, model)


def intent(name="inlet_velocity", low=1.0, high=5.0, unit="m/s"):
    return UserIntent(
        intent_id="intent-a", target="100 W heat-sink case", objective="minimize temperature",
        requested_variables=[name], hard_bounds={name: NumericRange(lower=low, upper=high, unit=unit)},
        source_text="Structured test intent",
    )


def test_verified_physics_formulas_and_future_unknowns():
    result = PhysicsFeatureExtractor().extract(profile())
    features = result.features
    assert features["reynolds_number"].value == pytest.approx(1.2 * 2 * 0.02 / 1.8e-5)
    assert features["mass_flow"].value == pytest.approx(1.2 * 2 * 0.01)
    assert features["heat_flux"].value == pytest.approx(100 / 0.2)
    assert features["velocity_scale"].value == 2
    assert features["characteristic_length"].value == 0.02
    assert features["thermal_load"].value == 100
    assert features["nusselt_number"].value is None
    assert features["nusselt_number"].evidence_status == EvidenceStatus.INSUFFICIENT
    assert all(item.profile_version == "ps-a" for item in features["reynolds_number"].evidence)


def test_missing_physics_is_null_not_guessed_from_100_w_title():
    model = profile()
    model.readbacks.pop("thermal_load_W")
    model.readbacks.pop("inlet_area_m2")
    model.parameters = [item for item in model.parameters if item["rule_id"] != "viscosity"]
    features = PhysicsFeatureExtractor().extract(model).features
    for key in ("thermal_load", "heat_flux", "mass_flow", "reynolds_number"):
        assert features[key].value is None
        assert features[key].evidence_status == EvidenceStatus.INSUFFICIENT
        assert features[key].missing_information


def test_direct_mapped_geometry_and_fixed_heat_source_gate():
    model = profile()
    snap = registry(model)
    planner = VariablePlanner()
    router = VariableRouter()
    velocity = planner.plan(intent(), model, snap)[0]
    assert velocity.role == VariableRole.DESIGN_VARIABLE
    direct = router.route(velocity, model, snap)
    assert direct.route == VariableRoute.DIRECT
    assert direct.capability_status == VerificationStatus.VERIFIED
    assert len(direct.parameter_ids) == 1

    angle_intent = intent("inlet_angle", -30, 30, "deg")
    angle = planner.plan(angle_intent, model, snap)[0]
    mapped = router.route(angle, model, snap)
    assert mapped.route == VariableRoute.MAPPED
    assert mapped.mapping_id == "inlet_direction_angle_xy.v1"
    assert len(mapped.parameter_ids) == 3

    geometry = planner.plan(intent("fan_physical_position", 0, 0.1, "m"), model, snap)[0]
    assert geometry.role == VariableRole.GEOMETRY_CANDIDATE
    geometry_route = router.route(geometry, model, snap)
    assert geometry_route.route == VariableRoute.GEOMETRY
    assert geometry_route.capability_status == VerificationStatus.UNVERIFIED
    with pytest.raises(PlanningContractError, match="heat_source_power"):
        planner.plan(intent("heat_source_power", 50, 150, "W"), model, snap)


def test_registered_mapping_formula_domain_and_unknown_mapping():
    mappings = MappingRegistry()
    mapped = mappings.require("inlet_direction_angle_xy.v1")
    x, y, z = mapped.forward(30)
    assert x == pytest.approx(math.sqrt(3) / 2)
    assert y == pytest.approx(0.5)
    assert z == 0
    with pytest.raises(PlanningContractError, match="not registered"):
        mappings.require("free-form-llm-python")
    with pytest.raises(PlanningContractError, match="domain"):
        mapped.forward(200)


def test_feasible_range_intersection_and_conflicting_bounds():
    model = profile()
    snap = registry(model)
    user = intent(low=1, high=5)
    candidate = VariablePlanner().plan(user, model, snap)[0]
    route = VariableRouter().route(candidate, model, snap)
    result = FeasibleRangeResolver().resolve(user, candidate, route, model, snap)
    assert result.feasible_range == NumericRange(lower=1, upper=5, unit="m/s")
    assert {item.source_type for item in result.constraint_sources} >= {"user", "registry", "model_readback"}
    with pytest.raises(PlanningContractError, match="Conflicting hard bounds"):
        other = intent(low=1200, high=1300)
        FeasibleRangeResolver().resolve(other, candidate, route, model, snap)


def test_mapped_feasible_range_and_unverified_route_blocked():
    model = profile()
    snap = registry(model)
    user = intent("inlet_angle", -30, 30, "deg")
    candidate = VariablePlanner().plan(user, model, snap)[0]
    route = VariableRouter().route(candidate, model, snap)
    result = FeasibleRangeResolver().resolve(user, candidate, route, model, snap)
    assert result.feasible_range == NumericRange(lower=-30, upper=30, unit="deg")
    assert "inlet_direction_angle_xy.v1" in result.mapping_preimage_rule

    no_direction = profile(with_direction=False)
    no_direction_snap = registry(no_direction)
    unresolved = VariableRouter().route(candidate, no_direction, no_direction_snap)
    assert unresolved.capability_status == VerificationStatus.UNVERIFIED
    with pytest.raises(PlanningContractError, match="unverified"):
        FeasibleRangeResolver().resolve(user, candidate, unresolved, no_direction, no_direction_snap)


def test_two_inlets_can_share_one_registered_angle_without_mixing_components():
    model = profile()
    model.parameters.extend(
        observation(f"flow_direction_{axis}", value, name="inlet-b")
        for axis, value in zip("xyz", (1.0, 0.0, 0.0))
    )
    snap = registry(model)
    user = intent("inlet_angle", -30, 30, "deg")
    candidate = VariablePlanner().plan(user, model, snap)[0]
    route = VariableRouter().route(candidate, model, snap)
    assert route.capability_status == VerificationStatus.VERIFIED
    assert len(route.parameter_ids) == 6
    feasible = FeasibleRangeResolver().resolve(user, candidate, route, model, snap)
    assert feasible.feasible_range.lower == -30
    signature = {"observations": model.parameters, "physics": model.physical_models}
    catalog = {item.key: item for item in resolve_parameters(signature)}
    mapping = build_registered_mapping_spec(route.mapping_id, route.parameter_ids, catalog)
    assert len(mapping.selected_fluent_parameter_ids) == 6
    assert len(mapping.forward_expression) == 7


def test_planner_rejects_unknown_name_and_unit_mismatch():
    model = profile()
    snap = registry(model)
    with pytest.raises(PlanningContractError, match="Unknown"):
        VariablePlanner().plan(intent("made_up", 1, 2, "m/s"), model, snap)
    with pytest.raises(PlanningContractError, match="unit"):
        VariablePlanner().plan(intent("inlet_velocity", 1, 2, "deg"), model, snap)
