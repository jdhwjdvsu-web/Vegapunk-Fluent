"""Independent best-candidate verification from the protected baseline."""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .history import atomic_json, utc_now
from .runner import DuplicateTrialError, FluentExperimentRunner, FluentStateUncertainError
from .execution_approval import digest
from .spec import ExperimentSpec
from .validity import evaluate_result_gate


def verification_is_trusted(record: Mapping[str, Any], parameters: Mapping[str, Any], reference_value: float) -> bool:
    """A display flag alone is not evidence of independent repeatability."""
    if not isinstance(record, Mapping):
        return False
    numeric = [record.get(key) for key in ("verification_objective", "absolute_difference", "allowed_difference")]
    gate = record.get("gate")
    return bool(record.get("status") == "verified" and record.get("verified") is True
                and record.get("identity_valid") is True and record.get("baseline_reloaded") is True
                and record.get("parameters") == dict(parameters) and record.get("reference_objective") == reference_value
                and isinstance(gate, dict) and gate.get("passed") is True and gate.get("status") == "PASS"
                and re.fullmatch(r"[0-9a-f]{64}", str(record.get("verification_contract_sha256", "")))
                and all(not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) for value in numeric)
                and 0 <= numeric[1] <= numeric[2]
                and math.isclose(numeric[1], abs(numeric[0] - reference_value), rel_tol=1e-9, abs_tol=1e-12))


async def run_independent_verification(
    spec: ExperimentSpec,
    output_dir: Path,
    best_parameters: Mapping[str, Any],
    reference_value: float,
    *,
    relative_tolerance: float = 0.002,
    absolute_tolerance: float = 1e-6,
    tolerance_mode: str = "auto",
    reference_scale: float | None = None,
    policy_basis: str = "Configured repeatability tolerance; not proof of improvement",
    runner_factory=FluentExperimentRunner,
) -> dict[str, Any]:
    """Rerun once in a fresh runner session and record reproducibility evidence."""

    for label, value in (("reference_value", reference_value),
                         ("relative_tolerance", relative_tolerance),
                         ("absolute_tolerance", absolute_tolerance)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{label} must be finite")
    if relative_tolerance < 0 or absolute_tolerance < 0:
        raise ValueError("verification tolerances must be nonnegative")
    if tolerance_mode not in {"auto", "absolute", "reference_scale"} or not policy_basis.strip():
        raise ValueError("verification requires a valid tolerance mode and policy basis")
    if reference_scale is not None and (isinstance(reference_scale, bool)
            or not isinstance(reference_scale, (int, float))
            or not math.isfinite(reference_scale) or reference_scale <= 0):
        raise ValueError("reference_scale must be positive and finite")
    if tolerance_mode == "reference_scale" and reference_scale is None:
        raise ValueError("reference_scale mode requires an explicit physical scale")
    if not math.isfinite((reference_scale or abs(reference_value)) * relative_tolerance):
        raise ValueError("verification tolerance scale overflows")
    definitions = {item.name: item for item in spec.parameters}
    if set(best_parameters) != set(definitions):
        raise ValueError("verification parameters do not match the experiment")
    parameters = {key: definitions[key].validate_value(value, key)
                  for key, value in best_parameters.items()}
    verification_dir = Path(output_dir) / "verification"
    result_path = verification_dir / "result.json"
    contract_sha256 = digest({"spec": spec.to_dict(), "parameters": parameters,
                              "reference_value": reference_value, "relative_tolerance": relative_tolerance,
                              "absolute_tolerance": absolute_tolerance, "tolerance_mode": tolerance_mode,
                              "reference_scale": reference_scale, "policy_basis": policy_basis})
    if result_path.exists():
        existing = json.loads(result_path.read_text(encoding="utf-8"))
        if existing.get("verification_contract_sha256") != contract_sha256:
            raise DuplicateTrialError("Existing verification has a different or legacy contract; use a new campaign")
        return existing  # Never submit a second solve, including an orphaned verification.
    verification_dir.mkdir(parents=True, exist_ok=True)
    request_path = verification_dir / "request.json"
    try:
        with request_path.open("x", encoding="utf-8") as stream:
            json.dump({"verification_contract_sha256": contract_sha256,
                       "created_at": utc_now(), "parameters": parameters}, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise DuplicateTrialError("Verification was already reserved/submitted; manual diagnosis required before any retry") from exc
    runner = runner_factory(spec, verification_dir)
    result: dict[str, Any]
    try:
        await runner.open_session()
        evaluated = await runner.evaluate_point(
            parameters,
            name="independent-best-verification",
            artifact_stem="best-verification",
        )
        gate = evaluate_result_gate(spec, evaluated)
        raw_actual = evaluated["objective_value"]
        if isinstance(raw_actual, bool) or not isinstance(raw_actual, (int, float)):
            raise ValueError("Verification objective must be a real number")
        actual = float(raw_actual)
        objective_report = next(item for item in spec.reports if item.name == spec.objective.report)
        unit = evaluated.get("objective_unit") or objective_report.unit
        temperature = (str(unit).strip().lower() in {"k", "kelvin", "c", "°c", "degc", "celsius"}
                       or objective_report.field_name == "temperature")
        mode = "absolute" if tolerance_mode == "absolute" or (tolerance_mode == "auto" and temperature) else tolerance_mode
        scale = reference_scale if mode == "reference_scale" else abs(reference_value) if mode == "auto" else None
        allowed = absolute_tolerance if scale is None else max(absolute_tolerance, scale * relative_tolerance)
        identity_valid = (evaluated.get("baseline_reloaded") is True
                          and evaluated.get("parameters") == parameters
                          and evaluated.get("name", "independent-best-verification") == "independent-best-verification")
        delta = abs(actual - reference_value)
        reproducible = math.isfinite(actual) and identity_valid and delta <= allowed
        result = {
            "status": "verified" if gate.passed and reproducible else "review",
            "verified": bool(gate.passed and reproducible),
            "verified_at": utc_now(),
            "reference_objective": float(reference_value),
            "verification_objective": actual if math.isfinite(actual) else None,
            "objective_unit": unit,
            "absolute_difference": delta if math.isfinite(delta) else None,
            "allowed_difference": allowed,
            "relative_tolerance": relative_tolerance,
            "absolute_tolerance": absolute_tolerance,
            "tolerance_mode": mode, "reference_scale": scale, "policy_basis": policy_basis,
            "identity_valid": identity_valid, "baseline_reloaded": evaluated.get("baseline_reloaded") is True,
            "improvement_verified": False, "improvement_status": "NOT_ASSESSED",
            "gate": gate.to_dict(),
            "parameters": parameters,
        }
    except FluentStateUncertainError as exc:
        result = {"status": "orphaned", "runtime_state": "ORPHANED", "verified": False,
                  "verified_at": utc_now(), "parameters": parameters, "error": str(exc),
                  "diagnosis": exc.diagnosis, "automatic_retry_allowed": False}
    except DuplicateTrialError as exc:
        result = {"status": "blocked", "verified": False, "verified_at": utc_now(),
                  "parameters": parameters, "error": str(exc), "automatic_retry_allowed": False}
    except Exception as exc:  # evidence must exist even when verification failed
        result = {
            "status": "failed",
            "verified": False,
            "verified_at": utc_now(),
            "parameters": dict(best_parameters),
            "error": str(exc),
        }
    finally:
        try:
            warning = await runner.close_session()
        except Exception as exc:
            warning = str(exc)
        if warning:
            result["disconnect_warning"] = warning
    verification_dir.mkdir(parents=True, exist_ok=True)
    result["verification_contract_sha256"] = contract_sha256
    atomic_json(result_path, result)
    return result
