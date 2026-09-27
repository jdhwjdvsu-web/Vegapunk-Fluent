"""Typed future geometry-verification port; V3 only persists suggestions."""

from __future__ import annotations

from pathlib import Path
import math
from typing import Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .history import atomic_json
from .mapping_schema import VerificationRoute
from .task_schema import StrictModel


class GeometryCandidate(StrictModel):
    value: float
    unit: str
    label: Literal["optimum-minus-delta", "optimum", "optimum-plus-delta"]

    @field_validator("value", mode="before")
    @classmethod
    def finite_value(cls, value):
        if isinstance(value, bool) or not math.isfinite(float(value)):
            raise ValueError("geometry candidate value must be finite and not bool")
        return value


class GeometryVerificationRequest(StrictModel):
    schema_version: Literal[1] = 1
    source_campaign_id: str
    source_trial_id: str
    capability_id: str
    mapping_id: str
    geometry_parameter: str
    candidates: list[GeometryCandidate] = Field(min_length=3, max_length=3)
    reference_conditions: dict[str, float]
    required_capabilities: list[str] = Field(
        default_factory=lambda: [
            "geometry_parameterization",
            "remeshing",
            "mesh_quality_validation",
        ]
    )
    verification_route: VerificationRoute = VerificationRoute.FUTURE_WORKBENCH_MCP
    automatic_execution: Literal[False] = False

    @field_validator("reference_conditions", mode="before")
    @classmethod
    def finite_reference_conditions(cls, value):
        if not isinstance(value, dict):
            raise ValueError("reference_conditions must be an object")
        for name, number in value.items():
            if not str(name).strip() or isinstance(number, bool) or not math.isfinite(float(number)):
                raise ValueError("reference conditions must be finite numeric values")
        return value

    @model_validator(mode="after")
    def exactly_one_of_each_candidate(self):
        labels = {item.label for item in self.candidates}
        if labels != {"optimum-minus-delta", "optimum", "optimum-plus-delta"}:
            raise ValueError("geometry verification requires minus/optimum/plus candidates")
        return self


class GeometryHandoffResult(StrictModel):
    mode: Literal["suggestion_only", "workbench_mcp"]
    submitted: bool
    requires_manual_geometry_update: bool
    request_artifact: str | None = None
    message: str


class GeometryHandoffPort(Protocol):
    async def submit(self, request: GeometryVerificationRequest) -> GeometryHandoffResult:
        ...


class SuggestionOnlyGeometryHandoff:
    """Persist a future request without network, MCP, CAD, meshing, or Workbench."""

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)

    async def submit(self, request: GeometryVerificationRequest) -> GeometryHandoffResult:
        path = self.output_dir / "geometry_verification_request.json"
        atomic_json(path, request.model_dump(mode="json"))
        return GeometryHandoffResult(
            mode="suggestion_only",
            submitted=False,
            requires_manual_geometry_update=True,
            request_artifact=str(path),
            message="当前仅生成几何修改建议，尚未连接 Workbench 自动执行。",
        )


class WorkbenchMcpGeometryHandoff:
    """Reserved adapter name. Network execution is intentionally not implemented in V3."""

    async def submit(self, request: GeometryVerificationRequest) -> GeometryHandoffResult:
        del request
        raise NotImplementedError("Workbench MCP geometry execution is outside Fluent Agent V3")
