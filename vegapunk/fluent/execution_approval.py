"""One admission contract for approved V3 and frozen-prior experiments.

Compilation is not approval. Only a server-side approval handler may seal the
complete compiled experiment; the Runner entry point rechecks it before tools.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .campaign import GATE_VERSION
from .profile_store import case_sha256

if TYPE_CHECKING:
    from .capability_registry import CapabilityRegistry
    from .experiment_orchestrator import ResolvedExperiment


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ExecutionApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    mode: Literal["V3_APPROVED", "FROZEN_PRIOR"]
    approval_id: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    payload_json: str = Field(min_length=1)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def execution_payload(experiment: ResolvedExperiment, registry: CapabilityRegistry) -> dict:
    return {
        "resolved_task": experiment.resolved_task.model_dump(mode="json"),
        "semantic_spec": experiment.semantic_spec.to_dict(),
        "native_spec": experiment.native_spec.to_dict(),
        "capability_registry": registry.llm_context(),
        "gate_version": GATE_VERSION,
    }


def seal_execution(
    experiment: ResolvedExperiment, registry: CapabilityRegistry, *,
    approval_id: str, approved_by: str, mode: Literal["V3_APPROVED", "FROZEN_PRIOR"],
) -> ExecutionApproval:
    if not approval_id.strip() or not approved_by.strip():
        raise PermissionError("Explicit full-experiment approval identity is required")
    has_prior = bool((experiment.semantic_spec.execution_contract or {}).get("pre_simulation"))
    if (mode == "FROZEN_PRIOR") != has_prior:
        raise PermissionError("Execution mode does not match the compiled prior contract")
    if has_prior and experiment.semantic_spec.execution_contract["pre_simulation"].get("approval_id") != approval_id:
        raise PermissionError("Full-experiment approval differs from frozen-prior approval")
    payload = execution_payload(experiment, registry)
    return ExecutionApproval(
        mode=mode, approval_id=approval_id, approved_by=approved_by,
        payload_json=canonical_json(payload), payload_sha256=digest(payload),
    )


def require_execution_approval(experiment: ResolvedExperiment, registry: CapabilityRegistry) -> ExecutionApproval:
    approval = experiment.execution_approval
    if approval is None:
        raise PermissionError("Full-experiment approval is required before execution")
    approval = ExecutionApproval.model_validate(approval)
    stored = json.loads(approval.payload_json)
    if digest(stored) != approval.payload_sha256 or digest(execution_payload(experiment, registry)) != approval.payload_sha256:
        raise PermissionError("Approved experiment changed; a new approval is required")
    has_prior = bool((experiment.semantic_spec.execution_contract or {}).get("pre_simulation"))
    if (approval.mode == "FROZEN_PRIOR") != has_prior:
        raise PermissionError("Execution mode does not match approved experiment")
    native = experiment.native_spec
    case_file = native.connection.connect_kwargs.get("case_file_name")
    if not case_file or not native.connection.baseline_sha256:
        raise PermissionError("Approved baseline Case identity is missing")
    if case_sha256(str(case_file)) != native.connection.baseline_sha256:
        raise PermissionError("Approved baseline Case changed")
    if native.solver.thermal_guard:
        from .thermal_guard import verify_thermal_data
        verify_thermal_data(native.solver.thermal_guard)
    return approval
