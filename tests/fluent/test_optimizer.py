import asyncio
import json
from typing import ClassVar

from vegapunk.fluent.optimizer import run_optimization

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
