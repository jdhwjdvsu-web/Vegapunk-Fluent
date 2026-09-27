"""Versioned facts exposed to, but never selected on behalf of, the LLM."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .metric_catalog import MetricCandidate
from .model_profile import UIParameter


CAPABILITY_REGISTRY_VERSION = "3.1"


@dataclass(frozen=True)
class Capability:
    capability_id: str
    version: str
    semantic_keys: tuple[str, ...]
    supported_zone_types: tuple[str, ...]
    required_physics: tuple[str, ...]
    required_model_capabilities: tuple[str, ...]
    available_parameter_ids: tuple[str, ...]
    available_metric_ids: tuple[str, ...]
    source_unit: str
    native_unit: str
    hard_range: tuple[float, float] | None
    coordinate_system: str | None
    assumptions: tuple[str, ...]
    description: str
    executable: bool

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


class CapabilityRegistry:
    """Case-scoped view over versioned server capabilities."""

    def __init__(
        self,
        profile: Mapping[str, Any],
        parameters: Mapping[str, UIParameter],
        metrics: Mapping[str, MetricCandidate],
    ) -> None:
        self.profile = profile
        self.parameters = dict(parameters)
        self.metrics = dict(metrics)
        self.version = CAPABILITY_REGISTRY_VERSION
        self._capabilities = self._build()

    def _build(self) -> dict[str, Capability]:
        metric_ids = tuple(sorted(self.metrics))
        result: dict[str, Capability] = {}
        for parameter in self.parameters.values():
            if parameter.rule_id.startswith("flow_direction_"):
                continue  # a single component is not an independent DIRECT control
            capability_id = f"direct.{parameter.rule_id or 'numeric'}.v1"
            current = result.get(capability_id)
            ids = tuple(sorted({*(current.available_parameter_ids if current else ()), parameter.key}))
            result[capability_id] = Capability(
                capability_id=capability_id,
                version="1.0",
                semantic_keys=(parameter.rule_id or parameter.key,),
                supported_zone_types=(parameter.category,),
                required_physics=(),
                required_model_capabilities=("active_constant_numeric_parameter",),
                available_parameter_ids=ids,
                available_metric_ids=metric_ids,
                source_unit=parameter.unit,
                native_unit=parameter.unit,
                hard_range=(parameter.hard_min, parameter.hard_max),
                coordinate_system=None,
                assumptions=("一对一绑定到扫描确认的活动、非只读常数节点",),
                description=f"直接控制 {parameter.label}",
                executable=True,
            )

        direction_groups = {}
        for parameter in self.parameters.values():
            if parameter.rule_id in {"flow_direction_x", "flow_direction_y", "flow_direction_z"}:
                group = direction_groups.setdefault((parameter.collection_path, parameter.object_name), {})
                group[parameter.rule_id] = parameter.key
        direction_ids = tuple(sorted(key for group in direction_groups.values()
                                     if set(group) == {"flow_direction_x", "flow_direction_y", "flow_direction_z"}
                                     for key in group.values()))
        result["mapped.fan_incidence_2d.v1"] = Capability(
            capability_id="mapped.fan_incidence_2d.v1",
            version="1.1",
            semantic_keys=("fan_installation_angle", "wind_direction", "relative_incidence_angle"),
            supported_zone_types=("pressure_inlet", "velocity_inlet"),
            required_physics=("flow",),
            required_model_capabilities=("flow_direction_x", "flow_direction_y"),
            available_parameter_ids=direction_ids,
            available_metric_ids=metric_ids,
            source_unit="deg",
            native_unit="dimensionless",
            hard_range=(-360.0, 360.0),
            coordinate_system="Case coordinate system; XY plane",
            assumptions=("固定几何代理", "每个入口必须选完整 XYZ；每个向量独立归一化；XY代理的Z必须为0"),
            description="入射角映射到扫描确认的入口方向分量；可同一角度联动多个入口，逐入口输出完整XYZ",
            executable=bool(direction_ids),
        )
        return result

    def get(self, capability_id: str) -> Capability | None:
        return self._capabilities.get(capability_id)

    def require(self, capability_id: str) -> Capability:
        capability = self.get(capability_id)
        if capability is None:
            raise ValueError(f"capability_id 不属于服务端 Capability Registry：{capability_id}")
        return capability

    def available(self) -> list[dict[str, Any]]:
        return [item.public_dict() for item in self._capabilities.values()]

    def validate_parameter_ids(self, capability_id: str, parameter_ids: Iterable[str]) -> list[str]:
        capability = self.require(capability_id)
        errors = []
        for parameter_id in parameter_ids:
            if parameter_id not in self.parameters:
                errors.append(f"parameter_id 不属于当前扫描目录：{parameter_id}")
            elif parameter_id not in capability.available_parameter_ids:
                errors.append(f"parameter_id 不属于能力 {capability_id}：{parameter_id}")
        if not capability.executable:
            errors.append(f"当前 Case 缺少能力 {capability_id} 所需的全部底层参数")
        return errors

    def validate_metric_ids(self, capability_id: str, metric_ids: Iterable[str]) -> list[str]:
        capability = self.require(capability_id)
        return [
            f"metric_id 不属于能力 {capability_id}：{metric_id}"
            for metric_id in metric_ids
            if metric_id not in self.metrics or metric_id not in capability.available_metric_ids
        ]

    def _case_evidence(self) -> dict[str, Any]:
        """Return system-owned facts that a planning node may safely cite.

        Paths never enter the model context.  The selected Case identity and a
        same-stem data artifact are converted to immutable fingerprints, while
        existing Fluent report definitions are reduced to their public names
        and physical selections.
        """

        signature = self.profile.get("signature") or {}
        reports: list[dict[str, Any]] = []
        report_groups = signature.get("reports") or signature.get("existing_reports") or {}
        for kind, definitions in report_groups.items():
            if not isinstance(definitions, Mapping):
                continue
            for name, definition in definitions.items():
                if not isinstance(definition, Mapping):
                    continue
                selections = (
                    definition.get("boundaries")
                    or definition.get("surface_names")
                    or definition.get("cell_zones")
                    or []
                )
                reports.append(
                    {
                        "name": str(definition.get("name") or name),
                        "kind": str(kind),
                        "report_type": definition.get("report_type"),
                        "field": definition.get("field"),
                        "selections": list(selections),
                    }
                )

        paired_data: dict[str, Any] | None = None
        case_value = self.profile.get("case_file")
        if case_value:
            case_path = Path(str(case_value))
            if case_path.name.lower().endswith(".cas.h5"):
                data_path = case_path.with_name(case_path.name[:-7] + ".dat.h5")
                if data_path.is_file():
                    digest = hashlib.sha256()
                    with data_path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    paired_data = {
                        "artifact_reference": "paired-case-data",
                        "sha256": digest.hexdigest(),
                        "same_stem_as_selected_case": True,
                    }

        return {
            "selected_case_sha256": self.profile.get("case_sha256"),
            "model_signature_sha256": self.profile.get("signature_sha256"),
            "paired_data_artifact": paired_data,
            "existing_reports": sorted(reports, key=lambda item: (item["kind"], item["name"])),
            "solver_readbacks": dict(signature.get("solver_readbacks") or {}),
            "execution_invariants": [
                "每个 Trial 从相同 Case 基线及其批准的配对 data 重新加载",
                "执行器只写入已批准的原生参数；其余几何、网格、材料、物理模型和边界设置由 Case SHA256 固定",
                "非参数化固定设置不需要用户提供虚构的参数 ID",
            ],
        }

    def llm_context(self) -> dict[str, Any]:
        return {
            "registry_version": self.version,
            "capabilities": self.available(),
            "case_parameter_ids": [item.public_dict() for item in self.parameters.values()],
            "case_metric_ids": [item.public_dict() for item in self.metrics.values()],
            "case_evidence": self._case_evidence(),
        }
