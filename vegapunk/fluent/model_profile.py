"""Model-derived parameter contract; no Case-specific names."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

MAX_PARAMETERS = 5
PROFILE_SCHEMA = 1

@dataclass(frozen=True)
class UIParameter:
    """One server-owned, user-selectable Fluent setting."""

    key: str
    label: str
    category: str
    zone: str
    collection_path: str
    object_name: str
    property_path: str
    unit: str
    hard_min: float
    hard_max: float
    recommended_min: float | None
    recommended_max: float | None
    default_value: float
    step: float
    description: str
    scale_to_native: float = 1.0
    log: bool = False

    source: str = "auto_discovered"
    confidence: str = "high"
    editable: bool = True
    rule_id: str = ""

    def validate_display_value(self, value: float, label: str) -> float:
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < self.hard_min or numeric > self.hard_max:
            raise ValueError(
                f"{label} 必须在 {self.hard_min:g}–{self.hard_max:g} {self.unit} 之间"
            )
        return numeric

    def native_value(self, display_value: float) -> float:
        return float(display_value) * self.scale_to_native

    def spec_dict(self, display_min: float, display_max: float) -> dict[str, Any]:
        return {
            "name": self.key,
            "collection_path": self.collection_path,
            "object_name": self.object_name,
            "property_path": self.property_path,
            "unit": self.unit,
            "minimum": self.native_value(display_min),
            "maximum": self.native_value(display_max),
            "step": None
            if self.log
            else (self.step * self.scale_to_native if self.step else None),
            "log": self.log,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "confidence": self.confidence,
            "editable": self.editable,
            "rule_id": self.rule_id,
            "current_value": self.default_value,
            "requires_range_confirmation": self.recommended_min is None,
            "key": self.key,
            "label": self.label,
            "category": self.category,
            "zone": self.zone,
            "unit": self.unit,
            "hard_min": self.hard_min,
            "hard_max": self.hard_max,
            "recommended_min": self.recommended_min,
            "recommended_max": self.recommended_max,
            "default_value": self.default_value,
            "step": self.step,
            "description": self.description,
            "log": self.log,
            "native_to_display": 1.0 / self.scale_to_native,
        }
