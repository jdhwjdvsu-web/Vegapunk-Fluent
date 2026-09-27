import asyncio
import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from vegapunk.fluent.campaign import campaign_fingerprint
from vegapunk.fluent.capability_registry import CapabilityRegistry
from vegapunk.fluent.experiment_orchestrator import (
    MappingRunnerAdapter,
    compile_resolved_experiment,
    run_resolved_optimization,
)
from vegapunk.fluent.mapping_schema import ResolvedTaskObject
from vegapunk.fluent.metric_catalog import resolve_metrics
from vegapunk.fluent.execution_approval import seal_execution

from .test_agent_v3 import fan_mapping, fan_registry, fan_task
from .test_planning import SPEC, model_signature, profile


def executable_fan_case():
    base_registry = fan_registry()
    metrics = {item.key: item for item in resolve_metrics(model_signature())}
    registry = CapabilityRegistry(profile(), base_registry.parameters, metrics)
    task = fan_task()
    objective_key = next(key for key in metrics if "product-velocity-avg" in key)
    task["objective"]["metric_key"] = objective_key
    task["solver_requirements"]["mass_balance_required"] = False
    task["termination"]["max_trials"] = 2
    task["termination"]["no_improvement_trials"] = None
    binding = {
        "variable_id": "fan_angle",
        "classification": "MAPPED_PROXY",
        "classification_reason": "LLM selected fixed-geometry incidence proxy",
        "selected_capability_id": "mapped.fan_incidence_2d.v1",
        "candidate_parameter_ids": ["flow-x-id", "flow-y-id", "flow-z-id"],
        "candidate_metric_ids": [objective_key],
        "confidence": 0.9,
        "missing_information": [],
    }
    resolved = ResolvedTaskObject.model_validate({
        "task": task,
        "variables": [{
            "intent": task["variables"][0],
            "binding": binding,
            "mapping_id": "fan-angle-proxy",
            "executable": True,
        }],
        "mappings": [fan_mapping()],
        "executable": True,
        "geometry_unsupported": [],
        "validation_errors": [],
        "capability_registry_version": registry.version,
    })
    template = json.loads(SPEC.read_text(encoding="utf-8"))
    return registry, resolved, template


class NativeRunner:
    calls = []

    def __init__(self, spec, output_dir):
        self.spec = spec
        self.output_dir = output_dir

    async def open_session(self):
        return None

    async def close_session(self):
        return None

    async def evaluate_point(self, parameters, **_kwargs):
        self.calls.append(dict(parameters))
        return {
            "status": "completed",
            "parameters": dict(parameters),
            "objective_value": float(parameters["flow-x-id"]),
            "objective_unit": "m/s",
            "elapsed_seconds": 0.01,
            "iterations_requested": 100,
            "reports": {},
            "constraints": [],
            "residuals": None,
            "baseline_reloaded": True,
        }


def test_resolved_proxy_compiles_to_semantic_and_native_specs():
    registry, resolved, template = executable_fan_case()
    experiment = compile_resolved_experiment(
        template, profile(), resolved, registry, "http://localhost:18000/mcp"
    )
    assert [item.name for item in experiment.semantic_spec.parameters] == ["fan_angle"]
    assert {item.name for item in experiment.native_spec.parameters} == {
        "flow-x-id", "flow-y-id", "flow-z-id",
    }
    assert experiment.semantic_spec.execution_contract["mappings"][0]["mapping_id"] == "fan-angle-proxy"


def test_mapping_adapter_applies_frozen_dsl_once_before_native_runner(tmp_path):
    registry, resolved, template = executable_fan_case()
    experiment = compile_resolved_experiment(
        template, profile(), resolved, registry, "http://localhost:18000/mcp"
    )
    NativeRunner.calls = []
    adapter = MappingRunnerAdapter(
        experiment.semantic_spec,
        tmp_path,
        native_spec=experiment.native_spec,
        resolved_task=resolved,
        registry=registry,
        native_runner_factory=NativeRunner,
    )
    result = asyncio.run(adapter.evaluate_point({"fan_angle": 10}, name="trial-0000"))
    assert result["parameters"] == {"fan_angle": 10}
    assert result["fluent_parameters"]["flow-x-id"] == pytest.approx(0.9396926208)
    assert result["fluent_parameters"]["flow-y-id"] == pytest.approx(0.3420201433)
    assert len(NativeRunner.calls) == 1


def test_mapping_change_changes_campaign_fingerprint():
    registry, resolved, template = executable_fan_case()
    first = compile_resolved_experiment(
        template, profile(), resolved, registry, "http://localhost:18000/mcp"
    )
    changed_raw = copy.deepcopy(resolved.model_dump(mode="json"))
    changed_raw["mappings"][0]["assumptions"].append("new approved assumption")
    changed = compile_resolved_experiment(
        template, profile(), changed_raw, registry, "http://localhost:18000/mcp"
    )
    assert campaign_fingerprint(first.semantic_spec) != campaign_fingerprint(changed.semantic_spec)


def test_job_adapter_preserves_semantic_campaign_and_separates_native_ledger(tmp_path):
    from vegapunk.fluent.campaign import load_or_create_campaign
    registry, resolved, template = executable_fan_case()
    template['connection']['job_endpoint'] = 'http://127.0.0.1:18001/mcp'
    experiment = compile_resolved_experiment(template, profile(), resolved, registry, 'http://localhost:18000/mcp')
    manifest = load_or_create_campaign(tmp_path, experiment.semantic_spec)
    adapter = MappingRunnerAdapter(experiment.semantic_spec, tmp_path,
        native_spec=experiment.native_spec, resolved_task=resolved, registry=registry)
    assert adapter.native_runner.manifest.campaign_id == manifest.campaign_id
    assert adapter.native_runner.ledger_root == tmp_path / 'native_execution'
    from vegapunk.fluent.history import TrialLedger, TrialState
    ledger = TrialLedger(adapter.native_runner.ledger_root, manifest.campaign_id, 'trial-0000')
    ledger.create({'flow-x-id': 1, 'flow-y-id': 0, 'flow-z-id': 0})
    ledger.transition(TrialState.SUBMITTED, job_id='test-job')
    ledger.transition(TrialState.RESULT_READY)
    adapter.mark_gated('trial-0000', {'passed': False})
    adapter.mark_told('trial-0000')
    assert ledger.load()['state'] == 'TOLD'
    assert ledger.load()['job_id'] == 'test-job'


def test_proxy_runs_through_existing_optuna_and_verification_with_fake_runner(tmp_path):
    registry, resolved, template = executable_fan_case()
    case = tmp_path / "baseline.cas.h5"
    case.write_bytes(b"offline fixture")
    from vegapunk.fluent.profile_store import case_sha256
    approved_profile = {**profile(), "case_file": str(case), "case_sha256": case_sha256(str(case))}
    experiment = compile_resolved_experiment(
        template, approved_profile, resolved, registry, "http://localhost:18000/mcp"
    )
    experiment = replace(experiment, execution_approval=seal_execution(
        experiment, registry, approval_id="offline-test", approved_by="tester", mode="V3_APPROVED",
    ))
    NativeRunner.calls = []
    summary = asyncio.run(run_resolved_optimization(
        experiment, tmp_path, registry, native_runner_factory=NativeRunner
    ))
    assert summary["completed_trials"] == 2
    assert summary["feasible_trials"] == 2
    assert summary["best_result"]["proxy_values"]["relative_incidence_angle"]
    assert summary["verification"]["verified"] is True
    assert all("flow-x-id" in call for call in NativeRunner.calls)
    assert (Path(tmp_path) / "campaign.json").is_file()
