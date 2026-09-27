"""Phase 1 tests: contracts only, with no Fluent or provider execution."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from vegapunk.fluent.pre_simulation import (
    CandidateVariable,
    EvidenceStatus,
    IterationPolicy,
    ModelProfile,
    NumericRange,
    OptimizationPrior,
    OptimizationTask,
    ParameterFeasibleRange,
    PhysicsFeatures,
    PriorProposal,
    PriorReasoningRequest,
    ProviderPrior,
    SolverPreset,
    UserIntent,
    VariableRoute,
    VariableRouteBinding,
)


def bounds(lower=-30, upper=30, unit="deg"):
    return {"lower": lower, "upper": upper, "unit": unit}


def evidence(source_type="user", profile_version=None):
    return {
        "source_type": source_type,
        "source_id": "angle-hard-bound",
        "fact": "User specified an inlet-angle hard bound",
        "profile_version": profile_version,
        "verification": "VERIFIED",
    }


def proposal(**changes):
    data = {
        "variable": "inlet_angle",
        "route": "MAPPED",
        "current_value": None,
        "unit": "deg",
        "feasible_range": bounds(),
        "recommended_range": bounds(-20, 20),
        "high_potential_range": bounds(-10, 10),
        "confidence": 0.6,
        "evidence_status": "PARTIAL",
        "evidence": [evidence()],
        "assumptions": ["Registered direction mapping remains valid"],
        "missing_information": ["Current-case heat transfer trend"],
        "reasoning_summary": "Illustrative non-executable proposal",
        "profile_version": "profile-1",
    }
    data.update(changes)
    return data


def profile(**changes):
    data = {
        "case_fingerprint": "sha256:case-1",
        "profile_version": "profile-1",
        "generated_at": "2026-09-23T12:00:00+08:00",
        "dependency_versions": {"scanner": "1.4", "schema": "1"},
        "state_digest": {"mesh": "digest-1"},
        "verification": "VERIFIED",
    }
    data.update(changes)
    return data


def feasible_range(**changes):
    data = {
        "variable": "inlet_angle",
        "feasible_range": bounds(),
        "constraint_sources": [evidence()],
        "mapping_preimage_rule": "registered-angle-map-v1",
        "verification": "VERIFIED",
    }
    data.update(changes)
    return data


def solver_preset(**changes):
    data = {
        "preset_id": "preset-1",
        "profile_version": "profile-1",
        "iteration_policy": {
            "minimum": 50,
            "maximum": 300,
            "check_interval": 25,
            "early_stop_enabled": False,
            "convergence_window": None,
        },
        "convergence_policy": {"monitor_ids": [], "residual_thresholds": {}},
        "confidence": 0.4,
        "preset_basis": [evidence("registry")],
        "assumptions": ["Fixed upper budget only"],
        "evidence_status": "PARTIAL",
        "verification": "UNVERIFIED",
    }
    data.update(changes)
    return data


def optimization_prior(**changes):
    data = {
        "prior_id": "prior-1",
        "profile_version": "profile-1",
        "feasible_ranges": [feasible_range()],
        "proposals": [proposal()],
        "fusion_policy_version": "contract-only-1",
    }
    data.update(changes)
    return data


def optimization_task(**changes):
    data = {
        "task_id": "task-1",
        "approval_id": "approval-1",
        "case_fingerprint": "sha256:case-1",
        "profile_version": "profile-1",
        "prior": optimization_prior(),
        "solver_preset": solver_preset(),
        "prior_validation": {
            "valid": True,
            "errors": [],
            "warnings": [],
            "profile_version": "profile-1",
            "validator_version": "1",
        },
        "runner_contract_version": "1",
    }
    data.update(changes)
    return data


def reasoning_request(**changes):
    data = {
        "user_intent": {
            "intent_id": "intent-1",
            "target": "100 W heat-sink case",
            "objective": "Reduce maximum temperature",
            "hard_bounds": {"inlet_angle": bounds()},
            "constraints": [],
            "source_text": "Explore inlet angle within -30 to 30 degrees",
        },
        "model_profile": profile(),
        "physics_features": {"profile_version": "profile-1", "features": {}},
        "candidate_variables": [{
            "variable": "inlet_angle", "unit": "deg",
            "user_rationale": "User nominated angle", "route_candidate": "MAPPED",
        }],
        "variable_routes": [{
            "variable": "inlet_angle", "route": "MAPPED",
            "parameter_ids": ["direction-x", "direction-y"],
            "mapping_id": "angle-map-v1", "capability_status": "VERIFIED",
            "evidence": [evidence("registry")],
        }],
        "capability_registry": {
            "registry_version": "1", "profile_version": "profile-1",
            "capabilities": [{
                "capability_id": "mapped.angle.v1", "route": "MAPPED",
                "parameter_ids": ["direction-x", "direction-y"],
                "bound": bounds(), "verification": "VERIFIED",
                "evidence": [evidence("registry")],
            }],
        },
        "feasible_ranges": [feasible_range()],
        "knowledge_prior": None,
        "historical_prior": None,
        "surrogate_prior": None,
    }
    data.update(changes)
    return data


def test_required_enums_and_all_core_contracts_round_trip():
    assert [item.value for item in EvidenceStatus] == ["SUPPORTED", "PARTIAL", "INSUFFICIENT"]
    assert [item.value for item in VariableRoute] == ["DIRECT", "MAPPED", "GEOMETRY"]
    assert NumericRange.model_validate(bounds()).unit == "deg"
    assert UserIntent.model_validate(reasoning_request()["user_intent"]).intent_id == "intent-1"
    assert ModelProfile.model_validate(profile()).profile_version == "profile-1"
    assert PhysicsFeatures.model_validate(reasoning_request()["physics_features"]).features == {}
    assert CandidateVariable.model_validate(reasoning_request()["candidate_variables"][0]).route_candidate == VariableRoute.MAPPED
    assert VariableRouteBinding.model_validate(reasoning_request()["variable_routes"][0]).mapping_id == "angle-map-v1"
    assert ParameterFeasibleRange.model_validate(feasible_range()).resolved_by == "deterministic"
    assert IterationPolicy.model_validate(solver_preset()["iteration_policy"]).maximum == 300
    assert SolverPreset.model_validate(solver_preset()).preset_basis
    assert PriorReasoningRequest.model_validate(reasoning_request()).knowledge_prior is None
    assert OptimizationPrior.model_validate(optimization_prior()).prior_id == "prior-1"
    task = OptimizationTask.model_validate(optimization_task())
    assert OptimizationTask.model_validate_json(task.model_dump_json()) == task


@pytest.mark.parametrize("changed", [
    {"recommended_range": bounds(-40, 20)},
    {"high_potential_range": bounds(-25, 10)},
    {"recommended_range": bounds(-20, 20, "rad")},
    {"high_potential_range": bounds(-10, 10, "rad")},
])
def test_proposal_rejects_outside_or_mismatched_ranges(changed):
    with pytest.raises(ValidationError):
        PriorProposal.model_validate(proposal(**changed))


def test_nullable_high_potential_and_insufficient_evidence_rule_beats_confidence():
    accepted = PriorProposal.model_validate(proposal(
        feasible_range=bounds(), recommended_range=bounds(), high_potential_range=None,
        evidence_status="INSUFFICIENT", confidence=1.0,
    ))
    assert accepted.high_potential_range is None
    with pytest.raises(ValidationError, match="INSUFFICIENT evidence requires high_potential_range"):
        PriorProposal.model_validate(proposal(evidence_status="INSUFFICIENT", confidence=1.0))
    with pytest.raises(ValidationError, match="INSUFFICIENT evidence cannot narrow"):
        PriorProposal.model_validate(proposal(
            evidence_status="INSUFFICIENT", high_potential_range=None, confidence=1.0,
        ))
    with pytest.raises(ValidationError, match="missing_information"):
        PriorProposal.model_validate(proposal(
            evidence_status="INSUFFICIENT", recommended_range=bounds(),
            high_potential_range=None, missing_information=[],
        ))


def test_supported_status_requires_evidence_and_confidence_is_finite():
    with pytest.raises(ValidationError, match="SUPPORTED evidence"):
        PriorProposal.model_validate(proposal(evidence_status="SUPPORTED", evidence=[]))
    for value in (True, float("nan"), float("inf"), "0.8"):
        with pytest.raises(ValidationError):
            PriorProposal.model_validate(proposal(confidence=value))


def test_no_observed_optimal_region_or_extra_fields_in_pre_simulation_models():
    for cls, payload in (
        (PriorProposal, proposal()),
        (OptimizationPrior, optimization_prior()),
        (OptimizationTask, optimization_task()),
    ):
        assert "observed_optimal_region" not in cls.model_fields
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            cls.model_validate({**payload, "observed_optimal_region": bounds(-5, 5)})


def test_model_profile_requires_identity_timestamp_and_versions():
    for field in ("case_fingerprint", "profile_version", "generated_at", "dependency_versions"):
        data = profile()
        del data[field]
        with pytest.raises(ValidationError):
            ModelProfile.model_validate(data)
    with pytest.raises(ValidationError, match="timezone"):
        ModelProfile.model_validate(profile(generated_at="2026-09-23T12:00:00"))
    with pytest.raises(ValidationError):
        ModelProfile.model_validate(profile(dependency_versions={}))
    with pytest.raises(ValidationError):
        ModelProfile.model_validate(profile(schema_version=2))
    assert ModelProfile.model_validate(profile(readbacks={
        "velocity": {"value": [1.0, 0.0, 0.0], "unit": "m/s", "verification": "VERIFIED"},
    })).readbacks["velocity"].value == [1.0, 0.0, 0.0]


def test_solver_preset_requires_basis_and_valid_iteration_policy():
    with pytest.raises(ValidationError):
        SolverPreset.model_validate(solver_preset(preset_basis=[], confidence=1.0))
    for confidence in (True, float("nan"), 1.5):
        with pytest.raises(ValidationError):
            SolverPreset.model_validate(solver_preset(confidence=confidence))
    with pytest.raises(ValidationError, match="minimum must not exceed maximum"):
        IterationPolicy.model_validate({**solver_preset()["iteration_policy"], "minimum": 301})
    with pytest.raises(ValidationError, match="convergence_window"):
        IterationPolicy.model_validate({**solver_preset()["iteration_policy"], "early_stop_enabled": True})
    with pytest.raises(ValidationError, match="early stop requires a convergence monitor"):
        SolverPreset.model_validate(solver_preset(iteration_policy={
            **solver_preset()["iteration_policy"], "early_stop_enabled": True,
            "convergence_window": 20,
        }))


def test_prior_rechecks_llm_copy_against_authoritative_feasible_range():
    forged = proposal(feasible_range=bounds(-100, 100), recommended_range=bounds(-20, 20))
    with pytest.raises(ValidationError, match="differs from deterministic range"):
        OptimizationPrior.model_validate(optimization_prior(proposals=[forged]))
    with pytest.raises(ValidationError, match="profile_version"):
        OptimizationPrior.model_validate(optimization_prior(proposals=[proposal(profile_version="old")]))


def test_task_requires_successful_validation_and_matching_versions():
    invalid = deepcopy(optimization_task())
    invalid["prior_validation"]["valid"] = False
    invalid["prior_validation"]["errors"] = ["range failed"]
    with pytest.raises(ValidationError, match="successful PriorValidation"):
        OptimizationTask.model_validate(invalid)
    with pytest.raises(ValidationError, match="solver preset profile_version"):
        OptimizationTask.model_validate(optimization_task(solver_preset=solver_preset(profile_version="old")))


def test_future_provider_cannot_supply_unverified_ranges():
    raw = {
        "provider_id": "knowledge-1", "source_kind": "knowledge",
        "variable": "inlet_angle", "profile_version": "profile-1",
        "verification": "UNVERIFIED", "recommended_range": bounds(-20, 20),
        "evidence": [evidence("knowledge")],
    }
    with pytest.raises(ValidationError, match="unverified provider"):
        ProviderPrior.model_validate(raw)
    assert ProviderPrior.model_validate({**raw, "recommended_range": None}).recommended_range is None
    assert ProviderPrior.model_validate({**raw, "verification": "VERIFIED"}).recommended_range is not None
    with pytest.raises(ValidationError, match="verified evidence"):
        ProviderPrior.model_validate({**raw, "verification": "VERIFIED", "evidence": []})


def test_reasoning_request_requires_one_shared_profile_and_matching_variable_contracts():
    with pytest.raises(ValidationError, match="current ModelProfile version"):
        PriorReasoningRequest.model_validate(reasoning_request(physics_features={
            "profile_version": "old", "features": {},
        }))
    with pytest.raises(ValidationError, match="must match"):
        PriorReasoningRequest.model_validate(reasoning_request(candidate_variables=[{
            "variable": "velocity", "unit": "m/s", "user_rationale": "User nominated",
            "route_candidate": "DIRECT",
        }]))
    bad = reasoning_request()
    bad["knowledge_prior"] = [{
        "provider_id": "prior-1", "source_kind": "historical",
        "variable": "inlet_angle", "profile_version": "profile-1",
        "verification": "UNKNOWN", "evidence": [],
    }]
    with pytest.raises(ValidationError, match="mismatched kind"):
        PriorReasoningRequest.model_validate(bad)
    stale_registry = reasoning_request()
    stale_registry["capability_registry"]["profile_version"] = "old"
    with pytest.raises(ValidationError, match="capability_registry"):
        PriorReasoningRequest.model_validate(stale_registry)
    outside_provider = reasoning_request()
    outside_provider["knowledge_prior"] = [{
        "provider_id": "prior-1", "source_kind": "knowledge",
        "variable": "inlet_angle", "profile_version": "profile-1",
        "verification": "VERIFIED", "recommended_range": bounds(-50, 50),
        "evidence": [evidence("knowledge")],
    }]
    with pytest.raises(ValidationError, match="exceeds deterministic"):
        PriorReasoningRequest.model_validate(outside_provider)
    stale_evidence = reasoning_request()
    stale_evidence["feasible_ranges"][0]["constraint_sources"][0]["profile_version"] = "old"
    with pytest.raises(ValidationError, match="request evidence"):
        PriorReasoningRequest.model_validate(stale_evidence)
