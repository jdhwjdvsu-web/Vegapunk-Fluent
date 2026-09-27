"""Offline verification policy and no duplicate verification solves."""
import asyncio

import pytest

from vegapunk.fluent.runner import DuplicateTrialError, FluentStateUncertainError
from vegapunk.fluent.spec import ExperimentSpec
from vegapunk.fluent.verification import run_independent_verification, verification_is_trusted
from vegapunk.fluent.history import atomic_json

from .test_validity import optimization_spec


class VerificationRunner:
    calls = 0
    delta = 0.3
    baseline = True
    wrong_parameters = False
    uncertain = False

    def __init__(self, spec, output):
        self.spec = spec

    async def open_session(self):
        pass

    async def evaluate_point(self, parameters, **kwargs):
        type(self).calls += 1
        if self.uncertain:
            raise FluentStateUncertainError("unknown solve completion", diagnosis={"solve_state": "UNKNOWN"})
        return {"status": "completed", "name": kwargs["name"],
                "parameters": {} if self.wrong_parameters else dict(parameters),
                "baseline_reloaded": self.baseline, "objective_value": 300 + self.delta,
                "mass_flow_in": 0.004, "mass_flow_out": 0.004,
                "objective_unit": "K", "constraints": [],
                "reports": {self.spec.objective.report: {"value": 300 + self.delta, "unit": "K"}},
                "convergence": {"required": False}, "monitor_history": {}}

    async def close_session(self):
        return None


def run(tmp_path, factory=VerificationRunner, **kwargs):
    spec = optimization_spec()
    return asyncio.run(run_independent_verification(
        spec, tmp_path, {"velocity": 0.5}, 300.0, runner_factory=factory, **kwargs))


def test_absolute_kelvin_is_not_a_temperature_rise_scale(tmp_path):
    result = run(tmp_path)
    assert result["verified"] is False
    assert result["allowed_difference"] == 1e-6
    explicit = run(tmp_path / "scale", tolerance_mode="reference_scale", reference_scale=20,
                   relative_tolerance=0.02, policy_basis="Specified 20 K temperature rise scale")
    assert explicit["verified"] is True
    assert explicit["allowed_difference"] == 0.4
    assert explicit["improvement_verified"] is False
    assert verification_is_trusted(explicit, {"velocity": 0.5}, 300)
    assert not verification_is_trusted({**explicit, "identity_valid": False}, {"velocity": 0.5}, 300)


@pytest.mark.parametrize("changes", [{"baseline": False}, {"wrong_parameters": True}])
def test_gate_and_tolerance_do_not_bypass_baseline_or_candidate_identity(tmp_path, changes):
    runner = type("InvalidRunner", (VerificationRunner,), changes)
    result = run(tmp_path, runner, absolute_tolerance=1)
    assert not result["verified"]
    assert not result["identity_valid"]


def test_orphan_verification_is_preserved_not_resubmitted(tmp_path):
    runner = type("OrphanRunner", (VerificationRunner,), {"uncertain": True, "calls": 0})
    result = run(tmp_path, runner)
    assert result["status"] == "orphaned"
    assert run(tmp_path, runner) == result
    assert runner.calls == 1
    with pytest.raises(DuplicateTrialError):
        run(tmp_path, runner, absolute_tolerance=0.5)
    assert runner.calls == 1


@pytest.mark.parametrize("kwargs", [{"reference_scale": float("nan")},
                                    {"absolute_tolerance": -1},
                                    {"tolerance_mode": "reference_scale"},
                                    {"relative_tolerance": True}, {"policy_basis": " "}])
def test_invalid_policy_rejected_before_solver(tmp_path, kwargs):
    runner = type("NoCallsRunner", (VerificationRunner,), {"calls": 0})
    with pytest.raises(ValueError):
        run(tmp_path, runner, **kwargs)
    assert runner.calls == 0


def test_crash_after_reservation_never_submits_another_verification(tmp_path):
    atomic_json(tmp_path / "verification" / "request.json", {"reserved": True})
    runner = type("NoCallsRunner", (VerificationRunner,), {"calls": 0})
    with pytest.raises(DuplicateTrialError, match="reserved/submitted"):
        run(tmp_path, runner)
    assert runner.calls == 0
