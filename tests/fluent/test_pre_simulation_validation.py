"""Phase 5 solver policy, validator and immutable approval tests."""

from __future__ import annotations

from copy import deepcopy

import pytest

from vegapunk.fluent.pre_simulation.prior import PriorFusion
from vegapunk.fluent.pre_simulation.evidence import profile_facts
from vegapunk.fluent.pre_simulation.schema import (
    EvidenceStatus, IterationPolicy, OptimizationPrior, PriorProposal, VariableRouteBinding,
)
from vegapunk.fluent.pre_simulation.validation import (
    PriorFreezeStore, PriorValidator, SolverBudgetPolicy, SolverPresetPlanner,
)

from .test_pre_simulation_prior import proposal, request


def policy(**changes):
    data = {
        "policy_id": "configured-100w-budget-v1",
        "min_iterations": 50,
        "initial_budget": 100,
        "hard_limit": 300,
        "check_interval": 25,
        "residual_thresholds": {},
        "monitor_ids": (),
        "convergence_window": None,
        "chunked_runner_verified": False,
    }
    data.update(changes)
    return SolverBudgetPolicy(**data)


def context():
    req = request()
    prior = PriorFusion().fuse(req, [PriorProposal.model_validate(proposal(req))])
    preset = SolverPresetPlanner().plan(req.model_profile, policy())
    return req, prior, preset


def validate(req, prior, preset, **options):
    return PriorValidator().validate(
        prior, preset, req.model_profile, req.capability_registry,
        req.variable_routes, current_case_fingerprint=req.model_profile.case_fingerprint,
        **options,
    )


def test_solver_preset_has_explainable_budgets_and_conservative_fallback():
    req, _, preset = context()
    assert preset.iteration_policy.min_iterations == 50
    assert preset.iteration_policy.initial_budget == 100
    assert preset.iteration_policy.hard_limit == 300
    assert preset.iteration_policy.check_interval == 25
    assert preset.iteration_policy.early_stop_enabled is False
    assert preset.evidence_status == EvidenceStatus.INSUFFICIENT
    assert preset.preset_basis[0].source_id == "configured-100w-budget-v1"
    assert "not a predicted optimum" in preset.preset_basis[0].fact
    with pytest.raises(ValueError, match="policy basis"):
        SolverPresetPlanner().plan(req.model_profile, policy(policy_id=""))
    with pytest.raises(ValueError, match="minimum"):
        IterationPolicy(min_iterations=100, initial_budget=50, hard_limit=300,
                        check_interval=10, early_stop_enabled=False)


def test_early_stop_requires_verified_runner_and_monitor():
    req = request()
    planner = SolverPresetPlanner()
    unverified = planner.plan(req.model_profile, policy(
        residual_thresholds={"continuity": 1e-4}, convergence_window=20,
        chunked_runner_verified=False,
    ))
    assert unverified.iteration_policy.early_stop_enabled is False
    verified = planner.plan(req.model_profile, policy(
        residual_thresholds={"continuity": 1e-4}, convergence_window=20,
        chunked_runner_verified=True,
    ))
    assert verified.iteration_policy.early_stop_enabled is True
    prior = PriorFusion().fuse(req, [PriorProposal.model_validate(proposal(req))])
    result = validate(req, prior, verified)
    assert not result.valid
    assert any("early-stop" in error for error in result.errors)
    assert validate(req, prior, verified, runner_supports_early_stop=True).valid


def test_validator_accepts_broad_insufficient_prior_but_flags_it():
    req, prior, preset = context()
    result = validate(req, prior, preset)
    assert result.valid
    assert result.errors == []
    assert any("insufficient" in item for item in result.warnings)


def test_validator_rejects_missing_mapping_geometry_stale_profile_and_unknowns():
    req, prior, preset = context()
    missing = [VariableRouteBinding.model_validate({
        **req.variable_routes[0].model_dump(mode="json"), "mapping_id": "not-registered",
    })]
    result = PriorValidator().validate(prior, preset, req.model_profile, req.capability_registry,
                                       missing, current_case_fingerprint=req.model_profile.case_fingerprint)
    assert any("mapping is not registered" in item for item in result.errors)
    geometry = [VariableRouteBinding.model_validate({
        **req.variable_routes[0].model_dump(mode="json"), "route": "GEOMETRY",
        "mapping_id": None, "capability_status": "UNVERIFIED",
    })]
    result = PriorValidator().validate(prior, preset, req.model_profile, req.capability_registry,
                                       geometry, current_case_fingerprint=req.model_profile.case_fingerprint)
    assert not result.valid
    assert any("route is not verified" in item for item in result.errors)
    stale = req.model_profile.model_copy(update={"profile_version": "new-profile"})
    result = PriorValidator().validate(prior, preset, stale, req.capability_registry,
                                       req.variable_routes, current_case_fingerprint=stale.case_fingerprint)
    assert any("stale" in item for item in result.errors)
    unknown = req.model_profile.model_copy(update={"unknown_fields": ["parameters"]})
    result = PriorValidator().validate(prior, preset, unknown, req.capability_registry,
                                       req.variable_routes, current_case_fingerprint=unknown.case_fingerprint)
    assert any("UNKNOWN" in item for item in result.errors)
    mismatch = PriorValidator().validate(prior, preset, req.model_profile, req.capability_registry,
                                         req.variable_routes, current_case_fingerprint="other")
    assert not mismatch.valid


def test_validator_rejects_untraceable_evidence_and_wrong_units():
    req, prior, preset = context()
    raw = prior.model_dump(mode="json")
    raw["proposals"][0]["evidence"][0]["source_id"] = "fake-source"
    forged = OptimizationPrior.model_validate(raw)
    assert any("not traceable" in item for item in validate(req, forged, preset).errors)
    bad_route = [VariableRouteBinding.model_validate({
        **req.variable_routes[0].model_dump(mode="json"), "route": "DIRECT", "mapping_id": None,
        "parameter_ids": [req.variable_routes[0].parameter_ids[0]],
    })]
    result = PriorValidator().validate(prior, preset, req.model_profile, req.capability_registry,
                                       bad_route, current_case_fingerprint=req.model_profile.case_fingerprint)
    assert not result.valid


def test_validator_and_fusion_agree_on_present_profile_evidence():
    req, prior, preset = context()
    raw = prior.model_dump(mode="json")
    raw["proposals"][0]["evidence"].append(next(
        ref.model_dump(mode="json") for ref in profile_facts(req.model_profile) if ref.source_id == "energy"))
    evidenced = OptimizationPrior.model_validate(raw)
    assert validate(req, evidenced, preset).valid
    raw["proposals"][0]["evidence"][-1]["source_id"] = "invented-field"
    assert any("not traceable" in item for item in validate(
        req, OptimizationPrior.model_validate(raw), preset,
    ).errors)


def test_freeze_revalidates_and_creates_immutable_versions(tmp_path):
    req, prior, preset = context()
    store = PriorFreezeStore(tmp_path)
    args = dict(
        prior=prior, preset=preset, profile=req.model_profile,
        registry=req.capability_registry, routes=req.variable_routes,
        approved_by="operator", approval_id="approval-a",
        current_case_fingerprint=req.model_profile.case_fingerprint,
    )
    first = store.freeze(**args)
    assert first.approval_status == "APPROVED"
    assert first.payload()["prior"]["prior_id"] == prior.prior_id
    assert store.load(first.prior_version) == first
    original_json = first.payload_json
    prior.proposals[0].reasoning_summary = "Changed after freeze"
    assert store.load(first.prior_version).payload_json == original_json
    # Replan uses a new prior and new approval, never overwrites the old file.
    revised = PriorFusion().fuse(req, [PriorProposal.model_validate(proposal(req))])
    args["prior"] = revised
    args["approval_id"] = "approval-b"
    newer = store.freeze(**args)
    assert newer.prior_version != first.prior_version
    assert store.load(first.prior_version).payload_json == original_json
    with pytest.raises(PermissionError, match="fingerprint"):
        store.freeze(**{**args, "current_case_fingerprint": "changed"})
    invalid_route = [VariableRouteBinding.model_validate({
        **req.variable_routes[0].model_dump(mode="json"), "mapping_id": "missing",
    })]
    with pytest.raises(PermissionError, match="PriorValidator"):
        store.freeze(**{**args, "routes": invalid_route})
