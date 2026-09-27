"""Phase 4 LLM proposal boundary and deterministic fusion tests."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from vegapunk.fluent.pre_simulation.physics import PhysicsFeatureExtractor
from vegapunk.fluent.pre_simulation.prior import PriorFusion, PriorProposalError, RuntimePriorReasoner
from vegapunk.fluent.pre_simulation.schema import PriorReasoningRequest
from vegapunk.fluent.pre_simulation.variables import (
    FeasibleRangeResolver, VariablePlanner, VariableRouter,
)

from .test_pre_simulation_variables import intent, profile, registry


def request():
    model = profile()
    user = intent("inlet_angle", -30, 30, "deg")
    capabilities = registry(model)
    candidate = VariablePlanner().plan(user, model, capabilities)[0]
    route = VariableRouter().route(candidate, model, capabilities)
    feasible = FeasibleRangeResolver().resolve(user, candidate, route, model, capabilities)
    return PriorReasoningRequest(
        user_intent=user, model_profile=model,
        physics_features=PhysicsFeatureExtractor().extract(model),
        candidate_variables=[candidate], variable_routes=[route],
        capability_registry=capabilities, feasible_ranges=[feasible],
    )


def proposal(req, *, status="INSUFFICIENT", narrowed=False, high=False, physical=False, confidence=0.2):
    bounds = req.feasible_ranges[0].feasible_range.model_dump()
    physical_ref = req.physics_features.features["reynolds_number"].evidence[0]
    user_ref = next(ref for ref in req.feasible_ranges[0].constraint_sources if ref.source_type == "user")
    return {
        "variable": "inlet_angle", "route": "MAPPED", "current_value": 0.0,
        "unit": "deg", "feasible_range": bounds,
        "recommended_range": {"lower": -20, "upper": 20, "unit": "deg"} if narrowed else bounds,
        "high_potential_range": {"lower": -5, "upper": 5, "unit": "deg"} if high else None,
        "confidence": confidence, "evidence_status": status,
        "evidence": [physical_ref.model_dump(mode="json")] if physical else [user_ref.model_dump(mode="json")],
        "assumptions": [], "missing_information": ["heat-transfer trend not proven"],
        "reasoning_summary": "Physical interpretation remains a proposal, not a solver command.",
        "profile_version": req.model_profile.profile_version,
    }


class MockRuntime:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def generate_json(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return self.response


def reason(req, response):
    runtime = MockRuntime(response)
    result = asyncio.run(RuntimePriorReasoner(runtime).propose(req))
    return result, runtime


def test_real_runtime_interface_only_outputs_structured_proposals():
    req = request()
    result, runtime = reason(req, {"proposals": [proposal(req)]})
    assert len(result) == 1
    assert result[0].high_potential_range is None
    assert len(runtime.calls) == 1
    prompt, options = runtime.calls[0]
    assert "authoritative_current_values" in prompt
    assert "schema" in options
    assert "tools" not in options
    assert "MCP commands" in options["system_prompt"]
    assert "json" in options["system_prompt"]


def test_supported_partial_and_insufficient_fusion_rules():
    req = request()
    fusion = PriorFusion()
    supported, _ = reason(req, {"proposals": [proposal(req, status="SUPPORTED", narrowed=True,
                                                   high=True, physical=True, confidence=0.8)]})
    with pytest.raises(PriorProposalError, match="variable-objective trend"):
        fusion.fuse(req, supported)
    partial, _ = reason(req, {"proposals": [proposal(req, status="PARTIAL", narrowed=True,
                                                 physical=True, confidence=0.5)]})
    with pytest.raises(PriorProposalError, match="variable-objective trend"):
        fusion.fuse(req, partial)
    broad, _ = reason(req, {"proposals": [proposal(req, status="SUPPORTED", physical=True)]})
    assert fusion.fuse(req, broad).proposals[0].high_potential_range is None
    insufficient, _ = reason(req, {"proposals": [proposal(req, confidence=1.0)]})
    assert fusion.fuse(req, insufficient).proposals[0].recommended_range == req.feasible_ranges[0].feasible_range
    with pytest.raises(ValidationError, match="INSUFFICIENT"):
        reason(req, {"proposals": [proposal(req, narrowed=True, confidence=1.0)]})


def test_partial_cannot_assert_high_potential_or_aggressive_narrowing():
    req = request()
    high, _ = reason(req, {"proposals": [proposal(req, status="PARTIAL", narrowed=True,
                                               high=True, physical=True)]})
    with pytest.raises(PriorProposalError, match="High-potential"):
        PriorFusion().fuse(req, high)
    raw = proposal(req, status="PARTIAL", narrowed=True, physical=True)
    raw["recommended_range"] = {"lower": -5, "upper": 5, "unit": "deg"}
    narrow, _ = reason(req, {"proposals": [raw]})
    with pytest.raises(PriorProposalError, match="conservative"):
        PriorFusion().fuse(req, narrow)


def test_unknown_evidence_and_no_physics_cannot_narrow():
    req = request()
    raw = proposal(req, status="SUPPORTED", narrowed=True, physical=True)
    raw["evidence"][0]["source_id"] = "invented-paper"
    proposed, _ = reason(req, {"proposals": [raw]})
    with pytest.raises(PriorProposalError, match="unknown evidence"):
        PriorFusion().fuse(req, proposed)
    user_only, _ = reason(req, {"proposals": [proposal(req, status="PARTIAL", narrowed=True)]})
    with pytest.raises(PriorProposalError, match="physical evidence"):
        PriorFusion().fuse(req, user_only)


def test_invalid_llm_json_route_current_value_version_and_code_are_rejected():
    req = request()
    for key, value, message in (
        ("route", "DIRECT", "altered route"),
        ("current_value", 45, "current value"),
        ("profile_version", "old", "stale"),
        ("reasoning_summary", "solver.settings.solution.iterate(100)", "executable"),
    ):
        raw = proposal(req)
        raw[key] = value
        with pytest.raises(PriorProposalError, match=message):
            reason(req, {"proposals": [raw]})
    raw = proposal(req)
    raw["feasible_range"] = {"lower": -60, "upper": 60, "unit": "deg"}
    raw["recommended_range"] = {"lower": -60, "upper": 60, "unit": "deg"}
    with pytest.raises(PriorProposalError, match="deterministic feasible"):
        reason(req, {"proposals": [raw]})
    with pytest.raises(ValidationError):
        reason(req, {"not_proposals": []})


def test_future_providers_are_disabled_in_v1_even_if_supplied():
    req = request()
    data = req.model_dump(mode="json")
    data["knowledge_prior"] = [{
        "provider_id": "knowledge-1", "source_kind": "knowledge",
        "variable": "inlet_angle", "profile_version": req.model_profile.profile_version,
        "verification": "UNKNOWN", "evidence": [],
    }]
    with pytest.raises(PriorProposalError, match="not enabled"):
        asyncio.run(RuntimePriorReasoner(MockRuntime({"proposals": []})).propose(
            PriorReasoningRequest.model_validate(data)
        ))
