"""Controlled 100 W Phase 7R validation; each stage must be invoked separately.

This is a validation harness, not a new Runner or optimizer. It never resumes
the historical uncertain Campaign and never executes a later stage on failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import optuna

from vegapunk.fluent.experiment_orchestrator import MappingRunnerAdapter, ResolvedExperiment
from vegapunk.fluent.history import atomic_json
from vegapunk.fluent.mapping_schema import ResolvedTaskObject
from vegapunk.fluent.optimizer import run_optimization
from vegapunk.fluent.runner import DuplicateTrialError, FluentExperimentRunner
from vegapunk.fluent.pre_simulation.integration import (
    CompiledPreSimulationExperiment, compile_frozen_experiment, run_frozen_optimization,
)
from vegapunk.fluent.pre_simulation.model_inspector import ModelInspector
from vegapunk.fluent.pre_simulation.validation import PriorFreezeStore
from vegapunk.fluent.pre_simulation.workflow import (
    adapt_approved_incidence_task, analyze_result, registry_for_profile,
)
from vegapunk.fluent.profile_store import case_sha256
from vegapunk.fluent.runtime_reliability import _fluent_process_alive, inspect_completed_trial
from vegapunk.fluent.validity import evaluate_result_gate


REPOSITORY = Path(__file__).resolve().parents[2]
ORIGINAL_ROOT = REPOSITORY / "runs" / "pre_simulation_phase7_20260924"
ORIGINAL_CAMPAIGN = ORIGINAL_ROOT / "real_100w_2trial"
APPROVAL_ROOT = (
    REPOSITORY / "runs" / "ui_100w_zero_tech_acceptance_20260920"
    / "web_tasks" / "task-20260920T063828830254Z"
)
CASE_FILE = REPOSITORY / "runs" / "pseudo_zero_20260916" / "zero_pseudo_final.cas.h5"
ENDPOINT = "http://127.0.0.1:18000/mcp"
FROZEN_VERSION = "frozen-146175f7812e4b2bba9f19e033565d1a"
OLD_UNCERTAIN_ANGLE = -7.527592869158251


def _new_stage_directory(root: Path, name: str) -> Path:
    target = root / name
    if target.exists():
        raise FileExistsError(f"{target} already exists; a stage never resumes an old execution")
    target.mkdir(parents=True)
    return target


def _assert_old_campaign_is_quarantined() -> None:
    log = ORIGINAL_CAMPAIGN / "run_log.jsonl"
    if not log.is_file() or not any(
        json.loads(line).get("event") == "trial_state_uncertain"
        for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ):
        raise RuntimeError("Historical uncertain Campaign audit is unavailable")
    if list((ORIGINAL_CAMPAIGN / "trial_results").glob("trial-0000.json")):
        raise RuntimeError("Historical uncertain Trial unexpectedly has a result; review manually")


async def _prepare(root: Path, *, max_trials: int, solve_timeout: int,
                   run_id: str, step: str) -> tuple[CompiledPreSimulationExperiment, object]:
    _assert_old_campaign_is_quarantined()
    if _fluent_process_alive() != "DEAD":
        raise RuntimeError("Fluent process state is not confirmed DEAD; refusing a new Trial")
    inspector = ModelInspector(ORIGINAL_ROOT)
    inspected = await inspector.inspect(str(CASE_FILE), ENDPOINT, {})
    profile = inspected.profile
    if profile.case_fingerprint != case_sha256(str(CASE_FILE)):
        raise RuntimeError("Case fingerprint changed during fresh preflight")
    approved = json.loads((APPROVAL_ROOT / "plan_approval.json").read_text(encoding="utf-8"))
    resolved = ResolvedTaskObject.model_validate(approved["payload"]["agent_v3"]["resolved_task"])
    registry = registry_for_profile(profile)
    task = adapt_approved_incidence_task(
        resolved, registry, profile,
        approved_case_fingerprint=approved["payload"]["model"]["case_sha256"],
        max_trials=max_trials,
    )
    archived = json.loads((APPROVAL_ROOT / "campaign.json").read_text(encoding="utf-8"))
    template = archived["payload"]
    template["connection"] = {
        "connect_kwargs": template["connection_scientific_settings"]["connect_kwargs"],
        "client_timeout_seconds": 300,
        "tool_timeout_seconds": 60,
        "connect_timeout_seconds": 300,
        "solve_timeout_seconds": solve_timeout,
    }
    frozen = PriorFreezeStore(ORIGINAL_ROOT / "frozen_priors").load(FROZEN_VERSION)
    compiled = compile_frozen_experiment(
        frozen, profile, str(CASE_FILE), task, template, registry, ENDPOINT,
    )
    scientific = compiled.experiment
    campaign_name = f"VegapunkFluentPhase7R{run_id}"
    sampler_seed = int(run_id[:8], 16)
    semantic = replace(
        scientific.semantic_spec, task_name=campaign_name,
        optimization=replace(scientific.semantic_spec.optimization, sampler_seed=sampler_seed),
    )
    native = replace(
        scientific.native_spec, task_name=campaign_name,
        optimization=replace(scientific.native_spec.optimization, sampler_seed=sampler_seed),
    )
    compiled = replace(compiled, experiment=ResolvedExperiment(semantic, native, scientific.resolved_task))
    atomic_json(root / f"fresh_preflight_{step}.json", {
        "case_fingerprint": profile.case_fingerprint,
        "profile_version": profile.profile_version,
        "inspection_mode": inspected.mode.value,
        "frozen_prior_version": frozen.prior_version,
        "prior_validation": compiled.task.prior_validation.model_dump(mode="json"),
        "source_approval_fingerprint": approved["fingerprint"],
        "old_campaign": str(ORIGINAL_CAMPAIGN),
        "old_campaign_resume_allowed": False,
        "fluent_process_before_execution": "DEAD",
        "mcp_session_before_execution": "DEAD",
        "run_id": run_id,
        "solve_timeout_seconds": solve_timeout,
        "max_trials": max_trials,
    })
    return compiled, registry


async def _fixed(root: Path, compiled: CompiledPreSimulationExperiment, registry,
                 run_id: str) -> None:
    output = _new_stage_directory(root, "step1_fixed")
    experiment = compiled.experiment
    adapter = MappingRunnerAdapter(
        experiment.semantic_spec, output, native_spec=experiment.native_spec,
        resolved_task=experiment.resolved_task, registry=registry,
    )
    name = f"fixed-{run_id}"
    await adapter.open_session()
    try:
        result = await adapter.evaluate_point({"inlet_angle": 0.0}, name=name, artifact_stem=name)
        if result.get("name") != name or result.get("baseline_reloaded") is not True:
            raise RuntimeError("Fixed design result identity or baseline reload is unconfirmed")
        gate = evaluate_result_gate(experiment.semantic_spec, result)
        document = {
            "trial_id": name, "status": "COMPLETE" if gate.status == "PASS" else "GATE_FAILED",
            "semantic_parameters": {"inlet_angle": 0.0},
            "fluent_parameters": result.get("fluent_parameters"),
            "objective_value": result.get("objective_value"),
            "objective_unit": result.get("objective_unit"),
            "constraints": result.get("constraints"),
            "gate": gate.to_dict(),
            "iterations_actual": result.get("iterations_actual"),
            "residuals": result.get("residuals"),
            "reports": result.get("reports"),
            "baseline_reloaded": result.get("baseline_reloaded"),
        }
        atomic_json(output / "fixed_result.json", document)
        atomic_json(root / "step1_status.json", {
            "status": document["status"], "trial_id": name,
            "result_file": str(output / "fixed_result.json"),
            "gate_status": gate.status,
        })
        if gate.status != "PASS":
            raise RuntimeError(f"Fixed design Gate did not PASS: {gate.status}")
    finally:
        warning = await adapter.close_session()
        if warning:
            atomic_json(output / "disconnect_warning.json", {"warning": warning})


def _require_stage(root: Path, name: str) -> None:
    path = root / name
    if not path.is_file() or json.loads(path.read_text(encoding="utf-8")).get("status") != "COMPLETE":
        raise RuntimeError(f"{name} is not COMPLETE; later real stages are forbidden")


async def _one_trial(root: Path, compiled: CompiledPreSimulationExperiment, registry) -> None:
    _require_stage(root, "step1_status.json")
    output = _new_stage_directory(root, "step2_optuna_one")
    experiment = compiled.experiment

    class GuardedAdapter(MappingRunnerAdapter):
        async def evaluate_point(self, parameters, *, name, artifact_stem=None):
            if math.isclose(float(parameters["inlet_angle"]), OLD_UNCERTAIN_ANGLE, abs_tol=1e-9):
                raise DuplicateTrialError("Historical uncertain angle must not be resubmitted")
            return await super().evaluate_point(parameters, name=name, artifact_stem=artifact_stem)

    def factory(spec, path):
        return GuardedAdapter(
            spec, path, native_spec=experiment.native_spec,
            resolved_task=experiment.resolved_task, registry=registry,
        )

    await run_optimization(
        experiment.semantic_spec, output, target_trials=1, runner_factory=factory,
    )
    _verify_one_trial(root, compiled)


def _verify_one_trial(root: Path, compiled: CompiledPreSimulationExperiment) -> None:
    """Reconcile a finished Step 2 from durable artifacts without another solve."""
    output = root / "step2_optuna_one"
    experiment = compiled.experiment
    trials = optuna.load_study(
        study_name=experiment.semantic_spec.optimization.study_name,
        storage=f"sqlite:///{(output / 'study.sqlite3').as_posix()}",
    ).trials
    if len(trials) != 1 or trials[0].state != optuna.trial.TrialState.COMPLETE:
        raise RuntimeError("One-Trial Optuna state did not reach COMPLETE")
    number = trials[0].number
    complete, reason, document = inspect_completed_trial(
        output / "trial_results" / f"trial-{number:04d}.json",
        number, trials[0].params, expected_constraints=len(experiment.semantic_spec.constraints),
    )
    if not complete or document is None or document["gates"]["status"] != "PASS":
        raise RuntimeError(f"One-Trial result is not complete and gated: {reason}")
    campaign = json.loads((output / "campaign.json").read_text(encoding="utf-8"))
    atomic_json(root / "step2_status.json", {
        "status": "COMPLETE", "campaign_id": campaign["campaign_id"],
        "trial_id": f"trial-{number:04d}", "trial_state": "COMPLETE",
        "result_file": str(output / "trial_results" / f"trial-{number:04d}.json"),
        "gate_status": document["gates"]["status"],
        "objective_value": document["objective_value"],
        "fluent_parameters": document.get("fluent_parameters"),
        "reconciled_from_durable_artifacts": True,
    })


async def _campaign(root: Path, compiled: CompiledPreSimulationExperiment, registry) -> None:
    _require_stage(root, "step2_status.json")
    output = _new_stage_directory(root, "step3_campaign")
    class GuardedNativeRunner(FluentExperimentRunner):
        async def evaluate_point(self, parameters, *, name, artifact_stem=None):
            x = next(float(value) for key, value in parameters.items() if "flow_direction_x" in key)
            y = next(float(value) for key, value in parameters.items() if "flow_direction_y" in key)
            angle = math.degrees(math.atan2(y, x))
            if math.isclose(angle, OLD_UNCERTAIN_ANGLE, abs_tol=1e-9):
                raise DuplicateTrialError("Historical uncertain angle must not be resubmitted")
            return await super().evaluate_point(parameters, name=name, artifact_stem=artifact_stem)

    summary = await run_frozen_optimization(
        compiled, output, registry, native_runner_factory=GuardedNativeRunner,
    )
    analysis = analyze_result(
        summary, compiled.task.prior,
        compiled.experiment.semantic_spec.objective.direction,
        real_fluent_executed=summary.get("solved_trials", 0) > 0,
    )
    atomic_json(root / "step3_analysis.json", analysis)
    atomic_json(root / "step3_status.json", {
        "status": "COMPLETE" if summary.get("completed_trials", 0) >= 2 else "INCOMPLETE",
        "campaign_id": summary.get("campaign_id"),
        "completed_trials": summary.get("completed_trials"),
        "failed_trials": summary.get("failed_trials"),
        "gate_counts": summary.get("gate_counts"),
        "observed_optimal_region": analysis.get("observed_optimal_region"),
    })
    if summary.get("completed_trials", 0) < 2:
        raise RuntimeError("Small Campaign has fewer than two COMPLETE Trials")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=("fixed", "optuna-one", "reconcile-optuna-one", "campaign"))
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--run-id", default=uuid4().hex[:12])
    parser.add_argument("--solve-timeout", type=int, default=1800)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{8,16}", args.run_id):
        raise ValueError("run-id must be 8–16 lowercase hexadecimal characters")
    if not 300 <= args.solve_timeout <= 86400:
        raise ValueError("solve timeout must be 300–86400 seconds")
    root = args.run_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    max_trials = 2 if args.step == "campaign" else 1
    compiled, registry = await _prepare(
        root, max_trials=max_trials, solve_timeout=args.solve_timeout,
        run_id=args.run_id, step=args.step,
    )
    try:
        if args.step == "fixed":
            await _fixed(root, compiled, registry, args.run_id)
        elif args.step == "optuna-one":
            await _one_trial(root, compiled, registry)
        elif args.step == "reconcile-optuna-one":
            _require_stage(root, "step1_status.json")
            _verify_one_trial(root, compiled)
        else:
            await _campaign(root, compiled, registry)
    except Exception as exc:
        atomic_json(root / f"{args.step}_failure.json", {
            "step": args.step, "error_type": type(exc).__name__, "reason": str(exc),
            "real_fluent_loop_confirmed": False,
        })
        raise


if __name__ == "__main__":
    asyncio.run(main())
