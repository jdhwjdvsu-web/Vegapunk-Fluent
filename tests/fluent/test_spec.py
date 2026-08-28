import pytest

from vegapunk.fluent.spec import ExperimentSpec, SpecError


def minimal_spec():
    return {
        "schema_version": 1,
        "task_name": "AutoFluent",
        "connection": {"endpoint": "http://127.0.0.1:18000/mcp"},
        "solver": {"iterations": 5},
        "parameters": [
            {
                "name": "velocity",
                "collection_path": "setup.boundary_conditions.velocity_inlet",
                "object_name": "cold-inlet",
                "property_path": "momentum.velocity_magnitude.value",
                "unit": "m/s",
                "minimum": 0.1,
                "maximum": 2.0,
            }
        ],
        "reports": [
            {
                "name": "outlet-temperature",
                "kind": "surface",
                "report_type": "surface-areaavg",
                "field": "temperature",
                "locations": ["outlet"],
            }
        ],
        "design_points": [{"name": "baseline", "values": {"velocity": 0.55}}],
        "objective": {"report": "outlet-temperature", "direction": "minimize"},
    }


def test_accepts_valid_spec():
    spec = ExperimentSpec.from_dict(minimal_spec())
    assert spec.design_points[0].values == {"velocity": 0.55}


def test_rejects_path_injection():
    raw = minimal_spec()
    raw["parameters"][0]["property_path"] = "momentum.value;print('unsafe')"
    with pytest.raises(SpecError, match="dot-separated"):
        ExperimentSpec.from_dict(raw)


def test_rejects_value_outside_allowlist_bounds():
    raw = minimal_spec()
    raw["design_points"][0]["values"]["velocity"] = 9.0
    with pytest.raises(SpecError, match="exceeds maximum"):
        ExperimentSpec.from_dict(raw)


def test_remote_endpoint_requires_explicit_opt_in():
    raw = minimal_spec()
    raw["connection"]["endpoint"] = "http://192.168.1.5:18000/mcp"
    with pytest.raises(SpecError, match="allow_remote_endpoint"):
        ExperimentSpec.from_dict(raw)
