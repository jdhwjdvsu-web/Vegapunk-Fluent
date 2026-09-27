import pytest

from vegapunk.fluent.codegen import (
    build_evaluate_point_code,
    normalize_spec_reports,
    parse_iteration_statistics,
)
from vegapunk.fluent.metric_catalog import resolve_metrics
from vegapunk.fluent.parameter_rules import resolve_parameters
from vegapunk.fluent.spec import ExperimentSpec


def naca_signature():
    return {
        "dimension": 2,
        "physics": {"energy": False, "turbulence": True},
        "boundaries": [
            {"name": "inlet", "type": "velocity_inlet"},
            {"name": "outlet", "type": "pressure_outlet"},
            {"name": "wall-airfoil", "type": "wall"},
        ],
        "observations": [
            {
                "rule_id": "velocity",
                "collection": "velocity_inlet",
                "object_name": "inlet",
                "value": 40.0,
                "editable": True,
            },
            *[
                {
                    "rule_id": f"flow_direction_{axis}",
                    "collection": "velocity_inlet",
                    "object_name": "inlet",
                    "value": value,
                    "editable": True,
                }
                for axis, value in zip("xyz", (0.9962, 0.0872, 0.0))
            ],
        ],
        "reports": {
            "lift": {
                "report-lift": {
                    "zones": ["wall-airfoil"],
                    "report_output_type": "Lift Coefficient",
                    "force_vector": [-0.0872, 0.9962, 1.0],
                }
            },
            "drag": {
                "report-drag": {
                    "zones": ["wall-airfoil"],
                    "report_output_type": "Drag Coefficient",
                    "force_vector": [0.9962, 0.0872, 1.0],
                }
            },
        },
    }


def naca_spec():
    parameters = {item.rule_id: item for item in resolve_parameters(naca_signature())}
    ratio = next(item for item in resolve_metrics(naca_signature()) if item.kind == "derived_ratio")
    values = {
        "speed": 40.0,
        "direction_x": 0.984807753,
        "direction_y": 0.173648178,
        "direction_z": 0.0,
    }
    raw_parameters = []
    for rule_id, name in (
        ("velocity", "speed"),
        ("flow_direction_x", "direction_x"),
        ("flow_direction_y", "direction_y"),
        ("flow_direction_z", "direction_z"),
    ):
        raw = parameters[rule_id].spec_dict(-1 if "direction" in name else 30, 1 if "direction" in name else 50)
        raw["name"] = name
        raw_parameters.append(raw)
    return ExperimentSpec.from_dict({
        "schema_version": 1,
        "task_name": "naca-aero",
        "connection": {
            "endpoint": "http://127.0.0.1:18000/mcp",
            "connect_kwargs": {"case_file_name": "C:/cases/naca.cas.h5"},
        },
        "solver": {"initialization": "none", "iterations": 20},
        "parameters": raw_parameters,
        "reports": [ratio.report_spec()],
        "design_points": [{"name": "ten-deg", "values": values}],
        "objective": {"report": ratio.report_name, "direction": "maximize"},
    })


def test_catalog_imports_existing_force_reports_and_derived_ratio():
    metrics = resolve_metrics(naca_signature())
    lift = next(item for item in metrics if item.kind == "existing" and item.report_type == "lift")
    drag = next(item for item in metrics if item.kind == "existing" and item.report_type == "drag")
    ratio = next(item for item in metrics if item.kind == "derived_ratio")
    assert lift.report_name == "report-lift"
    assert drag.report_name == "report-drag"
    assert ratio.numerator_report == "report-lift"
    assert ratio.denominator_report == "report-drag"
    assert ratio.unit == "dimensionless"


def test_velocity_direction_codegen_rotates_reports_and_couples_reference_speed():
    spec = naca_spec()
    code = build_evaluate_point_code(spec, spec.design_points[0])
    compile(code, "<naca>", "exec")
    assert "velocity_specification_method = 'Magnitude and Direction'" in code
    assert "report_definitions.lift['report-lift']" in code
    assert "report_definitions.drag['report-drag']" in code
    assert "setup.reference_values.velocity = 40.0" in code
    assert "report_defs=['report-lift', 'report-drag']" in code
    assert ".derived_ratio" not in code


def test_derived_ratio_is_allowlisted_and_denominator_guarded():
    spec = naca_spec()
    reports = normalize_spec_reports(
        [{"report-lift": [0.5, ""]}, {"report-drag": [0.025, ""]}], spec
    )
    assert reports[spec.objective.report]["value"] == pytest.approx(20.0)
    assert reports[spec.objective.report]["derived_from"] == ["report-lift", "report-drag"]
    with pytest.raises(ValueError, match="denominator"):
        normalize_spec_reports(
            [{"report-lift": [0.5, ""]}, {"report-drag": [0.0, ""]}], spec
        )


def test_iteration_statistics_reports_transaction_rows_and_absolute_numbers():
    stdout = """ iter continuity x-velocity time/iter\n 81 1e-3 2e-4 0:00:01 2\n 82 1e-6 2e-8 0:00:00 1\n"""
    assert parse_iteration_statistics(stdout) == {
        "iterations_actual": 2,
        "first_iteration_number": 81,
        "last_iteration_number": 82,
        "iteration_count_source": "solver_stdout_residual_rows",
    }
