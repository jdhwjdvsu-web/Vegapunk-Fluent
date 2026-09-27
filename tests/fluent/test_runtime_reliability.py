"""Phase 7R: layered timeouts, orphan safety, and complete-result recovery."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import optuna
import pytest

from vegapunk.fluent.campaign import campaign_fingerprint
from vegapunk.fluent.history import TrialState, atomic_json
from vegapunk.fluent.optimizer import run_optimization
from vegapunk.fluent.runner import (
    DuplicateTrialError, FluentExperimentRunner, FluentStateUncertainError,
)
from vegapunk.fluent.runtime_reliability import (
    RuntimeDiagnosis, diagnose_runtime, inspect_completed_trial,
)
from vegapunk.fluent.spec import ExperimentSpec, SpecError
from vegapunk.fluent.validity import evaluate_result_gate

from .test_optimizer import FakeRunner
from .test_spec import minimal_spec
from .test_validity import optimization_spec


class ToolClient:
    def __init__(self, *, timeout_on_run=False, error_on_run=False):
        self.calls = []
        self.timeout_on_run = timeout_on_run
        self.error_on_run = error_on_run
        self.connected = False
        self.session_id = "fixture-session-1"

    async def call_tool(self, name, arguments, *, timeout, raise_on_error):
        self.calls.append((name, timeout))
        if name == "run_code" and self.timeout_on_run:
            raise TimeoutError("request deadline")
        if name == "run_code" and self.error_on_run:
            return SimpleNamespace(
                structured_content=None, is_error=True,
                content=[SimpleNamespace(text="tool request timed out")],
            )
        data = {"status": "ok"}
        if name == "connect":
            self.connected = True
        if name == "disconnect":
            self.connected = False
        if name == "session_status":
            data.update(connected=self.connected, session_id=self.session_id)
        return SimpleNamespace(structured_content=data, is_error=False)


class ContextClient(ToolClient):
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def test_timeout_layers_are_configurable_and_not_scientific_campaign_identity(tmp_path):
    raw = minimal_spec()
    raw["connection"].update({
        "client_timeout_seconds": 320,
        "tool_timeout_seconds": 40,
        "connect_timeout_seconds": 120,
        "solve_timeout_seconds": 1800,
    })
    spec = ExperimentSpec.from_dict(raw)
    assert spec.connection.tool_timeout_seconds == 40
    assert spec.connection.connect_timeout_seconds == 120
    assert spec.connection.solve_timeout_seconds == 1800
    assert ExperimentSpec.from_dict(spec.to_dict()).connection == spec.connection
    base = ExperimentSpec.from_dict(minimal_spec())
    assert campaign_fingerprint(spec) == campaign_fingerprint(base)
    for key in ("tool_timeout_seconds", "connect_timeout_seconds", "solve_timeout_seconds"):
        invalid = minimal_spec()
        invalid["connection"][key] = 0
        with pytest.raises(SpecError, match=key):
            ExperimentSpec.from_dict(invalid)


def test_normal_tools_connect_and_solve_use_separate_timeouts(tmp_path):
    raw = minimal_spec()
    raw["connection"].update({
        "tool_timeout_seconds": 12, "connect_timeout_seconds": 34,
        "solve_timeout_seconds": 1800,
    })
    spec = ExperimentSpec.from_dict(raw)
    client = ContextClient()
    runner = FluentExperimentRunner(spec, tmp_path, client_factory=lambda *_: client)

    async def scenario():
        await runner.open_session()
        await runner._validated_run_code(client, "print('ok')")
        await runner.close_session()

    asyncio.run(scenario())
    assert client.calls[:5] == [
        ("session_status", 12), ("connect", 34),
        ("session_status", 12),
        ("validate_code", 12), ("run_code", 1800),
    ]


def test_submitted_run_timeout_is_uncertain_and_cleanup_does_not_mutate_fluent(tmp_path):
    raw = minimal_spec()
    raw["connection"]["solve_timeout_seconds"] = 8
    spec = ExperimentSpec.from_dict(raw)
    client = ContextClient(timeout_on_run=True)
    runner = FluentExperimentRunner(spec, tmp_path, client_factory=lambda *_: client)

    async def scenario():
        await runner.open_session()
        with pytest.raises(FluentStateUncertainError, match="must not be retried"):
            await runner._validated_run_code(client, "solver.settings.solution.run_calculation.iterate(1)")
        warning = await runner.close_session()
        return warning

    warning = asyncio.run(scenario())
    assert "uncertain" in warning
    assert ("run_code", 8) in client.calls
    assert not any(name == "disconnect" for name, _ in client.calls)


def test_submitted_run_tool_error_is_also_uncertain(tmp_path):
    spec = ExperimentSpec.from_dict(minimal_spec())
    client = ContextClient(error_on_run=True)
    runner = FluentExperimentRunner(spec, tmp_path, client_factory=lambda *_: client)

    async def scenario():
        await runner.open_session()
        with pytest.raises(FluentStateUncertainError):
            await runner._validated_run_code(client, "solver.settings.solution.run_calculation.iterate(1)")
        return await runner.close_session()

    assert "uncertain" in asyncio.run(scenario())


def test_runner_timeout_persists_diagnosis_and_duplicate_code_is_blocked(tmp_path, monkeypatch):
    raw = minimal_spec()
    raw["connection"]["connect_kwargs"] = {"case_file_name": "C:/offline/fixture.cas.h5"}
    spec = ExperimentSpec.from_dict(raw)
    client = ToolClient(timeout_on_run=True)
    client.connected = True
    runner = FluentExperimentRunner(spec, tmp_path)
    runner._client = client
    diagnosis = RuntimeDiagnosis(
        mcp_server="ALIVE", mcp_connection="UNKNOWN", fluent_process="ALIVE",
        fluent_session="UNKNOWN", trial_submission="UNKNOWN", request_dispatched=True,
        solve_state="UNKNOWN", possible_still_running=True, result_state="MISSING",
        result_reason="MISSING", generated_code_exists=True, solver_stdout_exists=False,
        final_info_exists=False, fluent_result_exists=False, gate_input_available=False,
        recommended_trial_state="ORPHANED", trial_id="trial-0000",
    )

    async def fake_diagnosis(*_args, **_kwargs):
        return diagnosis

    monkeypatch.setattr(
        "vegapunk.fluent.runtime_reliability.diagnose_runtime", fake_diagnosis,
    )
    with pytest.raises(FluentStateUncertainError) as first:
        asyncio.run(runner.evaluate_point({"velocity": 0.5}, name="trial-0000"))
    assert first.value.diagnosis["recommended_trial_state"] == "ORPHANED"
    saved = tmp_path / "runtime_diagnosis" / "trial-0000.json"
    assert json.loads(saved.read_text(encoding="utf-8"))["solve_state"] == "UNKNOWN"
    before = len(client.calls)
    with pytest.raises(DuplicateTrialError):
        asyncio.run(runner.evaluate_point({"velocity": 0.5}, name="trial-0000"))
    assert len(client.calls) == before


async def _session_alive(_endpoint, _timeout):
    return "ALIVE", "ALIVE"


def _complete_document(number=0, value=300.0):
    return {
        "schema_version": 1, "trial_id": f"trial-{number:04d}",
        "trial_number": number, "parameters": {"velocity": 0.5},
        "objective_value": value, "baseline_reloaded": True,
        "solve_confirmed": True, "gate_executed": True,
        "gates": {"status": "PASS", "passed": True,
                  "checks": [{"name": "solver_status", "passed": True}],
                  "constraint_vector": []},
    }


def test_diagnosis_distinguishes_missing_partial_and_complete_result(tmp_path):
    async def diagnose():
        return await diagnose_runtime(
            tmp_path, "trial-0000", "http://127.0.0.1:18000/mcp",
            parameters={"velocity": 0.5}, request_dispatched=True,
            server_probe=lambda *_: "ALIVE", process_probe=lambda: "ALIVE",
            session_probe=_session_alive,
        )

    missing = asyncio.run(diagnose())
    assert missing.result_state == "MISSING"
    assert missing.solve_state == "UNKNOWN"
    assert missing.possible_still_running is True
    assert missing.recommended_trial_state == "ORPHANED"
    path = tmp_path / "trial_results" / "trial-0000.json"
    path.parent.mkdir()
    path.write_text('{"schema_version": 1, "trial_number": 0}', encoding="utf-8")
    partial = asyncio.run(diagnose())
    assert partial.result_state == "PARTIAL"
    assert partial.gate_input_available is False
    atomic_json(path, _complete_document())
    complete = asyncio.run(diagnose())
    assert complete.result_state == "COMPLETE"
    assert complete.solve_state == "COMPLETE"
    assert complete.gate_input_available is True


def test_completed_file_requires_current_identity_gate_and_finite_values(tmp_path):
    path = tmp_path / "trial-0000.json"
    atomic_json(path, _complete_document())
    assert inspect_completed_trial(path, 0, {"velocity": 0.5}, expected_constraints=0)[0]
    assert not inspect_completed_trial(path, 1, {"velocity": 0.5})[0]
    assert not inspect_completed_trial(path, 0, {"velocity": 0.7})[0]
    for field in ("trial_id", "solve_confirmed", "gate_executed"):
        raw = _complete_document()
        raw.pop(field)
        atomic_json(path, raw)
        assert not inspect_completed_trial(path, 0, {"velocity": 0.5})[0]
    raw = _complete_document()
    raw["gates"] = None
    atomic_json(path, raw)
    assert not inspect_completed_trial(path, 0, {"velocity": 0.5})[0]
    raw = _complete_document(value=float("nan"))
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert not inspect_completed_trial(path, 0, {"velocity": 0.5})[0]


class UncertainRunner(FakeRunner):
    opens = 0
    attempts = 0

    async def open_session(self):
        type(self).opens += 1

    async def evaluate_point(self, parameters, *, name, artifact_stem=None):
        type(self).attempts += 1
        raise FluentStateUncertainError("submitted solve has unknown runtime state")


def test_uncertain_trial_is_orphaned_not_told_failed_or_resumed(tmp_path):
    UncertainRunner.opens = UncertainRunner.attempts = 0
    spec = optimization_spec()
    with pytest.raises(FluentStateUncertainError):
        asyncio.run(run_optimization(spec, tmp_path, target_trials=1, runner_factory=UncertainRunner))
    study = optuna.load_study(
        study_name=spec.optimization.study_name,
        storage=f"sqlite:///{(tmp_path / 'study.sqlite3').as_posix()}",
    )
    trial = study.trials[0]
    assert trial.state == optuna.trial.TrialState.RUNNING
    assert trial.user_attrs["runtime_state"] == "ORPHANED"
    assert UncertainRunner.attempts == 1
    ledger = next((tmp_path / "trial_ledger").glob("*/trial-0000.json"))
    assert json.loads(ledger.read_text(encoding="utf-8"))["state"] == TrialState.ORPHANED.value
    with pytest.raises(FluentStateUncertainError, match="cannot be resumed"):
        asyncio.run(run_optimization(spec, tmp_path, target_trials=1, runner_factory=UncertainRunner))
    assert UncertainRunner.opens == 1
    assert UncertainRunner.attempts == 1


def test_old_uncertain_campaign_event_blocks_even_when_optuna_state_is_fail(tmp_path):
    spec = optimization_spec()
    (tmp_path / "run_log.jsonl").write_text(
        json.dumps({"event": "trial_state_uncertain", "trial_number": 0}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(FluentStateUncertainError, match="cannot be resumed"):
        asyncio.run(run_optimization(spec, tmp_path, target_trials=1, runner_factory=FakeRunner))


def test_partial_running_result_blocks_recovery_without_tell(tmp_path):
    spec = optimization_spec()
    database = tmp_path / "study.sqlite3"
    study = optuna.create_study(
        study_name=spec.optimization.study_name,
        direction=spec.objective.direction,
        storage=f"sqlite:///{database.as_posix()}",
    )
    trial = study.ask()
    trial.suggest_float("velocity", 0.1, 2.0)
    result_path = tmp_path / "trial_results" / "trial-0000.json"
    atomic_json(result_path, {"schema_version": 1, "trial_number": 0})
    with pytest.raises(FluentStateUncertainError, match="Campaign is blocked"):
        asyncio.run(run_optimization(spec, tmp_path, target_trials=1, runner_factory=FakeRunner))
    assert optuna.load_study(
        study_name=spec.optimization.study_name,
        storage=f"sqlite:///{database.as_posix()}",
    ).trials[0].state == optuna.trial.TrialState.RUNNING


def test_complete_gated_file_recovers_only_tell_without_runner(tmp_path):
    spec = optimization_spec()
    database = tmp_path / "study.sqlite3"
    study = optuna.create_study(
        study_name=spec.optimization.study_name,
        direction=spec.objective.direction,
        storage=f"sqlite:///{database.as_posix()}",
    )
    trial = study.ask()
    trial.suggest_float("velocity", 0.1, 2.0)
    raw = _complete_document()
    raw["parameters"]["velocity"] = trial.params["velocity"]
    atomic_json(tmp_path / "trial_results" / "trial-0000.json", raw)
    summary = asyncio.run(run_optimization(
        spec, tmp_path, target_trials=1, runner_factory=UncertainRunner,
    ))
    assert summary["completed_trials"] == 1
    assert summary["gate_counts"] == {"PASS": 1}
    assert optuna.load_study(
        study_name=spec.optimization.study_name,
        storage=f"sqlite:///{database.as_posix()}",
    ).trials[0].state == optuna.trial.TrialState.COMPLETE


def test_duplicate_trial_artifact_is_not_submitted(tmp_path):
    spec = optimization_spec()
    code = tmp_path / "generated_code" / "trial-0000.py"
    code.parent.mkdir()
    code.write_text("old attempted trial", encoding="utf-8")
    with pytest.raises(DuplicateTrialError, match="artifacts"):
        asyncio.run(run_optimization(spec, tmp_path, target_trials=1, runner_factory=FakeRunner))
    assert not list((tmp_path / "trial_results").glob("*.json")) if (tmp_path / "trial_results").exists() else True


def test_complete_result_passes_existing_gate_and_session_reuse_remains_one_open(tmp_path):
    spec = optimization_spec()
    result = {"status": "completed", "objective_value": 300.0,
              "mass_flow_in": 0.004, "mass_flow_out": 0.004}
    assert evaluate_result_gate(spec, result).status == "PASS"

    class CountingRunner(FakeRunner):
        opens = 0
        closes = 0

        async def open_session(self):
            type(self).opens += 1

        async def close_session(self):
            type(self).closes += 1

    summary = asyncio.run(run_optimization(
        spec, tmp_path, target_trials=2, runner_factory=CountingRunner,
    ))
    assert summary["completed_trials"] == 2
    assert CountingRunner.opens == CountingRunner.closes == 1
