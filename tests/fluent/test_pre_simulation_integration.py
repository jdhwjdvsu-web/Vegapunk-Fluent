"""Phase 6 thin integration tests; the native Runner is mocked, never Fluent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from vegapunk.fluent.capability_registry import CapabilityRegistry
from vegapunk.fluent.codegen import build_design_point_code
from vegapunk.fluent.metric_catalog import resolve_metrics
from vegapunk.fluent.parameter_rules import resolve_parameters
from vegapunk.fluent.pre_simulation.integration import (
    FrozenTaskError, compile_frozen_experiment, run_frozen_optimization,
)
from vegapunk.fluent.pre_simulation.execution_inputs import snapshot_execution_inputs
from vegapunk.fluent.pre_simulation.physics import PhysicsFeatureExtractor
from vegapunk.fluent.pre_simulation.prior import PriorFusion
from vegapunk.fluent.pre_simulation.schema import PriorProposal, PriorReasoningRequest
from vegapunk.fluent.pre_simulation.validation import (
    PriorFreezeStore, SolverBudgetPolicy, SolverPresetPlanner,
)
from vegapunk.fluent.pre_simulation.variables import (
    FeasibleRangeResolver, VariablePlanner, VariableRouter, registry_snapshot,
)
from vegapunk.fluent.profile_store import case_sha256
from vegapunk.fluent.spec import ExperimentSpec, SpecError
from vegapunk.fluent.task_schema import SimulationTaskObject

from .test_pre_simulation_prior import proposal as proposal_data
from .test_pre_simulation_variables import intent, profile
from .test_planning import SPEC


def context(tmp_path):
    case_file = tmp_path / "small.cas.h5"
    case_file.write_bytes(b"offline compilation fixture; not a real Fluent Case")
    model = profile()
    model.case_fingerprint = case_sha256(str(case_file))
    model.dimension = 3
    model.fluent_version = "2025 R1"
    model.product_version = "25.1.0"
    model.boundary_zones = [
        {"name": "inlet", "type": "velocity_inlet"},
        {"name": "outlet", "type": "pressure_outlet"},
    ]
    model.fluid_zones = [{"name": "air", "type": "fluid"}]
    signature = {
        "observations": model.parameters,
        "physics": model.physical_models,
        "boundaries": model.boundary_zones,
        "cell_zones": model.fluid_zones,
    }
    parameters = {item.key: item for item in resolve_parameters(signature)}
    metrics = {item.key: item for item in resolve_metrics(signature)}
    existing = CapabilityRegistry({"signature": signature}, parameters, metrics)
    snap = registry_snapshot(existing, model)
    user = intent("inlet_angle", -30, 30, "deg")
    candidates = VariablePlanner().plan(user, model, snap)
    routes = [VariableRouter().route(candidates[0], model, snap)]
    ranges = [FeasibleRangeResolver().resolve(user, candidates[0], routes[0], model, snap)]
    req = PriorReasoningRequest(
        user_intent=user, model_profile=model,
        physics_features=PhysicsFeatureExtractor().extract(model),
        candidate_variables=candidates, variable_routes=routes,
        capability_registry=snap, feasible_ranges=ranges,
    )
    prior = PriorFusion().fuse(req, [PriorProposal.model_validate(proposal_data(req))])
    preset = SolverPresetPlanner().plan(model, SolverBudgetPolicy(
        policy_id="offline-budget-v1", min_iterations=10, initial_budget=20,
        hard_limit=30, check_interval=8, residual_thresholds={},
    ))
    objective = next(item for item in metrics.values() if item.kind == "volume" and "temperature-max" in item.key)
    task = SimulationTaskObject.model_validate({
        "research_question": "Optimize inlet angle for heat-sink cooling",
        "variables": [{
            "id": "inlet_angle", "name": "Inlet direction angle",
            "semantic_key": "wind_direction", "minimum": -30, "maximum": 30,
            "initial_value": 0, "unit": "deg",
        }],
        "objective": {
            "semantic_metric": "Maximum air temperature", "metric_key": objective.key,
            "direction": "minimize", "aggregation": "volume_max",
            "location": "air", "unit": "K",
        },
        "constraints": [],
        "solver_requirements": {"iterations": 30, "mass_balance_required": False},
        "termination": {"max_trials": 2, "max_wall_time_seconds": 300,
                        "maximum_failed_trials": 2},
        "assumptions": ["XY Cartesian; +X=0 deg; counter-clockwise positive"],
    })
    template = json.loads(SPEC.read_text(encoding="utf-8"))
    frozen = PriorFreezeStore(tmp_path / "frozen").freeze(
        prior, preset, model, snap, routes,
        approved_by="offline-test", approval_id="approval-offline",
        current_case_fingerprint=model.case_fingerprint,
        execution_inputs=snapshot_execution_inputs(task, template, "http://127.0.0.1:18000/mcp", str(case_file)),
    )
    return case_file, model, existing, frozen, task, template


class MockNativeRunner:
    calls = []

    def __init__(self, spec, output_dir):
        self.spec = spec
        self.output_dir = output_dir

    async def open_session(self):
        return None

    async def close_session(self):
        return None

    async def evaluate_point(self, parameters, **kwargs):
        self.calls.append(dict(parameters))
        x = next(value for key, value in parameters.items() if "direction_x" in key)
        return {
            "status": "completed", "parameters": dict(parameters),
            "objective_value": float(x), "objective_unit": "K",
            "elapsed_seconds": 0.01, "iterations_requested": 30,
            "reports": {}, "constraints": [], "residuals": None,
            "baseline_reloaded": True,
        }


def test_frozen_prior_compiles_through_existing_v3_contract_and_fixed_mapping(tmp_path):
    case_file, model, existing, frozen, task, template = context(tmp_path)
    compiled = compile_frozen_experiment(frozen, model, str(case_file), task,
                                         template, existing, "http://127.0.0.1:18000/mcp")
    semantic = compiled.experiment.semantic_spec
    native = compiled.experiment.native_spec
    assert [item.name for item in semantic.parameters] == ["inlet_angle"]
    assert semantic.parameters[0].minimum == -30
    assert semantic.parameters[0].maximum == 30
    assert len(native.parameters) == 3
    assert semantic.solver.iterations == 30
    assert native.solver.iteration_chunk_size == 8
    assert semantic.execution_contract["pre_simulation"]["frozen_prior_version"] == frozen.prior_version
    code = build_design_point_code(native, native.design_points[0])
    assert "[8, 8, 8, 6]" in code
    assert "iterate(iter_count=__vp_chunk_size)" in code
    assert compiled.experiment.resolved_task.mappings[0].mapping_id == "inlet_direction_angle_xy.v1"


def test_existing_optuna_ask_tell_and_runner_adapter_are_reused_with_mock(tmp_path):
    case_file, model, existing, frozen, task, template = context(tmp_path)
    compiled = compile_frozen_experiment(frozen, model, str(case_file), task,
                                         template, existing, "http://127.0.0.1:18000/mcp")
    MockNativeRunner.calls = []
    summary = asyncio.run(run_frozen_optimization(
        compiled, tmp_path / "campaign", existing, native_runner_factory=MockNativeRunner,
    ))
    assert summary["completed_trials"] == 2
    assert summary["feasible_trials"] == 2
    assert summary["verification"]["verified"] is True
    assert len(MockNativeRunner.calls) >= 2
    assert (tmp_path / "campaign" / "study.sqlite3").is_file()
    assert (tmp_path / "campaign" / "campaign.json").is_file()


def test_invalid_frozen_prior_profile_case_and_early_stop_are_rejected(tmp_path):
    case_file, model, existing, frozen, task, template = context(tmp_path)
    compile_args = (frozen, model, str(case_file), task, template, existing, "http://127.0.0.1:18000/mcp")
    stale = model.model_copy(update={"profile_version": "old"})
    with pytest.raises(FrozenTaskError, match="ModelProfile version"):
        compile_frozen_experiment(frozen, stale, str(case_file), task, template,
                                  existing, "http://127.0.0.1:18000/mcp")
    case_file.write_bytes(b"changed Case")
    with pytest.raises(FrozenTaskError, match="fingerprint"):
        compile_frozen_experiment(*compile_args)


def test_legacy_spec_path_remains_unchanged_when_chunking_absent(monkeypatch):
    monkeypatch.setenv("FLUENT_CASE_FILE", "C:/offline/legacy.cas.h5")
    raw = json.loads(SPEC.read_text(encoding="utf-8"))
    existing = ExperimentSpec.from_dict(raw)
    assert existing.solver.iteration_chunk_size is None
    assert "iteration_chunk_size" not in existing.to_dict()["solver"]
    bad = existing.to_dict()
    bad["solver"]["iteration_chunk_size"] = existing.solver.iterations + 1
    with pytest.raises(SpecError, match="iteration_chunk_size"):
        ExperimentSpec.from_dict(bad)
