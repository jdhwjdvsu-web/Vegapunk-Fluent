"""Small, same-origin web console for the Fluent optimization walking skeleton."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .direct_run import run_direct_temperature_case
from .optimizer import run_optimization
from .spec import ExperimentSpec, SpecError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC_PATH = PROJECT_ROOT / "config/fluent/mixing_elbow.optuna-demo.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "runs/fluent_demo_v01"
DEFAULT_UI_DIR = PROJECT_ROOT / "integrations/fluent/ui"


@dataclass(frozen=True)
class UIParameter:
    """One server-owned, user-selectable Fluent setting."""

    key: str
    label: str
    category: str
    zone: str
    collection_path: str
    object_name: str
    property_path: str
    unit: str
    hard_min: float
    hard_max: float
    recommended_min: float
    recommended_max: float
    default_value: float
    step: float
    description: str
    scale_to_native: float = 1.0
    log: bool = False

    def validate_display_value(self, value: float, label: str) -> float:
        numeric = float(value)
        if numeric < self.hard_min or numeric > self.hard_max:
            raise ValueError(
                f"{label} 必须在 {self.hard_min:g}–{self.hard_max:g} {self.unit} 之间"
            )
        return numeric

    def native_value(self, display_value: float) -> float:
        return float(display_value) * self.scale_to_native

    def spec_dict(self, display_min: float, display_max: float) -> dict[str, Any]:
        return {
            "name": self.key,
            "collection_path": self.collection_path,
            "object_name": self.object_name,
            "property_path": self.property_path,
            "unit": self.unit,
            "minimum": self.native_value(display_min),
            "maximum": self.native_value(display_max),
            "step": None
            if self.log
            else (self.step * self.scale_to_native if self.step else None),
            "log": self.log,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "zone": self.zone,
            "unit": self.unit,
            "hard_min": self.hard_min,
            "hard_max": self.hard_max,
            "recommended_min": self.recommended_min,
            "recommended_max": self.recommended_max,
            "default_value": self.default_value,
            "step": self.step,
            "description": self.description,
            "log": self.log,
            "native_to_display": 1.0 / self.scale_to_native,
        }


PARAMETER_CATALOG = (
    UIParameter(
        "cold_inlet_velocity", "冷入口速度", "边界条件", "cold-inlet",
        "setup.boundary_conditions.velocity_inlet", "cold-inlet",
        "momentum.velocity_magnitude.value", "m/s", 0.05, 10.0, 0.4, 1.1,
        1.0, 0.05, "控制冷流体进入弯管的速度。",
    ),
    UIParameter(
        "hot_inlet_velocity", "热入口速度", "边界条件", "hot-inlet",
        "setup.boundary_conditions.velocity_inlet", "hot-inlet",
        "momentum.velocity_magnitude.value", "m/s", 0.05, 10.0, 0.8, 1.6,
        1.2, 0.05, "控制热流体进入弯管的速度。",
    ),
    UIParameter(
        "cold_inlet_temperature", "冷入口温度", "边界条件", "cold-inlet",
        "setup.boundary_conditions.velocity_inlet", "cold-inlet",
        "thermal.temperature.value", "K", 250.0, 450.0, 283.15, 303.15,
        293.15, 0.5, "设置冷入口的静温。",
    ),
    UIParameter(
        "hot_inlet_temperature", "热入口温度", "边界条件", "hot-inlet",
        "setup.boundary_conditions.velocity_inlet", "hot-inlet",
        "thermal.temperature.value", "K", 250.0, 600.0, 303.15, 353.15,
        313.15, 0.5, "设置热入口的静温。",
    ),
    UIParameter(
        "outlet_gauge_pressure", "出口表压", "边界条件", "outlet",
        "setup.boundary_conditions.pressure_outlet", "outlet",
        "momentum.gauge_pressure.value", "Pa", -50000.0, 50000.0,
        -1000.0, 1000.0, 0.0, 100.0, "设置压力出口相对于操作压力的表压。",
    ),
    UIParameter(
        "cold_inlet_turbulence_intensity", "冷入口湍流强度", "湍流", "cold-inlet",
        "setup.boundary_conditions.velocity_inlet", "cold-inlet",
        "turbulence.turbulent_intensity", "%", 0.1, 30.0, 1.0, 10.0,
        5.0, 0.1, "网页使用百分数；提交 Fluent 时自动换算为比例。", 0.01,
    ),
    UIParameter(
        "hot_inlet_turbulence_intensity", "热入口湍流强度", "湍流", "hot-inlet",
        "setup.boundary_conditions.velocity_inlet", "hot-inlet",
        "turbulence.turbulent_intensity", "%", 0.1, 30.0, 1.0, 10.0,
        5.0, 0.1, "网页使用百分数；提交 Fluent 时自动换算为比例。", 0.01,
    ),
    UIParameter(
        "cold_inlet_hydraulic_diameter", "冷入口水力直径", "湍流", "cold-inlet",
        "setup.boundary_conditions.velocity_inlet", "cold-inlet",
        "turbulence.hydraulic_diameter", "m", 0.001, 2.0, 0.025, 0.15,
        0.1016, 0.001, "用于强度–水力直径湍流入口定义。",
    ),
    UIParameter(
        "hot_inlet_hydraulic_diameter", "热入口水力直径", "湍流", "hot-inlet",
        "setup.boundary_conditions.velocity_inlet", "hot-inlet",
        "turbulence.hydraulic_diameter", "m", 0.001, 2.0, 0.01, 0.08,
        0.0254, 0.001, "用于强度–水力直径湍流入口定义。",
    ),
    UIParameter(
        "air_density", "空气密度", "材料物性", "air",
        "setup.materials.fluid", "air", "density.value", "kg/m³",
        0.1, 5000.0, 0.8, 1.5, 1.225, 0.01, "修改当前 Case 中 air 材料的常密度。",
    ),
    UIParameter(
        "air_viscosity", "空气动力黏度", "材料物性", "air",
        "setup.materials.fluid", "air", "viscosity.value", "Pa·s",
        1e-7, 10.0, 1e-5, 3e-5, 1.7894e-5, 1e-6,
        "修改当前 Case 中 air 材料的常动力黏度。", log=True,
    ),
    UIParameter(
        "air_specific_heat", "空气定压比热", "材料物性", "air",
        "setup.materials.fluid", "air", "specific_heat.value", "J/(kg·K)",
        100.0, 10000.0, 900.0, 1200.0, 1006.43, 10.0,
        "修改当前 Case 中 air 材料的常定压比热。",
    ),
    UIParameter(
        "air_thermal_conductivity", "空气导热系数", "材料物性", "air",
        "setup.materials.fluid", "air", "thermal_conductivity.value", "W/(m·K)",
        0.001, 1000.0, 0.015, 0.05, 0.0242, 0.001,
        "修改当前 Case 中 air 材料的常导热系数。", log=True,
    ),
)
PARAMETERS_BY_KEY = {parameter.key: parameter for parameter in PARAMETER_CATALOG}
DEFAULT_PARAMETER_KEY = "cold_inlet_velocity"


def _parameter(key: str) -> UIParameter:
    try:
        return PARAMETERS_BY_KEY[key]
    except KeyError as exc:
        raise ValueError("所选参数不在当前 Case 的受控白名单中") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_loopback(hostname: str | None) -> bool:
    if not hostname:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _trial_documents(output_dir: Path) -> list[dict[str, Any]]:
    trials: list[dict[str, Any]] = []
    for path in sorted((output_dir / "trial_results").glob("trial-*.json")):
        document = _read_json(path)
        if document is not None:
            trials.append(document)
    return trials


class ParameterRangeRequest(BaseModel):
    """One bounded parameter selected for a web optimization Campaign."""

    model_config = ConfigDict(str_strip_whitespace=True)

    parameter_key: str = Field(min_length=1, max_length=128)
    range_min: float
    range_max: float

    @model_validator(mode="after")
    def validate_range(self) -> ParameterRangeRequest:
        parameter = _parameter(self.parameter_key)
        parameter.validate_display_value(self.range_min, "参数下限")
        parameter.validate_display_value(self.range_max, "参数上限")
        if self.range_min >= self.range_max:
            raise ValueError("参数上限必须大于下限")
        return self


class RunRequest(BaseModel):
    """Validated two-variable subset of the Fluent experiment contract."""

    model_config = ConfigDict(str_strip_whitespace=True)

    case_file: str = Field(min_length=1, max_length=1024)
    target_trials: int = Field(ge=1, le=100)
    parameters: list[ParameterRangeRequest] = Field(min_length=2, max_length=2)
    iterations: int = Field(ge=1, le=1_000_000)
    endpoint: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_distinct_parameters(self) -> RunRequest:
        keys = [item.parameter_key for item in self.parameters]
        if len(set(keys)) != len(keys):
            raise ValueError("两个优化变量不能相同")
        return self


class DirectParameterValue(BaseModel):
    """One exact parameter value used by an audited direct run."""

    model_config = ConfigDict(str_strip_whitespace=True)

    parameter_key: str = Field(min_length=1, max_length=128)
    value: float

    @model_validator(mode="after")
    def validate_value(self) -> DirectParameterValue:
        _parameter(self.parameter_key).validate_display_value(self.value, "参数值")
        return self


class DirectRunRequest(BaseModel):
    """Exact, bounded two-parameter run submitted from the local workbench."""

    model_config = ConfigDict(str_strip_whitespace=True)

    case_file: str = Field(min_length=1, max_length=1024)
    parameters: list[DirectParameterValue] = Field(min_length=2, max_length=2)
    iterations: int = Field(ge=1, le=1_000_000)
    endpoint: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_distinct_parameters(self) -> DirectRunRequest:
        keys = [item.parameter_key for item in self.parameters]
        if len(set(keys)) != len(keys):
            raise ValueError("两个审计参数不能相同")
        return self


def _load_raw_spec(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SpecError(f"cannot read Fluent experiment spec: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SpecError(f"invalid Fluent experiment spec: {exc}") from exc
    if not isinstance(value, dict):
        raise SpecError("experiment spec must be an object")
    return value


def _default_form(raw: dict[str, Any]) -> dict[str, Any]:
    connection = raw.get("connection", {})
    connect_kwargs = connection.get("connect_kwargs", {})
    case_file = str(connect_kwargs.get("case_file_name", ""))
    if case_file.startswith("${"):
        case_file = os.environ.get("FLUENT_CASE_FILE", "")
    optimization = raw.get("optimization") or {}
    solver = raw.get("solver") or {}
    selected = _parameter(DEFAULT_PARAMETER_KEY)
    second = _parameter("hot_inlet_temperature")
    return {
        "case_file": case_file,
        "target_trials": int(optimization.get("target_trials", 6)),
        "parameter_key": selected.key,
        "range_min": selected.recommended_min,
        "range_max": selected.recommended_max,
        "parameters": [
            {
                "parameter_key": selected.key,
                "range_min": selected.recommended_min,
                "range_max": selected.recommended_max,
            },
            {
                "parameter_key": second.key,
                "range_min": second.recommended_min,
                "range_max": second.recommended_max,
            },
        ],
        "direct_parameters": [
            {"parameter_key": selected.key, "value": selected.default_value},
            {"parameter_key": second.key, "value": second.default_value},
        ],
        "iterations": int(solver.get("iterations", 100)),
        "endpoint": os.environ.get(
            "FLUENT_MCP_ENDPOINT",
            str(connection.get("endpoint", "http://127.0.0.1:18000/mcp")),
        ),
    }


def _restore_form_from_campaign(
    defaults: dict[str, Any], output_dir: Path
) -> None:
    """Restore restart-safe case and two-parameter values from the audit trail."""

    campaign = _read_json(output_dir / "campaign.json") or {}
    payload = campaign.get("payload")
    if not isinstance(payload, dict):
        return
    baseline = payload.get("baseline")
    if isinstance(baseline, dict) and baseline.get("path"):
        defaults["case_file"] = str(baseline["path"])
    raw_parameters = payload.get("parameters")
    if not isinstance(raw_parameters, list):
        return
    parameters = []
    for raw_parameter in raw_parameters[:2]:
        if not isinstance(raw_parameter, dict):
            continue
        key = str(raw_parameter.get("name", ""))
        if key not in PARAMETERS_BY_KEY:
            continue
        catalog = _parameter(key)
        factor = 1.0 / catalog.scale_to_native
        parameters.append(
            {
                "parameter_key": key,
                "range_min": float(raw_parameter["minimum"]) * factor,
                "range_max": float(raw_parameter["maximum"]) * factor,
            }
        )
    if len(parameters) != 2:
        return
    defaults["parameters"] = parameters
    summary = _read_json(output_dir / "demo_summary.json") or {}
    best = summary.get("best_params")
    if not isinstance(best, dict):
        best = {}
    defaults["direct_parameters"] = [
        {
            "parameter_key": item["parameter_key"],
            "value": float(
                best.get(
                    item["parameter_key"],
                    _parameter(item["parameter_key"]).native_value(
                        _parameter(item["parameter_key"]).default_value
                    ),
                )
            )
            / _parameter(item["parameter_key"]).scale_to_native,
        }
        for item in parameters
    ]


def build_run_spec(spec_path: Path, form: RunRequest) -> ExperimentSpec:
    """Apply the UI's bounded fields to the full audited experiment template."""

    raw = _load_raw_spec(spec_path)
    connection = dict(raw.get("connection") or {})
    connect_kwargs = dict(connection.get("connect_kwargs") or {})
    connect_kwargs["case_file_name"] = form.case_file
    connection["connect_kwargs"] = connect_kwargs
    connection["endpoint"] = form.endpoint
    parsed = urlparse(form.endpoint)
    connection["allow_remote_endpoint"] = not _is_loopback(parsed.hostname)
    raw["connection"] = connection

    selected_parameters = [
        (
            _parameter(item.parameter_key),
            item.range_min,
            item.range_max,
        )
        for item in form.parameters
    ]
    raw["parameters"] = [
        parameter.spec_dict(range_min, range_max)
        for parameter, range_min, range_max in selected_parameters
    ]
    raw["design_points"] = [
        {
            "name": "baseline",
            "values": {
                parameter.key: parameter.native_value(parameter.default_value)
                for parameter, _range_min, _range_max in selected_parameters
            },
        }
    ]

    solver = dict(raw.get("solver") or {})
    solver["iterations"] = form.iterations
    raw["solver"] = solver

    optimization = dict(raw.get("optimization") or {})
    optimization["target_trials"] = form.target_trials
    optimization["n_startup_trials"] = min(
        int(optimization.get("n_startup_trials", 2)), form.target_trials
    )
    raw["optimization"] = optimization
    return ExperimentSpec.from_dict(raw)


def build_direct_run_spec(
    spec_path: Path, form: DirectRunRequest
) -> ExperimentSpec:
    """Build one exact point while retaining the audited Fluent path allowlist."""

    raw = _load_raw_spec(spec_path)
    connection = dict(raw.get("connection") or {})
    connect_kwargs = dict(connection.get("connect_kwargs") or {})
    connect_kwargs.update(
        {
            "case_file_name": form.case_file,
            "ui_mode": "hidden_gui",
            "graphics_driver": "msw",
            "cleanup_on_exit": True,
        }
    )
    connection["connect_kwargs"] = connect_kwargs
    connection["endpoint"] = form.endpoint
    parsed = urlparse(form.endpoint)
    connection["allow_remote_endpoint"] = not _is_loopback(parsed.hostname)
    connection["reuse_existing_session"] = False
    connection["disconnect_on_exit"] = True
    raw["connection"] = connection

    selected_parameters = [
        (_parameter(item.parameter_key), item.value) for item in form.parameters
    ]
    raw["parameters"] = [
        parameter.spec_dict(parameter.hard_min, parameter.hard_max)
        for parameter, _value in selected_parameters
    ]

    solver = dict(raw.get("solver") or {})
    solver["iterations"] = form.iterations
    raw["solver"] = solver
    native_values = {
        parameter.key: parameter.native_value(value)
        for parameter, value in selected_parameters
    }
    raw["task_name"] = "AutoFluentWebDirectParameterRun"
    raw["design_points"] = [
        {
            "name": "direct-two-parameter-audit",
            "values": native_values,
        }
    ]
    optimization = dict(raw.get("optimization") or {})
    optimization["target_trials"] = 1
    optimization["n_startup_trials"] = 1
    raw["optimization"] = optimization
    return ExperimentSpec.from_dict(raw)


@dataclass
class WebRuntime:
    spec_path: Path
    output_dir: Path
    allow_remote_control: bool = False
    objective_direction: str = "minimize"
    task: asyncio.Task[dict[str, Any]] | None = field(default=None, repr=False)
    job_status: str = "idle"
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    active_target: int | None = None
    direct_task: asyncio.Task[dict[str, Any]] | None = field(default=None, repr=False)
    direct_status: str = "idle"
    direct_started_at: str | None = None
    direct_finished_at: str | None = None
    direct_error: str | None = None
    direct_result: dict[str, Any] | None = None
    workspace_root: Path = field(init=False)
    task_id: str = "legacy"
    task_label: str = "原任务"
    task_created_at: str = field(default_factory=_utc_now)
    archived_tasks: list[dict[str, Any]] = field(default_factory=list)
    conversation_revision: int = 0

    def __post_init__(self) -> None:
        self.workspace_root = self.output_dir
        saved = _read_json(self._task_state_path)
        if saved is None:
            return
        current = saved.get("current")
        archives = saved.get("archives")
        if isinstance(archives, list):
            self.archived_tasks = [item for item in archives if isinstance(item, dict)]
        if not isinstance(current, dict):
            return
        candidate = Path(str(current.get("output_dir", ""))).resolve()
        tasks_root = (self.workspace_root / "web_tasks").resolve()
        if candidate != tasks_root and tasks_root not in candidate.parents:
            return
        self.output_dir = candidate
        self.task_id = str(current.get("id") or self.task_id)
        self.task_label = str(current.get("label") or self.task_label)
        self.task_created_at = str(current.get("created_at") or self.task_created_at)

    @property
    def _task_state_path(self) -> Path:
        return self.workspace_root / "web_tasks" / "task_state.json"

    def _task_record(self) -> dict[str, Any]:
        return {
            "id": self.task_id,
            "label": self.task_label,
            "created_at": self.task_created_at,
            "output_dir": str(self.output_dir),
        }

    def _persist_task_state(self) -> None:
        path = self._task_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {"current": self._task_record(), "archives": self.archived_tasks},
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def is_busy(self) -> bool:
        self.refresh_task()
        self.refresh_direct_task()
        return bool(
            (self.task is not None and not self.task.done())
            or (self.direct_task is not None and not self.direct_task.done())
        )

    def new_task(self, label: str | None = None) -> dict[str, Any]:
        if self.is_busy():
            raise RuntimeError("Fluent 正在计算，完成或终止后才能新建任务")
        archived = {**self._task_record(), "archived_at": _utc_now()}
        self.archived_tasks.append(archived)
        task_id = datetime.now(timezone.utc).strftime("task-%Y%m%dT%H%M%S%fZ")
        self.task_id = task_id
        self.task_label = (label or "新建 Fluent 任务").strip()[:120]
        self.task_created_at = _utc_now()
        self.output_dir = self.workspace_root / "web_tasks" / task_id
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.task = None
        self.job_status = "idle"
        self.started_at = None
        self.finished_at = None
        self.error = None
        self.active_target = None
        self.direct_task = None
        self.direct_status = "idle"
        self.direct_started_at = None
        self.direct_finished_at = None
        self.direct_error = None
        self.direct_result = None
        self.conversation_revision += 1
        self._persist_task_state()
        return self._task_record()

    def clear_conversation(self) -> int:
        self.conversation_revision += 1
        return self.conversation_revision

    def can_control(self, request: Request) -> bool:
        client_host = request.client.host if request.client is not None else None
        return self.allow_remote_control or _is_loopback(client_host)

    async def mcp_status(self, endpoint: str) -> dict[str, Any]:
        parsed = urlparse(endpoint)
        if not parsed.hostname or parsed.scheme not in {"http", "https"}:
            return {"status": "invalid", "detail": "MCP 地址格式无效"}
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(parsed.hostname, port), timeout=0.7
            )
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError):
            return {"status": "offline", "detail": f"{parsed.hostname}:{port} 未连接"}
        return {"status": "online", "detail": f"{parsed.hostname}:{port} 已连接"}

    def result_state(self) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        summary = _read_json(self.output_dir / "demo_summary.json")
        trials = _trial_documents(self.output_dir)
        complete = [
            item
            for item in trials
            if item.get("state") in {"COMPLETE", "PASS"}
            and isinstance(item.get("objective_value"), (int, float))
        ]
        if summary is not None or complete:
            summary = dict(summary or {})
            summary["completed_trials"] = len(complete)
            summary["trials"] = trials
            if complete:
                select = max if self.objective_direction == "maximize" else min
                best = select(complete, key=lambda item: float(item["objective_value"]))
                summary["best_trial_number"] = best.get("trial_number")
                summary["best_params"] = best.get("parameters")
                summary["best_value"] = float(best["objective_value"])
        return summary, trials

    def refresh_task(self) -> None:
        if self.task is None or not self.task.done() or self.finished_at is not None:
            return
        self.finished_at = _utc_now()
        if self.task.cancelled():
            self.job_status = "failed"
            self.error = "任务已取消；请确认 Fluent 状态后再继续"
            return
        exception = self.task.exception()
        if exception is not None:
            self.job_status = "failed"
            self.error = str(exception)
        else:
            self.job_status = "completed"
            self.error = None

    def refresh_direct_task(self) -> None:
        if (
            self.direct_task is None
            or not self.direct_task.done()
            or self.direct_finished_at is not None
        ):
            return
        self.direct_finished_at = _utc_now()
        if self.direct_task.cancelled():
            self.direct_status = "failed"
            self.direct_error = "单点计算已取消；请确认 Fluent 状态后再继续"
            return
        exception = self.direct_task.exception()
        if exception is not None:
            self.direct_status = "failed"
            self.direct_error = str(exception)
            return
        self.direct_status = "completed"
        self.direct_error = None
        self.direct_result = self.direct_task.result()

    async def start(self, spec: ExperimentSpec, target: int) -> None:
        self.refresh_task()
        self.refresh_direct_task()
        if self.task is not None and not self.task.done():
            raise RuntimeError("已有优化任务正在运行")
        if self.direct_task is not None and not self.direct_task.done():
            raise RuntimeError("已有单点 Fluent 任务正在运行")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.job_status = "running"
        self.started_at = _utc_now()
        self.finished_at = None
        self.error = None
        self.active_target = target
        self.task = asyncio.create_task(
            run_optimization(spec, self.output_dir, target_trials=target),
            name="fluent-optimization",
        )

    async def start_direct(
        self, spec: ExperimentSpec, form: DirectRunRequest
    ) -> None:
        self.refresh_task()
        self.refresh_direct_task()
        if self.task is not None and not self.task.done():
            raise RuntimeError("已有优化任务正在运行")
        if self.direct_task is not None and not self.direct_task.done():
            raise RuntimeError("已有单点 Fluent 任务正在运行")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.direct_status = "running"
        self.direct_started_at = _utc_now()
        self.direct_finished_at = None
        self.direct_error = None
        self.direct_result = None
        selected_parameters = [
            (_parameter(item.parameter_key), item.value) for item in form.parameters
        ]
        classification = (
            "baseline_range"
            if all(
                parameter.recommended_min <= value <= parameter.recommended_max
                for parameter, value in selected_parameters
            )
            else "out_of_baseline_range"
        )
        self.direct_task = asyncio.create_task(
            run_direct_temperature_case(
                spec,
                self.output_dir,
                parameter_values={
                    parameter.key: parameter.native_value(value)
                    for parameter, value in selected_parameters
                },
                iterations=form.iterations,
                classification=classification,
                parameter_metadata=[
                    {
                        "key": parameter.key,
                        "label": parameter.label,
                        "unit": parameter.unit,
                        "display_value": value,
                    }
                    for parameter, value in selected_parameters
                ],
            ),
            name="fluent-direct-temperature-run",
        )


def create_app(
    *,
    spec_path: str | Path = DEFAULT_SPEC_PATH,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    ui_dir: str | Path = DEFAULT_UI_DIR,
    allow_remote_control: bool = False,
) -> FastAPI:
    spec_file = Path(spec_path).resolve()
    output = Path(output_dir).resolve()
    static_dir = Path(ui_dir).resolve()
    raw_spec = _load_raw_spec(spec_file)
    runtime = WebRuntime(
        spec_path=spec_file,
        output_dir=output,
        allow_remote_control=allow_remote_control,
        objective_direction=str(
            (raw_spec.get("objective") or {}).get("direction", "minimize")
        ),
    )
    defaults = _default_form(raw_spec)
    _restore_form_from_campaign(defaults, runtime.output_dir)
    initial_defaults = dict(defaults)

    app = FastAPI(title="Vegapunk Fluent Lab", version="0.1.0")
    app.state.fluent_runtime = runtime

    @app.get("/api/state")
    async def get_state(request: Request) -> dict[str, Any]:
        runtime.refresh_task()
        runtime.refresh_direct_task()
        summary, trials = runtime.result_state()
        endpoint = defaults["endpoint"]
        mcp = await runtime.mcp_status(endpoint)
        completed = len(
            [item for item in trials if item.get("state") in {"COMPLETE", "PASS"}]
        )
        target = runtime.active_target or (
            summary.get("target_completed_trials")
            if summary
            else defaults["target_trials"]
        )
        return {
            "server": {
                "can_control": runtime.can_control(request),
                "control_mode": "本机可操作"
                if runtime.can_control(request)
                else "远程只读",
            },
            "job": {
                "status": runtime.job_status,
                "started_at": runtime.started_at,
                "finished_at": runtime.finished_at,
                "error": runtime.error,
                "completed_trials": completed,
                "target_trials": target,
            },
            "direct_run": {
                "status": runtime.direct_status,
                "started_at": runtime.direct_started_at,
                "finished_at": runtime.direct_finished_at,
                "error": runtime.direct_error,
                "result": runtime.direct_result,
            },
            "task": {
                **runtime._task_record(),
                "conversation_revision": runtime.conversation_revision,
                "archives": runtime.archived_tasks[-20:],
            },
            "parameters": [item.public_dict() for item in PARAMETER_CATALOG],
            "mcp": {"endpoint": endpoint, **mcp},
            "defaults": defaults,
            "summary": summary,
            "trials": trials,
        }

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def start_run(request: Request, form: RunRequest) -> dict[str, Any]:
        if not runtime.can_control(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="远程页面为只读；请在运行服务的电脑上打开本机地址",
            )
        try:
            spec = build_run_spec(spec_file, form)
            await runtime.start(spec, form.target_trials)
        except (SpecError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        defaults.update(form.model_dump())
        return {"accepted": True, "status": "running"}

    @app.post("/api/direct-runs", status_code=status.HTTP_202_ACCEPTED)
    async def start_direct_run(
        request: Request, form: DirectRunRequest
    ) -> dict[str, Any]:
        if not runtime.can_control(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="远程页面为只读；请在运行服务的电脑上提交计算",
            )
        try:
            spec = build_direct_run_spec(spec_file, form)
            await runtime.start_direct(spec, form)
        except (SpecError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        defaults.update(
            {
                "case_file": form.case_file,
                "iterations": form.iterations,
                "endpoint": form.endpoint,
                "direct_parameters": [item.model_dump() for item in form.parameters],
            }
        )
        return {"accepted": True, "status": "running"}

    @app.post("/api/agent/reset")
    async def reset_agent_conversation(request: Request) -> dict[str, Any]:
        if not runtime.can_control(request):
            raise HTTPException(status_code=403, detail="远程页面为只读")
        revision = runtime.clear_conversation()
        return {"cleared": True, "conversation_revision": revision}

    @app.post("/api/tasks/new", status_code=status.HTTP_201_CREATED)
    async def create_new_task(request: Request) -> dict[str, Any]:
        if not runtime.can_control(request):
            raise HTTPException(status_code=403, detail="远程页面为只读")
        try:
            task_record = runtime.new_task()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        defaults.clear()
        defaults.update(initial_defaults)
        return {"created": True, "task": task_record}

    @app.get(
        "/api/direct-runs/{run_id}/temperature-contour.png",
        include_in_schema=False,
    )
    async def direct_temperature_contour(run_id: str) -> FileResponse:
        runtime.refresh_direct_task()
        result = runtime.direct_result
        if result is None or result.get("run_id") != run_id:
            raise HTTPException(status_code=404, detail="温度云图不存在")
        path = Path(str((result.get("artifacts") or {}).get("temperature_contour")))
        if not path.is_file():
            raise HTTPException(status_code=404, detail="温度云图文件不存在")
        return FileResponse(path, media_type="image/png", filename=path.name)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    app.mount("/assets", StaticFiles(directory=static_dir), name="fluent-ui")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Vegapunk Fluent Lab UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--allow-remote-control",
        action="store_true",
        help="allow LAN/Tailscale clients to start Fluent runs (unsafe without auth)",
    )
    args = parser.parse_args()
    app = create_app(
        spec_path=args.spec,
        output_dir=args.output_dir,
        allow_remote_control=args.allow_remote_control,
    )
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
