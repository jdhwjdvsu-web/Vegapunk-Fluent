"""Optuna ask/tell walking skeleton for serial Fluent evaluations."""

from __future__ import annotations

import json
import math
import os
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .campaign import CampaignManifest, load_or_create_campaign
from .runner import (
    DuplicateTrialError,
    FluentExperimentRunner,
    FluentStateUncertainError,
)
from .history import TrialLedger, TrialState
from .runtime_reliability import inspect_completed_trial
from .runtime_reconciliation import reconcile_runtime, sync_finalized_ledgers
from .spec import ExperimentSpec
from .validity import evaluate_result_gate, validate_parameter_values


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    temporary.replace(path)


def _append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _has_event(path: Path, event_name: str) -> bool:
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if json.loads(line).get("event") == event_name:
                return True
        except (json.JSONDecodeError, AttributeError):
            continue
    return False


def _invalid_probe(spec: ExperimentSpec) -> dict[str, float]:
    values = {point.name: point.minimum for point in spec.parameters}
    first = spec.parameters[0]
    assert first.minimum is not None and first.maximum is not None
    values[first.name] = first.maximum + max(first.maximum - first.minimum, 1.0)
    return {name: float(value) for name, value in values.items() if value is not None}


def _sampling_phase(number: int, startup_trials: int) -> str:
    return "startup_random" if number < startup_trials else "tpe"


def _study_summary(
    study,
    output_dir: Path,
    target_trials: int,
    *,
    termination_reason: str | None = None,
) -> dict[str, Any]:
    import optuna

    counts = Counter(trial.state.name for trial in study.trials)
    completed = [
        trial
        for trial in study.trials
        if trial.state == optuna.trial.TrialState.COMPLETE
    ]
    feasible = [
        trial
        for trial in completed
        if all(
            float(value) <= 0
            for value in trial.user_attrs.get("constraint_vector", ())
        )
    ]
    trial_results = []
    for path in sorted((output_dir / "trial_results").glob("trial-*.json")):
        try:
            number = int(path.stem.removeprefix("trial-"))
            matching = next((trial for trial in study.trials if trial.number == number), None)
            if matching is None:
                continue
            complete, _, document = inspect_completed_trial(
                path, number, matching.params,
            )
            if complete and document is not None:
                document = {**document, "optuna_state": matching.state.name, "result_integrity": "COMPLETE"}
                trial_results.append(document)
        except (OSError, ValueError):
            continue
    if feasible:
        trusted_ids = {document["trial_number"] for document in trial_results
                       if document.get("gates", {}).get("status") == "PASS"
                       and document.get("gates", {}).get("passed") is True
                       and document.get("optuna_state") == "COMPLETE"}
        feasible = [trial for trial in feasible if trial.number in trusted_ids]
    if feasible:
        select = max if study.direction.name == "MAXIMIZE" else min
        best = select(feasible, key=lambda trial: float(trial.value))
    else:
        best = None
    gate_counts = Counter(
        str(document.get("gates", {}).get("status", "UNKNOWN"))
        for document in trial_results
    )
    durations = [
        float(document["duration_seconds"])
        for document in trial_results
        if document.get("duration_seconds") is not None
    ]
    solved_trials = len(trial_results)
    failed_trials = counts.get("FAIL", 0)
    if termination_reason is None and len(completed) >= target_trials:
        termination_reason = (
            "target_completed"
            if feasible
            else "budget_exhausted_without_feasible_solution"
        )
    elif termination_reason is None:
        termination_reason = "incomplete"
    return {
        "schema_version": 1,
        "study_name": study.study_name,
        "target_completed_trials": target_trials,
        "completed_trials": len(completed),
        "solved_trials": solved_trials,
        "feasible_trials": len(feasible),
        "failed_trials": failed_trials,
        "orphaned_trials": sum(trial.user_attrs.get("runtime_state") == "ORPHANED" for trial in study.trials),
        "unknown_runtime_trials": sum(trial.user_attrs.get("runtime_state") in {"UNKNOWN", "UNKNOWN_RUNTIME_STATE"} for trial in study.trials),
        "best_status": "BEST_OBSERVED" if best else "NO_TRUSTED_RESULT",
        "improvement_verified": False,
        "termination_reason": termination_reason,
        "state_counts": dict(sorted(counts.items())),
        "best_trial_number": best.number if best is not None else None,
        "best_params": dict(best.params) if best is not None else None,
        "best_value": float(best.value) if best is not None else None,
        "gate_counts": dict(sorted(gate_counts.items())),
        "mean_trial_seconds": sum(durations) / len(durations) if durations else None,
        "total_trial_seconds": sum(durations),
        "convergence_trend": [
            {
                "trial_number": document.get("trial_number"),
                "objective_value": document.get("objective_value"),
                "gate": document.get("gates", {}).get("status"),
            }
            for document in trial_results
        ],
        "trials": trial_results,
        "sqlite_path": str(output_dir / "study.sqlite3"),
        "jsonl_path": str(output_dir / "run_log.jsonl"),
        "updated_at": _utc_now(),
    }


def _optuna_constraints(trial) -> tuple[float, ...]:
    values = trial.user_attrs.get("constraint_vector", ())
    return tuple(float(value) for value in values)


def _write_optimization_summary(
    summary: dict[str, Any],
    manifest: CampaignManifest,
    output: Path,
    *,
    objective_direction: str,
) -> dict[str, Any]:
    events = []
    log_path = output / "run_log.jsonl"
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except (json.JSONDecodeError, TypeError):
                continue
    failure_events = [event for event in events if "failed" in event.get("event", "")]
    retry_events = [event for event in events if "retry" in event.get("event", "")]
    document = {
        "schema_version": 1,
        "campaign_id": manifest.campaign_id,
        "campaign_fingerprint": manifest.fingerprint,
        "parent_campaign_id": manifest.parent_campaign_id,
        "best_trial": {
            "trial_number": summary.get("best_trial_number"),
            "parameters": summary.get("best_params"),
            "objective_value": summary.get("best_value"),
        },
        "completed": summary.get("completed_trials", 0),
        "failed": len(failure_events),
        "retried": len(retry_events),
        "failure_statistics": dict(
            Counter(event.get("event", "unknown") for event in failure_events)
        ),
        "constraint_statistics": summary.get("gate_counts", {}),
        "convergence_trend": summary.get("convergence_trend", []),
        "numerical_uncertainty": None,
        "runtime": {
            "mean_trial_seconds": summary.get("mean_trial_seconds"),
            "total_trial_seconds": summary.get("total_trial_seconds"),
        },
        "updated_at": _utc_now(),
    }
    _atomic_json(output / "optimization_summary.json", document)
    final_info = {
        "fluent_optimization": {
            "means": {
                "objective_score": (
                    (
                        float(summary["best_value"])
                        if objective_direction == "maximize"
                        else -float(summary["best_value"])
                    )
                    if summary.get("best_value") is not None
                    else 0.0
                ),
                "completed_trial_rate": (
                    summary.get("completed_trials", 0)
                    / max(summary.get("target_completed_trials", 1), 1)
                ),
            },
            "campaign_id": manifest.campaign_id,
            "best_trial": document["best_trial"],
            "artifact": "optimization_summary.json",
        }
    }
    _atomic_json(output / "final_info.json", final_info)
    return document


def _recover_pending_tells(study, spec: ExperimentSpec, output: Path) -> int:
    """Finish only Optuna ``tell`` when a crash happened after Gate persistence."""

    import optuna

    reconciliation = reconcile_runtime(study, spec, output)
    if reconciliation["status"] != "CONSISTENT":
        raise FluentStateUncertainError("Unsafe pending-tell recovery: " + "; ".join(reconciliation["issues"]))

    recovered = 0
    for frozen in study.get_trials(
        deepcopy=False, states=(optuna.trial.TrialState.RUNNING,)
    ):
        result_path = output / "trial_results" / f"trial-{frozen.number:04d}.json"
        complete, reason, document = inspect_completed_trial(
            result_path, frozen.number, frozen.params,
            expected_constraints=len(spec.constraints),
        )
        if not complete or document is None:
            raise FluentStateUncertainError(
                f"RUNNING trial-{frozen.number:04d} has no complete gated result ({reason}); "
                "manual diagnosis is required before this Campaign can resume"
            )
        gate_data = document.get("gates", {})
        gate_status = gate_data.get("status") or (
            "PASS" if gate_data.get("passed") else "DIVERGED"
        )
        live_trial = optuna.trial.Trial(study, frozen._trial_id)
        live_trial.set_user_attr(
            "constraint_vector", gate_data.get("constraint_vector", [])
        )
        live_trial.set_user_attr("gate_status", gate_status)
        live_trial.set_user_attr("gate_passed", gate_data.get("passed"))
        live_trial.set_user_attr("result_file", str(result_path))
        live_trial.set_user_attr("recovery_method", "PERSISTED_GATE_TELL_ONLY")
        if gate_status in {"PASS", "CONSTRAINT"}:
            study.tell(frozen.number, float(document["objective_value"]))
        else:
            study.tell(frozen.number, state=optuna.trial.TrialState.FAIL)
        recovered += 1
    return recovered


def _block_uncertain_campaign(study, spec: ExperimentSpec, output: Path, log_path: Path) -> None:
    """Never allocate another point after an unconfirmed submitted solve."""
    import optuna

    reconciliation = reconcile_runtime(study, spec, output)
    if reconciliation["status"] == "BLOCKED":
        raise FluentStateUncertainError("; ".join(reconciliation["issues"]), diagnosis=reconciliation)

    if _has_event(log_path, "trial_state_uncertain") or _has_event(log_path, "trial_orphaned_on_recovery"):
        raise FluentStateUncertainError(
            "Campaign contains an uncertain/orphaned Trial and cannot be resumed"
        )
    for frozen in study.get_trials(deepcopy=False, states=(optuna.trial.TrialState.RUNNING,)):
        result_path = output / "trial_results" / f"trial-{frozen.number:04d}.json"
        complete, reason, _ = inspect_completed_trial(
            result_path, frozen.number, frozen.params,
            expected_constraints=len(spec.constraints),
        )
        if complete:
            continue  # _recover_pending_tells will finish only the persisted tell.
        live = optuna.trial.Trial(study, frozen._trial_id)
        live.set_user_attr("runtime_state", TrialState.ORPHANED.value)
        _append_jsonl(log_path, {
            "event": "trial_orphaned_on_recovery", "timestamp": _utc_now(),
            "trial_number": frozen.number, "reason": reason,
        })
        raise FluentStateUncertainError(
            f"RUNNING trial-{frozen.number:04d} has no complete gated result ({reason}); "
            "Campaign is blocked before any new Fluent submission"
        )


async def run_optimization(
    spec: ExperimentSpec,
    output_dir: str | Path,
    *,
    target_trials: int | None = None,
    runner_factory: Callable[..., FluentExperimentRunner] | None = None,
) -> dict[str, Any]:
    """Resume a study and run until the requested number of valid trials completes."""

    try:
        import optuna
    except ImportError as exc:
        raise RuntimeError(
            "Optuna is required for the Fluent optimization demo"
        ) from exc
    if spec.optimization is None:
        raise ValueError("the experiment spec requires an optimization block")
    optimization = spec.optimization
    target = target_trials or optimization.target_trials
    if target < 1:
        raise ValueError("target_trials must be at least one")

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = load_or_create_campaign(output, spec)
    log_path = output / "run_log.jsonl"
    if optimization.verify_invalid_parameter_gate and not _has_event(
        log_path, "parameter_rejected_preflight"
    ):
        invalid = _invalid_probe(spec)
        _, gate = validate_parameter_values(spec, invalid)
        if gate.passed:
            raise RuntimeError(
                "the deliberately invalid preflight parameter was accepted"
            )
        event = {
            "event": "parameter_rejected_preflight",
            "timestamp": _utc_now(),
            "parameters": invalid,
            "gate": gate.to_dict(),
            "mcp_called": False,
        }
        _append_jsonl(log_path, event)
        _atomic_json(output / "preflight_rejection.json", event)

    sampler = optuna.samplers.TPESampler(
        seed=optimization.sampler_seed,
        n_startup_trials=optimization.n_startup_trials,
        constraints_func=_optuna_constraints,
    )
    database_path = output / "study.sqlite3"
    study = optuna.create_study(
        study_name=optimization.study_name,
        direction=spec.objective.direction,
        sampler=sampler,
        storage=f"sqlite:///{database_path.as_posix()}",
        load_if_exists=True,
    )
    _block_uncertain_campaign(study, spec, output, log_path)
    recovered_tells = _recover_pending_tells(study, spec, output)
    sync_finalized_ledgers(study, spec, output)
    if recovered_tells:
        _append_jsonl(
            log_path,
            {
                "event": "pending_tells_recovered",
                "timestamp": _utc_now(),
                "count": recovered_tells,
            },
        )
    complete_state = optuna.trial.TrialState.COMPLETE
    completed_count = sum(trial.state == complete_state for trial in study.trials)
    if completed_count >= target:
        summary = _study_summary(study, output, target)
        _atomic_json(output / "demo_summary.json", summary)
        _write_optimization_summary(
            summary,
            manifest,
            output,
            objective_direction=spec.objective.direction,
        )
        return summary

    if runner_factory is None:
        if spec.connection.job_endpoint:
            from .controller import FluentJobController

            runner_factory = FluentJobController
        else:
            runner_factory = FluentExperimentRunner
    runner = runner_factory(spec, output)
    await runner.open_session()
    attempts = 0
    max_attempts = max(target * 2, target + 6)
    run_started = time.monotonic()
    failed_count = sum(
        trial.state == optuna.trial.TrialState.FAIL for trial in study.trials
    )
    feasible_existing = [
        trial
        for trial in study.trials
        if trial.state == complete_state
        and all(float(value) <= 0 for value in trial.user_attrs.get("constraint_vector", ()))
    ]
    best_seen = None
    if feasible_existing:
        select = max if spec.objective.direction == "maximize" else min
        best_seen = float(select(feasible_existing, key=lambda item: float(item.value)).value)
    no_improvement_count = 0
    termination_reason: str | None = None
    try:
        while completed_count < target:
            # V3 max_trials is a total candidate budget, not a requested number
            # of successful solves. Invalid results must still consume it.
            contract_task = (spec.execution_contract or {}).get('task', {})
            total_budget = contract_task.get('termination', {}).get('max_trials')
            allocated = sum(t.state != optuna.trial.TrialState.WAITING for t in study.get_trials(deepcopy=False))
            if total_budget is not None and allocated >= int(total_budget):
                termination_reason = 'max_trials_reached'
                break
            if attempts >= max_attempts:
                termination_reason = "maximum_attempts_exhausted"
                break
            if (
                optimization.max_wall_time_seconds is not None
                and time.monotonic() - run_started >= optimization.max_wall_time_seconds
            ):
                termination_reason = "max_wall_time_reached"
                break
            if (
                optimization.maximum_failed_trials is not None
                and failed_count > 0
                and failed_count >= optimization.maximum_failed_trials
            ):
                termination_reason = "maximum_failed_trials_reached"
                break
            attempts += 1
            trial = study.ask()
            trial_name = f"trial-{trial.number:04d}"
            if ((output / "trial_results" / f"{trial_name}.json").exists()
                    or (output / "generated_code" / f"{trial_name}.py").exists()):
                trial.set_user_attr("runtime_state", TrialState.ORPHANED.value)
                _append_jsonl(log_path, {
                    "event": "duplicate_trial_blocked", "timestamp": _utc_now(),
                    "trial_number": trial.number, "reason": "trial artifacts already exist",
                })
                raise DuplicateTrialError(
                    f"{trial_name} already has artifacts; refusing duplicate execution"
                )
            phase = _sampling_phase(trial.number, optimization.n_startup_trials)
            parameters = {
                parameter.name: trial.suggest_float(
                    parameter.name,
                    parameter.minimum,
                    parameter.maximum,
                    step=parameter.step,
                    log=parameter.log,
                )
                for parameter in spec.parameters
            }
            normalized, parameter_gate = validate_parameter_values(spec, parameters)
            trial.set_user_attr("sampling_phase", phase)
            if not parameter_gate.passed or normalized is None:
                study.tell(trial, state=optuna.trial.TrialState.FAIL)
                _append_jsonl(
                    log_path,
                    {
                        "event": "trial_failed_parameter_gate",
                        "timestamp": _utc_now(),
                        "trial_number": trial.number,
                        "parameters": parameters,
                        "gate": parameter_gate.to_dict(),
                    },
                )
                continue

            started_at = _utc_now()
            ledger = None
            if not spec.connection.job_endpoint:
                ledger = TrialLedger(output, manifest.campaign_id, trial_name)
                ledger.create(normalized)
                ledger.transition(TrialState.SUBMITTED)
                ledger.transition(TrialState.RUNNING)
            try:
                result = await runner.evaluate_point(
                    normalized,
                    name=trial_name,
                    artifact_stem=trial_name,
                )
                if result.get("name") not in (None, trial_name):
                    raise FluentStateUncertainError(
                        "Runner returned a result for a different Trial identity"
                    )
                reported_parameters = result.get("parameters")
                if isinstance(reported_parameters, dict) and (
                    set(reported_parameters) != set(normalized) or any(
                        not isinstance(reported_parameters[key], (int, float))
                        or not math.isclose(
                            float(reported_parameters[key]), float(normalized[key]),
                            rel_tol=0, abs_tol=1e-9,
                        ) for key in normalized
                    )
                ):
                    raise FluentStateUncertainError(
                        "Runner returned parameters for a different Trial identity"
                    )
                gate = evaluate_result_gate(spec, result)
                if gate.status in {"PASS", "CONSTRAINT"} and (
                    not isinstance(result.get("objective_value"), (int, float))
                    or not math.isfinite(float(result["objective_value"]))
                    or any(not math.isfinite(float(value)) for value in gate.constraint_vector)
                ):
                    raise FluentStateUncertainError(
                        "Gate marked an invalid objective or constraint vector as tellable"
                    )
                if hasattr(runner, "mark_gated"):
                    runner.mark_gated(trial_name, gate.to_dict())
                trial_document = {
                    "schema_version": 1,
                    "trial_id": trial_name,
                    "trial_number": trial.number,
                    "sampling_phase": phase,
                    "started_at": started_at,
                    "completed_at": _utc_now(),
                    "parameters": normalized,
                    "fluent_parameters": result.get("fluent_parameters"),
                    "objective_value": result["objective_value"],
                    "objective_unit": result.get("objective_unit"),
                    "mass_flow_in": result.get("mass_flow_in"),
                    "mass_flow_out": result.get("mass_flow_out"),
                    "mass_flow_unit": result.get("mass_flow_unit"),
                    "gates": gate.to_dict(),
                    "duration_seconds": result.get("elapsed_seconds"),
                    "iterations_requested": result.get("iterations_requested"),
                    "iterations_actual": result.get("iterations_actual"),
                    "first_iteration_number": result.get("first_iteration_number"),
                    "last_iteration_number": result.get("last_iteration_number"),
                    "iteration_count_source": result.get("iteration_count_source"),
                    "residuals": result.get("residuals"),
                    "reports": result.get("reports"),
                    "baseline_reloaded": result.get("baseline_reloaded"),
                    "state": gate.status,
                    "solve_confirmed": result.get("status") == "completed",
                    "gate_executed": True,
                    "fluent_session_id": result.get("fluent_session_id"),
                    "session_identity_status": result.get("session_identity_status", "UNAVAILABLE"),
                }
                result_path = (
                    output / "trial_results" / f"trial-{trial.number:04d}.json"
                )
                _atomic_json(result_path, trial_document)
                if ledger is not None:
                    ledger.transition(TrialState.RESULT_READY, result={
                        "trial_id": trial_name,
                        "objective_value": result.get("objective_value"),
                        "result_file": str(result_path),
                    })
                    ledger.transition(TrialState.GATED, gate=gate.to_dict())
                trial.set_user_attr("result_file", str(result_path))
                trial.set_user_attr("gate_passed", gate.passed)
                trial.set_user_attr("gate_status", gate.status)
                trial.set_user_attr("constraint_vector", list(gate.constraint_vector))
                trial.set_user_attr(
                    "duration_seconds", float(result.get("elapsed_seconds", 0.0))
                )
                if "mass_balance_relative_error" in gate.metrics:
                    trial.set_user_attr(
                        "mass_balance_relative_error",
                        gate.metrics["mass_balance_relative_error"],
                    )
                if gate.status in {"PASS", "CONSTRAINT"}:
                    study.tell(trial, float(result["objective_value"]))
                    completed_count += 1
                    event_name = (
                        "trial_completed"
                        if gate.status == "PASS"
                        else "trial_completed_with_constraint"
                    )
                else:
                    study.tell(trial, state=optuna.trial.TrialState.FAIL)
                    failed_count += 1
                    event_name = "trial_failed_by_result_gate"
                if hasattr(runner, "mark_told"):
                    runner.mark_told(trial_name)
                if ledger is not None:
                    ledger.transition(TrialState.TOLD)
                _append_jsonl(log_path, {"event": event_name, **trial_document})
                if gate.status == "PASS":
                    value = float(result["objective_value"])
                    improved = best_seen is None or (
                        value > best_seen
                        if spec.objective.direction == "maximize"
                        else value < best_seen
                    )
                    if improved:
                        best_seen = value
                        no_improvement_count = 0
                    else:
                        no_improvement_count += 1
                    target_value = optimization.target_objective
                    if target_value is not None and (
                        (spec.objective.direction == "maximize" and value >= target_value)
                        or (spec.objective.direction == "minimize" and value <= target_value)
                    ):
                        termination_reason = "target_objective_reached"
                        break
                    if (
                        optimization.no_improvement_trials is not None
                        and no_improvement_count >= optimization.no_improvement_trials
                    ):
                        termination_reason = "no_improvement_limit_reached"
                        break
            except FluentStateUncertainError as exc:
                trial.set_user_attr("runtime_state", TrialState.ORPHANED.value)
                if ledger is not None:
                    ledger.transition(
                        TrialState.ORPHANED, error=str(exc),
                        diagnosis=getattr(exc, "diagnosis", None),
                    )
                _append_jsonl(
                    log_path,
                    {
                        "event": "trial_state_uncertain",
                        "timestamp": _utc_now(),
                        "trial_number": trial.number,
                        "parameters": normalized,
                        "error": str(exc),
                        "runtime_state": TrialState.ORPHANED.value,
                        "diagnosis": getattr(exc, "diagnosis", None),
                    },
                )
                raise
            except DuplicateTrialError as exc:
                trial.set_user_attr("runtime_state", TrialState.ORPHANED.value)
                if ledger is not None:
                    ledger.transition(TrialState.ORPHANED, error=str(exc))
                _append_jsonl(log_path, {
                    "event": "duplicate_trial_blocked", "timestamp": _utc_now(),
                    "trial_number": trial.number, "error": str(exc),
                })
                raise
            except Exception as exc:  # noqa: BLE001 - one failed trial must be audited
                if ledger is not None and (ledger.load() or {}).get("state") in {
                    TrialState.RESULT_READY.value, TrialState.GATED.value,
                }:
                    _append_jsonl(log_path, {
                        "event": "pending_tell_preserved", "timestamp": _utc_now(),
                        "trial_number": trial.number, "error": str(exc),
                    })
                    raise  # Complete gated file can be checked by _recover_pending_tells.
                study.tell(trial, state=optuna.trial.TrialState.FAIL)
                failed_count += 1
                if ledger is not None:
                    ledger.transition(TrialState.FAILED, error=str(exc))
                _append_jsonl(
                    log_path,
                    {
                        "event": "trial_failed",
                        "timestamp": _utc_now(),
                        "trial_number": trial.number,
                        "parameters": normalized,
                        "error": str(exc),
                    },
                )
    finally:
        warning = await runner.close_session()
        if warning:
            _append_jsonl(
                log_path,
                {
                    "event": "disconnect_warning",
                    "timestamp": _utc_now(),
                    "warning": warning,
                },
            )
        reconcile_runtime(study, spec, output)

    final_reconciliation = reconcile_runtime(study, spec, output)
    if final_reconciliation["status"] != "CONSISTENT":
        raise FluentStateUncertainError("Final runtime evidence is inconsistent: " +
                                       "; ".join(final_reconciliation["issues"]), diagnosis=final_reconciliation)

    summary = _study_summary(
        study, output, target, termination_reason=termination_reason
    )
    _atomic_json(output / "demo_summary.json", summary)
    _write_optimization_summary(
        summary,
        manifest,
        output,
        objective_direction=spec.objective.direction,
    )
    if summary["best_trial_number"] is not None:
        best = {
            "trial_number": summary["best_trial_number"],
            "params": summary["best_params"],
            "value": summary["best_value"],
        }
        _atomic_json(output / "best_result.json", best)
    return summary
