import json
from pathlib import Path

from fastapi.testclient import TestClient

from vegapunk.fluent.web import (
    DirectRunRequest,
    PARAMETER_CATALOG,
    RunRequest,
    build_direct_run_spec,
    build_run_spec,
    create_app,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT_ROOT / "config/fluent/mixing_elbow.optuna-demo.json"


def test_build_run_spec_applies_only_bounded_ui_fields():
    form = RunRequest(
        case_file=r"C:\cases\mixing_elbow.cas.h5",
        target_trials=8,
        parameter_key="hot_inlet_temperature",
        range_min=300.0,
        range_max=340.0,
        iterations=150,
        endpoint="http://192.0.2.1:18000/mcp",
    )

    spec = build_run_spec(SPEC_PATH, form)

    assert spec.connection.connect_kwargs["case_file_name"] == form.case_file
    assert spec.connection.allow_remote_endpoint is True
    assert spec.parameters[0].name == "hot_inlet_temperature"
    assert spec.parameters[0].minimum == 300.0
    assert spec.parameters[0].maximum == 340.0
    assert spec.parameters[0].property_path == "thermal.temperature.value"
    assert spec.solver.iterations == 150
    assert spec.optimization is not None
    assert spec.optimization.target_trials == 8


def test_build_direct_run_spec_uses_exact_value_and_graphics_session():
    form = DirectRunRequest(
        case_file=r"C:\cases\mixing_elbow.cas.h5",
        parameter_key="cold_inlet_turbulence_intensity",
        value=5.0,
        iterations=200,
        endpoint="http://127.0.0.1:18000/mcp",
    )

    spec = build_direct_run_spec(SPEC_PATH, form)

    assert spec.parameters[0].minimum == 0.001
    assert spec.parameters[0].maximum == 0.3
    assert spec.design_points[0].values == {
        "cold_inlet_turbulence_intensity": 0.05
    }
    assert spec.solver.iterations == 200
    assert spec.connection.connect_kwargs["ui_mode"] == "hidden_gui"
    assert spec.connection.connect_kwargs["graphics_driver"] == "msw"


def test_state_api_reads_saved_results_and_allows_enabled_control(tmp_path):
    trials_dir = tmp_path / "trial_results"
    trials_dir.mkdir()
    trial = {
        "trial_number": 0,
        "parameters": {"cold_inlet_velocity": 0.55},
        "objective_value": 300.25,
        "state": "COMPLETE",
    }
    (trials_dir / "trial-0000.json").write_text(json.dumps(trial), encoding="utf-8")
    summary = {
        "target_completed_trials": 3,
        "completed_trials": 1,
        "best_trial_number": 0,
        "best_params": {"cold_inlet_velocity": 0.55},
        "best_value": 300.25,
    }
    (tmp_path / "demo_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    app = create_app(
        spec_path=SPEC_PATH,
        output_dir=tmp_path,
        allow_remote_control=True,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/state")
        page = client.get("/")

    assert response.status_code == 200
    payload = response.json()
    assert payload["server"]["can_control"] is True
    assert payload["job"]["completed_trials"] == 1
    assert payload["summary"]["best_value"] == 300.25
    assert payload["trials"] == [trial]
    assert payload["direct_run"]["status"] == "idle"
    assert {item["category"] for item in payload["parameters"]} == {
        "边界条件",
        "湍流",
        "材料物性",
    }
    assert len(payload["parameters"]) == len(PARAMETER_CATALOG)
    assert payload["task"]["id"] == "legacy"
    assert page.status_code == 200
    assert "Vegapunk-Fluent" in page.text


def test_remote_client_gets_read_only_state(tmp_path):
    app = create_app(spec_path=SPEC_PATH, output_dir=tmp_path)

    with TestClient(app, base_url="http://192.0.2.20") as client:
        payload = client.get("/api/state").json()
        rejected = client.post(
            "/api/runs",
            json={
                "case_file": r"C:\cases\mixing_elbow.cas.h5",
                "target_trials": 6,
                "parameter_key": "cold_inlet_velocity",
                "range_min": 0.4,
                "range_max": 1.1,
                "iterations": 100,
                "endpoint": "http://127.0.0.1:18000/mcp",
            },
        )
        direct_rejected = client.post(
            "/api/direct-runs",
            json={
                "case_file": r"C:\cases\mixing_elbow.cas.h5",
                "parameter_key": "cold_inlet_velocity",
                "value": 10,
                "iterations": 200,
                "endpoint": "http://127.0.0.1:18000/mcp",
            },
        )

    assert payload["server"] == {
        "can_control": False,
        "control_mode": "远程只读",
    }
    assert rejected.status_code == 403
    assert direct_rejected.status_code == 403


def test_new_task_archives_without_deleting_previous_results(tmp_path):
    previous = tmp_path / "demo_summary.json"
    previous.write_text('{"best_value": 299.0}', encoding="utf-8")
    app = create_app(
        spec_path=SPEC_PATH,
        output_dir=tmp_path,
        allow_remote_control=True,
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post("/api/tasks/new")
        state = client.get("/api/state").json()
        cleared = client.post("/api/agent/reset")

    assert created.status_code == 201
    assert previous.is_file()
    assert state["task"]["id"].startswith("task-")
    assert state["task"]["archives"][-1]["id"] == "legacy"
    assert state["summary"] is None
    assert state["trials"] == []
    assert cleared.json()["conversation_revision"] == 2
