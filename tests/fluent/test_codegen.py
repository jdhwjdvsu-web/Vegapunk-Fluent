from vegapunk.fluent.codegen import (
    RESULT_MARKER,
    build_design_point_code,
    build_evaluate_point_code,
    extract_result_payload,
    normalize_computed_reports,
    parse_last_residuals,
)
from vegapunk.fluent.spec import ExperimentSpec

from .test_spec import minimal_spec


def test_codegen_uses_validated_setting_path_and_quoted_object_name():
    spec = ExperimentSpec.from_dict(minimal_spec())
    code = build_design_point_code(spec, spec.design_points[0])
    assert (
        "velocity_inlet['cold-inlet'].momentum.velocity_magnitude.value = 0.55" in code
    )
    assert "VEGAPUNK_FLUENT_RESULT=" in code


def test_isolated_trial_reloads_case_before_applying_parameters():
    raw = minimal_spec()
    raw["connection"]["connect_kwargs"] = {"case_file_name": "C:\\cases\\base.cas.h5"}
    spec = ExperimentSpec.from_dict(raw)
    code = build_evaluate_point_code(spec, spec.design_points[0])
    reload_position = code.index("read_case")
    report_position = code.index("report_definitions")
    parameter_position = code.index("velocity_inlet['cold-inlet']")
    initialize_position = code.index("hybrid_initialize")
    assert reload_position < report_position < parameter_position < initialize_position


def test_extract_and_normalize_report_payload():
    stdout = (
        "solver output\n"
        + RESULT_MARKER
        + "{'name': 'p1', 'computed_reports': "
        + "[{'outlet-temperature': [295.8, 'K']}]}\n"
    )
    payload = extract_result_payload(stdout)
    reports = normalize_computed_reports(
        payload["computed_reports"], {"outlet-temperature"}
    )
    assert reports == {"outlet-temperature": {"value": 295.8, "unit": "K"}}


def test_parse_last_residual_row():
    stdout = """
 iter  continuity  x-velocity  y-velocity  energy  time/iter
  1  1.0e-2  2.0e-3  3.0e-3  1.0e-5  0:00:01
  5  8.0e-4  5.0e-4  4.0e-4  8.0e-7  0:00:00
"""
    residuals = parse_last_residuals(stdout)
    assert residuals["iteration"] == 5
    assert residuals["values"]["continuity"] == 8.0e-4


def test_numerical_monitor_codegen_samples_last_n_report_values():
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
        "numerical_monitors": [
            {"report": "outlet-temperature", "window": 5}
        ],
    }
    spec = ExperimentSpec.from_dict(raw)
    code = build_design_point_code(spec, spec.design_points[0])
    assert "for __vp_chunk_size in [1, 1, 1, 1, 1]" in code
    assert "monitor_history_raw" in code
