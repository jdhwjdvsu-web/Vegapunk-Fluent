"""Run constrained Fluent design points through the official PyFluent-MCP."""

from __future__ import annotations

import asyncio
import hashlib
import json
import operator
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .codegen import (
    build_evaluate_point_code,
    extract_result_payload,
    normalize_computed_reports,
    normalize_spec_reports,
    parse_iteration_statistics,
    parse_last_residuals,
)
from .spec import ConstraintSpec, ExperimentSpec
from .history import append_jsonl, atomic_json
from .session_identity import SessionObservation, continuity_error


class FluentExperimentError(RuntimeError):
    """Raised when the MCP or Fluent execution contract fails."""


class FluentStateUncertainError(FluentExperimentError):
    """Raised when a submitted solve may still be running and must not be retried."""

    def __init__(self, message: str, *, diagnosis: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnosis = diagnosis


class DuplicateTrialError(FluentExperimentError):
    """An existing trial identity must not be submitted to Fluent again."""


_COMPARISONS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
}


def _structured(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if text:
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return {}


def _tool_error(result: Any) -> str | None:
    if getattr(result, "is_error", False):
        return "MCP tool returned an error"
    data = _structured(result)
    if data.get("status") == "error":
        return str(data.get("message") or data.get("error_code") or "unknown MCP error")
    return None


def _evaluate_constraints(
    reports: dict[str, dict[str, Any]], constraints: tuple[ConstraintSpec, ...]
) -> list[dict[str, Any]]:
    evaluations = []
    for constraint in constraints:
        actual = float(reports[constraint.report]["value"])
        passed = _COMPARISONS[constraint.operator](actual, constraint.value)
        evaluations.append(
            {
                "report": constraint.report,
                "operator": constraint.operator,
                "limit": constraint.value,
                "actual": actual,
                "passed": passed,
            }
        )
    return evaluations


def _evaluate_residuals(
    residuals: dict[str, Any] | None, thresholds: dict[str, float]
) -> dict[str, Any]:
    if not thresholds:
        return {"required": False, "passed": None, "checks": []}
    values = residuals.get("values", {}) if residuals else {}
    checks = []
    for name, threshold in thresholds.items():
        actual = values.get(name)
        checks.append(
            {
                "name": name,
                "threshold": threshold,
                "actual": actual,
                "passed": actual is not None and actual <= threshold,
            }
        )
    return {
        "required": True,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


class FluentExperimentRunner:
    """Orchestrate one validated experiment over a FastMCP client."""

    def __init__(
        self, spec: ExperimentSpec, output_dir: str | Path, client_factory=None
    ):
        self.spec = spec
        self.output_dir = Path(output_dir)
        self.client_factory = client_factory
        self._client_context = None
        self._client = None
        self._connected_by_runner = False
        self._state_uncertain = False
        self._session_observation: SessionObservation | None = None

    async def _check_session(self, stage: str, trial_id: str | None = None) -> None:
        try:
            status = await self._call(self._client, "session_status", {})
            current = SessionObservation.from_status(status)
            error = continuity_error(self._session_observation, current)
            append_jsonl(self.output_dir / "session_audit.jsonl", {
                **current.to_dict(), "stage": stage, "trial_id": trial_id,
                "continuity": "BLOCKED" if error else "CONFIRMED" if current.fluent_session_id else "UNVERIFIED_IDENTITY",
            })
        except Exception as exc:
            self._state_uncertain = True
            raise FluentStateUncertainError("Read-only session check failed; no new Fluent submission is permitted",
                                           diagnosis={"request_dispatched": False, "stage": stage}) from exc
        if error:
            self._state_uncertain = True
            raise FluentStateUncertainError(error, diagnosis={**current.to_dict(),
                                            "request_dispatched": False, "stage": stage})
        self._session_observation = current

    def _new_client(self):
        if self.client_factory is not None:
            return self.client_factory(
                self.spec.connection.endpoint,
                self.spec.connection.client_timeout_seconds,
            )
        try:
            from fastmcp import Client
        except ImportError as exc:
            raise FluentExperimentError(
                "fastmcp is required in the Vegapunk runtime to call PyFluent-MCP"
            ) from exc
        return Client(
            self.spec.connection.endpoint,
            timeout=self.spec.connection.client_timeout_seconds,
        )

    async def _call(
        self, client, name: str, arguments: dict[str, Any], *, timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        result = await client.call_tool(
            name,
            arguments,
            timeout=timeout_seconds or self.spec.connection.tool_timeout_seconds,
            raise_on_error=False,
        )
        error = _tool_error(result)
        if error:
            raise FluentExperimentError(f"{name} failed: {error}")
        return _structured(result)

    async def _validated_run_code(
        self, client, code: str, *, run_timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        validation = await self._call(client, "validate_code", {"code": code})
        if validation.get("status") != "ok":
            raise FluentExperimentError(
                f"PyFluent-MCP rejected generated code: {validation.get('message')}"
            )
        try:
            execution = await self._call(
                client, "run_code", {"code": code},
                timeout_seconds=run_timeout_seconds or self.spec.connection.solve_timeout_seconds,
            )
        except Exception as exc:
            self._state_uncertain = True
            raise FluentStateUncertainError(
                "the MCP connection failed after Fluent code was submitted; "
                "the solver state is uncertain and this trial must not be retried"
            ) from exc
        if execution.get("status") != "ok":
            raise FluentExperimentError(
                f"Fluent code execution failed: {execution.get('message')}"
            )
        return execution

    async def open_session(self) -> None:
        """Open one MCP client and one serial Fluent session for multiple trials."""

        if self._client is not None:
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._client_context = self._new_client()
        try:
            self._client = await self._client_context.__aenter__()
            status = await self._call(self._client, "session_status", {})
            if not isinstance(status.get("connected"), bool):
                raise FluentExperimentError("session_status did not confirm connection state; refusing connect")
            if status.get("connected"):
                if not self.spec.connection.reuse_existing_session:
                    raise FluentExperimentError(
                        "PyFluent-MCP already has a solver session; set "
                        "reuse_existing_session=true only when that session is intentional"
                    )
            else:
                connect_result = await self._call(
                    self._client,
                    "connect",
                    {"connect_kwargs": self.spec.connection.connect_kwargs},
                    timeout_seconds=self.spec.connection.connect_timeout_seconds,
                )
                if connect_result.get("status") != "ok":
                    raise FluentExperimentError(
                        f"Fluent connection failed: {connect_result.get('message')}"
                    )
                self._connected_by_runner = True
            await self._check_session("open_session")
        except Exception:
            if self._client_context is not None:
                await self._client_context.__aexit__(None, None, None)
            self._client_context = None
            self._client = None
            self._connected_by_runner = False
            raise

    def _validate_parameters(self, parameters: Mapping[str, Any]) -> dict[str, float]:
        by_name = {parameter.name: parameter for parameter in self.spec.parameters}
        missing = sorted(set(by_name) - set(parameters))
        extra = sorted(set(parameters) - set(by_name))
        if missing or extra:
            raise FluentExperimentError(
                f"parameter mismatch; missing={missing}, extra={extra}"
            )
        return {
            name: by_name[name].validate_value(value, f"parameters.{name}")
            for name, value in parameters.items()
        }

    async def evaluate_point(
        self,
        parameters: Mapping[str, Any],
        *,
        name: str,
        artifact_stem: str | None = None,
    ) -> dict[str, Any]:
        """Reload the baseline case, solve once, and return structured reports."""

        if self._client is None:
            raise FluentExperimentError(
                "open_session() must be called before evaluate_point()"
            )
        from .spec import DesignPointSpec

        values = self._validate_parameters(parameters)
        if self.spec.solver.thermal_guard:
            from .thermal_guard import verify_thermal_data
            verify_thermal_data(self.spec.solver.thermal_guard)
        point = DesignPointSpec(name=name, values=values)
        code = build_evaluate_point_code(self.spec, point)
        stem = re_safe_name(artifact_stem or name, fallback="trial")
        code_dir = self.output_dir / "generated_code"
        log_dir = self.output_dir / "solver_stdout"
        code_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
        if ((code_dir / f"{stem}.py").exists()
                or (self.output_dir / "trial_results" / f"{stem}.json").exists()):
            raise DuplicateTrialError(
                f"trial identity {stem} already has execution artifacts; refusing duplicate submission"
            )
        if self._state_uncertain:
            raise FluentStateUncertainError("Runner is blocked by an unresolved runtime state")
        await self._check_session("before_trial", stem)
        (code_dir / f"{stem}.py").write_text(code, encoding="utf-8")

        started = time.perf_counter()
        try:
            execution = await self._validated_run_code(self._client, code)
        except FluentStateUncertainError as exc:
            from .runtime_reliability import diagnose_runtime, save_diagnosis
            try:
                diagnosis = await diagnose_runtime(
                    self.output_dir, stem, self.spec.connection.endpoint,
                    parameters=values, request_dispatched=True,
                )
                save_diagnosis(self.output_dir, diagnosis)
                exc.diagnosis = diagnosis.to_dict()
            except Exception as diagnosis_exc:  # Preserve the submitted-solve error.
                exc.diagnosis = {"diagnosis_error": str(diagnosis_exc)}
            raise
        elapsed = time.perf_counter() - started
        stdout = str(execution.get("stdout", ""))
        (log_dir / f"{stem}.log").write_text(stdout, encoding="utf-8")
        payload = extract_result_payload(stdout)
        reports = normalize_spec_reports(payload.get("computed_reports"), self.spec)
        monitor_history: dict[str, list[float]] = {}
        if self.spec.optimization is not None:
            monitor_names = {
                monitor.report
                for monitor in self.spec.optimization.numerical_monitors
            }
            monitor_history = {name: [] for name in monitor_names}
            for snapshot in payload.get("monitor_history_raw", []):
                normalized_snapshot = normalize_computed_reports(
                    snapshot, monitor_names
                )
                for report_name in monitor_names:
                    monitor_history[report_name].append(
                        float(normalized_snapshot[report_name]["value"])
                    )
        objective_report = reports[self.spec.objective.report]
        constraints = _evaluate_constraints(reports, self.spec.constraints)
        residuals = parse_last_residuals(stdout)
        iteration_statistics = parse_iteration_statistics(stdout)
        convergence = _evaluate_residuals(
            residuals, self.spec.solver.residual_thresholds
        )
        result: dict[str, Any] = {
            "name": name,
            "status": "completed",
            "parameters": payload.get("parameters", values),
            "objective_value": float(objective_report["value"]),
            "objective_unit": objective_report.get("unit"),
            "reports": reports,
            "residuals": residuals,
            "convergence": convergence,
            "constraints": constraints,
            "monitor_history": monitor_history,
            "iterations_requested": self.spec.solver.iterations,
            **iteration_statistics,
            "elapsed_seconds": elapsed,
            "baseline_reloaded": True,
            "fluent_session_id": self._session_observation.fluent_session_id if self._session_observation else None,
            "session_identity_status": self._session_observation.identity_status if self._session_observation else "UNAVAILABLE",
            "error": None,
        }
        if (
            self.spec.optimization is not None
            and self.spec.optimization.enable_legacy_mass_balance
        ):
            optimization = self.spec.optimization
            inlet = reports[optimization.mass_flow_in_report]
            outlet = reports[optimization.mass_flow_out_report]
            result.update(
                {
                    "mass_flow_in": abs(float(inlet["value"])),
                    "mass_flow_out": abs(float(outlet["value"])),
                    "mass_flow_unit": inlet.get("unit") or outlet.get("unit"),
                }
            )
        if self.spec.solver.thermal_guard:
            from .thermal_guard import evaluate_thermal_guard
            result['thermal_guard'] = evaluate_thermal_guard(self.spec.solver.thermal_guard, payload.get('thermal_history_raw', []), stdout, self.spec.solver.residual_thresholds)
            result['thermal_history_raw'] = payload.get('thermal_history_raw', [])
        return result

    async def close_session(self) -> str | None:
        """Disconnect a runner-owned solver and close the MCP client context."""

        warning = None
        try:
            if self._state_uncertain:
                warning = "solver state uncertain; no automatic Fluent exit or disconnect was sent"
            elif (
                self._client is not None
                and self._connected_by_runner
                and self.spec.connection.disconnect_on_exit
            ):
                try:
                    await self._check_session("before_cleanup")
                except FluentStateUncertainError as exc:
                    return "solver session uncertain; no exit or disconnect was sent: " + str(exc)
                try:
                    if self.spec.solver.thermal_guard:
                        # This serial CHT workflow owns the session. Wait for its
                        # local processes to exit before the next licensed Job.
                        await self._validated_run_code(
                            self._client, 'solver.exit(timeout=15, wait=15)\n',
                            run_timeout_seconds=self.spec.connection.tool_timeout_seconds,
                        )
                except Exception as exc:
                    warning = 'explicit owned-session exit: ' + str(exc)
                    if self._state_uncertain:
                        return warning + '; no disconnect was sent'
                try:
                    await self._call(self._client, "disconnect", {})
                except Exception as exc:  # noqa: BLE001 - preserve cleanup warning
                    warning = str(exc)
        finally:
            if self._client_context is not None:
                await self._client_context.__aexit__(None, None, None)
            self._client_context = None
            self._client = None
            self._connected_by_runner = False
            self._state_uncertain = False
            self._session_observation = None
        return warning

    async def run(self) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        canonical_spec = json.dumps(
            self.spec.to_dict(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        result_document: dict[str, Any] = {
            "schema_version": 1,
            "task_name": self.spec.task_name,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "spec_sha256": hashlib.sha256(canonical_spec.encode("utf-8")).hexdigest(),
            "endpoint": self.spec.connection.endpoint,
            "design_points": [],
        }
        await self.open_session()
        try:
            for index, point in enumerate(self.spec.design_points):
                safe_name = re_safe_name(point.name, fallback=f"point-{index + 1}")
                try:
                    evaluated = await self.evaluate_point(
                        point.values, name=point.name, artifact_stem=safe_name
                    )
                    valid = all(
                        item["passed"] for item in evaluated.get("constraints", [])
                    ) and (
                        not evaluated["convergence"]["required"]
                        or evaluated["convergence"]["passed"]
                    )
                    point_result = {**evaluated, "status": "ok", "valid": valid}
                except FluentStateUncertainError:
                    raise
                except Exception as exc:  # noqa: BLE001 - record point failure in batch mode
                    point_result = {
                        "name": point.name,
                        "status": "error",
                        "parameters": point.values,
                        "error": str(exc),
                        "valid": False,
                    }
                result_document["design_points"].append(point_result)
        finally:
            warning = await self.close_session()
            if warning:
                result_document["disconnect_warning"] = warning

        self._finalize(result_document)
        return result_document

    def _finalize(self, document: dict[str, Any]) -> None:
        successful = [
            point for point in document["design_points"] if point.get("status") == "ok"
        ]
        valid = [point for point in successful if point.get("valid")]
        objective = self.spec.objective
        if valid:
            reverse = objective.direction == "maximize"
            best = sorted(
                valid,
                key=lambda point: point["reports"][objective.report]["value"],
                reverse=reverse,
            )[0]
            objective_value = float(best["reports"][objective.report]["value"])
            objective_score = (
                objective_value
                if objective.direction == "maximize"
                else -objective_value
            )
            document["best_design_point"] = best["name"]
            document["objective"] = {
                "report": objective.report,
                "direction": objective.direction,
                "value": objective_value,
                "score": objective_score,
                "unit": best["reports"][objective.report].get("unit"),
            }
        else:
            objective_score = 0.0
            document["best_design_point"] = None
            document["objective"] = {
                "report": objective.report,
                "direction": objective.direction,
                "value": None,
                "score": objective_score,
                "unit": None,
            }
        total = len(document["design_points"])
        constraint_checks = [
            check for point in successful for check in point.get("constraints", [])
        ]
        convergence_required = any(
            point.get("convergence", {}).get("required") for point in successful
        )
        convergence_passed = [
            point.get("convergence", {}).get("passed") is True for point in successful
        ]
        means = {
            "objective_score": objective_score,
            "successful_design_point_rate": len(successful) / total if total else 0.0,
            "valid_design_point_rate": len(valid) / total if total else 0.0,
            "constraint_pass_rate": (
                sum(bool(check["passed"]) for check in constraint_checks)
                / len(constraint_checks)
                if constraint_checks
                else 1.0
            ),
            "convergence_score": (
                sum(convergence_passed) / len(convergence_passed)
                if convergence_required and convergence_passed
                else 1.0
            ),
        }
        document["completed_at"] = datetime.now(timezone.utc).isoformat()
        atomic_json(self.output_dir / "fluent_result.json", document)
        final_info = {
            self.spec.task_name: {
                "means": means,
                "best_design_point": document["best_design_point"],
                "objective": document["objective"],
                "artifact": "fluent_result.json",
            }
        }
        atomic_json(self.output_dir / "final_info.json", final_info)


def re_safe_name(value: str, fallback: str) -> str:
    """Return a filesystem-safe audit artifact stem."""

    import re

    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return safe[:80] or fallback


def run_experiment(spec: ExperimentSpec, output_dir: str | Path) -> dict[str, Any]:
    """Synchronous convenience entry point used by task launchers."""

    return asyncio.run(FluentExperimentRunner(spec, output_dir).run())
