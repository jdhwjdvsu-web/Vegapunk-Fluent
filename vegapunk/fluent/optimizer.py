"""Optuna ask/tell walking skeleton for serial Fluent evaluations."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .campaign import CampaignManifest, load_or_create_campaign
from .runner import (
    FluentExperimentRunner,
    FluentStateUncertainError,
)
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


def _study_summary(study, output_dir: Path, target_trials: int) -> dict[str, Any]:
    import optuna

    counts = Counter(trial.state.name for trial in study.trials)
    completed = [
        trial
        for trial in study.trials
        if trial.state == optuna.trial.TrialState.COMPLETE
    ]
    trial_results = []
    for path in sorted((output_dir / "trial_results").glob("trial-*.json")):
        try:
            trial_results.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    best = study.best_trial if completed else None
    gate_counts = Counter(
        str(document.get("gates", {}).get("status", "UNKNOWN"))
        for document in trial_results
    )
    durations = [
        float(document["duration_seconds"])
        for document in trial_results
        if document.get("duration_seconds") is not None
    ]
    return {
        "schema_version": 1,
        "study_name": study.study_name,
        "target_completed_trials": target_trials,
        "completed_trials": len(completed),
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

    recovered = 0
    for frozen in study.get_trials(
        deepcopy=False, states=(optuna.trial.TrialState.RUNNING,)
    ):
        result_path = output / "trial_results" / f"trial-{frozen.number:04d}.json"
        if not result_path.exists():
            continue
        document = json.loads(result_path.read_text(encoding="utf-8"))
        gate_data = document.get("gates", {})
        gate_status = gate_data.get("status") or (
            "PASS" if gate_data.get("passed") else "DIVERGED"
        )
        live_trial = optuna.trial.Trial(study, frozen._trial_id)
        live_trial.set_user_attr(
            "constraint_vector", gate_data.get("constraint_vector", [])
        )
        if gate_status in {"PASS", "CONSTRAINT"}:
            study.tell(frozen.number, float(document["objective_value"]))
        else:
            study.tell(frozen.number, state=optuna.trial.TrialState.FAIL)
        recovered += 1
    return recovered


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
    recovered_tells = _recover_pending_tells(study, spec, output)
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
    try:
        while completed_count < target:
            if attempts >= max_attempts:
                raise RuntimeError(
                    f"stopped after {attempts} attempts with {completed_count} completed trials"
                )
            attempts += 1
            trial = study.ask()
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
            try:
                result = await runner.evaluate_point(
                    normalized,
                    name=f"trial-{trial.number:04d}",
                    artifact_stem=f"trial-{trial.number:04d}",
                )
                gate = evaluate_result_gate(spec, result)
                if hasattr(runner, "mark_gated"):
                    runner.mark_gated(f"trial-{trial.number:04d}", gate.to_dict())
                trial_document = {
                    "schema_version": 1,
                    "trial_number": trial.number,
                    "sampling_phase": phase,
                    "started_at": started_at,
                    "completed_at": _utc_now(),
                    "parameters": normalized,
                    "objective_value": result["objective_value"],
                    "objective_unit": result.get("objective_unit"),
                    "mass_flow_in": result.get("mass_flow_in"),
                    "mass_flow_out": result.get("mass_flow_out"),
                    "mass_flow_unit": result.get("mass_flow_unit"),
                    "gates": gate.to_dict(),
                    "duration_seconds": result.get("elapsed_seconds"),
                    "iterations_requested": result.get("iterations_requested"),
                    "residuals": result.get("residuals"),
                    "reports": result.get("reports"),
                    "baseline_reloaded": result.get("baseline_reloaded"),
                    "state": gate.status,
                }
                result_path = (
                    output / "trial_results" / f"trial-{trial.number:04d}.json"
                )
                _atomic_json(result_path, trial_document)
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
                    event_name = "trial_failed_by_result_gate"
                if hasattr(runner, "mark_told"):
                    runner.mark_told(f"trial-{trial.number:04d}")
                _append_jsonl(log_path, {"event": event_name, **trial_document})
            except FluentStateUncertainError as exc:
                study.tell(trial, state=optuna.trial.TrialState.FAIL)
                _append_jsonl(
                    log_path,
                    {
                        "event": "trial_state_uncertain",
                        "timestamp": _utc_now(),
                        "trial_number": trial.number,
                        "parameters": normalized,
                        "error": str(exc),
                    },
                )
                raise
            except Exception as exc:  # noqa: BLE001 - one failed trial must be audited
                study.tell(trial, state=optuna.trial.TrialState.FAIL)
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

    summary = _study_summary(study, output, target)
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
