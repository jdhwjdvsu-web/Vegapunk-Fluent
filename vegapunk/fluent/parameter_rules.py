"""Bounded, versioned rules by Fluent type, never by model or zone name."""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass

from .model_profile import UIParameter

RULE_VERSION = "1.0"


def scalar_value(value, rule):
    """Accept numeric literals with explicit known units, never expressions/UDFs."""
    if isinstance(value, str):
        match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*\[([^\]]+)\]\s*", value)
        if not match:
            return None
        units = {"hydraulic_diameter": {"m": 1, "mm": 0.001, "cm": 0.01, "in": 0.0254, "ft": 0.3048},
                 "velocity": {"m/s": 1}, "temperature": {"K": 1},
                 "gauge_pressure": {"Pa": 1}, "backflow_temperature": {"K": 1}}
        factor = units.get(rule.id, {}).get(match.group(2).strip())
        return float(match.group(1)) * factor if factor is not None else None
    return value


@dataclass(frozen=True)
class ParameterRule:
    id: str
    collection: str
    path: str
    label: str
    unit: str
    hard_min: float
    hard_max: float
    category: str = "边界条件"
    physics: str | None = None
    option_path: str | None = None
    scale: float = 1.0


# Engineering guardrails, not universal Fluent physical validity bounds.
# Unreliable zero-centred ranges deliberately require human input.
RULES = (
    ParameterRule("velocity", "velocity_inlet", "momentum.velocity_magnitude.value", "入口速度", "m/s", 0.000001, 1000, option_path="momentum.velocity_magnitude.option"),
    ParameterRule("temperature", "velocity_inlet", "thermal.temperature.value", "入口温度", "K", 1, 5000, physics="energy", option_path="thermal.temperature.option"),
    ParameterRule("gauge_pressure", "pressure_outlet", "momentum.gauge_pressure.value", "出口表压", "Pa", -1e8, 1e8, option_path="momentum.gauge_pressure.option"),
    ParameterRule("backflow_temperature", "pressure_outlet", "thermal.backflow_total_temperature.value", "回流温度", "K", 1, 5000, physics="energy", option_path="thermal.backflow_total_temperature.option"),
    ParameterRule("turbulence_intensity", "velocity_inlet", "turbulence.turbulent_intensity", "湍流强度", "%", 0.001, 100, category="湍流", physics="turbulence", scale=0.01),
    ParameterRule("hydraulic_diameter", "velocity_inlet", "turbulence.hydraulic_diameter", "水力直径", "m", 1e-6, 1000, category="湍流", physics="turbulence"),
    ParameterRule("density", "fluid", "density.value", "密度", "kg/m³", 1e-6, 1e6, category="材料物性", option_path="density.option"),
    ParameterRule("viscosity", "fluid", "viscosity.value", "动力黏度", "Pa·s", 1e-12, 1e4, category="材料物性", option_path="viscosity.option"),
    ParameterRule("specific_heat", "fluid", "specific_heat.value", "定压比热", "J/(kg·K)", 1e-6, 1e7, category="材料物性", physics="energy", option_path="specific_heat.option"),
    ParameterRule("thermal_conductivity", "fluid", "thermal_conductivity.value", "导热系数", "W/(m·K)", 1e-9, 1e6, category="材料物性", physics="energy", option_path="thermal_conductivity.option"),
    ParameterRule("solid_density", "solid", "density.value", "固体密度", "kg/m³", 1e-6, 1e6, category="材料物性", option_path="density.option"),
    ParameterRule("solid_conductivity", "solid", "thermal_conductivity.value", "固体导热系数", "W/(m·K)", 1e-9, 1e6, category="材料物性", physics="energy", option_path="thermal_conductivity.option"),
)


def collection_path(rule: ParameterRule) -> str:
    parent = "materials" if rule.category == "材料物性" else "boundary_conditions"
    return f"setup.{parent}.{rule.collection}"


def resolve_parameters(signature: dict) -> list[UIParameter]:
    """Only accept observed active, scalar, constant nodes matching our rules."""
    candidates = []
    rules = {rule.id: rule for rule in RULES}
    seen = set()
    for observation in signature.get("observations", []):
        rule = rules.get(observation.get("rule_id"))
        if rule is None or observation.get("editable") is not True:
            continue
        if rule.physics and signature.get("physics", {}).get(rule.physics) is not True:
            continue
        value = scalar_value(observation.get("value"), rule)
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            continue
        name = observation.get("object_name")
        if not isinstance(name, str) or not name or len(name) > 256:
            continue
        identity = (rule.id, name)
        if identity in seen:
            continue
        seen.add(identity)
        value /= rule.scale
        hard_min, hard_max = rule.hard_min, rule.hard_max
        for field, lower in (("native_min", True), ("native_max", False)):
            bound = observation.get(field)
            if isinstance(bound, (float, int)) and not isinstance(bound, bool) and math.isfinite(bound):
                if lower:
                    hard_min = max(hard_min, bound / rule.scale)
                else:
                    hard_max = min(hard_max, bound / rule.scale)
        if not hard_min <= value <= hard_max or hard_min >= hard_max:
            continue
        # Always include a digest: a-b and a_b must not collide.
        slug = re.sub(r"[^a-zA-Z0-9_]", "_", name)[:64].lower()
        digest = hashlib.sha256(f"{rule.collection}:{name}:{rule.id}".encode()).hexdigest()[:10]
        key = f"{slug}_{rule.id}_{digest}"
        low = max(hard_min, value * 0.7) if value > 0 else None
        high = min(hard_max, value * 1.3) if value > 0 else None
        if rule.physics == "energy" and rule.unit == "K" and value > 0:
            low, high = max(hard_min, value - 10), min(hard_max, value + 10)
        if low is not None:
            low, high = float(f"{low:.10g}"), float(f"{high:.10g}")
        if low is not None and low >= high:
            low = high = None
        candidates.append(UIParameter(
            key=key, label=f"{name} · {rule.label}", category=rule.category,
            zone=name, collection_path=collection_path(rule), object_name=name,
            property_path=rule.path, unit=rule.unit, hard_min=hard_min,
            hard_max=hard_max, recommended_min=low, recommended_max=high,
            default_value=value, step=0.0,
            description="仅开放已读取的活动常数。硬边界是软件保护范围，不保证工程安全；建议范围须确认。",
            scale_to_native=rule.scale, rule_id=rule.id,
        ))
    return candidates
