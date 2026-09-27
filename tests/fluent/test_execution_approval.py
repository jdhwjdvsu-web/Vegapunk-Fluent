"""P0 full-experiment approval: all failures occur before Runner creation."""

from __future__ import annotations

import asyncio
import json
import hashlib
from dataclasses import replace

import pytest

from vegapunk.fluent.execution_approval import (
    canonical_json, digest, require_execution_approval, seal_execution,
)
from vegapunk.fluent.experiment_orchestrator import run_resolved_optimization
from vegapunk.fluent.history import atomic_json
from vegapunk.fluent.pre_simulation.execution_inputs import snapshot_execution_inputs
from vegapunk.fluent.pre_simulation.integration import compile_frozen_experiment
from vegapunk.fluent.pre_simulation.model_inspector import ModelInspector
from vegapunk.fluent.pre_simulation.workflow import PreSimulationWorkflow
from vegapunk.fluent.task_schema import SimulationTaskObject, SolverRequirements

from .test_pre_simulation_integration import context
from .test_pre_simulation_workflow import MissingEvidenceRuntime, ReadOnlyFixtureSource, budget
from .test_thermal_guard import guard


ENDPOINT = "http://127.0.0.1:18000/mcp"


def compiled_context(tmp_path):
    case, model, registry, frozen, task, template = context(tmp_path)
    compiled = compile_frozen_experiment(frozen, model, str(case), task, template, registry, ENDPOINT)
    return compiled.experiment, registry


class NeverRunner:
    def __init__(self, *_args, **_kwargs):
        raise AssertionError("Rejected approval must not create a Runner")


def test_compilation_does_not_replace_explicit_execution_approval(tmp_path):
    experiment, registry = compiled_context(tmp_path)
    unapproved = replace(experiment, execution_approval=None)
    with pytest.raises(PermissionError, match="approval is required"):
        asyncio.run(run_resolved_optimization(
            unapproved, tmp_path / "run", registry, native_runner_factory=NeverRunner,
        ))
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("change", [
    "objective", "budget", "gate", "mapping", "native_parameter", "mode_marker",
])
def test_compiled_contract_mutation_is_rejected_before_runner(tmp_path, change):
    experiment, registry = compiled_context(tmp_path)
    if change == "objective":
        experiment = replace(experiment, semantic_spec=replace(
            experiment.semantic_spec,
            objective=replace(experiment.semantic_spec.objective, direction="maximize"),
        ))
    elif change == "budget":
        experiment.resolved_task.task.termination.max_trials = 1
    elif change == "gate":
        experiment = replace(experiment, native_spec=replace(
            experiment.native_spec, solver=replace(experiment.native_spec.solver,
                                                 residual_thresholds={"continuity": 0.1}),
        ))
    elif change == "mapping":
        experiment.resolved_task.mappings[0].assumptions.append("changed after approval")
    elif change == "native_parameter":
        first, *rest = experiment.native_spec.parameters
        experiment = replace(experiment, native_spec=replace(
            experiment.native_spec, parameters=(replace(first, maximum=0.5), *rest),
        ))
    else:
        raw = dict(experiment.semantic_spec.execution_contract)
        raw.pop("pre_simulation")
        experiment = replace(experiment, semantic_spec=replace(
            experiment.semantic_spec, execution_contract=raw,
        ))
    with pytest.raises(PermissionError, match="changed"):
        asyncio.run(run_resolved_optimization(
            experiment, tmp_path / "run", registry, native_runner_factory=NeverRunner,
        ))
    assert not (tmp_path / "run").exists()


@pytest.mark.parametrize("change", ["objective", "termination", "solver", "bounds", "template", "endpoint", "fixed_condition", "constraint"])
def test_frozen_approval_binds_all_task_and_template_inputs(tmp_path, change):
    case, model, registry, frozen, task, template = context(tmp_path)
    raw = task.model_dump(mode="json")
    endpoint = ENDPOINT
    if change == "objective":
        raw["objective"]["direction"] = "maximize"
    elif change == "termination":
        raw["termination"]["max_wall_time_seconds"] = 600
    elif change == "solver":
        raw["solver_requirements"]["iterations"] = 31
    elif change == "bounds":
        raw["variables"][0]["maximum"] = 20
    elif change == "template":
        template["optimization"]["sampler_seed"] = 999
    elif change == "fixed_condition":
        raw["fixed_conditions"] = [{
            "id": "ambient", "name": "Ambient temperature", "value": 300,
            "unit": "K", "source": "user", "verification": "documented_assumption",
        }]
    elif change == "constraint":
        raw["constraints"] = [{
            "semantic_metric": "Maximum temperature", "metric_key": raw["objective"]["metric_key"],
            "operator": "<=", "value": 350, "unit": "K",
        }]
    else:
        endpoint = "http://127.0.0.1:18001/mcp"
    changed_task = SimulationTaskObject.model_validate(raw)
    with pytest.raises(PermissionError, match="inputs changed"):
        compile_frozen_experiment(frozen, model, str(case), changed_task, template, registry, endpoint)


def test_legacy_prior_only_record_remains_readable_but_is_not_execution_authority(tmp_path):
    case, model, registry, frozen, task, template = context(tmp_path)
    payload = frozen.payload()
    payload.pop("execution_inputs")
    payload["approval_scope"] = "PRIOR_ONLY"
    legacy = replace(frozen, payload_json=canonical_json(payload), payload_sha256=digest(payload))
    assert legacy.payload()["prior"]["prior_id"]
    with pytest.raises(PermissionError, match="prior-only"):
        compile_frozen_experiment(legacy, model, str(case), task, template, registry, ENDPOINT)


def test_baseline_drift_and_mismatched_mode_are_rejected(tmp_path):
    experiment, registry = compiled_context(tmp_path)
    with pytest.raises(PermissionError, match="mode"):
        seal_execution(experiment, registry, approval_id="new", approved_by="operator", mode="V3_APPROVED")
    with pytest.raises(PermissionError, match="differs"):
        seal_execution(experiment, registry, approval_id="new", approved_by="operator", mode="FROZEN_PRIOR")
    case = experiment.native_spec.connection.connect_kwargs["case_file_name"]
    from pathlib import Path
    Path(case).write_bytes(b"changed after compilation")
    with pytest.raises(PermissionError, match="Case changed"):
        require_execution_approval(experiment, registry)


def test_campaign_approval_artifact_cannot_be_overwritten(tmp_path):
    experiment, registry = compiled_context(tmp_path)
    output = tmp_path / "run"
    previous = experiment.execution_approval.model_copy(update={"approved_by": "other approver"})
    atomic_json(output / "execution_approval.json", previous.model_dump(mode="json"))
    with pytest.raises(PermissionError, match="Campaign execution approval changed"):
        asyncio.run(run_resolved_optimization(
            experiment, output, registry, native_runner_factory=NeverRunner,
        ))
    assert json.loads((output / "execution_approval.json").read_text(encoding="utf-8"))["approved_by"] == "other approver"


def test_thermal_snapshot_binds_paired_data(tmp_path):
    case, _, _, _, task, template = context(tmp_path)
    data = tmp_path / "small.dat.h5"
    data.write_bytes(b"approved data")
    thermal = {**guard(), "data_file": "paired-case-data", "data_sha256": hashlib.sha256(b"approved data").hexdigest()}
    task.solver_requirements = SolverRequirements(
        initialization="none", iterations=30, mass_balance_required=False,
        residual_thresholds={"continuity": 0.001}, thermal_guard=thermal,
    )
    snapshot = snapshot_execution_inputs(task, template, ENDPOINT, str(case))
    assert snapshot.data_file == str(data)
    assert snapshot.data_fingerprint == thermal["data_sha256"]
    data.write_bytes(b"changed data")
    with pytest.raises(PermissionError, match="Data fingerprint"):
        snapshot_execution_inputs(task, template, ENDPOINT, str(case))


def test_task_changed_after_planning_requires_replan_before_approval(tmp_path):
    case, model, _, _, task, template = context(tmp_path)
    workflow = PreSimulationWorkflow(
        tmp_path / "workflow", ModelInspector(tmp_path / "profile", source=ReadOnlyFixtureSource(model)),
        MissingEvidenceRuntime(),
    )

    async def scenario():
        pending = await workflow.plan(task, str(case), ENDPOINT, {}, budget())
        pending.v3_task.objective.direction = "maximize"
        with pytest.raises(PermissionError, match="replan"):
            await workflow.approve_and_compile(
                pending, approved_by="operator", approval_id="approval", template=template, connect_kwargs={},
            )

    asyncio.run(scenario())
    assert not list(workflow.freeze_store.root.glob("*.json"))
