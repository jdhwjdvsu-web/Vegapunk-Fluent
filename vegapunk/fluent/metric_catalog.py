"""Model-derived, executable Fluent metric catalog.

The catalog exposes only report definitions that Vegapunk knows how to compile.
It never exposes arbitrary Fluent setting paths or model-provided Python.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any


METRIC_VERSION = "1.1"


@dataclass(frozen=True)
class MetricCandidate:
    key: str
    label: str
    category: str
    kind: str
    report_type: str | None
    field_name: str | None
    locations: tuple[str, ...]
    unit: str
    recommended_direction: str
    roles: tuple[str, ...] = ("objective", "constraint", "monitor")
    source: str = "auto_discovered"
    confidence: str = "high"
    existing_report_name: str | None = None
    numerator_report: str | None = None
    denominator_report: str | None = None
    epsilon: float = 1e-12

    @property
    def report_name(self) -> str:
        if self.kind == "existing" and self.existing_report_name:
            return self.existing_report_name
        return f"vp-{self.key}"[:120]

    def report_spec(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.report_name,
            "kind": self.kind,
            "locations": list(self.locations),
        }
        if self.kind in {"surface", "volume"}:
            value.update(report_type=self.report_type, field=self.field_name)
        elif self.kind == "existing":
            value.update(report_type=self.report_type)
        elif self.kind == "derived_ratio":
            value.update(
                numerator_report=self.numerator_report,
                denominator_report=self.denominator_report,
                epsilon=self.epsilon,
                unit=self.unit,
            )
        return value

    def public_dict(self) -> dict[str, Any]:
        return {**asdict(self), "report_name": self.report_name}


def _key(prefix: str, locations: tuple[str, ...], suffix: str) -> str:
    identity = "|".join((prefix, *locations, suffix))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", "-".join(locations)).strip("-")[:54]
    return f"{prefix}-{slug or 'all'}-{suffix}-{digest}".lower()


def _cell_zones(signature: dict[str, Any]) -> list[tuple[str, str]]:
    zones: list[tuple[str, str]] = []
    for item in signature.get("cell_zones", []):
        if isinstance(item, str) and item:
            zones.append((item, "fluid"))
        elif isinstance(item, dict):
            name, kind = item.get("name"), item.get("type")
            if isinstance(name, str) and name and kind in {"fluid", "solid"}:
                zones.append((name, kind))
    return zones


def resolve_metrics(signature: dict[str, Any]) -> list[MetricCandidate]:
    """Resolve conservative report candidates from verified zone types and physics."""

    boundaries = [
        (item.get("name"), item.get("type"))
        for item in signature.get("boundaries", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    ]
    candidates: list[MetricCandidate] = []
    energy = signature.get("physics", {}).get("energy") is True

    existing_by_kind: dict[str, list[MetricCandidate]] = {"lift": [], "drag": []}
    report_state = signature.get("reports", {})
    if isinstance(report_state, dict):
        for report_kind in ("lift", "drag"):
            definitions = report_state.get(report_kind, {})
            if not isinstance(definitions, dict):
                continue
            for report_name, state in definitions.items():
                if not isinstance(report_name, str) or not report_name or not isinstance(state, dict):
                    continue
                zones = tuple(sorted(str(item) for item in state.get("zones", []) if str(item)))
                output_type = str(state.get("report_output_type", ""))
                is_coefficient = "coefficient" in output_type.lower()
                candidate = MetricCandidate(
                    key=_key("existing", (report_name,), report_kind),
                    label=f"{report_name} · 现有{('升力' if report_kind == 'lift' else '阻力')}{'系数' if is_coefficient else ''}",
                    category="气动力",
                    kind="existing",
                    report_type=report_kind,
                    field_name=None,
                    locations=zones or (report_name,),
                    unit="dimensionless" if is_coefficient else "N",
                    recommended_direction="maximize" if report_kind == "lift" else "minimize",
                    source="existing_report_definition",
                    existing_report_name=report_name,
                )
                candidates.append(candidate)
                existing_by_kind[report_kind].append(candidate)

    # Pair only reports acting on the same zones. Ambiguous combinations remain
    # separate catalog entries and require an explicit user/agent selection.
    for lift in existing_by_kind["lift"]:
        for drag in existing_by_kind["drag"]:
            if lift.locations != drag.locations:
                continue
            locations = lift.locations
            candidates.append(MetricCandidate(
                key=_key("derived", locations, f"{lift.report_name}-over-{drag.report_name}"),
                label=f"{lift.report_name}/{drag.report_name} · 升阻比",
                category="气动力",
                kind="derived_ratio",
                report_type=None,
                field_name=None,
                locations=locations,
                unit="dimensionless",
                recommended_direction="maximize",
                source="derived_from_existing_reports",
                numerator_report=lift.report_name,
                denominator_report=drag.report_name,
            ))

    for name, kind in boundaries:
        location = (name,)
        if kind in {"velocity_inlet", "pressure_inlet", "mass_flow_inlet", "pressure_outlet", "outflow"}:
            candidates.append(MetricCandidate(
                key=_key("surface", location, "pressure-avg"),
                label=f"{name} · 面平均静压", category="压力",
                kind="surface", report_type="surface-areaavg", field_name="pressure",
                locations=location, unit="Pa", recommended_direction="minimize",
            ))
        if kind in {"pressure_outlet", "outflow"}:
            candidates.append(MetricCandidate(
                key=_key("surface", location, "velocity-avg"),
                label=f"{name} · 面平均速度", category="流动",
                kind="surface", report_type="surface-areaavg", field_name="velocity-magnitude",
                locations=location, unit="m/s", recommended_direction="maximize",
            ))
        if energy and kind not in {"interior", "interface", "periodic", "symmetry"}:
            candidates.extend((
                MetricCandidate(
                    key=_key("surface", location, "temperature-avg"),
                    label=f"{name} · 面平均温度", category="温度",
                    kind="surface", report_type="surface-areaavg", field_name="temperature",
                    locations=location, unit="K", recommended_direction="minimize",
                ),
                MetricCandidate(
                    key=_key("surface", location, "temperature-max"),
                    label=f"{name} · 表面最高温度", category="温度",
                    kind="surface", report_type="surface-facetmax", field_name="temperature",
                    locations=location, unit="K", recommended_direction="minimize",
                ),
            ))

    if energy:
        for name, kind in _cell_zones(signature):
            location = (name,)
            prefix = "固体" if kind == "solid" else "流体"
            candidates.extend((
                MetricCandidate(
                    key=_key("volume", location, "temperature-max"),
                    label=f"{name} · {prefix}最高温度", category="温度",
                    kind="volume", report_type="volume-max", field_name="temperature",
                    locations=location, unit="K", recommended_direction="minimize",
                ),
                MetricCandidate(
                    key=_key("volume", location, "temperature-avg"),
                    label=f"{name} · {prefix}体积平均温度", category="温度",
                    kind="volume", report_type="volume-average", field_name="temperature",
                    locations=location, unit="K", recommended_direction="minimize",
                ),
            ))

    inlet_types = {"velocity_inlet", "pressure_inlet", "mass_flow_inlet"}
    outlet_types = {"pressure_outlet", "outflow"}
    for direction, types, label in (
        ("in", inlet_types, "入口总质量流量"),
        ("out", outlet_types, "出口总质量流量"),
    ):
        locations = tuple(sorted(name for name, kind in boundaries if kind in types))
        if locations:
            candidates.append(MetricCandidate(
                key=_key("flux", locations, f"mass-{direction}"),
                label=label, category="守恒",
                kind="flux", report_type=None, field_name=None,
                locations=locations, unit="kg/s", recommended_direction="minimize",
                roles=("gate",),
            ))

    unique: dict[str, MetricCandidate] = {}
    for candidate in candidates:
        unique[candidate.key] = candidate
    return list(unique.values())
