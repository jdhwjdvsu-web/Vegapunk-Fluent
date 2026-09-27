"""Read-only diagnosis and strict completed-result checks for synchronous Fluent calls."""

from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import re
import socket
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from .history import atomic_json


_TRIAL_NAME = re.compile(r"^trial-(\d+)$")
_GATE_STATUSES = frozenset({"PASS", "CONSTRAINT", "DIVERGED"})


def _finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def inspect_completed_trial(
    path: Path, trial_number: int, parameters: Mapping[str, float],
    *, expected_constraints: int | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """A file is usable for Gate/tell recovery only if its complete contract matches."""
    if not path.is_file():
        return False, "MISSING", None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, "PARTIAL: unreadable JSON", None
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        return False, "PARTIAL: invalid trial document", None
    if isinstance(document.get("trial_number"), bool) or document.get("trial_number") != trial_number:
        return False, "PARTIAL: trial ID mismatch", None
    if document.get("trial_id") != f"trial-{trial_number:04d}":
        return False, "PARTIAL: trial ID mismatch", None
    if document.get("solve_confirmed") is not True:
        return False, "PARTIAL: solve completion not confirmed", None
    if document.get("gate_executed") is not True:
        return False, "PARTIAL: Gate execution not confirmed", None
    actual = document.get("parameters")
    if not isinstance(actual, dict) or set(actual) != set(parameters) or any(
        not _finite_number(actual[key]) or not math.isclose(
            float(actual[key]), float(parameters[key]), rel_tol=0, abs_tol=1e-9,
        ) for key in parameters
    ):
        return False, "PARTIAL: trial parameters mismatch", None
    if not _finite_number(document.get("objective_value")):
        return False, "PARTIAL: objective missing or non-finite", None
    if document.get("baseline_reloaded") is not True:
        return False, "PARTIAL: baseline reload not confirmed", None
    gate = document.get("gates")
    if not isinstance(gate, dict) or gate.get("status") not in _GATE_STATUSES:
        return False, "PARTIAL: Gate result missing", None
    if not isinstance(gate.get("checks"), list) or not gate["checks"]:
        return False, "PARTIAL: Gate checks missing", None
    if any(not isinstance(check, dict) for check in gate["checks"]):
        return False, "PARTIAL: Gate checks invalid", None
    vector = gate.get("constraint_vector")
    if not isinstance(vector, list) or any(not _finite_number(value) for value in vector):
        return False, "PARTIAL: constraint vector missing or invalid", None
    if expected_constraints is not None and len(vector) != expected_constraints:
        return False, "PARTIAL: constraint vector length mismatch", None
    if gate["status"] == "PASS" and gate.get("passed") is not True:
        return False, "PARTIAL: PASS Gate is inconsistent", None
    if gate["status"] == "PASS" and (any(value > 0 for value in vector)
                                     or any(check.get("passed") is False for check in gate["checks"])):
        return False, "PARTIAL: PASS Gate conflicts with checks or constraints", None
    if gate["status"] == "CONSTRAINT" and not any(value > 0 for value in vector):
        return False, "PARTIAL: CONSTRAINT Gate lacks a violated constraint", None
    if gate["status"] != "PASS" and gate.get("passed") is not False:
        return False, "PARTIAL: non-PASS Gate is inconsistent", None
    return True, "COMPLETE", document


def _tcp_alive(endpoint: str, timeout: float) -> str:
    parsed = urlparse(endpoint)
    if not parsed.hostname:
        return "UNKNOWN"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=timeout):
            return "ALIVE"
    except OSError:
        return "DEAD"


def _fluent_process_alive() -> str:
    try:
        import psutil
        for process in psutil.process_iter(["name"]):
            names = (process.info.get("name") or "").lower()
            if names.startswith(("fluent", "fl_mpi")):
                return "ALIVE"
        return "DEAD"
    except ImportError:
        if os.name != "nt":
            return "UNKNOWN"
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                text=True, timeout=3, check=True,
            )
            names = [row[0].lower() for row in csv.reader(result.stdout.splitlines()) if row]
            return "ALIVE" if any(name.startswith(("fluent", "fl_mpi")) for name in names) else "DEAD"
        except (OSError, subprocess.SubprocessError, IndexError):
            return "UNKNOWN"
    except Exception:  # Process enumeration may be denied; never guess.
        return "UNKNOWN"


async def _mcp_session(endpoint: str, timeout: float) -> tuple[str, str]:
    try:
        from fastmcp import Client
        from .direct_run import _call
        async with Client(endpoint, timeout=timeout) as client:
            status = await asyncio.wait_for(
                _call(client, "session_status", {}), timeout=timeout,
            )
        return "ALIVE", "ALIVE" if status.get("connected") is True else "DEAD"
    except Exception:
        return "UNKNOWN", "UNKNOWN"


@dataclass(frozen=True)
class RuntimeDiagnosis:
    mcp_server: str
    mcp_connection: str
    fluent_process: str
    fluent_session: str
    trial_submission: str
    request_dispatched: bool
    solve_state: str
    possible_still_running: bool
    result_state: str
    result_reason: str
    generated_code_exists: bool
    solver_stdout_exists: bool
    final_info_exists: bool
    fluent_result_exists: bool
    gate_input_available: bool
    recommended_trial_state: str
    trial_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def diagnose_runtime(
    output_dir: Path, trial_id: str, endpoint: str, *,
    parameters: Mapping[str, float] | None = None,
    request_dispatched: bool,
    probe_timeout_seconds: float = 3,
    server_probe: Callable[[str, float], str] = _tcp_alive,
    process_probe: Callable[[], str] = _fluent_process_alive,
    session_probe: Callable[[str, float], Any] = _mcp_session,
) -> RuntimeDiagnosis:
    """Observe files, socket, process names, and session status without Fluent writes."""
    match = _TRIAL_NAME.fullmatch(trial_id)
    number = int(match.group(1)) if match else None
    result_path = output_dir / "trial_results" / f"{trial_id}.json"
    if number is not None and parameters is not None:
        complete, reason, _ = inspect_completed_trial(result_path, number, parameters)
    else:
        complete, reason = False, "MISSING" if not result_path.exists() else "PARTIAL: no trial identity"
    stdout_path = output_dir / "solver_stdout" / f"{trial_id}.log"
    stdout_exists = stdout_path.is_file()
    partial_artifact = result_path.exists() or stdout_exists or (output_dir / "fluent_result.json").exists() \
        or (output_dir / "final_info.json").exists()
    result_state = "COMPLETE" if complete else "PARTIAL" if partial_artifact else "MISSING"
    mcp_server = await asyncio.to_thread(server_probe, endpoint, probe_timeout_seconds)
    fluent_process = await asyncio.to_thread(process_probe)
    try:
        mcp_connection, fluent_session = await asyncio.wait_for(
            session_probe(endpoint, probe_timeout_seconds), timeout=probe_timeout_seconds + 1,
        )
    except Exception:
        mcp_connection, fluent_session = "UNKNOWN", "UNKNOWN"
    possible_running = request_dispatched and fluent_process == "ALIVE" and not complete
    return RuntimeDiagnosis(
        mcp_server=mcp_server, mcp_connection=mcp_connection,
        fluent_process=fluent_process, fluent_session=fluent_session,
        trial_submission="CONFIRMED" if complete else "UNKNOWN",
        request_dispatched=request_dispatched,
        solve_state="COMPLETE" if complete else "UNKNOWN",
        possible_still_running=possible_running,
        result_state=result_state, result_reason=reason,
        generated_code_exists=(output_dir / "generated_code" / f"{trial_id}.py").is_file(),
        solver_stdout_exists=stdout_exists,
        final_info_exists=(output_dir / "final_info.json").is_file(),
        fluent_result_exists=(output_dir / "fluent_result.json").is_file(),
        gate_input_available=complete,
        recommended_trial_state="COMPLETE" if complete else "ORPHANED" if request_dispatched
        else "UNKNOWN_RUNTIME_STATE",
        trial_id=trial_id,
    )


def save_diagnosis(output_dir: Path, diagnosis: RuntimeDiagnosis) -> Path:
    path = output_dir / "runtime_diagnosis" / f"{diagnosis.trial_id}.json"
    atomic_json(path, diagnosis.to_dict())
    return path
