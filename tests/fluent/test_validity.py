from vegapunk.fluent.spec import ExperimentSpec
from vegapunk.fluent.validity import evaluate_result_gate, validate_parameter_values

from .test_spec import minimal_spec


def optimization_spec() -> ExperimentSpec:
    raw = minimal_spec()
    raw["reports"].extend(
        [
            {"name": "mass-in", "kind": "flux", "locations": ["inlet"]},
            {"name": "mass-out", "kind": "flux", "locations": ["outlet"]},
        ]
    )
    raw["optimization"] = {
        "mass_flow_in_report": "mass-in",
        "mass_flow_out_report": "mass-out",
        "mass_balance_relative_tolerance": 0.001,
    }
    return ExperimentSpec.from_dict(raw)


def test_parameter_gate_rejects_out_of_bounds_before_execution():
    spec = optimization_spec()
    normalized, gate = validate_parameter_values(spec, {"velocity": 9.0})
    assert normalized is None
    assert gate.passed is False
    assert "exceeds maximum" in gate.reasons[0]


def test_result_gate_accepts_finite_mass_conserving_trial():
    spec = optimization_spec()
    gate = evaluate_result_gate(
        spec,
        {
            "status": "completed",
            "objective_value": 295.0,
            "mass_flow_in": 0.004,
            "mass_flow_out": -0.004000001,
        },
    )
    assert gate.passed is True
    assert gate.metrics["mass_balance_relative_error"] < 0.001


def test_result_gate_rejects_non_finite_or_unbalanced_trial():
    spec = optimization_spec()
    gate = evaluate_result_gate(
        spec,
        {
            "status": "completed",
            "objective_value": float("nan"),
            "mass_flow_in": 0.004,
            "mass_flow_out": 0.003,
        },
    )
    assert gate.passed is False
    assert len(gate.reasons) == 2
