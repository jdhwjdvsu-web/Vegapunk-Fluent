"""Immutable inputs needed to approve a whole experiment, not just its prior."""

from __future__ import annotations

from pathlib import Path
import hashlib
from typing import Any, Literal, Mapping

from pydantic import Field

from ..campaign import GATE_VERSION
from ..execution_approval import canonical_json
from ..profile_store import case_sha256
from ..task_schema import SimulationTaskObject
from .schema import StrictContract


class FrozenExperimentInputs(StrictContract):
    schema_version: Literal[1] = 1
    v3_task: SimulationTaskObject
    template: dict[str, Any]
    endpoint: str = Field(min_length=1)
    case_file: str = Field(min_length=1)
    case_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    data_file: str | None = None
    data_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    gate_version: str = Field(min_length=1)


def snapshot_execution_inputs(
    task: SimulationTaskObject, template: Mapping[str, Any], endpoint: str, case_file: str,
) -> FrozenExperimentInputs:
    # Canonical JSON detaches all references to mutable task/template objects.
    import json
    copied_template = json.loads(canonical_json(dict(template)))
    data_file = None
    data_fingerprint = None
    if task.solver_requirements.thermal_guard is not None:
        if not case_file.endswith(".cas.h5"):
            raise ValueError("Thermal baseline requires paired .cas.h5/.dat.h5")
        data_file = str(Path(case_file[:-7] + ".dat.h5"))
        with Path(data_file).open("rb") as stream:
            data_fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
        if data_fingerprint != task.solver_requirements.thermal_guard.data_sha256:
            raise PermissionError("Approved thermal baseline Data fingerprint differs")
    return FrozenExperimentInputs(
        v3_task=SimulationTaskObject.model_validate(task.model_dump(mode="json")),
        template=copied_template, endpoint=endpoint, case_file=case_file,
        case_fingerprint=case_sha256(case_file), data_file=data_file,
        data_fingerprint=data_fingerprint, gate_version=GATE_VERSION,
    )
