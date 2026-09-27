import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vegapunk.fluent.codegen import build_report_setup_code
from vegapunk.fluent.metric_catalog import resolve_metrics
from vegapunk.fluent.parameter_rules import resolve_parameters
from vegapunk.fluent.planning import (
    approve_plan,
    assert_approval,
    compile_experiment_spec,
    default_plan,
    normalize_plan,
)
from vegapunk.fluent.simulation_agent import SimulationAgent
from vegapunk.fluent.web import create_app
from vegapunk.fluent.verification import run_independent_verification


SPEC = Path(__file__).resolve().parents[2] / "config/fluent/mixing_elbow.optuna-demo.json"


def model_signature():
    return {
        "fluent_version": "Ansys Fluent 2026 R1",
        "dimension": 3,
        "physics": {"energy": True, "turbulence": True},
        "boundaries": [
            {"name": "feed", "type": "velocity_inlet"},
            {"name": "product", "type": "pressure_outlet"},
        ],
        "cell_zones": [
            {"name": "air", "type": "fluid"},
            {"name": "heater", "type": "solid"},
        ],
        "observations": [
            {"rule_id": "velocity", "object_name": "feed", "value": 1.0, "editable": True},
            {"rule_id": "temperature", "object_name": "feed", "value": 300.0, "editable": True},
        ],
    }


def profile():
    parameters = resolve_parameters(model_signature())
    return {
        "model_id": "model-1",
        "name": "generic.cas.h5",
        "case_file": "C:/cases/generic.cas.h5",
        "case_sha256": "a" * 64,
        "signature_sha256": "b" * 64,
        "fluent_version": "Ansys Fluent 2026 R1",
        "product_version": "26.1.0",
        "dimension": 3,
        "research_question": "降低发热体最高温度",
        "recommended_parameters": [item.key for item in parameters],
        "signature": model_signature(),
    }


def test_metric_catalog_supports_volume_max_and_mass_balance():
    metrics = resolve_metrics(model_signature())
    heater_max = next(item for item in metrics if item.kind == "volume" and "最高温度" in item.label and "heater" in item.label)
    assert heater_max.report_type == "volume-max"
    assert {item.kind for item in metrics if "质量流量" in item.label} == {"flux"}


def test_volume_report_codegen_uses_cell_zones():
    template = json.loads(SPEC.read_text(encoding="utf-8"))
    parameters = {item.key: item for item in resolve_parameters(model_signature())}
    metrics = {item.key: item for item in resolve_metrics(model_signature())}
    plan = default_plan(profile(), parameters, metrics)
    spec = compile_experiment_spec(template, profile(), plan, parameters, metrics, "http://localhost:18000/mcp")
    code = build_report_setup_code(spec)
    assert ".volume" in code
    assert ".cell_zones = ['heater']" in code
    assert "volume-max" in code


def test_approval_is_bound_to_model_and_full_plan():
    parameters = {item.key: item for item in resolve_parameters(model_signature())}
    metrics = {item.key: item for item in resolve_metrics(model_signature())}
    plan = default_plan(profile(), parameters, metrics)
    approval = approve_plan(profile(), plan, "researcher")
    assert_approval(profile(), plan, approval)
    changed = json.loads(json.dumps(plan))
    changed["budget"]["target_trials"] += 1
    with pytest.raises(PermissionError, match="失效"):
        assert_approval(profile(), changed, approval)


def test_generic_plan_compiles_without_template_objective_or_legacy_mass_names():
    template = json.loads(SPEC.read_text(encoding="utf-8"))
    parameters = {item.key: item for item in resolve_parameters(model_signature())}
    metrics = {item.key: item for item in resolve_metrics(model_signature())}
    plan = default_plan(profile(), parameters, metrics)
    plan["checks"]["mass_balance"] = False
    plan = normalize_plan(plan, parameters, metrics)
    spec = compile_experiment_spec(template, profile(), plan, parameters, metrics, "http://localhost:18000/mcp")
    assert spec.objective.report.startswith("vp-volume-heater-temperature-max")
    assert spec.optimization.enable_legacy_mass_balance is False
    assert all(report.name not in {"mass-flow-in", "mass-flow-out", "outlet-temperature"} for report in spec.reports)


def test_agent_rejects_catalog_escape_and_never_approves():
    parameters = {item.key: item for item in resolve_parameters(model_signature())}
    metrics = {item.key: item for item in resolve_metrics(model_signature())}
    response = SimulationAgent._validate_response(
        {
            "message": "我建议修改设置。",
            "needs_information": False,
            "questions": [],
            "plan_patch": {
                "parameter_keys": [next(iter(parameters)), "arbitrary.python.path"],
                "objective_metric_key": "missing-metric",
                "objective_direction": "minimize",
                "target_trials": 10,
                "iterations": 100,
            },
        },
        parameters,
        metrics,
    )
    assert response["plan_patch"]["parameter_keys"] == [next(iter(parameters))]
    assert response["plan_patch"]["objective_metric_key"] is None
    assert response["needs_information"] is True
    assert "approval" not in response


def test_web_plan_approval_becomes_stale_after_budget_change(tmp_path, monkeypatch):
    case = tmp_path / "generic.cas.h5"
    case.write_bytes(b"generic")

    async def scan(*args):
        return model_signature()

    monkeypatch.setattr("vegapunk.fluent.adaptive.scan_model", scan)
    app = create_app(spec_path=SPEC, output_dir=tmp_path / "runs", allow_remote_control=True)
    with TestClient(app) as client:
        analyzed = client.post("/api/models/analyze", json={
            "case_file": str(case), "endpoint": "http://localhost:18000/mcp",
            "product_version": "26.1.0", "question": "降低发热体最高温度",
        })
        assert analyzed.status_code == 200
        state = client.get("/api/state").json()
        assert state["plan"]["objective"]["metric_key"].startswith("volume-heater-temperature-max")
        assert state["approval"]["status"] == "required"
        saved_initial = client.post("/api/plans", json=state["plan"])
        assert saved_initial.status_code == 200
        approved = client.post("/api/plans/approve", json={"approved_by": "researcher"})
        assert approved.status_code == 200
        assert client.get("/api/state").json()["approval"]["status"] == "approved"

        changed = state["plan"]
        changed["budget"]["target_trials"] += 1
        saved = client.post("/api/plans", json=changed)
        assert saved.status_code == 200
        assert saved.json()["approval"]["status"] == "stale"


def test_agent_api_is_explicit_when_no_model_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    case = tmp_path / "generic.cas.h5"
    case.write_bytes(b"generic")

    async def scan(*args):
        return model_signature()

    monkeypatch.setattr("vegapunk.fluent.adaptive.scan_model", scan)
    app = create_app(spec_path=SPEC, output_dir=tmp_path / "runs", allow_remote_control=True)
    with TestClient(app) as client:
        client.post("/api/models/analyze", json={
            "case_file": str(case), "endpoint": "http://localhost:18000/mcp",
            "product_version": "26.1.0", "question": "温度",
        })
        state = client.get("/api/state").json()
        assert state["agent"]["configured"] is False
        response = client.post("/api/agent/messages", json={"message": "帮我制定方案"})
        assert response.status_code == 409
        assert "OPENAI_API_KEY" in response.json()["detail"]


def test_independent_verification_requires_gate_and_tolerance(tmp_path):
    template = json.loads(SPEC.read_text(encoding="utf-8"))
    parameters = {item.key: item for item in resolve_parameters(model_signature())}
    metrics = {item.key: item for item in resolve_metrics(model_signature())}
    plan = default_plan(profile(), parameters, metrics)
    plan["checks"]["mass_balance"] = False
    plan = normalize_plan(plan, parameters, metrics)
    spec = compile_experiment_spec(
        template, profile(), plan, parameters, metrics, "http://localhost:18000/mcp"
    )

    class FakeRunner:
        def __init__(self, *_args):
            pass

        async def open_session(self):
            return None

        async def evaluate_point(self, values, **_kwargs):
            return {
                "status": "completed",
                "parameters": dict(values),
                "objective_value": 300.3,
                "objective_unit": "K",
                "baseline_reloaded": True,
                "reports": {spec.objective.report: {"value": 300.3, "unit": "K"}},
                "constraints": [],
                "convergence": {"required": False, "passed": None},
                "monitor_history": {},
            }

        async def close_session(self):
            return None

    result = asyncio.run(run_independent_verification(
        spec, tmp_path, spec.design_points[0].values, 300.0, runner_factory=FakeRunner
    ))
    assert result["verified"] is False
    assert result["tolerance_mode"] == "absolute"
    assert result["allowed_difference"] == 1e-6
    explicitly_tolerated = asyncio.run(run_independent_verification(
        spec, tmp_path / "explicit", spec.design_points[0].values, 300.0,
        absolute_tolerance=0.5, runner_factory=FakeRunner,
    ))
    assert explicitly_tolerated["verified"] is True
    assert result["gate"]["status"] == "PASS"
    assert (tmp_path / "verification" / "result.json").is_file()
