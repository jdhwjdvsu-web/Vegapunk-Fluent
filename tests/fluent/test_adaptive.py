import asyncio
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from vegapunk.fluent.adaptive import AdaptiveModels
from vegapunk.fluent.model_introspection import build_introspection_code
from vegapunk.fluent.parameter_rules import resolve_parameters
from vegapunk.fluent.parameter_ranker import rank_parameters
from vegapunk.fluent.profile_store import case_sha256, profile_key
from vegapunk.fluent.web import (RunRequest, DirectRunRequest, ParameterRangeRequest,
                                build_run_spec, build_direct_run_spec, create_app)

SPEC = Path(__file__).resolve().parents[2] / "config/fluent/mixing_elbow.optuna-demo.json"


def signature():
    return {"fluent_version": "Ansys Fluent 2026 R1", "dimension": 3,
            "physics": {"energy": True, "turbulence": True},
            "boundaries": [{"name": "feed-inlet", "type": "velocity_inlet"}],
            "materials": [{"name": "water-liquid", "type": "fluid"}],
            "reports": {}, "warnings": [], "observations": [
                {"rule_id": rule, "object_name": name, "value": value, "editable": True}
                for rule, name, value in [
                    ("velocity", "feed-inlet", 0.8), ("temperature", "feed-inlet", 300),
                    ("hydraulic_diameter", "feed-inlet", "4 [in]"),
                    ("turbulence_intensity", "feed-inlet", 0.05),
                    ("density", "water-liquid", 998), ("gauge_pressure", "product-outlet", 0)]]}


def test_rule_discovery_is_case_independent_and_units_safe():
    params = resolve_parameters(signature())
    assert len(params) == 6
    assert {p.object_name for p in params} == {"feed-inlet", "water-liquid", "product-outlet"}
    by_rule = {p.rule_id: p for p in params}
    assert by_rule["hydraulic_diameter"].default_value == pytest.approx(0.1016)
    assert by_rule["turbulence_intensity"].default_value == 5
    assert by_rule["turbulence_intensity"].native_value(5) == 0.05
    assert by_rule["gauge_pressure"].recommended_min is None
    assert by_rule["gauge_pressure"].public_dict()["requires_range_confirmation"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "x * 2", "1 [unknown]", None])
def test_rule_rejects_nonconstant_or_unknown_values(bad):
    data = signature()
    data["observations"] = [{"rule_id": "velocity", "object_name": "feed", "value": bad, "editable": True}]
    assert resolve_parameters(data) == []


def test_inactive_physics_readonly_and_unknown_rules_are_not_opened():
    data = signature()
    data["physics"] = {"energy": False, "turbulence": False}
    data["observations"][0]["editable"] = False
    data["observations"].append({"rule_id": "python", "editable": True, "value": 1})
    assert {p.rule_id for p in resolve_parameters(data)} == {"density", "gauge_pressure"}


def test_observed_bounds_and_name_collisions():
    data = signature()
    observation = data["observations"][0]
    observation.update(native_min=0.4, native_max=2)
    data["observations"].append({**observation, "object_name": "feed_inlet"})
    params = resolve_parameters(data)
    assert len({p.key for p in params}) == len(params)
    assert params[0].hard_min == 0.4 and params[0].hard_max == 2


def test_ranker_prefers_question_matched_boundary_over_material():
    params = resolve_parameters(signature())
    ranked = rank_parameters(params, "研究入口温度")
    assert next(p for p in params if p.key == ranked[0]["key"]).rule_id == "temperature"
    assert ranked[0]["reasons"]


@pytest.mark.parametrize("count", [1, 2, 3, 5])
def test_dynamic_spec_accepts_n_parameters(count):
    params = resolve_parameters(signature())[:count]
    request = RunRequest(case_file="C:/case.cas.h5", endpoint="http://127.0.0.1:18000/mcp",
                         iterations=20, target_trials=3, parameters=[
        dict(parameter_key=p.key, range_min=p.recommended_min, range_max=p.recommended_max) for p in params])
    spec = build_run_spec(SPEC, request, {p.key: p for p in params})
    assert len(spec.parameters) == count
    assert set(spec.design_points[0].values) == {p.key for p in params}


@pytest.mark.parametrize("count", [1, 3, 5])
def test_direct_spec_accepts_n_parameters(count):
    params = resolve_parameters(signature())[:count]
    form = DirectRunRequest(case_file="C:/case.cas.h5", endpoint="http://localhost:18000/mcp", iterations=10,
                            parameters=[dict(parameter_key=p.key, value=p.default_value) for p in params])
    spec = build_direct_run_spec(SPEC, form, {p.key: p for p in params})
    assert len(spec.parameters) == count
    assert spec.design_points[0].values == {p.key: p.native_value(p.default_value) for p in params}


@pytest.mark.parametrize("count", [0, 6])
def test_dynamic_spec_rejects_count_outside_v1(count):
    with pytest.raises(ValidationError):
        RunRequest(case_file="a", endpoint="http://localhost", iterations=1, target_trials=1,
                   parameters=[dict(parameter_key=str(i), range_min=1, range_max=2) for i in range(count)])


def test_nonfinite_and_duplicate_api_values_rejected():
    with pytest.raises(ValidationError):
        ParameterRangeRequest(parameter_key="a", range_min=float("nan"), range_max=1)
    with pytest.raises(ValidationError):
        DirectRunRequest(case_file="a", endpoint="http://localhost", iterations=1,
                         parameters=[dict(parameter_key="a", value=1)] * 2)


def test_scanner_fixed_shape_is_read_only():
    import ast
    tree = ast.parse(build_introspection_code())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            assert not any(isinstance(target, ast.Attribute) for target in node.targets)
    code = build_introspection_code()
    assert "cold-inlet" not in code and "water-liquid" not in code
    assert "iterate(" not in code and "set_state(" not in code
    assert "is_read_only()" in code


def test_cache_fingerprint_and_overrides(tmp_path, monkeypatch):
    case = tmp_path / "sample.cas.h5"
    case.write_bytes(b"test model")
    calls = []
    async def scan(*args):
        calls.append(args)
        return signature()
    monkeypatch.setattr("vegapunk.fluent.adaptive.scan_model", scan)
    service = AdaptiveModels(tmp_path, json.loads(SPEC.read_text()))
    async def check():
        p, cached = await service.analyze(str(case), "http://localhost", 3, "26.1.0", "温度")
        assert not cached and not p["contract_confirmed"]
        parameter = next(iter(service.catalog().values()))
        service.save_ranges([ParameterRangeRequest(parameter_key=parameter.key, range_min=0.5, range_max=1)], tmp_path / "task")
        p2, cached = await service.analyze(str(case), "http://localhost", 3, "26.1.0", "流速")
        assert cached and p2["user_overrides"][parameter.key] == [0.5, 1]
        assert len(calls) == 1
        case.write_bytes(b"changed model")
        p3, cached = await service.analyze(str(case), "http://localhost", 3, "26.1.0", "")
        assert not cached and p3["model_id"] != p["model_id"]
        assert not p3["user_overrides"]
        _, cached = await service.analyze(str(case), "http://localhost", 3, "26.1.0", "", force=True)
        assert not cached
    asyncio.run(check())


def test_web_discovers_saves_restores_and_blocks_unknown_contract(tmp_path, monkeypatch):
    case = tmp_path / "renamed.cas.h5"
    case.write_bytes(b"different case")
    async def scan(*args):
        return signature()
    monkeypatch.setattr("vegapunk.fluent.adaptive.scan_model", scan)
    output = tmp_path / "runs"
    app = create_app(spec_path=SPEC, output_dir=output, allow_remote_control=True)
    with TestClient(app) as client:
        result = client.post("/api/models/analyze", json={"case_file": str(case), "endpoint": "http://localhost:18000/mcp", "product_version": "26.1.0"})
        assert result.status_code == 200, result.text
        state = client.get("/api/state").json()
        assert len(state["parameters"]) == 6
        assert len(state["defaults"]["parameters"]) == 2
        assert state["model"]["execution_ready"] is False
        assert client.get("/api/models").json()["models"]
        request = {**state["defaults"]}
        request = {key: request[key] for key in ("case_file", "endpoint", "parameters", "iterations", "target_trials")}
        blocked = client.post("/api/runs", json=request)
        assert blocked.status_code == 409 and "Objective" in blocked.json()["detail"]
        saved = client.post("/api/models/ranges", json={"parameters": request["parameters"][:1]})
        assert saved.status_code == 200
    restored = create_app(spec_path=SPEC, output_dir=output, allow_remote_control=True)
    with TestClient(restored) as client:
        state = client.get("/api/state").json()
        assert len(state["defaults"]["parameters"]) == 1
        assert client.post("/api/tasks/new").status_code == 201
        assert client.get("/api/state").json()["parameters"] == []
        assert (output / "model_selection.json").exists()


def test_changed_case_invalidates_selection_and_rejects_execution(tmp_path, monkeypatch):
    case = tmp_path / "baseline.cas.h5"
    case.write_bytes(b"original")
    template = json.loads(SPEC.read_text())
    template["connection"]["baseline_sha256"] = case_sha256(str(case))
    async def scan(*args):
        return signature()
    monkeypatch.setattr("vegapunk.fluent.adaptive.scan_model", scan)
    service = AdaptiveModels(tmp_path, template)
    async def check():
        await service.analyze(str(case), "http://localhost", 3, "26.1.0", "")
        assert service.profile["contract_confirmed"]
        p = next(iter(service.catalog().values()))
        form = RunRequest(case_file=str(case), endpoint="http://localhost", iterations=1, target_trials=1,
                          parameters=[dict(parameter_key=p.key, range_min=0.5, range_max=1)])
        assert await service.validate_run(form)
        case.write_bytes(b"changed")
        with pytest.raises(ValueError, match="模型已变化"):
            await service.validate_run(form)
        assert service.profile is None
    asyncio.run(check())


def test_cross_site_mutations_rejected_and_busy_scan_cannot_reset(tmp_path):
    app = create_app(spec_path=SPEC, output_dir=tmp_path, allow_remote_control=True)
    with TestClient(app) as client:
        response = client.post("/api/tasks/new", headers={"Origin": "https://evil.invalid"})
        assert response.status_code == 403
        app.state.adaptive_models.busy = True
        assert client.post("/api/tasks/new").status_code == 409


def test_introspection_never_takes_over_existing_session(tmp_path, monkeypatch):
    from vegapunk.fluent.model_introspection import scan_model
    calls = []
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    async def call(client, name, args):
        calls.append(name)
        return {"connected": True}
    monkeypatch.setattr("fastmcp.Client", Client)
    monkeypatch.setattr("vegapunk.fluent.model_introspection._call", call)
    with pytest.raises(RuntimeError, match="已有活动会话"):
        asyncio.run(scan_model("http://localhost", {}, tmp_path))
    assert calls == ["session_status"]


def test_introspection_disconnects_only_owned_session_after_validation_failure(tmp_path, monkeypatch):
    from vegapunk.fluent.model_introspection import scan_model
    calls = []
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    async def call(client, name, args):
        calls.append(name)
        return {"connected": False, "status": "rejected" if name == "validate_code" else "ok"}
    monkeypatch.setattr("fastmcp.Client", Client)
    monkeypatch.setattr("vegapunk.fluent.model_introspection._call", call)
    with pytest.raises(RuntimeError, match="未通过"):
        asyncio.run(scan_model("http://localhost", {}, tmp_path))
    assert calls == ["session_status", "connect", "validate_code", "disconnect"]
