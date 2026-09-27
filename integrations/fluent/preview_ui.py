"""Serve the Fluent workbench with deterministic sample data and no real control path."""

from __future__ import annotations

import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


UI_DIR = Path(__file__).resolve().parent / "ui"


TASK = {
    "schema_version": 3,
    "research_question": "研究 NACA0012 来流攻角对升阻比的影响，在质量守恒检查通过时寻找最大升阻比。",
    "variables": [{"id": "angle_of_attack", "name": "来流攻角", "semantic_key": "angle_of_attack", "minimum": -4, "maximum": 12, "initial_value": 2, "unit": "deg", "description": "自由来流相对翼弦的夹角"}],
    "objective": {"semantic_metric": "升阻比", "metric_key": "lift_to_drag_ratio", "direction": "maximize", "aggregation": "ratio", "location": "airfoil", "unit": "1"},
    "constraints": [],
    "solver_requirements": {"iterations": 400, "initialization": "hybrid", "residual_thresholds": {}, "mass_balance_required": True, "mass_balance_relative_tolerance": 0.001, "verification_relative_tolerance": 0.002, "verification_absolute_tolerance": 1e-6},
    "termination": {"max_trials": 12, "max_wall_time_seconds": 3600, "no_improvement_trials": 5, "target_objective": None, "maximum_failed_trials": 3},
    "assumptions": ["二维稳态不可压流", "角度正方向遵循当前模型坐标约定"],
    "questions": [],
    "needs_information": False,
}

BINDING = {
    "variable_id": "angle_of_attack",
    "classification": "MAPPED_PROXY",
    "classification_reason": "当前固定几何 Case 通过压力入口方向分量表达来流角度。",
    "selected_capability_id": "pressure_inlet_direction_angle",
    "candidate_parameter_ids": ["pressure_inlet_direction_x", "pressure_inlet_direction_y"],
    "candidate_metric_ids": ["lift_coefficient", "drag_coefficient", "lift_to_drag_ratio"],
    "confidence": 0.94,
    "missing_information": [],
}

MAPPING = {
    "mapping_id": "aoa_to_inlet_direction",
    "schema_version": 1,
    "mapping_version": "1.0",
    "source_variables": ["angle_of_attack"],
    "context_variables": [],
    "proxy_variables": ["inlet_angle_proxy"],
    "selected_capability_id": "pressure_inlet_direction_angle",
    "selected_fluent_parameter_ids": ["pressure_inlet_direction_x", "pressure_inlet_direction_y"],
    "forward_expression": {"inlet_angle_proxy": {"ref": "angle_of_attack"}, "pressure_inlet_direction_x": {"op": "cos_deg", "args": [{"ref": "inlet_angle_proxy"}]}, "pressure_inlet_direction_y": {"op": "sin_deg", "args": [{"ref": "inlet_angle_proxy"}]}},
    "reverse_expression": {"angle_of_attack": {"ref": "inlet_angle_proxy"}},
    "source_units": {"angle_of_attack": "deg"},
    "context_values": {},
    "output_units": {"inlet_angle_proxy": "deg", "pressure_inlet_direction_x": "1", "pressure_inlet_direction_y": "1", "angle_of_attack": "deg"},
    "coordinate_convention": "x 沿翼弦正向，y 向上",
    "angle_convention": "逆时针为正",
    "assumptions": ["入口方向向量归一化"],
    "validity_conditions": ["固定 NACA0012 几何", "二维稳态流"],
    "verification_route": "MANUAL_GEOMETRY",
    "confidence": 0.94,
    "automatic_geometry_execution": False,
}

PARAMETERS = [
    {"key": "pressure_inlet_direction_x", "label": "入口方向 X", "description": "压力入口单位方向向量 X 分量", "category": "边界条件", "unit": "1", "hard_min": -1, "hard_max": 1, "recommended_min": 0.978, "recommended_max": 1, "default_value": 1, "step": 0.001, "native_to_display": 1, "requires_range_confirmation": False},
    {"key": "pressure_inlet_direction_y", "label": "入口方向 Y", "description": "压力入口单位方向向量 Y 分量", "category": "边界条件", "unit": "1", "hard_min": -1, "hard_max": 1, "recommended_min": -0.07, "recommended_max": 0.208, "default_value": 0, "step": 0.001, "native_to_display": 1, "requires_range_confirmation": False},
]

METRICS = [
    {"key": "lift_to_drag_ratio", "label": "升阻比", "kind": "derived", "report_type": "ratio", "locations": ["airfoil"], "unit": "1", "roles": ["objective"], "recommended_direction": "maximize"},
]

TRIALS = [
    {"trial_number": index, "state": "COMPLETE", "sampling_phase": "startup_random" if index < 2 else "tpe", "parameters": {"angle_of_attack": angle}, "objective_value": ratio, "duration_seconds": 21 + index * 1.4, "gates": {"passed": True, "metrics": {"mass_balance_relative_error": 0.00018 + index * 0.000002}}}
    for index, (angle, ratio) in enumerate([(-2, 18.4), (8, 31.2), (3, 42.8), (5, 48.6), (6, 50.3), (6.8, 49.9), (5.7, 50.7), (5.9, 50.9)])
]


def state_payload() -> dict:
    resolved = {"schema_version": 3, "task": TASK, "variables": [{"intent": TASK["variables"][0], "binding": BINDING, "mapping_id": MAPPING["mapping_id"], "executable": True}], "mappings": [MAPPING], "executable": True, "geometry_unsupported": [], "validation_errors": [], "capability_registry_version": "preview-1", "mapping_dsl_schema_version": 1, "automatic_geometry_execution": False}
    return {
        "server": {"can_control": True, "control_mode": "样例预览"},
        "job": {"status": "completed", "started_at": "2026-09-20T09:00:00+08:00", "finished_at": "2026-09-20T09:05:00+08:00", "error": None, "completed_trials": len(TRIALS), "target_trials": 12},
        "direct_run": {"status": "idle", "started_at": None, "finished_at": None, "error": None, "result": None},
        "task": {"label": "NACA0012 攻角研究", "conversation_revision": 1, "planning_mode": "agent_v3", "archives": []},
        "parameters": PARAMETERS,
        "metrics": METRICS,
        "model": {"status": "ready", "execution_ready": True, "max_parameters": 5, "profile": {"model_id": "preview-naca0012", "name": "NACA0012 2D", "case_file": "D:\\samples\\naca0012.cas.h5", "case_sha256": "preview", "signature_sha256": "preview", "fluent_version": "25.2", "signature": {"warnings": []}}},
        "plan": None,
        "approval": {"status": "required"},
        "agent": {"configured": True, "model_id": "sample-planner", "reason": None, "attempt": None, "state": {"status": "awaiting_approval", "agent_message": "方案已完成结构化验证。来流攻角通过压力入口方向分量代理，执行前请确认坐标系与角度正方向。", "task_object": TASK, "variable_bindings": [BINDING], "mapping_specs": [MAPPING], "resolved_task": resolved, "clarification_questions": [], "mapping_review_issues": [], "active_issues": [], "issue_history": [], "validation_errors": [], "geometry_unsupported": [], "geometry_recommendations": []}},
        "mcp": {"status": "online", "detail": "样例服务，不连接 Fluent", "endpoint": "preview://disabled"},
        "defaults": {"case_file": "D:\\samples\\naca0012.cas.h5", "dimension": 2, "target_trials": 12, "iterations": 400, "endpoint": "preview://disabled", "parameters": [{"parameter_key": "pressure_inlet_direction_x", "range_min": 0.978, "range_max": 1}, {"parameter_key": "pressure_inlet_direction_y", "range_min": -0.07, "range_max": 0.208}], "direct_parameters": [{"parameter_key": "pressure_inlet_direction_x", "value": 0.995}, {"parameter_key": "pressure_inlet_direction_y", "value": 0.1}]},
        "summary": {"best_value": 50.9, "best_trial_number": 7, "best_params": {"angle_of_attack": 5.9}, "feasible_trials": 8, "termination_reason": "no_improvement", "verification": {"verified": True, "status": "passed", "verification_objective": 50.82, "objective_unit": "1", "allowed_difference": 0.102}},
        "trials": TRIALS,
    }


class PreviewHandler(BaseHTTPRequestHandler):
    server_version = "VegapunkPreview/1.0"

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/state":
            self._json(state_payload())
            return
        if path == "/api/models":
            self._json({"models": []})
            return
        if path.startswith("/api/agent/attempts/"):
            self._json({"attempt_id": path.rsplit("/", 1)[-1], "task_id": "preview", "status": "awaiting_approval", "graph_status": "awaiting_approval", "current_node": "human_approval_gate", "response": {"message": "样例方案已生成"}})
            return
        target = UI_DIR / ("index.html" if path == "/" else path.removeprefix("/assets/"))
        if path.startswith("/assets/") and target.is_file() or path == "/" and target.is_file():
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"detail": "preview route not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        path = urlparse(self.path).path
        if path == "/api/models/analyze":
            self._json({"profile": state_payload()["model"]["profile"], "cached": True})
        elif path == "/api/agent/messages":
            self._json({"accepted": True, "attempt_id": "preview-attempt", "status": "queued", "deadline_at": "2026-09-20T12:00:00+08:00", "effective_reasoning": "medium"}, 202)
        elif path == "/api/plans/approve":
            self._json({"approved": True, "fingerprint": "preview-only", "agent_status": "ready_to_run"})
        elif path in {"/api/runs", "/api/direct-runs"}:
            self._json({"accepted": True, "status": "preview_only", "detail": "样例预览不会执行 Fluent"}, 202)
        elif path in {"/api/agent/reset", "/api/tasks/new", "/api/models/ranges", "/api/plans"}:
            self._json({"ok": True, "conversation_revision": 2, "created": path == "/api/tasks/new"})
        else:
            self._json({"detail": "preview route not found"}, 404)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[preview] {self.address_string()} {format % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a safe, sample-only Fluent UI preview")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    print(f"Safe Fluent UI preview: http://127.0.0.1:{args.port}/?preview=1")
    ThreadingHTTPServer((args.host, args.port), PreviewHandler).serve_forever()


if __name__ == "__main__":
    main()
