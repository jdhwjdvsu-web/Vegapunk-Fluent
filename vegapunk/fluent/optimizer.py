"""Optuna ask/tell walking skeleton for serial Fluent evaluations."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    return {
        "schema_version": 1,
        "study_name": study.study_name,
        "target_completed_trials": target_trials,
        "completed_trials": len(completed),
        "state_counts": dict(sorted(counts.items())),
        "best_trial_number": best.number if best is not None else None,
        "best_params": dict(best.params) if best is not None else None,
        "best_value": float(best.value) if best is not None else None,
        "trials": trial_results,
        "sqlite_path": str(output_dir / "study.sqlite3"),
        "jsonl_path": str(output_dir / "run_log.jsonl"),
        "updated_at": _utc_now(),
    }


async def run_optimization(
    spec: ExperimentSpec,
    output_dir: str | Path,
    *,
    target_trials: int | None = None,
    runner_factory: Callable[..., FluentExperimentRunner] = FluentExperimentRunner,
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
    )
    database_path = output / "study.sqlite3"
    study = optuna.create_study(
        study_name=optimization.study_name,
        direction=spec.objective.direction,
        sampler=sampler,
        storage=f"sqlite:///{database_path.as_posix()}",
        load_if_exists=True,
    )
    complete_state = optuna.trial.TrialState.COMPLETE
    completed_count = sum(trial.state == complete_state for trial in study.trials)
    if completed_count >= target:
        summary = _study_summary(study, output, target)
        _atomic_json(output / "demo_summary.json", summary)
        return summary

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
                    "state": "COMPLETE" if gate.passed else "PRUNED",
                }
                result_path = (
                    output / "trial_results" / f"trial-{trial.number:04d}.json"
                )
                _atomic_json(result_path, trial_document)
                trial.set_user_attr("result_file", str(result_path))
                trial.set_user_attr("gate_passed", gate.passed)
                trial.set_user_attr(
                    "duration_seconds", float(result.get("elapsed_seconds", 0.0))
                )
                if "mass_balance_relative_error" in gate.metrics:
                    trial.set_user_attr(
                        "mass_balance_relative_error",
                        gate.metrics["mass_balance_relative_error"],
                    )
                if gate.passed:
                    study.tell(trial, float(result["objective_value"]))
                    completed_count += 1
                    event_name = "trial_completed"
                else:
                    study.tell(trial, state=optuna.trial.TrialState.PRUNED)
                    event_name = "trial_pruned_by_result_gate"
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
    if summary["best_trial_number"] is not None:
        best = {
            "trial_number": summary["best_trial_number"],
            "params": summary["best_params"],
            "value": summary["best_value"],
        }
        _atomic_json(output / "best_result.json", best)
    return summary
