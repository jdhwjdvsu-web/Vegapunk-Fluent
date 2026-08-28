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

from .optimizer import run_optimization
from .spec import ExperimentSpec, SpecError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC_PATH = PROJECT_ROOT / "config/fluent/mixing_elbow.optuna-demo.json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "runs/fluent_demo_v01"
DEFAULT_UI_DIR = PROJECT_ROOT / "integrations/fluent/ui"


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


class RunRequest(BaseModel):
    """Validated user-editable subset of the Fluent experiment contract."""

    model_config = ConfigDict(str_strip_whitespace=True)

    case_file: str = Field(min_length=1, max_length=1024)
    target_trials: int = Field(ge=1, le=100)
    velocity_min: float = Field(ge=0.001, le=1000)
    velocity_max: float = Field(ge=0.001, le=1000)
    iterations: int = Field(ge=1, le=1_000_000)
    endpoint: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def validate_range(self) -> RunRequest:
        if self.velocity_min >= self.velocity_max:
            raise ValueError("速度上限必须大于下限")
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
    parameters = raw.get("parameters") or [{}]
    optimization = raw.get("optimization") or {}
    solver = raw.get("solver") or {}
    return {
        "case_file": case_file,
        "target_trials": int(optimization.get("target_trials", 6)),
        "velocity_min": float(parameters[0].get("minimum", 0.4)),
        "velocity_max": float(parameters[0].get("maximum", 1.1)),
        "iterations": int(solver.get("iterations", 100)),
        "endpoint": os.environ.get(
            "FLUENT_MCP_ENDPOINT",
            str(connection.get("endpoint", "http://127.0.0.1:18000/mcp")),
        ),
    }


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

    parameters = [dict(item) for item in raw.get("parameters") or []]
    if len(parameters) != 1:
        raise SpecError("the current Fluent UI supports exactly one numeric parameter")
    parameters[0]["minimum"] = form.velocity_min
    parameters[0]["maximum"] = form.velocity_max
    raw["parameters"] = parameters

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
            if item.get("state") == "COMPLETE"
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

    async def start(self, spec: ExperimentSpec, target: int) -> None:
        self.refresh_task()
        if self.task is not None and not self.task.done():
            raise RuntimeError("已有优化任务正在运行")
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

    app = FastAPI(title="Vegapunk Fluent Lab", version="0.1.0")
    app.state.fluent_runtime = runtime

    @app.get("/api/state")
    async def get_state(request: Request) -> dict[str, Any]:
        runtime.refresh_task()
        summary, trials = runtime.result_state()
        endpoint = defaults["endpoint"]
        mcp = await runtime.mcp_status(endpoint)
        completed = len([item for item in trials if item.get("state") == "COMPLETE"])
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
        except (SpecError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        defaults.update(form.model_dump())
        return {"accepted": True, "status": "running"}

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
