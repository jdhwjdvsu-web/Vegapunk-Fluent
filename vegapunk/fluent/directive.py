"""Human-approved Vegapunk-to-Fluent OptimizationDirective contract."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .history import atomic_json, utc_now


_ACTIONS = {"create_child", "increase_budget", "resume", "stop"}


@dataclass(frozen=True)
class OptimizationDirective:
    parent_campaign_id: str | None
    objective: dict[str, Any]
    search_space: list[dict[str, Any]]
    constraints: list[dict[str, Any]]
    budget: int
    action: str
    rationale: str
    approved: bool = False
    approved_by: str | None = None
    approved_at: str | None = None
    schema_version: int = 1

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> OptimizationDirective:
        action = str(raw.get("action", "")).strip()
        if action not in _ACTIONS:
            raise ValueError(f"directive.action must be one of {sorted(_ACTIONS)}")
        budget = int(raw.get("budget", 0))
        if action != "stop" and budget < 1:
            raise ValueError("directive.budget must be positive")
        objective = raw.get("objective")
        search_space = raw.get("search_space")
        constraints = raw.get("constraints", [])
        if not isinstance(objective, dict):
            raise ValueError("directive.objective must be an object")
        if not isinstance(search_space, list) or not search_space:
            raise ValueError("directive.search_space must be a non-empty list")
        if not isinstance(constraints, list):
            raise ValueError("directive.constraints must be a list")
        rationale = str(raw.get("rationale", "")).strip()
        if not rationale:
            raise ValueError("directive.rationale must be non-empty")
        approved = bool(raw.get("approved", False))
        approved_by = raw.get("approved_by")
        if approved and (not isinstance(approved_by, str) or not approved_by.strip()):
            raise ValueError("approved directives require approved_by")
        return cls(
            parent_campaign_id=raw.get("parent_campaign_id"),
            objective=objective,
            search_space=search_space,
            constraints=constraints,
            budget=budget,
            action=action,
            rationale=rationale,
            approved=approved,
            approved_by=approved_by.strip() if isinstance(approved_by, str) else None,
            approved_at=raw.get("approved_at"),
        )

    def assert_approved(self) -> None:
        if self.action != "stop" and not self.approved:
            raise PermissionError(
                "OptimizationDirective requires explicit human approval"
            )

    def approve(self, approved_by: str) -> OptimizationDirective:
        name = approved_by.strip()
        if not name:
            raise ValueError("approved_by must be non-empty")
        return OptimizationDirective(
            **{
                **asdict(self),
                "approved": True,
                "approved_by": name,
                "approved_at": utc_now(),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_optimization_directive(path: str | Path) -> OptimizationDirective:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("OptimizationDirective must be a JSON object")
    return OptimizationDirective.from_dict(raw)


def save_optimization_directive(
    path: str | Path, directive: OptimizationDirective
) -> None:
    atomic_json(Path(path), directive.to_dict())
