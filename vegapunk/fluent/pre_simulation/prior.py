"""LLM proposal boundary and deterministic V1 fusion (no provider execution)."""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any
from uuid import uuid4

from pydantic import Field

from vegapunk.mas.models.runtime import ReasoningConfig

from .schema import (
    EvidenceStatus,
    ModelProfile,
    OptimizationPrior,
    PriorProposal,
    PriorReasoningRequest,
    StrictContract,
    VariableRoute,
)
from .variables import _catalog
from .evidence import evidence_catalog, profile_evidence_ids, validate_fact_citations, validate_range_inference


FUSION_POLICY_VERSION = "2"
_EXECUTABLE_TEXT = re.compile(
    r"\b(?:import|exec|eval)\s*(?:\(|\s)|solver\.settings|"
    r"\b(?:solve|iterate|initialize|set_state)\s*\(", re.IGNORECASE,
)


class PriorProposalError(ValueError):
    pass


class PriorProposalEnvelope(StrictContract):
    proposals: list[PriorProposal] = Field(min_length=1)


def _allowed_evidence_refs(request: PriorReasoningRequest) -> set[tuple[str, str]]:
    return {(ref.source_type, ref.source_id) for ref in evidence_catalog(
        request.model_profile, request.feasible_ranges, request.capability_registry, request.physics_features,
    )}


def _current_value(request: PriorReasoningRequest, variable: str) -> float | None:
    route = next(item for item in request.variable_routes if item.variable == variable)
    catalog = _catalog(request.model_profile)
    if route.route == VariableRoute.DIRECT and len(route.parameter_ids) == 1:
        parameter = catalog.get(route.parameter_ids[0])
        return parameter.default_value if parameter else None
    if route.route == VariableRoute.MAPPED:
        groups = {}
        for key in route.parameter_ids:
            if key in catalog:
                parameter = catalog[key]
                groups.setdefault(parameter.zone, {})[parameter.rule_id] = parameter.default_value
        angles = []
        for components in groups.values():
            if not {"flow_direction_x", "flow_direction_y", "flow_direction_z"} <= components.keys():
                return None
            if abs(components["flow_direction_z"]) > 1e-9:
                return None
            angles.append(math.degrees(math.atan2(components["flow_direction_y"], components["flow_direction_x"])))
        if angles and all(math.isclose(value, angles[0], abs_tol=1e-6) for value in angles):
            return angles[0]
    return None


class RuntimePriorReasoner:
    """Calls only the configured JSON model runtime; returns no executable action."""

    def __init__(self, runtime: Any, *, model_id: str | None = None, timeout_seconds: float = 120) -> None:
        if runtime is None:
            raise PriorProposalError("A configured model runtime is required; no heuristic LLM substitute")
        self.runtime = runtime
        self.model_id = model_id
        self.timeout_seconds = timeout_seconds

    async def propose(self, request: PriorReasoningRequest) -> list[PriorProposal]:
        if any((request.knowledge_prior, request.historical_prior, request.surrogate_prior)):
            raise PriorProposalError("V1 external prior providers are not enabled")
        current_values = {item.variable: _current_value(request, item.variable)
                          for item in request.candidate_variables}
        prompt = json.dumps({
            "stage": "pre_simulation_prior_reasoner",
            "request": request.model_dump(mode="json"),
            "authoritative_current_values": current_values,
            "authoritative_evidence_facts": [ref.model_dump(mode="json") for ref in evidence_catalog(
                request.model_profile, request.feasible_ranges, request.capability_registry, request.physics_features,
            )],
            "allowed_evidence_refs": [
                {"source_type": source_type, "source_id": source_id}
                for source_type, source_id in sorted(_allowed_evidence_refs(request))
            ],
            "rules": [
                "Copy feasible_range exactly; never derive or expand it.",
                "Only propose recommended_range and optional high_potential_range.",
                "If key evidence is missing: INSUFFICIENT, recommended=feasible, high_potential=null.",
                "Explain assumptions and missing information; do not fabricate physical measurements.",
                "Copy evidence facts, verification and versions exactly; put inference only in reasoning_summary.",
                "V1 has no validated variable-objective trend: recommended=feasible, high_potential=null.",
            ],
        }, ensure_ascii=False, allow_nan=False)
        raw = await asyncio.wait_for(self.runtime.generate_json(
            prompt,
            schema=PriorProposalEnvelope.model_json_schema(),
            system_prompt=(
                "You are a physics reasoning node, not a Fluent controller. "
                "Return a json object containing PriorProposal objects only. Never emit Python, PyFluent code, "
                "MCP commands, solver settings or boundary mutations. Cite supplied evidence."
                " Evidence source_type and source_id must exactly match allowed_evidence_refs."
            ),
            model_id=self.model_id,
            reasoning=ReasoningConfig(effort="medium", context="current_turn", mode="standard"),
        ), timeout=self.timeout_seconds)
        envelope = PriorProposalEnvelope.model_validate(raw)
        expected = {item.variable for item in request.candidate_variables}
        actual = [item.variable for item in envelope.proposals]
        if len(set(actual)) != len(actual) or set(actual) != expected:
            raise PriorProposalError("LLM proposals must match candidate variables one-to-one")
        routes = {item.variable: item.route for item in request.variable_routes}
        feasible = {item.variable: item.feasible_range for item in request.feasible_ranges}
        for item in envelope.proposals:
            if item.profile_version != request.model_profile.profile_version:
                raise PriorProposalError("LLM proposal uses stale ModelProfile version")
            if item.route != routes[item.variable] or item.feasible_range != feasible[item.variable]:
                raise PriorProposalError("LLM proposal altered route or deterministic feasible range")
            value = current_values[item.variable]
            if (value is None) != (item.current_value is None) or (
                value is not None and not math.isclose(value, item.current_value, abs_tol=1e-9)
            ):
                raise PriorProposalError("LLM proposal altered authoritative current value")
            text_fields = [item.reasoning_summary, *item.assumptions, *item.missing_information,
                           *(ref.fact for ref in item.evidence)]
            if any(_EXECUTABLE_TEXT.search(text) for text in text_fields):
                raise PriorProposalError("LLM proposal contains executable instructions")
        return envelope.proposals


class PriorFusion:
    """V1 uses physical evidence, user bounds and registry/engineering rules only."""

    def fuse(self, request: PriorReasoningRequest, proposals: list[PriorProposal]) -> OptimizationPrior:
        expected = {item.variable for item in request.candidate_variables}
        actual = [item.variable for item in proposals]
        if len(set(actual)) != len(actual) or set(actual) != expected:
            raise PriorProposalError("Fusion requires one proposal per candidate")
        if any((request.knowledge_prior, request.historical_prior, request.surrogate_prior)):
            raise PriorProposalError("V1 cannot fuse external providers")
        authoritative = {item.variable: item.feasible_range for item in request.feasible_ranges}
        allowed_refs = _allowed_evidence_refs(request)
        physics_refs = {
            (ref.source_type, ref.source_id)
            for feature in request.physics_features.features.values()
            if feature.value is not None
            for ref in feature.evidence
        }
        routes = {item.variable: item.route for item in request.variable_routes}
        try:
            validate_fact_citations(proposals, evidence_catalog(
                request.model_profile, request.feasible_ranges, request.capability_registry, request.physics_features,
            ))
        except ValueError as exc:
            raise PriorProposalError(str(exc)) from exc
        for item in proposals:
            if item.profile_version != request.model_profile.profile_version:
                raise PriorProposalError("Stale proposal profile version")
            if item.feasible_range != authoritative[item.variable] or item.route != routes[item.variable]:
                raise PriorProposalError("Proposal changed authoritative range or route")
            unknown_refs = [(ref.source_type, ref.source_id) for ref in item.evidence
                            if (ref.source_type, ref.source_id) not in allowed_refs]
            if unknown_refs:
                raise PriorProposalError(f"Proposal cites unknown evidence: {unknown_refs}")
            has_physics = any((ref.source_type, ref.source_id) in physics_refs for ref in item.evidence)
            narrowed = item.recommended_range != item.feasible_range
            if narrowed and not has_physics:
                raise PriorProposalError("Range narrowing requires traceable physical evidence")
            if item.high_potential_range is not None and (
                item.evidence_status != EvidenceStatus.SUPPORTED or not has_physics
            ):
                raise PriorProposalError("High-potential range requires SUPPORTED physical evidence")
            if item.evidence_status == EvidenceStatus.SUPPORTED and not has_physics:
                raise PriorProposalError("SUPPORTED status requires traceable physical evidence")
            if item.evidence_status == EvidenceStatus.PARTIAL and narrowed:
                feasible_width = item.feasible_range.upper - item.feasible_range.lower
                recommended_width = item.recommended_range.upper - item.recommended_range.lower
                if recommended_width < 0.5 * feasible_width:
                    raise PriorProposalError("PARTIAL evidence cannot narrow beyond V1 conservative policy")
        try:
            validate_range_inference(proposals)
        except ValueError as exc:
            raise PriorProposalError(str(exc)) from exc
        return OptimizationPrior(
            prior_id="prior-" + uuid4().hex,
            profile_version=request.model_profile.profile_version,
            feasible_ranges=request.feasible_ranges,
            proposals=proposals,
            fusion_policy_version=FUSION_POLICY_VERSION,
        )
