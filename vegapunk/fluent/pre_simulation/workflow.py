"""Optional V3 → pre-simulation → approved existing-runner workflow."""

from __future__ import annotations

import math
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..capability_registry import CapabilityRegistry
from ..history import atomic_json
from ..mapping_runtime import apply_mapping, validate_mapping_spec
from ..mapping_schema import ResolvedTaskObject
from ..metric_catalog import resolve_metrics
from ..parameter_rules import resolve_parameters
from ..profile_store import case_sha256
from ..task_schema import SimulationTaskObject
from .integration import (
    CompiledPreSimulationExperiment,
    compile_frozen_experiment,
    run_frozen_optimization,
)
from .model_inspector import ModelInspector
from .physics import PhysicsFeatureExtractor
from .prior import PriorFusion, RuntimePriorReasoner
from .schema import (
    CandidateVariable,
    CapabilityRegistrySnapshot,
    ModelProfile,
    OptimizationPrior,
    ParameterFeasibleRange,
    PhysicsFeatures,
    PriorProposal,
    PriorReasoningRequest,
    PriorValidation,
    SolverPreset,
    UserIntent,
    VariableRouteBinding,
    VerificationStatus,
)
from .validation import PriorFreezeStore, PriorValidator, SolverBudgetPolicy, SolverPresetPlanner
from .variables import (
    FeasibleRangeResolver,
    PlanningContractError,
    VariablePlanner,
    VariableRouter,
    build_registered_mapping_spec,
    registry_snapshot,
)
from .execution_inputs import snapshot_execution_inputs
from ..execution_approval import digest
from .evidence import evidence_catalog, EVIDENCE_POLICY_VERSION


def intent_from_v3(task: SimulationTaskObject) -> UserIntent:
    if task.needs_information or task.information_gaps():
        raise PlanningContractError("Simulation Agent V3 task has unresolved information")
    if any(item.minimum is None or item.maximum is None or item.unit is None for item in task.variables):
        raise PlanningContractError("V3 variables require explicit hard bounds and units")
    from .schema import NumericRange

    return UserIntent(
        intent_id="v3-" + hashlib.sha256(task.research_question.encode("utf-8")).hexdigest()[:32],
        target=task.research_question[:500],
        objective=task.objective.semantic_metric,
        requested_variables=[item.id for item in task.variables],
        hard_bounds={item.id: NumericRange(lower=item.minimum, upper=item.maximum, unit=item.unit)
                     for item in task.variables},
        constraints=[item.semantic_metric for item in task.constraints],
        fixed_conditions=[item.name for item in task.fixed_conditions],
        source_text=task.research_question,
    )


def registry_for_profile(profile: ModelProfile) -> CapabilityRegistry:
    signature = {
        "observations": profile.parameters,
        "physics": profile.physical_models,
        "boundaries": profile.boundary_zones,
        "cell_zones": [*profile.fluid_zones, *profile.solid_zones],
        "reports": profile.reports,
        "solver_readbacks": profile.solver_settings,
    }
    parameters = {item.key: item for item in resolve_parameters(signature)}
    metrics = {item.key: item for item in resolve_metrics(signature)}
    return CapabilityRegistry({"signature": signature, "case_sha256": profile.case_fingerprint},
                              parameters, metrics)


def adapt_approved_incidence_task(
    resolved: ResolvedTaskObject, registry: CapabilityRegistry, profile: ModelProfile,
    *, approved_case_fingerprint: str, max_trials: int | None = None,
) -> SimulationTaskObject:
    """Bridge an already-approved V3 angle proxy to the server-owned V1 mapping.

    This only changes the variable identifier/semantic label used internally.
    The old executable mapping is validated against the current Case and checked
    against the registered replacement at representative points; it is never run.
    """
    if approved_case_fingerprint != profile.case_fingerprint:
        raise PlanningContractError("Approved V3 Case fingerprint differs from current ModelProfile")
    if (not resolved.executable or resolved.validation_errors or len(resolved.task.variables) != 1
            or len(resolved.variables) != 1 or len(resolved.mappings) != 1):
        raise PlanningContractError("Only one executable approved incidence mapping can be adapted")
    original = resolved.task.variables[0]
    binding = resolved.variables[0]
    old = resolved.mappings[0]
    if (original.id != "inletIncidenceAngle" or original.semantic_key != "relative_incidence_angle"
            or original.unit != "deg" or original.minimum is None or original.maximum is None
            or original.minimum < -180 or original.maximum > 180
            or binding.intent != original or binding.mapping_id != old.mapping_id
            or old.mapping_id != "inletIncidenceAngle.fan_incidence_2d.v1"
            or old.source_variables != [original.id] or old.context_variables
            or old.selected_capability_id != "mapped.fan_incidence_2d.v1"):
        raise PlanningContractError("Approved V3 angle semantics do not match the registered XY direction proxy")
    mapping_errors = validate_mapping_spec(old, resolved.task, registry)
    if mapping_errors:
        raise PlanningContractError(
            "Approved V3 mapping no longer validates against the current Capability Registry: "
            + "; ".join(mapping_errors)
        )
    canonical = build_registered_mapping_spec(
        "inlet_direction_angle_xy.v1", old.selected_fluent_parameter_ids, registry.parameters,
    )
    probes = {original.minimum, original.maximum, (original.minimum + original.maximum) / 2, 0.0}
    for angle in sorted(probes):
        if not original.minimum <= angle <= original.maximum:
            continue
        actual = apply_mapping(old, {original.id: angle}, {})
        expected = apply_mapping(canonical, {"inlet_angle": angle}, {})
        if actual.keys() != expected.keys() or any(
            not math.isclose(actual[key], expected[key], rel_tol=0, abs_tol=1e-9) for key in actual
        ):
            raise PlanningContractError("Approved V3 mapping differs from the registered XY direction proxy")
    raw = resolved.task.model_dump(mode="json")
    raw["variables"][0]["id"] = "inlet_angle"
    raw["variables"][0]["semantic_key"] = "wind_direction"
    raw["assumptions"].append(
        "Approved V3 inletIncidenceAngle is adapted to server-owned inlet_angle; original mapping is not executed"
    )
    if max_trials is not None:
        if not 1 <= max_trials <= resolved.task.termination.max_trials:
            raise PlanningContractError("Trial limit must not exceed the approved V3 limit")
        raw["termination"]["max_trials"] = max_trials
        raw["termination"]["maximum_failed_trials"] = min(
            raw["termination"]["maximum_failed_trials"], max_trials,
        )
    return SimulationTaskObject.model_validate(raw)


@dataclass(frozen=True)
class PendingPreSimulationPlan:
    v3_task: SimulationTaskObject
    case_file: str
    endpoint: str
    profile: ModelProfile
    user_intent: UserIntent
    physics_features: PhysicsFeatures
    candidate_variables: list[CandidateVariable]
    routes: list[VariableRouteBinding]
    feasible_ranges: list[ParameterFeasibleRange]
    proposals: list[PriorProposal]
    prior: OptimizationPrior
    solver_preset: SolverPreset
    validation: PriorValidation
    registry: CapabilityRegistry
    registry_snapshot: CapabilityRegistrySnapshot
    planning_task_sha256: str


class PreSimulationWorkflow:
    def __init__(self, root: Path, inspector: ModelInspector, runtime: Any, *, model_id: str | None = None) -> None:
        self.root = root
        self.inspector = inspector
        self.reasoner = RuntimePriorReasoner(runtime, model_id=model_id)
        self.mappings = VariableRouter().mappings
        self.freeze_store = PriorFreezeStore(root / "frozen_priors")

    async def plan(
        self, v3_task: SimulationTaskObject, case_file: str, endpoint: str,
        connect_kwargs: dict, policy: SolverBudgetPolicy,
    ) -> PendingPreSimulationPlan:
        inspected = await self.inspector.inspect(case_file, endpoint, connect_kwargs)
        profile = inspected.profile
        user_intent = intent_from_v3(v3_task)
        physics = PhysicsFeatureExtractor().extract(profile)
        existing = registry_for_profile(profile)
        snapshot = registry_snapshot(existing, profile)
        candidates = VariablePlanner().plan(user_intent, profile, snapshot)
        routes = [VariableRouter(self.mappings).route(item, profile, snapshot) for item in candidates]
        try:
            feasible = [
                FeasibleRangeResolver(self.mappings).resolve(user_intent, item, route, profile, snapshot)
                for item, route in zip(candidates, routes, strict=True)
            ]
        except PlanningContractError as exc:
            atomic_json(self.root / "pre_simulation_blocked.json", {
                "phase": "route_or_feasible_range", "reason": str(exc),
                "case_fingerprint": profile.case_fingerprint,
                "profile_version": profile.profile_version,
                "routes": [item.model_dump(mode="json") for item in routes],
            })
            raise
        request = PriorReasoningRequest(
            user_intent=user_intent, model_profile=profile, physics_features=physics,
            candidate_variables=candidates, variable_routes=routes,
            capability_registry=snapshot, feasible_ranges=feasible,
        )
        proposals = await self.reasoner.propose(request)
        prior = PriorFusion().fuse(request, proposals)
        preset = SolverPresetPlanner().plan(profile, policy)
        validation = PriorValidator(self.mappings).validate(
            prior, preset, profile, snapshot, routes,
            current_case_fingerprint=case_sha256(case_file),
        )
        pending = PendingPreSimulationPlan(
            v3_task, case_file, endpoint, profile, user_intent, physics, candidates,
            routes, feasible, proposals, prior, preset, validation, existing, snapshot,
            digest(v3_task.model_dump(mode="json")),
        )
        atomic_json(self.root / "planning_audit.json", {
            "case_fingerprint": profile.case_fingerprint,
            "model_profile": profile.model_dump(mode="json"),
            "inspection_mode": inspected.mode.value,
            "user_intent": user_intent.model_dump(mode="json"),
            "physics_features": physics.model_dump(mode="json"),
            "evidence_policy_version": EVIDENCE_POLICY_VERSION,
            "authoritative_evidence_facts": [ref.model_dump(mode="json") for ref in evidence_catalog(
                profile, feasible, snapshot, physics,
            )],
            "candidate_variables": [item.model_dump(mode="json") for item in candidates],
            "variable_routes": [item.model_dump(mode="json") for item in routes],
            "feasible_ranges": [item.model_dump(mode="json") for item in feasible],
            "prior_proposals": [item.model_dump(mode="json") for item in proposals],
            "optimization_prior": prior.model_dump(mode="json"),
            "solver_preset": preset.model_dump(mode="json"),
            "validation": validation.model_dump(mode="json"),
            "approval_status": "PENDING",
        })
        return pending

    async def approve_and_compile(
        self, pending: PendingPreSimulationPlan, *, approved_by: str, approval_id: str,
        template: Mapping[str, Any], connect_kwargs: dict,
    ) -> CompiledPreSimulationExperiment:
        if not pending.validation.valid:
            raise PermissionError("PriorValidator did not pass; approval is blocked")
        if digest(pending.v3_task.model_dump(mode="json")) != pending.planning_task_sha256:
            raise PermissionError("Task changed after physics planning; replan is required")
        current = await self.inspector.inspect(pending.case_file, pending.endpoint, connect_kwargs)
        if current.profile.profile_version != pending.profile.profile_version:
            raise PermissionError("ModelProfile changed after planning; replan is required")
        fingerprint = case_sha256(pending.case_file)
        frozen = self.freeze_store.freeze(
            pending.prior, pending.solver_preset, pending.profile,
            pending.registry_snapshot, pending.routes,
            approved_by=approved_by, approval_id=approval_id,
            current_case_fingerprint=fingerprint,
            execution_inputs=snapshot_execution_inputs(
                pending.v3_task, template, pending.endpoint, pending.case_file,
            ),
        )
        compiled = compile_frozen_experiment(
            frozen, pending.profile, pending.case_file, pending.v3_task,
            template, pending.registry, pending.endpoint,
        )
        atomic_json(self.root / "approval_audit.json", {
            "frozen_prior_version": frozen.prior_version,
            "frozen_prior_sha256": frozen.payload_sha256,
            "approval_id": approval_id,
            "approved_by": approved_by,
            "approval_status": frozen.approval_status,
            "optimization_task": compiled.task.model_dump(mode="json"),
            "case_fingerprint": fingerprint,
        })
        return compiled

    async def run_and_analyze(
        self, pending: PendingPreSimulationPlan,
        compiled: CompiledPreSimulationExperiment,
        output_dir: Path,
        *, native_runner_factory=None,
    ) -> dict:
        return await self.run_compiled(
            compiled, output_dir, pending.registry,
            native_runner_factory=native_runner_factory,
        )

    async def run_compiled(
        self, compiled: CompiledPreSimulationExperiment, output_dir: Path,
        registry: CapabilityRegistry, *, native_runner_factory=None,
    ) -> dict:
        """Resume a frozen task without another LLM call or mutable re-approval."""
        try:
            summary = await run_frozen_optimization(
                compiled, output_dir, registry,
                native_runner_factory=native_runner_factory,
            )
        except Exception as exc:
            atomic_json(self.root / "execution_failure_audit.json", {
                "frozen_prior_version": compiled.frozen_prior_version,
                "case_fingerprint": compiled.task.case_fingerprint,
                "error_type": type(exc).__name__,
                "reason": str(exc),
                "execution_status": "REAL_FLUENT_EXECUTION_UNCONFIRMED",
            })
            raise
        real_fluent = native_runner_factory is None and bool(summary.get("solved_trials"))
        analysis = analyze_result(
            summary, compiled.task.prior, compiled.experiment.semantic_spec.objective.direction,
            real_fluent_executed=real_fluent,
        )
        atomic_json(self.root / "result_analysis.json", analysis)
        atomic_json(self.root / "final_audit.json", {
            "planning_audit": str(self.root / "planning_audit.json"),
            "approval_audit": str(self.root / "approval_audit.json"),
            "frozen_prior_version": compiled.frozen_prior_version,
            "trial_history": summary.get("trials", []),
            "gate_counts": summary.get("gate_counts", {}),
            "best_trial": summary.get("best_result"),
            "evaluated_parameter_span": analysis["evaluated_parameter_span"],
            "best_observed_parameter": analysis["best_observed_parameter"],
            "best_observed_objective": analysis["best_observed_objective"],
            "replan_history": [],
            "result_analysis": analysis,
        })
        return analysis


def analyze_result(
    summary: Mapping[str, Any], prior: OptimizationPrior, direction: str,
    *, real_fluent_executed: bool,
) -> dict:
    trials = [item for item in summary.get("trials", []) if isinstance(item, dict)]
    if direction not in {"minimize", "maximize"}:
        raise ValueError("Unknown objective direction")
    def finite(value):
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
    feasible = [item for item in trials
                if isinstance(item.get("gates"), dict) and isinstance(item.get("parameters"), dict)
                and item["gates"].get("status") == "PASS" and item["gates"].get("passed") is True
                and item.get("optuna_state") == "COMPLETE" and item.get("result_integrity") == "COMPLETE"
                and item.get("solve_confirmed") is True and item.get("gate_executed") is True
                and item.get("baseline_reloaded") is True and finite(item.get("objective_value"))
                and isinstance(item.get("trial_number"), int) and not isinstance(item.get("trial_number"), bool)
                and item.get("trial_id") == f"trial-{item['trial_number']:04d}"
                and all(finite(item.get("parameters", {}).get(p.variable)) for p in prior.proposals)]
    feasible = sorted(feasible, key=lambda item: item["objective_value"], reverse=direction == "maximize")
    best_document = feasible[0] if feasible else None
    verification = summary.get("verification") or {}
    from ..verification import verification_is_trusted
    verified = bool(best_document and verification_is_trusted(
        verification, best_document["parameters"], best_document["objective_value"]))
    spans = []
    if real_fluent_executed and feasible:
        for proposal in prior.proposals:
            values = [float(item["parameters"][proposal.variable]) for item in feasible]
            spans.append({"variable": proposal.variable,
                          "span": {"lower": min(values), "upper": max(values), "unit": proposal.unit},
                          "completed_trial_ids": [item["trial_id"] for item in feasible],
                          "sample_count": len(values), "not_optimal_region": True})
    sensitivity = None
    if len(feasible) >= 5:
        sensitivity = {}
        targets = [float(item["objective_value"]) for item in feasible]
        target_mean = sum(targets) / len(targets)
        target_spread = sum((value - target_mean) ** 2 for value in targets)
        for proposal in prior.proposals:
            values = [item.get("parameters", {}).get(proposal.variable) for item in feasible]
            if any(not isinstance(value, (int, float)) for value in values):
                continue
            numbers = [float(value) for value in values]
            mean = sum(numbers) / len(numbers)
            spread = sum((value - mean) ** 2 for value in numbers)
            if spread > 0 and target_spread > 0:
                sensitivity[proposal.variable] = {
                    "method": "sample_pearson_correlation_not_causal",
                    "value": sum((x - mean) * (y - target_mean) for x, y in zip(numbers, targets, strict=True))
                    / math.sqrt(spread * target_spread),
                    "sample_count": len(numbers),
                }
    decision = "FINISH" if real_fluent_executed and verified else "REVIEW" if real_fluent_executed else "OFFLINE_ONLY"
    best_parameters = best_document["parameters"] if best_document else None
    best_objective = best_document["objective_value"] if best_document else None
    return {
        "execution_status": "REAL_FLUENT_EXECUTED" if real_fluent_executed else "REAL_FLUENT_NOT_EXECUTED",
        "decision": decision,
        "best_trial": {"trial_id": best_document["trial_id"], "parameters": best_parameters,
                       "objective_value": best_objective} if best_document else None,
        "best_parameters": best_parameters,
        "objective_value": best_objective,
        "best_observed_parameter": best_parameters,
        "best_observed_objective": best_objective,
        "best_trial_id": best_document["trial_id"] if best_document else None,
        "best_status": "OFFLINE_ONLY" if not real_fluent_executed else "BEST_VERIFIED" if verified else "BEST_OBSERVED" if best_document else "NO_TRUSTED_RESULT",
        "best_gate_status": best_document.get("gates", {}).get("status") if best_document else None,
        "best_residuals": best_document.get("residuals") if best_document else None,
        "parameter_sensitivity": sensitivity,
        "completed_trials": summary.get("completed_trials"),
        "failed_trials": summary.get("failed_trials"),
        "gate_counts": summary.get("gate_counts"),
        "verification": summary.get("verification"),
        "prior_predicted_regions": [
            {"variable": item.variable,
             "recommended_range": item.recommended_range.model_dump(mode="json"),
             "high_potential_range": item.high_potential_range.model_dump(mode="json")
             if item.high_potential_range else None}
            for item in prior.proposals
        ],
        "evaluated_parameter_span": spans,
        "observed_optimal_region": None,  # Deprecated read compatibility, never generated by new logic.
        "deprecated_fields": ["observed_optimal_region"],
        "prior_vs_result": {
            "benefit_status": "NOT_DEMONSTRATED",
            "reason": "Frozen-domain execution does not establish prior efficiency or excluded-region quality",
            "exclusion_of_better_region": "NOT_ASSESSABLE",
            "confidence_calibration": "NOT_ASSESSABLE",
            "baseline_comparison": "NOT_EXECUTED",
        },
        "uncertainty": [
            {"variable": item.variable, "evidence_status": item.evidence_status.value,
             "missing_information": item.missing_information}
            for item in prior.proposals
        ],
    }
