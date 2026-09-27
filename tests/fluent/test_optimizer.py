import asyncio
import json
from dataclasses import replace
from typing import ClassVar

from vegapunk.fluent.optimizer import run_optimization
from vegapunk.fluent.spec import ConstraintSpec

from .test_validity import optimization_spec


class FakeRunner:
    calls: ClassVar[list[dict[str, float]]] = []

    def __init__(self, spec, output_dir):
        self.spec = spec
        self.output_dir = output_dir

    async def open_session(self):
        return None

    async def evaluate_point(self, parameters, *, name, artifact_stem=None):
        self.calls.append(dict(parameters))
        value = parameters["velocity"]
        return {
            "status": "completed",
            "parameters": dict(parameters),
            "objective_value": value,
            "objective_unit": "K",
            "mass_flow_in": 0.004,
            "mass_flow_out": 0.004000001,
            "mass_flow_unit": "kg/s",
            "elapsed_seconds": 0.01,
            "iterations_requested": 5,
            "residuals": None,
            "reports": {},
            "baseline_reloaded": True,
        }

    async def close_session(self):
        return None


class ConstraintRunner(FakeRunner):
    mode: ClassVar[str] = "all_infeasible"

    async def evaluate_point(self, parameters, *, name, artifact_stem=None):
        result = await super().evaluate_point(parameters, name=name, artifact_stem=artifact_stem)
        infeasible = self.mode == "all_infeasible" or (
            self.mode == "mixed" and float(parameters["velocity"]) < 1.0
        )
        result["constraints"] = [{
            "report": "engineering-limit",
            "operator": "<=",
            "limit": 0.0,
            "actual": 1.0 if infeasible else 0.0,
            "passed": not infeasible,
        }]
        return result


class ConstantRunner(FakeRunner):
    async def evaluate_point(self, parameters, *, name, artifact_stem=None):
        result = await super().evaluate_point(parameters, name=name, artifact_stem=artifact_stem)
        result["objective_value"] = 10.0
        return result


class FailedRunner(FakeRunner):
    async def evaluate_point(self, parameters, *, name, artifact_stem=None):
        self.calls.append(dict(parameters))
        raise RuntimeError("fake solver failure")


def constrained_spec():
    return replace(optimization_spec(), constraints=(ConstraintSpec(
        report="engineering-limit", operator="<=", value=0.0),))


def test_optuna_study_resumes_and_preserves_startup_then_tpe(tmp_path):
    FakeRunner.calls = []
    spec = optimization_spec()
    first = asyncio.run(
        run_optimization(spec, tmp_path, target_trials=2, runner_factory=FakeRunner)
    )
    assert first["completed_trials"] == 2
    resumed = asyncio.run(
        run_optimization(spec, tmp_path, target_trials=4, runner_factory=FakeRunner)
    )
    assert resumed["completed_trials"] == 4
    assert len(FakeRunner.calls) == 4
    phases = [trial["sampling_phase"] for trial in resumed["trials"]]
    assert phases == ["startup_random", "startup_random", "tpe", "tpe"]
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "run_log.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events.count("parameter_rejected_preflight") == 1
    assert events.count("trial_completed") == 4
    assert (tmp_path / "study.sqlite3").exists()
    assert (tmp_path / "campaign.json").exists()
    optimization_summary = json.loads(
        (tmp_path / "optimization_summary.json").read_text(encoding="utf-8")
    )
    assert optimization_summary["completed"] == 4
    assert optimization_summary["campaign_fingerprint"]
    assert (tmp_path / "final_info.json").exists()


def test_all_constraint_trials_finish_without_best_or_verification_candidate(tmp_path):
    ConstraintRunner.calls = []
    ConstraintRunner.mode = "all_infeasible"
    summary = asyncio.run(
        run_optimization(
            constrained_spec(), tmp_path, target_trials=3, runner_factory=ConstraintRunner
        )
    )
    assert summary["solved_trials"] == 3
    assert summary["completed_trials"] == 3
    assert summary["feasible_trials"] == 0
    assert summary["failed_trials"] == 0
    assert summary["best_trial_number"] is None
    assert summary["best_params"] is None
    assert summary["termination_reason"] == "budget_exhausted_without_feasible_solution"
    assert not (tmp_path / "best_result.json").exists()


def test_partial_feasibility_selects_only_feasible_best(tmp_path):
    ConstraintRunner.calls = []
    ConstraintRunner.mode = "mixed"
    summary = asyncio.run(
        run_optimization(
            constrained_spec(), tmp_path, target_trials=6, runner_factory=ConstraintRunner
        )
    )
    assert 0 < summary["feasible_trials"] < summary["completed_trials"]
    assert summary["best_params"]["velocity"] >= 1.0
    assert summary["termination_reason"] == "target_completed"


def test_resume_after_all_infeasible_remains_stable(tmp_path):
    ConstraintRunner.calls = []
    ConstraintRunner.mode = "all_infeasible"
    spec = constrained_spec()
    first = asyncio.run(
        run_optimization(
            spec, tmp_path, target_trials=2, runner_factory=ConstraintRunner
        )
    )
    resumed = asyncio.run(
        run_optimization(
            spec, tmp_path, target_trials=2, runner_factory=ConstraintRunner
        )
    )
    assert resumed["completed_trials"] == first["completed_trials"] == 2
    assert resumed["feasible_trials"] == 0
    assert resumed["best_trial_number"] is None
    assert resumed["termination_reason"] == "budget_exhausted_without_feasible_solution"


def test_target_objective_is_deterministic_early_stop(tmp_path):
    base = optimization_spec()
    spec = replace(
        base,
        optimization=replace(base.optimization, target_trials=10, target_objective=1000.0),
    )
    summary = asyncio.run(run_optimization(spec, tmp_path, runner_factory=FakeRunner))
    assert summary["completed_trials"] == 1
    assert summary["termination_reason"] == "target_objective_reached"


def test_no_improvement_and_failure_limits_are_deterministic(tmp_path):
    base = optimization_spec()
    plateau = replace(
        base,
        optimization=replace(base.optimization, target_trials=10, no_improvement_trials=2),
    )
    summary = asyncio.run(run_optimization(plateau, tmp_path / "plateau", runner_factory=ConstantRunner))
    assert summary["completed_trials"] == 3
    assert summary["termination_reason"] == "no_improvement_limit_reached"

    failed = replace(
        base,
        optimization=replace(base.optimization, target_trials=10, maximum_failed_trials=1),
    )
    summary = asyncio.run(run_optimization(failed, tmp_path / "failed", runner_factory=FailedRunner))
    assert summary["failed_trials"] == 1
    assert summary["termination_reason"] == "maximum_failed_trials_reached"
