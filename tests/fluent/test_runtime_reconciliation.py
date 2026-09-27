"""Runtime evidence must agree before solve or tell recovery."""
import asyncio
import json

import optuna
import pytest

from vegapunk.fluent.history import TrialLedger, TrialState, atomic_json
from vegapunk.fluent.campaign import load_or_create_campaign
from vegapunk.fluent.optimizer import run_optimization
from vegapunk.fluent.runner import FluentStateUncertainError
from vegapunk.fluent.runtime_reconciliation import reconcile_runtime

from .test_runtime_reliability import _complete_document
from .test_validity import optimization_spec


class NoSolver:
    def __init__(self, *args):
        raise AssertionError("reconciliation must not construct a Runner")


def setup_study(tmp_path, *, complete=False):
    spec = optimization_spec()
    load_or_create_campaign(tmp_path, spec)
    study = optuna.create_study(study_name=spec.optimization.study_name,
        direction=spec.objective.direction,
        storage=f"sqlite:///{(tmp_path / 'study.sqlite3').as_posix()}")
    trial = study.ask()
    trial.suggest_float("velocity", 0.1, 2)
    document = _complete_document()
    document["parameters"] = trial.params
    atomic_json(tmp_path / "trial_results" / "trial-0000.json", document)
    if complete:
        trial.set_user_attr("constraint_vector", [])
        study.tell(trial, document["objective_value"])
    return spec, study, trial, document


def resume(spec, output):
    return asyncio.run(run_optimization(spec, output, target_trials=1, runner_factory=NoSolver))


def make_ledger(root, output):
    campaign_id = json.loads((output / "campaign.json").read_text(encoding="utf-8"))["campaign_id"]
    return TrialLedger(root, campaign_id, "trial-0000")


@pytest.mark.parametrize("source", ["semantic", "native", "optuna"])
def test_orphan_anywhere_vetoes_even_complete_result(tmp_path, source):
    spec, study, trial, _ = setup_study(tmp_path)
    if source == "optuna":
        trial.set_user_attr("runtime_state", "ORPHANED")
    else:
        root = tmp_path if source == "semantic" else tmp_path / "native_execution"
        ledger = make_ledger(root, tmp_path)
        ledger.create(trial.params)
        ledger.transition(TrialState.SUBMITTED)
        ledger.transition(TrialState.ORPHANED)
    with pytest.raises(FluentStateUncertainError):
        resume(spec, tmp_path)
    assert study.get_trials()[0].state.name == "RUNNING"
    report = json.loads((tmp_path / "runtime_reconciliation.json").read_text(encoding="utf-8"))
    assert report["automatic_submission_allowed"] is False


@pytest.mark.parametrize("damage", ["missing", "objective", "constraints"])
def test_optuna_complete_is_not_authoritative_without_matching_evidence(tmp_path, damage):
    spec, study, _, document = setup_study(tmp_path, complete=True)
    path = tmp_path / "trial_results" / "trial-0000.json"
    if damage == "missing":
        path.unlink()
    else:
        if damage == "objective":
            document["objective_value"] += 1
        else:
            document["gates"]["constraint_vector"] = [0]
        atomic_json(path, document)
    with pytest.raises(FluentStateUncertainError):
        resume(spec, tmp_path)
    assert study.get_trials()[0].state.name == "COMPLETE"


@pytest.mark.parametrize("already_told", [False, True])
def test_gated_crash_window_recovers_persistence_only(tmp_path, already_told):
    spec, study, trial, document = setup_study(tmp_path, complete=already_told)
    ledger = make_ledger(tmp_path, tmp_path)
    ledger.create(trial.params)
    ledger.transition(TrialState.SUBMITTED)
    ledger.transition(TrialState.RUNNING)
    ledger.transition(TrialState.RESULT_READY)
    ledger.transition(TrialState.GATED, gate=document["gates"])
    before = reconcile_runtime(study, spec, tmp_path)
    assert before["status"] == "CONSISTENT"
    assert study.get_trials()[0].state.name == ("COMPLETE" if already_told else "RUNNING")
    assert resume(spec, tmp_path)["completed_trials"] == 1
    assert ledger.load()["state"] == "TOLD"
    assert ledger.load()["recovery"] == "PERSISTENCE_ONLY_NO_SOLVE"
    assert resume(spec, tmp_path)["completed_trials"] == 1


def test_told_ledger_cannot_force_optuna_running_to_complete(tmp_path):
    spec, study, trial, document = setup_study(tmp_path)
    ledger = make_ledger(tmp_path, tmp_path)
    ledger.create(trial.params)
    for state in (TrialState.SUBMITTED, TrialState.RESULT_READY, TrialState.GATED, TrialState.TOLD):
        ledger.transition(state)
    with pytest.raises(FluentStateUncertainError, match="TOLD conflicts"):
        resume(spec, tmp_path)
    assert study.get_trials()[0].state.name == "RUNNING"


@pytest.mark.parametrize("kind", ["orphan", "reserved"])
def test_uncertain_verification_vetoes_new_optimization_submission(tmp_path, kind):
    spec, _, _, _ = setup_study(tmp_path, complete=True)
    verification = tmp_path / "verification"
    if kind == "orphan":
        atomic_json(verification / "result.json", {"status": "orphaned", "runtime_state": "ORPHANED"})
    else:
        atomic_json(verification / "request.json", {"contract": "fixture"})
    with pytest.raises(FluentStateUncertainError, match="verification"):
        resume(spec, tmp_path)
