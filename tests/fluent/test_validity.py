import pytest

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


def test_parameter_gate_rejects_safe_linear_combination_constraint():
    raw = optimization_spec().to_dict()
    raw["parameter_constraints"] = [
        {
            "name": "velocity_budget",
            "coefficients": {"velocity": 2.0},
            "operator": "<=",
            "value": 1.5,
        }
    ]
    spec = ExperimentSpec.from_dict(raw)
    normalized, gate = validate_parameter_values(spec, {"velocity": 1.0})
    assert normalized is None
    assert gate.status == "PARAMETER"
    assert "velocity_budget" in gate.reasons[0]


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
    assert gate.status == "DIVERGED"
    assert len(gate.reasons) == 2


def test_trusted_engineering_constraint_is_not_classified_as_divergence():
    spec = optimization_spec()
    gate = evaluate_result_gate(
        spec,
        {
            "status": "completed",
            "objective_value": 295.0,
            "mass_flow_in": 0.004,
            "mass_flow_out": 0.004,
            "constraints": [
                {
                    "report": "pressure-drop",
                    "operator": "<=",
                    "limit": 10.0,
                    "actual": 12.0,
                    "passed": False,
                }
            ],
        },
    )
    assert gate.status == "CONSTRAINT"
    assert gate.constraint_vector == (2.0,)


def test_configured_component_conservation_gate_rejects_salt_imbalance():
    raw = optimization_spec().to_dict()
    raw["reports"].extend(
        [
            {"name": "salt-in", "kind": "flux", "locations": ["feed"]},
            {"name": "salt-concentrate", "kind": "flux", "locations": ["brine"]},
            {"name": "salt-permeate", "kind": "flux", "locations": ["permeate"]},
        ]
    )
    raw["optimization"]["conservation_checks"] = [
        {
            "name": "salt_balance",
            "input_reports": ["salt-in"],
            "output_reports": ["salt-concentrate", "salt-permeate"],
            "relative_tolerance": 0.01,
        }
    ]
    spec = ExperimentSpec.from_dict(raw)
    gate = evaluate_result_gate(
        spec,
        {
            "status": "completed",
            "objective_value": 295.0,
            "mass_flow_in": 1.0,
            "mass_flow_out": 1.0,
            "reports": {
                "salt-in": {"value": 1.0},
                "salt-concentrate": {"value": 0.8},
                "salt-permeate": {"value": 0.1},
            },
        },
    )
    assert gate.status == "DIVERGED"
    assert gate.metrics["salt_balance_relative_error"] == pytest.approx(0.1)


def test_last_n_monitor_gate_accepts_stationary_history_and_rejects_drift():
    raw = optimization_spec().to_dict()
    raw["optimization"]["numerical_monitors"] = [
        {
            "report": "outlet-temperature",
            "window": 5,
            "max_relative_slope": 0.001,
            "max_relative_span": 0.005,
        }
    ]
    spec = ExperimentSpec.from_dict(raw)
    base = {
        "status": "completed",
        "objective_value": 295.0,
        "mass_flow_in": 1.0,
        "mass_flow_out": 1.0,
    }
    steady = evaluate_result_gate(
        spec,
        {
            **base,
            "monitor_history": {
                "outlet-temperature": [295.0, 295.01, 295.0, 295.01, 295.0]
            },
        },
    )
    assert steady.status == "PASS"
    drifting = evaluate_result_gate(
        spec,
        {
            **base,
            "monitor_history": {
                "outlet-temperature": [290.0, 291.0, 292.0, 293.0, 294.0]
            },
        },
    )
    assert drifting.status == "DIVERGED"
