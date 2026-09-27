"""Phase 7 offline E2E and post-run analysis; no real Fluent invocation."""

from __future__ import annotations

import asyncio
import json

import pytest

from vegapunk.fluent.pre_simulation.model_inspector import ModelInspector, StateConsistencySnapshot
from vegapunk.fluent.pre_simulation.validation import SolverBudgetPolicy
from vegapunk.fluent.pre_simulation.variables import PlanningContractError
from vegapunk.fluent.pre_simulation.workflow import (
    PreSimulationWorkflow, adapt_approved_incidence_task, analyze_result,
)
from vegapunk.fluent.pre_simulation.integration import compile_frozen_experiment
from vegapunk.fluent.mapping_schema import ResolvedTaskObject

from .test_pre_simulation_integration import MockNativeRunner, context


class ReadOnlyFixtureSource:
    def __init__(self, model):
        self.model = model
        self.full_calls = 0

    async def check_state(self, case_file, endpoint, connect_kwargs):
        return StateConsistencySnapshot(
            case_fingerprint=self.model.case_fingerprint,
            structural_digest={"case_file": self.model.case_fingerprint},
            local_digest=self.model.case_fingerprint,
        )

    async def read_full(self, case_file, endpoint, connect_kwargs, audit_dir):
        self.full_calls += 1
        return {
            "solver_type": "pressure-based",
            "dimension": self.model.dimension,
            "fluent_version": self.model.fluent_version,
            "product_version": self.model.product_version,
            "physics": self.model.physical_models,
            "materials": self.model.materials,
            "boundaries": self.model.boundary_zones,
            "cell_zones": [*self.model.fluid_zones, *self.model.solid_zones],
            "observations": self.model.parameters,
            "reports": self.model.reports,
            "solver_readbacks": self.model.solver_settings,
            "warnings": [],
        }


class MissingEvidenceRuntime:
    def __init__(self):
        self.calls = 0

    async def generate_json(self, prompt, **kwargs):
        self.calls += 1
        request = json.loads(prompt)["request"]
        feasible = request["feasible_ranges"][0]
        user_ref = next(item for item in feasible["constraint_sources"] if item["source_type"] == "user")
        return {"proposals": [{
            "variable": "inlet_angle", "route": "MAPPED", "current_value": 0,
            "unit": "deg", "feasible_range": feasible["feasible_range"],
            "recommended_range": feasible["feasible_range"], "high_potential_range": None,
            "confidence": 0.2, "evidence_status": "INSUFFICIENT",
            "evidence": [user_ref], "assumptions": [],
            "missing_information": ["verified heat-transfer trend"],
            "reasoning_summary": "No supported range reduction before CFD.",
            "profile_version": request["model_profile"]["profile_version"],
        }]}


def budget():
    return SolverBudgetPolicy(
        policy_id="offline-verified-budget", min_iterations=10,
        initial_budget=20, hard_limit=30, check_interval=8,
        residual_thresholds={},
    )


def test_offline_complete_plan_approval_mock_runner_and_audit(tmp_path):
    case_file, model, _, _, v3_task, template = context(tmp_path)
    source = ReadOnlyFixtureSource(model)
    runtime = MissingEvidenceRuntime()
    workflow = PreSimulationWorkflow(
        tmp_path / "workflow", ModelInspector(tmp_path / "inspector", source=source), runtime,
    )

    async def scenario():
        pending = await workflow.plan(v3_task, str(case_file), "http://127.0.0.1:18000/mcp", {}, budget())
        assert pending.validation.valid
        assert pending.feasible_ranges[0].feasible_range.lower == -30
        assert pending.proposals[0].high_potential_range is None
        compiled = await workflow.approve_and_compile(
            pending, approved_by="offline-test", approval_id="approval-1",
            template=template, connect_kwargs={},
        )
        assert compiled.task.prior_validation.valid
        analysis = await workflow.run_and_analyze(
            pending, compiled, tmp_path / "campaign", native_runner_factory=MockNativeRunner,
        )
        return pending, compiled, analysis

    pending, compiled, analysis = asyncio.run(scenario())
    assert runtime.calls == 1
    assert source.full_calls == 1
    assert analysis["execution_status"] == "REAL_FLUENT_NOT_EXECUTED"
    assert analysis["observed_optimal_region"] is None
    assert analysis["best_parameters"] is not None
    assert (tmp_path / "workflow" / "planning_audit.json").is_file()
    assert (tmp_path / "workflow" / "approval_audit.json").is_file()
    final_audit = json.loads((tmp_path / "workflow" / "final_audit.json").read_text(encoding="utf-8"))
    assert final_audit["frozen_prior_version"] == compiled.frozen_prior_version
    assert len(final_audit["trial_history"]) >= 2
    assert "observed_optimal_region" not in final_audit
    assert final_audit["evaluated_parameter_span"] == []


def test_unverified_geometry_or_missing_mapping_blocks_before_llm(tmp_path):
    case_file, model, _, _, v3_task, _ = context(tmp_path)
    model.parameters = [item for item in model.parameters if not item["rule_id"].startswith("flow_direction_")]
    runtime = MissingEvidenceRuntime()
    workflow = PreSimulationWorkflow(
        tmp_path / "blocked", ModelInspector(tmp_path / "inspector", source=ReadOnlyFixtureSource(model)), runtime,
    )
    with pytest.raises(PlanningContractError, match="unverified"):
        asyncio.run(workflow.plan(v3_task, str(case_file), "http://127.0.0.1:18000/mcp", {}, budget()))
    assert runtime.calls == 0
    blocked = json.loads((tmp_path / "blocked" / "pre_simulation_blocked.json").read_text(encoding="utf-8"))
    assert blocked["phase"] == "route_or_feasible_range"


def test_observed_region_requires_real_completed_feasible_trials():
    from .test_pre_simulation_prior import request, proposal
    from vegapunk.fluent.pre_simulation.prior import PriorFusion
    from vegapunk.fluent.pre_simulation.schema import PriorProposal

    req = request()
    prior = PriorFusion().fuse(req, [PriorProposal.model_validate(proposal(req))])
    summary = {
        "best_trial_number": 0, "best_params": {"inlet_angle": -10.0},
        "best_value": 300.0, "verification": {"verified": True,
            "parameters": {"inlet_angle": -10.0}, "reference_objective": 300.0,
            "gate": {"passed": True, "status": "PASS"}, "status": "verified",
            "baseline_reloaded": True, "identity_valid": True, "verification_objective": 300.0,
            "absolute_difference": 0.0, "allowed_difference": 0.1,
            "verification_contract_sha256": "a" * 64},
        "completed_trials": 2, "failed_trials": 0, "gate_counts": {"PASS": 2},
        "trials": [
            {"trial_number": 0, "parameters": {"inlet_angle": -10.0},
             "objective_value": 300.0, "gates": {"status": "PASS"}},
            {"trial_number": 1, "parameters": {"inlet_angle": 10.0},
             "objective_value": 302.0, "gates": {"status": "PASS"}},
        ],
    }
    for item in summary["trials"]:
        item.update(trial_id=f"trial-{item['trial_number']:04d}", optuna_state="COMPLETE",
                    result_integrity="COMPLETE", solve_confirmed=True,
                    gate_executed=True, baseline_reloaded=True)
        item["gates"]["passed"] = True
    offline = analyze_result(summary, prior, "minimize", real_fluent_executed=False)
    assert offline["observed_optimal_region"] is None
    assert offline["decision"] == "OFFLINE_ONLY"
    # Pure result-analysis contract test; no claim that these inputs came from Fluent.
    post_run = analyze_result(summary, prior, "minimize", real_fluent_executed=True)
    assert post_run["observed_optimal_region"] is None
    assert post_run["evaluated_parameter_span"][0]["span"] == {
        "lower": -10.0, "upper": 10.0, "unit": "deg",
    }
    assert post_run["prior_predicted_regions"][0]["recommended_range"] == {
        "lower": -30.0, "upper": 30.0, "unit": "deg",
    }
    assert post_run["decision"] == "FINISH"
    assert post_run["best_status"] == "BEST_VERIFIED"
    assert post_run["prior_vs_result"]["benefit_status"] == "NOT_DEMONSTRATED"


def test_approved_legacy_incidence_alias_requires_equivalent_mapping_and_case(tmp_path):
    case_file, model, registry, frozen, task, template = context(tmp_path)
    compiled = compile_frozen_experiment(
        frozen, model, str(case_file), task, template, registry, "http://127.0.0.1:18000/mcp",
    )
    raw = compiled.experiment.resolved_task.model_dump(mode="json")
    raw["task"]["variables"][0]["id"] = "inletIncidenceAngle"
    raw["task"]["variables"][0]["semantic_key"] = "relative_incidence_angle"
    raw["variables"][0]["intent"] = raw["task"]["variables"][0]
    raw["variables"][0]["binding"]["variable_id"] = "inletIncidenceAngle"
    raw["variables"][0]["mapping_id"] = "inletIncidenceAngle.fan_incidence_2d.v1"
    mapping = raw["mappings"][0]
    mapping["mapping_id"] = "inletIncidenceAngle.fan_incidence_2d.v1"
    mapping["source_variables"] = ["inletIncidenceAngle"]
    mapping["source_units"] = {"inletIncidenceAngle": "deg"}
    mapping["output_units"]["inletIncidenceAngle"] = mapping["output_units"].pop("inlet_angle")
    mapping["forward_expression"]["inlet_angle_proxy"]["ref"] = "inletIncidenceAngle"
    mapping["reverse_expression"] = {"inletIncidenceAngle": mapping["reverse_expression"].pop("inlet_angle")}
    mapping["verification_route"] = "MANUAL_GEOMETRY"
    resolved = ResolvedTaskObject.model_validate(raw)
    adapted = adapt_approved_incidence_task(
        resolved, registry, model, approved_case_fingerprint=model.case_fingerprint, max_trials=1,
    )
    assert adapted.variables[0].id == "inlet_angle"
    assert adapted.termination.max_trials == 1
    with pytest.raises(PlanningContractError, match="fingerprint"):
        adapt_approved_incidence_task(resolved, registry, model, approved_case_fingerprint="wrong")
    raw["mappings"][0]["forward_expression"]["inlet_angle_proxy"]["value"] = 10.0
    raw["mappings"][0]["forward_expression"]["inlet_angle_proxy"]["ref"] = None
    altered = ResolvedTaskObject.model_validate(raw)
    with pytest.raises(PlanningContractError, match="mapping"):
        adapt_approved_incidence_task(
            altered, registry, model, approved_case_fingerprint=model.case_fingerprint,
        )
