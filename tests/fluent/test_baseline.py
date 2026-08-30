from vegapunk.fluent.baseline import analyze_repeatability


def test_repeatability_requires_three_cold_and_ten_sequential_runs():
    records = []
    for index in range(3):
        records.append(
            {
                "phase": "cold",
                "metrics": {"pressure_drop": 100.0 + index * 0.01},
                "process_memory_mb": 1000,
            }
        )
    for index in range(10):
        records.append(
            {
                "phase": "sequence",
                "metrics": {"pressure_drop": 100.01 - index * 0.001},
                "process_memory_mb": 1000 + index,
            }
        )
    result = analyze_repeatability(records, {"pressure_drop": 0.001})
    assert result["passed"] is True


def test_repeatability_uses_configured_metric_tolerance():
    records = [
        {
            "phase": "cold" if index < 3 else "sequence",
            "metrics": {"outlet_concentration": 1.0 + index * 0.02},
            "process_memory_mb": 1000,
        }
        for index in range(13)
    ]
    result = analyze_repeatability(records, {"outlet_concentration": 0.01})
    assert result["passed"] is False
    assert result["metric_checks"][0]["passed"] is False
