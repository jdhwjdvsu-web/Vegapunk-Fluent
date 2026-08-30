from vegapunk.fluent.acceptance import EXPECTED_SCENARIOS, validate_acceptance_records


def valid_record(name):
    fault = name not in {f"normal-{index:02d}" for index in range(1, 14)}
    return {
        "scenario_id": name,
        "state": "FAILED" if fault else "TOLD",
        "gate_status": "DIVERGED" if fault else "PASS",
        "selected_as_best": False,
        "fluent_execution_count": 0 if name == "parameter-out-of-range" else 1,
        "ledger_consistent": True,
        "result_files_consistent": True,
    }


def test_fixed_twenty_point_admission_accepts_consistent_terminal_evidence():
    records = [valid_record(name) for name in EXPECTED_SCENARIOS]
    records[0]["selected_as_best"] = True
    result = validate_acceptance_records(records)
    assert result["passed"] is True
    assert result["observed_points"] == 20


def test_fixed_twenty_point_admission_rejects_duplicate_or_invalid_best():
    records = [valid_record(name) for name in EXPECTED_SCENARIOS]
    records[1]["fluent_execution_count"] = 2
    records[-1]["selected_as_best"] = True
    result = validate_acceptance_records(records)
    assert result["passed"] is False
    assert result["duplicate_computations"] == ["normal-02"]
    assert result["invalid_selected_as_best"] == ["controller-restart"]
