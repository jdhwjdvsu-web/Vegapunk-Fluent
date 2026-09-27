"""Small, same-origin web console for the Fluent optimization walking skeleton."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .direct_run import run_direct_temperature_case
from .agent_graph import FluentAgentGraph
from .planning_attempts import PlanningAttemptStore
from .capability_registry import CapabilityRegistry
from .experiment_orchestrator import (
    ResolvedExperiment,
    compile_resolved_experiment,
    run_resolved_optimization,
)
from .optimizer import run_optimization
from .planning import (
    approve_plan,
    approve_resolved_task,
    assert_approval,
    assert_resolved_approval,
    compile_experiment_spec,
    default_plan,
    normalize_plan,
    save_plan,
)
from .simulation_agent import SimulationAgent
from .spec import ExperimentSpec, SpecError
from .verification import run_independent_verification, verification_is_trusted
from .execution_approval import ExecutionApproval, seal_execution, require_execution_approval

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC_PATH = PROJECT_ROOT / "config/fluent/mixing_elbow.optuna-demo.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "runs/fluent_demo_v01"
DEFAULT_UI_DIR = PROJECT_ROOT / "integrations/fluent/ui"


from .adaptive import AdaptiveModels
from .model_profile import UIParameter, MAX_PARAMETERS
from .profile_store import case_sha256
from .legacy_catalog import _parameter  # Compatibility for pre-discovery Python callers only.


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

    model_config = ConfigDict(str_strip_whitespace=True, allow_inf_nan=False, extra="forbid")

    parameter_key: str = Field(min_length=1, max_length=128)
    range_min: float
    range_max: float

    @model_validator(mode="after")
    def validate_range(self) -> ParameterRangeRequest:
        if self.range_min >= self.range_max:
            raise ValueError("参数上限必须大于下限")
        return self


class AnalyzeModelRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    case_file: str = Field(min_length=1, max_length=1024)
    endpoint: str = Field(min_length=1, max_length=2048)
    dimension: int = Field(default=3, ge=2, le=3)
    product_version: str = Field(default="", pattern=r"^(?:[0-9]{2}\.[0-9](?:\.[0-9])?)?$")
    question: str = Field(default="", max_length=4000)
    force: bool = False

    @model_validator(mode="after")
    def validate_endpoint(self):
        parsed = urlparse(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("MCP 地址必须是无内嵌凭据的 HTTP(S) 地址")
        return self


class SaveRangesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parameters: list[ParameterRangeRequest] = Field(min_length=1, max_length=MAX_PARAMETERS)

    @model_validator(mode="after")
    def unique(self):
        if len({item.parameter_key for item in self.parameters}) != len(self.parameters):
            raise ValueError("参数不能重复")
        return self


class ObjectiveRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    metric_key: str = Field(min_length=1, max_length=128)
    direction: str = Field(pattern=r"^(minimize|maximize)$")


class MetricConstraintRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, allow_inf_nan=False, extra="forbid")
    metric_key: str = Field(min_length=1, max_length=128)
    operator: str = Field(pattern=r"^(?:<|<=|>|>=|==)$")
    value: float


class ChecksRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    finite_outputs: bool = True
    mass_balance: bool = False
    mass_balance_relative_tolerance: float = Field(default=0.001, gt=0, lt=1)
    residual_thresholds: dict[str, float] = Field(default_factory=dict)


class BudgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_trials: int = Field(default=6, ge=1, le=1000)
    iterations: int = Field(default=100, ge=1, le=1_000_000)


class PlanRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, allow_inf_nan=False, extra="forbid")
    schema_version: int = Field(default=1, ge=1, le=1)
    research_question: str = Field(default="", max_length=4000)
    parameters: list[ParameterRangeRequest] = Field(min_length=1, max_length=MAX_PARAMETERS)
    objective: ObjectiveRequest
    constraints: list[MetricConstraintRequest] = Field(default_factory=list, max_length=20)
    checks: ChecksRequest = Field(default_factory=ChecksRequest)
    budget: BudgetRequest = Field(default_factory=BudgetRequest)


class ApprovePlanRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    approved_by: str = Field(default="local-user", min_length=1, max_length=120)
    plan_revision: int | None = Field(default=None, ge=1)
    planning_attempt_id: str | None = Field(default=None, max_length=120)


class AgentMessageRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    message: str = Field(min_length=1, max_length=8000)
    input_category: Literal[
        "initial_request",
        "physical_clarification",
        "plan_revision",
        "technical_repair",
        "retry",
        "user_message",
    ] = "user_message"
    reasoning: Literal["low", "medium", "high"] | None = None


class RunRequest(BaseModel):
    """Validated multi-variable subset of the Fluent experiment contract."""

    model_config = ConfigDict(str_strip_whitespace=True, allow_inf_nan=False, extra="forbid")

    case_file: str = Field(min_length=1, max_length=1024)
    target_trials: int = Field(ge=1, le=1000)
    parameters: list[ParameterRangeRequest] | None = Field(default=None, min_length=1, max_length=MAX_PARAMETERS)
    iterations: int = Field(ge=1, le=1_000_000)
    endpoint: str = Field(min_length=1, max_length=2048)
    start_request_id: str | None = Field(default=None, min_length=8, max_length=160)

    @model_validator(mode="after")
    def validate_distinct_parameters(self) -> RunRequest:
        keys = [item.parameter_key for item in (self.parameters or [])]
        if len(set(keys)) != len(keys):
            raise ValueError("优化变量不能相同")
        return self


class DirectParameterValue(BaseModel):
    """One exact parameter value used by an audited direct run."""

    model_config = ConfigDict(str_strip_whitespace=True, allow_inf_nan=False, extra="forbid")

    parameter_key: str = Field(min_length=1, max_length=128)
    value: float

    @model_validator(mode="after")
    def validate_value(self) -> DirectParameterValue:
        return self


class DirectRunRequest(BaseModel):
    """Exact, bounded multi-parameter run submitted from the local workbench."""

    model_config = ConfigDict(str_strip_whitespace=True, allow_inf_nan=False, extra="forbid")

    case_file: str = Field(min_length=1, max_length=1024)
    parameters: list[DirectParameterValue] = Field(min_length=1, max_length=MAX_PARAMETERS)
    iterations: int = Field(ge=1, le=1_000_000)
    endpoint: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_distinct_parameters(self) -> DirectRunRequest:
        keys = [item.parameter_key for item in self.parameters]
        if len(set(keys)) != len(keys):
            raise ValueError("审计参数不能相同")
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
    return {
        "case_file": case_file,
        "dimension": int(connect_kwargs.get("dimension", 3)),
        "target_trials": int(optimization.get("target_trials", 6)),
        "parameters": [],
        "direct_parameters": [],
        "iterations": int(solver.get("iterations", 100)),
        "endpoint": os.environ.get(
            "FLUENT_MCP_ENDPOINT",
            str(connection.get("endpoint", "http://127.0.0.1:18000/mcp")),
        ),
    }


def _restore_form_from_campaign(
    defaults: dict[str, Any], output_dir: Path
) -> None:
    """Restore restart-safe case and multi-parameter values from the audit trail."""

    campaign = _read_json(output_dir / "campaign.json") or {}
    payload = campaign.get("payload")
    if not isinstance(payload, dict):
        return
    baseline = payload.get("baseline")
    if isinstance(baseline, dict) and baseline.get("path"):
        defaults["case_file"] = str(baseline["path"])


def build_run_spec(spec_path: Path, form: RunRequest, catalog: dict[str, UIParameter] | None = None) -> ExperimentSpec:
    """Apply the UI's bounded fields to the full audited experiment template."""

    lookup = (lambda key: catalog[key]) if catalog is not None else _parameter
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
            lookup(item.parameter_key),
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
                parameter.key: parameter.native_value(min(max(parameter.default_value, _range_min), _range_max))
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
    spec_path: Path, form: DirectRunRequest, catalog: dict[str, UIParameter] | None = None
) -> ExperimentSpec:
    """Build one exact point while retaining the audited Fluent path allowlist."""

    lookup = (lambda key: catalog[key]) if catalog is not None else _parameter
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
        (lookup(item.parameter_key), item.value) for item in form.parameters
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
            "name": "direct-parameter-audit",
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
    planning_mode: str | None = None
    start_requests: dict[str, dict[str, Any]] = field(default_factory=dict)

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
        mode = current.get("planning_mode")
        self.planning_mode = mode if mode in {"agent_v3", "manual"} else None
        saved_requests = current.get("start_requests")
        self.start_requests = dict(saved_requests) if isinstance(saved_requests, dict) else {}

    @property
    def _task_state_path(self) -> Path:
        return self.workspace_root / "web_tasks" / "task_state.json"

    def _task_record(self) -> dict[str, Any]:
        return {
            "id": self.task_id,
            "label": self.task_label,
            "created_at": self.task_created_at,
            "output_dir": str(self.output_dir),
            "planning_mode": self.planning_mode,
            "start_requests": self.start_requests,
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
        self.planning_mode = None
        self.start_requests = {}
        self.conversation_revision += 1
        self._persist_task_state()
        return self._task_record()

    def clear_conversation(self) -> int:
        self.conversation_revision += 1
        self.planning_mode = None
        self.start_requests = {}
        self._persist_task_state()
        return self.conversation_revision

    def set_planning_mode(self, mode: str) -> None:
        if mode not in {"agent_v3", "manual"}:
            raise ValueError(f"不支持的规划模式：{mode}")
        self.planning_mode = mode
        self._persist_task_state()

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
        async def optimize_and_verify() -> dict[str, Any]:
            summary = await run_optimization(
                spec, self.output_dir, target_trials=target
            )
            if (
                summary.get("best_params")
                and isinstance(summary.get("best_value"), (int, float))
            ):
                summary["verification"] = await run_independent_verification(
                    spec,
                    self.output_dir,
                    summary["best_params"],
                    float(summary["best_value"]),
                )
                summary["best_status"] = "BEST_VERIFIED" if verification_is_trusted(
                    summary["verification"], summary["best_params"], summary["best_value"]) else "BEST_OBSERVED"
                from .history import atomic_json
                atomic_json(self.output_dir / "demo_summary.json", summary)
            return summary

        self.task = asyncio.create_task(
            optimize_and_verify(), name="fluent-optimization-and-verification"
        )

    async def start_resolved(
        self,
        experiment: ResolvedExperiment,
        registry: CapabilityRegistry,
        agent_graph: FluentAgentGraph,
    ) -> None:
        self.refresh_task()
        self.refresh_direct_task()
        if self.is_busy():
            raise RuntimeError("已有 Fluent 计算正在运行")
        require_execution_approval(experiment, registry)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.job_status = "running"
        self.started_at = _utc_now()
        self.finished_at = None
        self.error = None
        self.active_target = experiment.resolved_task.task.termination.max_trials

        async def optimize_record_and_interpret() -> dict[str, Any]:
            summary = await run_resolved_optimization(
                experiment, self.output_dir, registry
            )
            from .history import atomic_json

            atomic_json(self.output_dir / "demo_summary.json", summary)
            await agent_graph.record_experiment_result(
                self.task_id,
                {
                    "campaign_id": summary.get("campaign_id"),
                    "active_trial_id": (
                        f"trial-{int(summary['best_trial_number']):04d}"
                        if summary.get("best_trial_number") is not None
                        else None
                    ),
                    "solved_trials": summary.get("solved_trials", 0),
                    "completed_trials": summary.get("completed_trials", 0),
                    "feasible_trials": summary.get("feasible_trials", 0),
                    "failed_trials": summary.get("failed_trials", 0),
                    "best_result": summary.get("best_result"),
                    "termination_reason": summary.get("termination_reason"),
                },
            )
            return summary

        self.task = asyncio.create_task(
            optimize_record_and_interpret(), name="fluent-agent-v3-optimization"
        )

    async def start_direct(
        self, spec: ExperimentSpec, form: DirectRunRequest, catalog: dict[str, UIParameter] | None = None
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
        lookup = (lambda key: catalog[key]) if catalog is not None else _parameter
        selected_parameters = [
            (lookup(item.parameter_key), item.value) for item in form.parameters
        ]
        classification = (
            "baseline_range"
            if all(
                parameter.recommended_min is not None
                and parameter.recommended_max is not None
                and parameter.recommended_min <= value <= parameter.recommended_max
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
    agent_runtime: Any | None = None,
    agent_model_id: str | None = None,
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
    models = AdaptiveModels(output, raw_spec)
    models.restore_selection(runtime.output_dir)
    models.apply_defaults(defaults)
    control_lock = asyncio.Lock()
    agent_lock = asyncio.Lock()
    agent_config = (_read_json(PROJECT_ROOT / "config/fluent/agent_v3.json") or {}).get("agent", {})
    attempt_store = PlanningAttemptStore(
        output / "model_library" / "planning_attempts",
        timeout_seconds=float(agent_config.get("attempt_timeout_seconds", 480)),
    )
    agent = (
        SimulationAgent(agent_runtime, model_id=agent_model_id)
        if agent_runtime is not None
        else SimulationAgent.from_environment()
    )

    def current_identity(expected: dict[str, Any]) -> bool:
        profile = models.profile or {}
        attempt_id = expected.get("planning_attempt_id")
        return (
            expected.get("task_id") == runtime.task_id
            and expected.get("conversation_revision") == runtime.conversation_revision
            and expected.get("model_id") == profile.get("model_id")
            and expected.get("case_sha256") == profile.get("case_sha256")
            and expected.get("model_signature_sha256") == profile.get("signature_sha256")
            and (attempt_id is None or attempt_store.is_current(runtime.task_id, str(attempt_id)))
        )

    agent_graph = FluentAgentGraph(
        agent.runtime,
        model_id=agent.model_id,
        database_path=output / "model_library" / "agent_checkpoints.sqlite3",
        output_dir_provider=lambda: runtime.output_dir,
        identity_checker=current_identity,
        timeout_seconds=float(agent_config.get("timeout_seconds", 120)),
        max_classification_revisions=int(agent_config.get("max_classification_revisions", 3)),
        max_mapping_revisions=int(agent_config.get("max_mapping_revisions", 3)),
        network_retries=int(agent_config.get("network_retries", 1)),
        unavailable_reason=agent.reason,
    )
    if models.profile:
        agent_graph.bind_registry(
            CapabilityRegistry(models.profile, models.catalog(), models.metrics())
        )

    def active_attempt() -> dict[str, Any] | None:
        return attempt_store.active_for_task(runtime.task_id)

    def planning_busy() -> bool:
        return agent_lock.locked() or active_attempt() is not None

    def plan_path() -> Path:
        return runtime.output_dir / "experiment_plan.json"

    def approval_path() -> Path:
        return runtime.output_dir / "plan_approval.json"

    def v3_approval_context(graph_state: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "task_id": runtime.task_id,
            "conversation_revision": runtime.conversation_revision,
            "plan_revision": int(graph_state.get("plan_revision", 0)),
            "planning_attempt_id": graph_state.get("planning_attempt_id"),
        }

    def current_plan() -> dict[str, Any] | None:
        value = _read_json(plan_path())
        if value is not None or not models.profile:
            return value
        value = default_plan(
            models.profile,
            models.catalog(),
            models.metrics(),
            target_trials=int(defaults.get("target_trials", 6)),
            iterations=int(defaults.get("iterations", 100)),
        )
        if value is not None:
            save_plan(plan_path(), value)
        return value

    def approval_state(
        plan: dict[str, Any] | None,
        graph_state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        approval = _read_json(approval_path())
        if not models.profile or not approval:
            return {"status": "required", "record": None}
        try:
            is_v3_approval = bool(approval.get("payload", {}).get("agent_v3"))
            if runtime.planning_mode == "agent_v3" and not is_v3_approval:
                raise PermissionError("当前任务需要 V3 审批")
            if runtime.planning_mode == "manual" and is_v3_approval:
                raise PermissionError("当前任务需要手动方案审批")
            if is_v3_approval:
                if not approval.get("execution_approval"):
                    raise PermissionError("旧 V3 审批缺少完整执行契约")
                resolved = (graph_state or {}).get("resolved_task")
                if not resolved:
                    raise PermissionError("V3 Graph State 不存在")
                assert_resolved_approval(
                    models.profile,
                    resolved,
                    approval,
                    v3_approval_context(graph_state or {}),
                )
            else:
                if not plan:
                    raise PermissionError("实验方案不存在")
                assert_approval(models.profile, plan, approval)
        except (PermissionError, ValueError, KeyError):
            return {"status": "stale", "record": approval}
        return {"status": "approved", "record": approval}

    async def execute_planning_attempt(attempt_id: str) -> None:
        record = attempt_store.get(attempt_id)
        if record is None:
            return
        attempt_store.update(attempt_id, status="running", started_at=_utc_now())
        identity = dict(record["identity"])
        try:
            reply_state = await asyncio.wait_for(
                agent_graph.message(
                    str(record["message"]),
                    task_id=str(record["task_id"]),
                    conversation_revision=int(identity["conversation_revision"]),
                    model_id=str(identity["model_id"]),
                    case_sha256=str(identity["case_sha256"]),
                    model_signature_sha256=str(identity["model_signature_sha256"]),
                    planning_attempt_id=attempt_id,
                    input_category=str(record["input_category"]),
                    effective_reasoning=str(record["effective_reasoning"]),
                ),
                timeout=float(record["timeout_seconds"]),
            )
        except asyncio.TimeoutError:
            await agent_graph.set_terminal_state(
                str(record["task_id"]),
                status="timed_out",
                message="本次完整规划超过总时间预算，已终止。可以显式重试。",
                error="planning_attempt_deadline_exceeded",
            )
            attempt_store.update(
                attempt_id,
                status="timed_out",
                graph_status="timed_out",
                current_node=None,
                finished_at=_utc_now(),
                error="planning_attempt_deadline_exceeded",
            )
            return
        except asyncio.CancelledError:
            if (attempt_store.get(attempt_id) or {}).get("status") not in {"cancelled", "interrupted"}:
                attempt_store.update(
                    attempt_id,
                    status="cancelled",
                    graph_status="cancelled",
                    current_node=None,
                    finished_at=_utc_now(),
                    error="local_cancellation",
                )
            return
        except Exception as exc:
            attempt_store.update(
                attempt_id,
                status="failed",
                graph_status="failed",
                current_node=None,
                finished_at=_utc_now(),
                error=f"{type(exc).__name__}: {exc}",
            )
            return

        graph_status = str(reply_state.get("status") or "completed")
        attempt_store.update(
            attempt_id,
            status=graph_status,
            graph_status=graph_status,
            current_node=reply_state.get("current_node"),
            finished_at=_utc_now(),
            response={
                "message": reply_state.get("agent_message", ""),
                "questions": reply_state.get("clarification_questions", []),
                "has_task_object": bool(reply_state.get("task_object")),
                "has_resolved_task": bool(reply_state.get("resolved_task")),
                "validation_errors": reply_state.get("validation_errors", []),
            },
        )

    async def submit_planning_attempt(form: AgentMessageRequest) -> dict[str, Any]:
        if not models.profile:
            raise RuntimeError("请先分析 Fluent 模型")
        reasoning = form.reasoning or str(agent_config.get("reasoning_effort", "medium"))
        identity = {
            "task_id": runtime.task_id,
            "conversation_revision": runtime.conversation_revision,
            "model_id": models.profile["model_id"],
            "case_sha256": models.profile["case_sha256"],
            "model_signature_sha256": models.profile["signature_sha256"],
        }
        record = attempt_store.create(
            task_id=runtime.task_id,
            message=form.message,
            input_category=form.input_category,
            identity=identity,
            model_id=agent.model_id,
            effective_reasoning=reasoning,
        )
        runtime.set_planning_mode("agent_v3")
        task = asyncio.create_task(
            execute_planning_attempt(str(record["attempt_id"])),
            name=f"fluent-planning-{record['attempt_id']}",
        )
        attempt_store.attach(str(record["attempt_id"]), task)
        return record

    app = FastAPI(title="Vegapunk Fluent Lab", version="0.1.0")
    app.state.fluent_runtime = runtime
    app.state.adaptive_models = models
    app.state.simulation_agent = agent
    app.state.fluent_agent_graph = agent_graph
    app.state.planning_attempts = attempt_store

    @app.on_event("startup")
    async def start_agent_checkpoint() -> None:
        await agent_graph.start()

    @app.on_event("shutdown")
    async def close_agent_checkpoint() -> None:
        pending = []
        for attempt_id, task in list(attempt_store.tasks.items()):
            if not task.done():
                attempt_store.update(
                    attempt_id,
                    status="interrupted",
                    graph_status="interrupted",
                    current_node=None,
                    finished_at=_utc_now(),
                    error="服务关闭导致规划中断",
                )
                task.cancel()
                pending.append(task)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await agent_graph.close()

    @app.middleware("http")
    async def same_origin_mutations(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if (request.headers.get("sec-fetch-site") == "cross-site"
                    or (origin and origin.rstrip("/") != str(request.base_url).rstrip("/"))):
                return JSONResponse(status_code=403, content={"detail": "拒绝跨站控制请求"})
        return await call_next(request)

    @app.get("/api/models")
    async def model_library() -> dict:
        return {"models": [{"model_id": item["model_id"], "name": item["name"],
                            "case_file": item["case_file"], "fluent_version": item["fluent_version"],
                            "product_version": item.get("product_version", ""),
                            "dimension": item["dimension"], "updated_at": item["updated_at"]}
                           for item in models.store.list()]}

    @app.post("/api/models/analyze")
    async def analyze_model(request: Request, form: AnalyzeModelRequest) -> dict:
        if not runtime.can_control(request):
            raise HTTPException(status_code=403, detail="远程页面为只读")
        if control_lock.locked() or planning_busy() or runtime.is_busy():
            raise HTTPException(status_code=409, detail="已有模型扫描或计算正在运行")
        try:
            async with control_lock:
                profile, cached = await models.analyze(**form.model_dump())
                # Changing model is a task boundary: do not mix previous Trial results.
                if (runtime.output_dir / "campaign.json").exists() or _trial_documents(runtime.output_dir) or runtime.direct_result:
                    runtime.new_task()
                models.persist_selection(runtime.output_dir)
                models.apply_defaults(defaults)
                agent_graph.bind_registry(
                    CapabilityRegistry(profile, models.catalog(), models.metrics())
                )
            return {"profile": profile, "cached": cached}
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/models/ranges")
    async def save_model_ranges(request: Request, form: SaveRangesRequest) -> dict:
        if not runtime.can_control(request):
            raise HTTPException(status_code=403, detail="远程页面为只读")
        if control_lock.locked() or runtime.is_busy():
            raise HTTPException(status_code=409, detail="请等待当前操作完成")
        try:
            models.save_ranges(form.parameters, runtime.output_dir)
            models.apply_defaults(defaults)
            plan = current_plan()
            if plan is not None:
                updated = dict(plan)
                updated["parameters"] = [item.model_dump() for item in form.parameters]
                updated = normalize_plan(updated, models.catalog(), models.metrics())
                save_plan(plan_path(), updated)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"saved": True}

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
        plan = current_plan()
        graph_state = (
            await agent_graph.get_state(runtime.task_id) if models.profile else None
        )
        attempt = attempt_store.latest_for_task(runtime.task_id)
        if attempt and attempt.get("status") in {"queued", "running", "cancelling"} and graph_state:
            attempt["current_node"] = graph_state.get("current_node")
            attempt["graph_status"] = graph_state.get("status")
        return {
            "server": {
                "can_control": runtime.can_control(request),
                "control_mode": "本机可操作"
                if runtime.can_control(request)
                else "远程只读",
            },
            "runtime_reconciliation": _read_json(runtime.output_dir / "runtime_reconciliation.json"),
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
            "parameters": [item.public_dict() for item in models.catalog().values()],
            "metrics": [item.public_dict() for item in models.metrics().values()],
            "model": models.state(),
            "plan": plan,
            "approval": approval_state(plan, graph_state),
            "agent": {**agent_graph.state_info(), "state": graph_state, "attempt": attempt},
            "mcp": {"endpoint": endpoint, **mcp},
            "defaults": defaults,
            "summary": summary,
            "trials": trials,
        }

    @app.post("/api/plans")
    async def save_experiment_plan(request: Request, form: PlanRequest) -> dict[str, Any]:
        if not runtime.can_control(request):
            raise HTTPException(status_code=403, detail="远程页面为只读")
        if runtime.is_busy() or models.busy:
            raise HTTPException(status_code=409, detail="请等待当前操作完成")
        if planning_busy():
            raise HTTPException(status_code=409, detail="Agent 正在规划，不能同时保存手动方案")
        if not models.profile:
            raise HTTPException(status_code=409, detail="请先分析 Fluent 模型")
        if runtime.planning_mode == "agent_v3":
            raise HTTPException(status_code=409, detail="当前任务已进入 Agent V3 模式；请新建任务后再使用手动方案")
        try:
            if runtime.planning_mode is None:
                existing_graph = await agent_graph.get_state(runtime.task_id)
                if existing_graph and (
                    existing_graph.get("task_object")
                    or existing_graph.get("resolved_task")
                    or existing_graph.get("status") not in {None, "idle"}
                ):
                    raise ValueError("这是缺少 planning_mode 的历史任务；请新建任务后显式选择规划方式")
            runtime.set_planning_mode("manual")
            plan = normalize_plan(form.model_dump(), models.catalog(), models.metrics())
            save_plan(plan_path(), plan)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        graph_state = await agent_graph.get_state(runtime.task_id)
        return {"saved": True, "plan": plan, "approval": approval_state(plan, graph_state)}

    @app.post("/api/plans/approve")
    async def approve_experiment_plan(
        request: Request, form: ApprovePlanRequest
    ) -> dict[str, Any]:
        if not runtime.can_control(request):
            raise HTTPException(status_code=403, detail="远程页面为只读")
        if runtime.is_busy() or models.busy:
            raise HTTPException(status_code=409, detail="请等待当前操作完成")
        if planning_busy():
            raise HTTPException(status_code=409, detail="Agent 正在规划，不能审批旧状态")
        if not models.profile:
            raise HTTPException(status_code=409, detail="没有可审批的实验方案")
        try:
            graph_state = await agent_graph.get_state(runtime.task_id)
            if runtime.planning_mode == "agent_v3":
                resolved = (graph_state or {}).get("resolved_task")
                if (
                    not graph_state
                    or graph_state.get("status") != "awaiting_approval"
                    or not resolved
                    or not resolved.get("executable")
                    or graph_state.get("validation_errors")
                    or any(
                        issue.get("status") == "open" and issue.get("severity") == "blocking"
                        for issue in graph_state.get("active_issues", [])
                    )
                ):
                    raise ValueError("V3 规划尚未形成可执行且已验证的待审批任务")
                if (
                    form.plan_revision != int((graph_state or {}).get("plan_revision", 0))
                    or form.planning_attempt_id != (graph_state or {}).get("planning_attempt_id")
                ):
                    raise ValueError("审批请求不是当前 V3 revision/attempt，请刷新后重试")
                record = approve_resolved_task(
                    models.profile,
                    resolved,
                    form.approved_by,
                    v3_approval_context(graph_state),
                )
                registry = CapabilityRegistry(models.profile, models.catalog(), models.metrics())
                experiment = compile_resolved_experiment(
                    raw_spec, models.profile, resolved, registry, models.profile["endpoint"],
                )
                record["execution_approval"] = seal_execution(
                    experiment, registry, approval_id=record["fingerprint"],
                    approved_by=form.approved_by, mode="V3_APPROVED",
                ).model_dump(mode="json")
            elif runtime.planning_mode == "manual":
                plan = current_plan()
                if plan is None:
                    raise ValueError("没有可审批的手动实验方案")
                record = approve_plan(models.profile, plan, form.approved_by)
            else:
                raise ValueError("请先选择 Agent V3 或手动规划模式")
            from .history import atomic_json
            atomic_json(approval_path(), record)
            if runtime.planning_mode == "agent_v3":
                graph_state = await agent_graph.approve(
                    runtime.task_id, record["fingerprint"]
                )
        except (PermissionError, RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "approved": True,
            "fingerprint": record["fingerprint"],
            "agent_status": (graph_state or {}).get("status"),
            "execution_mode": (record.get("execution_approval") or {}).get("mode", "MANUAL_LEGACY"),
        }

    @app.post("/api/agent/messages", status_code=status.HTTP_202_ACCEPTED)
    async def agent_message(request: Request, form: AgentMessageRequest) -> dict[str, Any]:
        if not runtime.can_control(request):
            raise HTTPException(status_code=403, detail="远程页面为只读")
        if not models.profile:
            raise HTTPException(status_code=409, detail="请先分析 Fluent 模型")
        if runtime.is_busy() or models.busy or control_lock.locked():
            raise HTTPException(status_code=409, detail="当前任务正在扫描、提交或执行")
        if planning_busy():
            raise HTTPException(status_code=409, detail="当前任务已有一条 Agent 规划消息正在处理")
        try:
            if not agent_graph.configured:
                raise RuntimeError(agent.reason or "大模型助手尚未配置")
            async with agent_lock:
                record = await submit_planning_attempt(form)
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "accepted": True,
            "attempt_id": record["attempt_id"],
            "status": record["status"],
            "deadline_at": record["deadline_at"],
            "effective_reasoning": record["effective_reasoning"],
        }

    @app.get("/api/agent/attempts/{attempt_id}")
    async def get_planning_attempt(attempt_id: str) -> dict[str, Any]:
        record = attempt_store.get(attempt_id)
        if record is None or record.get("task_id") != runtime.task_id:
            raise HTTPException(status_code=404, detail="规划 attempt 不存在")
        if record.get("status") in {"queued", "running", "cancelling"}:
            graph_state = await agent_graph.get_state(runtime.task_id)
            if graph_state:
                record["current_node"] = graph_state.get("current_node")
                record["graph_status"] = graph_state.get("status")
        return record

    @app.post("/api/agent/attempts/{attempt_id}/cancel")
    async def cancel_planning_attempt(request: Request, attempt_id: str) -> dict[str, Any]:
        if not runtime.can_control(request):
            raise HTTPException(status_code=403, detail="远程页面为只读")
        record = attempt_store.get(attempt_id)
        if record is None or record.get("task_id") != runtime.task_id:
            raise HTTPException(status_code=404, detail="规划 attempt 不存在")
        running_task = attempt_store.tasks.get(attempt_id)
        record = attempt_store.cancel(attempt_id)
        if running_task is not None and not running_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(running_task), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        await agent_graph.set_terminal_state(
            runtime.task_id,
            status="cancelled",
            message="本次规划已取消。",
            error="local_cancellation",
        )
        return record

    @app.post("/api/agent/attempts/{attempt_id}/retry", status_code=status.HTTP_202_ACCEPTED)
    async def retry_planning_attempt(request: Request, attempt_id: str) -> dict[str, Any]:
        if not runtime.can_control(request):
            raise HTTPException(status_code=403, detail="远程页面为只读")
        previous = attempt_store.get(attempt_id)
        if previous is None or previous.get("task_id") != runtime.task_id:
            raise HTTPException(status_code=404, detail="规划 attempt 不存在")
        if previous.get("status") in {"queued", "running", "cancelling"}:
            raise HTTPException(status_code=409, detail="当前 attempt 尚未结束")
        if runtime.is_busy() or models.busy or control_lock.locked() or planning_busy():
            raise HTTPException(status_code=409, detail="当前任务正在扫描、规划、提交或执行")
        retry_form = AgentMessageRequest(
            message=str(previous["message"]),
            input_category="retry",
            reasoning=previous.get("effective_reasoning"),
        )
        try:
            async with agent_lock:
                record = await submit_planning_attempt(retry_form)
        except (RuntimeError, ValueError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "accepted": True,
            "attempt_id": record["attempt_id"],
            "status": record["status"],
            "retry_of": attempt_id,
        }

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def start_run(request: Request, form: RunRequest) -> dict[str, Any]:
        if not runtime.can_control(request):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="远程页面为只读；请在运行服务的电脑上打开本机地址",
            )
        try:
            async with control_lock:
                if planning_busy():
                    raise RuntimeError("Agent 正在规划，不能启动旧状态")
                if form.start_request_id and form.start_request_id in runtime.start_requests:
                    prior = runtime.start_requests[form.start_request_id]
                    return {
                        "accepted": True,
                        "status": prior.get("status", "running"),
                        "mode": prior.get("mode"),
                        "idempotent_replay": True,
                    }
                if runtime.is_busy():
                    raise RuntimeError("已有计算正在运行")
                plan = current_plan()
                approval = _read_json(approval_path())
                if approval is None:
                    raise PermissionError("请先保存并确认 Objective / Gate 实验方案")
                graph_state = await agent_graph.get_state(runtime.task_id)
                is_v3_approval = bool(approval.get("payload", {}).get("agent_v3"))
                if runtime.planning_mode == "agent_v3":
                    if not form.start_request_id:
                        raise PermissionError("V3 启动必须携带审批指纹作为幂等键")
                    if not is_v3_approval:
                        raise PermissionError("当前任务需要 V3 审批，禁止回退到手动方案")
                    if not graph_state or not graph_state.get("resolved_task"):
                        raise PermissionError("V3 ResolvedTaskObject 不存在")
                    if graph_state.get("status") != "ready_to_run":
                        raise PermissionError("V3 任务尚未完成审批或当前状态不可执行")
                    if any(
                        issue.get("status") == "open" and issue.get("severity") == "blocking"
                        for issue in graph_state.get("active_issues", [])
                    ):
                        raise PermissionError("V3 任务仍存在阻断问题")
                    assert_resolved_approval(
                        models.profile,
                        graph_state["resolved_task"],
                        approval,
                        v3_approval_context(graph_state),
                    )
                    resolved = graph_state["resolved_task"]
                    task_object = resolved["task"]
                    if form.case_file != models.profile["case_file"] or form.endpoint != models.profile["endpoint"]:
                        raise ValueError("模型或 MCP 地址已改变，请重新规划")
                    if form.target_trials != task_object["termination"]["max_trials"]:
                        raise PermissionError("Trial 预算与已审批 V3 任务不一致")
                    if form.iterations != task_object["solver_requirements"]["iterations"]:
                        raise PermissionError("求解迭代数与已审批 V3 任务不一致")
                    if await asyncio.to_thread(case_sha256, form.case_file) != models.profile["case_sha256"]:
                        raise ValueError("Case SHA256 已变化，请重新分析和审批")
                    registry = CapabilityRegistry(
                        models.profile, models.catalog(), models.metrics()
                    )
                    experiment = compile_resolved_experiment(
                        raw_spec,
                        models.profile,
                        resolved,
                        registry,
                        form.endpoint,
                    )
                    if not approval.get("execution_approval"):
                        raise PermissionError("旧 V3 审批缺少完整执行契约，请重新审批")
                    execution_approval = ExecutionApproval.model_validate(approval["execution_approval"])
                    if execution_approval.approval_id != approval["fingerprint"]:
                        raise PermissionError("完整执行契约与 V3 审批身份不一致")
                    experiment = replace(experiment, execution_approval=execution_approval)
                    require_execution_approval(experiment, registry)
                    runtime.objective_direction = experiment.semantic_spec.objective.direction
                    await runtime.start_resolved(experiment, registry, agent_graph)
                    runtime.start_requests[form.start_request_id] = {
                        "mode": "agent_v3",
                        "status": "running",
                        "started_at": _utc_now(),
                    }
                    runtime._persist_task_state()
                    models.record_run(runtime.output_dir)
                    defaults.update(form.model_dump())
                    return {"accepted": True, "status": "running", "mode": "agent_v3"}
                if runtime.planning_mode != "manual":
                    raise PermissionError("请先选择并审批一种规划模式")
                if is_v3_approval:
                    raise PermissionError("当前任务是手动模式，禁止使用 V3 审批记录")
                if plan is None:
                    raise PermissionError("实验方案不存在")
                if not form.parameters:
                    raise ValueError("手动模式至少需要一个优化参数")
                submitted = json.loads(json.dumps(plan))
                submitted["parameters"] = [item.model_dump() for item in form.parameters]
                submitted["budget"] = {
                    "target_trials": form.target_trials,
                    "iterations": form.iterations,
                }
                submitted = normalize_plan(
                    submitted, models.catalog(), models.metrics()
                )
                if submitted != plan:
                    raise PermissionError(
                        "提交内容与已审批方案不一致，请重新保存并确认"
                    )
                catalog = await models.validate_approved_run(form, plan, approval)
                spec = compile_experiment_spec(
                    raw_spec,
                    models.profile,
                    plan,
                    catalog,
                    models.metrics(),
                    form.endpoint,
                )
                models.save_ranges(form.parameters, runtime.output_dir)
                runtime.objective_direction = spec.objective.direction
                await runtime.start(spec, form.target_trials)
                models.record_run(runtime.output_dir)
        except (SpecError, PermissionError, RuntimeError, ValueError) as exc:
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
            async with control_lock:
                if planning_busy():
                    raise RuntimeError("Agent 正在规划，不能启动单点计算")
                if runtime.is_busy():
                    raise RuntimeError("已有计算正在运行")
                catalog = await models.validate_run(form)
                spec = models.bind_connection(build_direct_run_spec(spec_file, form, catalog))
                await runtime.start_direct(spec, form, catalog)
                models.record_run(runtime.output_dir)
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
        if planning_busy() or control_lock.locked() or runtime.is_busy():
            raise HTTPException(status_code=409, detail="当前任务正在规划、提交或执行")
        revision = runtime.clear_conversation()
        if models.profile:
            await agent_graph.reset(
                task_id=runtime.task_id,
                conversation_revision=revision,
                model_id=models.profile["model_id"],
                case_sha256=models.profile["case_sha256"],
                model_signature_sha256=models.profile["signature_sha256"],
            )
        return {"cleared": True, "conversation_revision": revision}

    @app.post("/api/tasks/new", status_code=status.HTTP_201_CREATED)
    async def create_new_task(request: Request) -> dict[str, Any]:
        if not runtime.can_control(request):
            raise HTTPException(status_code=403, detail="远程页面为只读")
        try:
            if models.busy or control_lock.locked() or planning_busy():
                raise RuntimeError("模型扫描、Agent 规划或提交正在进行")
            task_record = runtime.new_task()
            models.profile = None
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        defaults.clear()
        defaults.update(initial_defaults)
        models.apply_defaults(defaults)
        return {"created": True, "task": task_record}

    @app.get("/api/agent/state")
    async def get_agent_state() -> dict[str, Any]:
        state_value = await agent_graph.get_state(runtime.task_id)
        return {"agent": agent_graph.state_info(), "state": state_value}

    @app.get("/api/tasks/current/plan")
    async def get_current_agent_plan() -> dict[str, Any]:
        state_value = await agent_graph.get_state(runtime.task_id)
        return {"task_object": (state_value or {}).get("task_object")}

    @app.get("/api/tasks/current/resolved-task")
    async def get_current_resolved_task() -> dict[str, Any]:
        state_value = await agent_graph.get_state(runtime.task_id)
        return {"resolved_task": (state_value or {}).get("resolved_task")}

    @app.get("/api/tasks/current/geometry-recommendation")
    async def get_current_geometry_recommendation() -> dict[str, Any]:
        state_value = await agent_graph.get_state(runtime.task_id)
        return {"geometry_recommendations": (state_value or {}).get("geometry_recommendations", [])}

    @app.get("/api/tasks/current/geometry-verification-request")
    async def get_current_geometry_verification_request() -> dict[str, Any]:
        return {"request": _read_json(runtime.output_dir / "geometry_verification_request.json")}

    @app.get("/api/tasks/current/autonomy-report")
    async def get_current_autonomy_report() -> dict[str, Any]:
        state_value = await agent_graph.get_state(runtime.task_id) or {}
        audit_events: list[dict[str, Any]] = []
        audit_dir = runtime.output_dir / "planning_audit"
        for path in sorted(audit_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    audit_events.append(event)
        inputs = list(state_value.get("input_history", []))
        user_message_count = sum(
            1 for item in state_value.get("messages", []) if item.get("role") == "user"
        )
        uncategorized_user_inputs = max(0, user_message_count - len(inputs))
        intervention_coverage_complete = uncategorized_user_inputs == 0
        category_counts = {
            category: sum(1 for item in inputs if item.get("category") == category)
            for category in {
                "initial_request", "physical_clarification", "plan_revision",
                "technical_repair", "retry", "user_message",
            }
        }
        resolved = state_value.get("resolved_task") or {}
        classifications = [
            item.get("binding", {}).get("classification")
            for item in resolved.get("variables", [])
        ]
        trials = _trial_documents(runtime.output_dir)
        verification = _read_json(runtime.output_dir / "verification" / "result.json")
        approval = _read_json(approval_path())
        planning_attempts = [
            record for record in attempt_store.records.values()
            if record.get("task_id") == runtime.task_id
        ]
        model_audit_coverage_complete = bool(planning_attempts) or not state_value.get("messages")
        return {
            "task_id": runtime.task_id,
            "planning_mode": runtime.planning_mode,
            "plan_revision": state_value.get("plan_revision", 0),
            "planning_attempts": planning_attempts,
            "model_calls": {
                "coverage_complete": model_audit_coverage_complete,
                "recorded": len(audit_events),
                "completed": sum(1 for item in audit_events if item.get("outcome") == "completed"),
                "failed_or_timed_out": sum(1 for item in audit_events if item.get("outcome") in {"failed", "timed_out"}),
                "network_retries": sum(int(item.get("network_retry_count", 0)) for item in audit_events),
                "provider_request_ids_available": sum(1 for item in audit_events if item.get("provider_request_id")),
                "token_usage_available": sum(1 for item in audit_events if item.get("token_usage") is not None),
                "evidence_path": str(audit_dir),
            },
            "human_intervention": {
                "coverage_complete": intervention_coverage_complete,
                "uncategorized_user_inputs": uncategorized_user_inputs,
                "category_counts": category_counts,
                "physical_clarifications": category_counts["physical_clarification"],
                "technical_hints": category_counts["technical_repair"],
                "recorded_manual_contract_edits": 0,
                "approved": bool(approval),
                "note": None if intervention_coverage_complete else (
                    "该任务早于结构化干预审计；零计数不能解释为没有人工技术提示"
                ),
            },
            "execution": {
                "trial_records": len(trials),
                "fluent_execution_recorded": bool(trials),
                "independent_verification_recorded": bool(verification),
                "result_class": "proxy" if "MAPPED_PROXY" in classifications else "direct" if classifications else None,
                "geometry_verification_recorded": bool(_read_json(runtime.output_dir / "geometry_verification_result.json")),
            },
        }

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
    # Load developer-local credentials without overriding explicitly supplied
    # process environment variables.  The repository .gitignore excludes .env.
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
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
