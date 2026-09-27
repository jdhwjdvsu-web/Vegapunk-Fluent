"""Deterministic plan compilation and server-owned approval records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .directive import OptimizationDirective
from .history import atomic_json, utc_now
from .metric_catalog import METRIC_VERSION, MetricCandidate
from .model_profile import UIParameter
from .parameter_rules import RULE_VERSION
from .spec import ExperimentSpec


PLAN_SCHEMA_VERSION = 1
MAX_TRIAL_BUDGET = 1000


def canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def default_plan(
    profile: Mapping[str, Any],
    parameters: Mapping[str, UIParameter],
    metrics: Mapping[str, MetricCandidate],
    *,
    target_trials: int = 6,
    iterations: int = 100,
) -> dict[str, Any] | None:
    """Create a reviewable draft; it is deliberately not an approval."""

    selected = [
        parameters[key]
        for key in profile.get("recommended_parameters", [])
        if key in parameters
        and parameters[key].recommended_min is not None
        and parameters[key].recommended_max is not None
    ][:2]
    objective_metrics = [item for item in metrics.values() if "objective" in item.roles]
    if not selected or not objective_metrics:
        return None
    question = str(profile.get("research_question", ""))
    def score(metric: MetricCandidate) -> int:
        value = 0
        if "最高温度" in question and "最高温度" in metric.label:
            value += 100
        if "发热体" in question and "固体" in metric.label:
            value += 80
        if "温度" in question and metric.category == "温度":
            value += 40
        if ("压降" in question or "压力" in question) and metric.category == "压力":
            value += 50
        if "流量" in question and metric.category == "流动":
            value += 50
        if metric.kind == "surface":
            value += 3
        return value
    objective = sorted(objective_metrics, key=score, reverse=True)[0]
    has_in = any(item.kind == "flux" and "mass-in" in item.key for item in metrics.values())
    has_out = any(item.kind == "flux" and "mass-out" in item.key for item in metrics.values())
    return normalize_plan(
        {
            "research_question": question,
            "parameters": [
                {"parameter_key": item.key, "range_min": item.recommended_min, "range_max": item.recommended_max}
                for item in selected
            ],
            "objective": {"metric_key": objective.key, "direction": objective.recommended_direction},
            "constraints": [],
            "checks": {"mass_balance": has_in and has_out, "mass_balance_relative_tolerance": 0.001},
            "budget": {"target_trials": target_trials, "iterations": iterations},
        }, parameters, metrics,
    )


def normalize_plan(
    raw: Mapping[str, Any],
    parameters: Mapping[str, UIParameter],
    metrics: Mapping[str, MetricCandidate],
) -> dict[str, Any]:
    selected = raw.get("parameters")
    if not isinstance(selected, list) or not 1 <= len(selected) <= 5:
        raise ValueError("方案必须包含 1–5 个优化参数")
    normalized_parameters = []
    seen: set[str] = set()
    for item in selected:
        if not isinstance(item, Mapping):
            raise ValueError("参数方案格式无效")
        key = str(item.get("parameter_key", ""))
        if key in seen or key not in parameters:
            raise ValueError("方案参数重复或不属于当前模型目录")
        seen.add(key)
        parameter = parameters[key]
        low = parameter.validate_display_value(item.get("range_min"), "参数下限")
        high = parameter.validate_display_value(item.get("range_max"), "参数上限")
        if low >= high:
            raise ValueError("参数下限必须小于上限")
        normalized_parameters.append({"parameter_key": key, "range_min": low, "range_max": high})

    objective = raw.get("objective")
    if not isinstance(objective, Mapping):
        raise ValueError("必须选择目标指标")
    objective_key = str(objective.get("metric_key", ""))
    if objective_key not in metrics or "objective" not in metrics[objective_key].roles:
        raise ValueError("目标指标不属于当前模型的可执行目录")
    direction = str(objective.get("direction", ""))
    if direction not in {"minimize", "maximize"}:
        raise ValueError("目标方向必须是 minimize 或 maximize")

    constraints = []
    for item in raw.get("constraints", []):
        if not isinstance(item, Mapping):
            raise ValueError("约束格式无效")
        metric_key = str(item.get("metric_key", ""))
        if metric_key not in metrics or "constraint" not in metrics[metric_key].roles:
            raise ValueError("约束指标不属于当前模型的可执行目录")
        operator = str(item.get("operator", ""))
        if operator not in {"<", "<=", ">", ">=", "=="}:
            raise ValueError("约束运算符无效")
        value = float(item.get("value"))
        if not (-1e300 < value < 1e300):
            raise ValueError("约束值必须是有限数")
        constraints.append({"metric_key": metric_key, "operator": operator, "value": value})

    budget = raw.get("budget") or {}
    target_trials = int(budget.get("target_trials", 6))
    iterations = int(budget.get("iterations", 100))
    if not 1 <= target_trials <= MAX_TRIAL_BUDGET:
        raise ValueError(f"Trial 预算必须在 1–{MAX_TRIAL_BUDGET} 之间")
    if not 1 <= iterations <= 1_000_000:
        raise ValueError("迭代次数必须在 1–1000000 之间")

    checks = raw.get("checks") or {}
    mass_balance = bool(checks.get("mass_balance", False))
    inlet = next((m for m in metrics.values() if m.kind == "flux" and "mass-in" in m.key), None)
    outlet = next((m for m in metrics.values() if m.kind == "flux" and "mass-out" in m.key), None)
    if mass_balance and (inlet is None or outlet is None):
        raise ValueError("当前模型未发现完整入口/出口，不能启用质量守恒 Gate")
    tolerance = float(checks.get("mass_balance_relative_tolerance", 0.001))
    if not 0 < tolerance < 1:
        raise ValueError("质量守恒相对容差必须在 0–1 之间")

    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "research_question": str(raw.get("research_question", "")).strip()[:4000],
        "parameters": normalized_parameters,
        "objective": {"metric_key": objective_key, "direction": direction},
        "constraints": constraints,
        "checks": {
            "finite_outputs": True,
            "mass_balance": mass_balance,
            "mass_balance_relative_tolerance": tolerance,
            "residual_thresholds": {
                str(name): float(value)
                for name, value in (checks.get("residual_thresholds") or {}).items()
                if float(value) > 0
            },
        },
        "budget": {"target_trials": target_trials, "iterations": iterations},
    }


def approval_payload(
    profile: Mapping[str, Any],
    plan: Mapping[str, Any],
    resolved_task: Mapping[str, Any] | None = None,
    approval_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "model": {
            "model_id": profile["model_id"],
            "case_sha256": profile["case_sha256"],
            "signature_sha256": profile["signature_sha256"],
            "fluent_version": profile["fluent_version"],
        },
        "mapping": {"parameter_rules": RULE_VERSION, "metric_rules": METRIC_VERSION},
        "plan": plan,
    }
    if resolved_task is not None:
        from .mapping_schema import ResolvedTaskObject

        resolved = ResolvedTaskObject.model_validate(resolved_task)
        payload["agent_v3"] = {
            "resolved_task": resolved.model_dump(mode="json"),
            "capability_registry_version": resolved.capability_registry_version,
            "mapping_dsl_schema_version": resolved.mapping_dsl_schema_version,
            "automatic_geometry_execution": False,
        }
        if approval_context is not None:
            payload["agent_v3"]["planning_context"] = dict(approval_context)
    return payload


def approve_plan(
    profile: Mapping[str, Any],
    plan: Mapping[str, Any],
    approved_by: str,
    resolved_task: Mapping[str, Any] | None = None,
    approval_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    name = approved_by.strip()
    if not name:
        raise ValueError("审批人不能为空")
    payload = approval_payload(profile, plan, resolved_task, approval_context)
    directive = OptimizationDirective(
        parent_campaign_id=None,
        objective=dict(plan["objective"]),
        search_space=list(plan["parameters"]),
        constraints=list(plan["constraints"]),
        budget=int(plan["budget"]["target_trials"]),
        action="create_child",
        rationale="用户确认的 Fluent 实验方案",
    ).approve(name)
    return {
        "fingerprint": canonical_sha256(payload),
        "approved_at": directive.approved_at,
        "approved_by": directive.approved_by,
        "payload": payload,
        "directive": directive.to_dict(),
    }


def assert_approval(
    profile: Mapping[str, Any],
    plan: Mapping[str, Any],
    approval: Mapping[str, Any] | None,
    resolved_task: Mapping[str, Any] | None = None,
    approval_context: Mapping[str, Any] | None = None,
) -> None:
    if not approval:
        raise PermissionError("当前实验方案尚未由用户确认")
    expected = canonical_sha256(approval_payload(profile, plan, resolved_task, approval_context))
    if approval.get("fingerprint") != expected:
        raise PermissionError("模型或实验方案已变化，旧审批已失效")
    OptimizationDirective.from_dict(dict(approval.get("directive") or {})).assert_approved()


def approve_resolved_task(
    profile: Mapping[str, Any],
    resolved_task: Mapping[str, Any],
    approved_by: str,
    approval_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a V3 approval without weakening the legacy directive contract."""
    from .mapping_schema import ResolvedTaskObject

    resolved = ResolvedTaskObject.model_validate(resolved_task)
    if not resolved.executable or resolved.geometry_unsupported or resolved.validation_errors:
        raise PermissionError("不可执行或存在未解决问题的 ResolvedTaskObject 不能审批")
    plan = {
        "schema_version": 3,
        "research_question": resolved.task.research_question,
        "parameters": [
            {
                "parameter_key": item.intent.id,
                "range_min": item.intent.minimum,
                "range_max": item.intent.maximum,
                "binding_mode": item.binding.classification.value,
            }
            for item in resolved.variables
        ],
        "objective": resolved.task.objective.model_dump(mode="json"),
        "constraints": [item.model_dump(mode="json") for item in resolved.task.constraints],
        "solver_requirements": resolved.task.solver_requirements.model_dump(mode="json"),
        "termination": resolved.task.termination.model_dump(mode="json"),
        "budget": {
            "target_trials": resolved.task.termination.max_trials,
            "iterations": resolved.task.solver_requirements.iterations,
        },
    }
    return approve_plan(
        profile,
        plan,
        approved_by,
        resolved.model_dump(mode="json"),
        approval_context,
    )


def assert_resolved_approval(
    profile: Mapping[str, Any],
    resolved_task: Mapping[str, Any],
    approval: Mapping[str, Any] | None,
    approval_context: Mapping[str, Any] | None = None,
) -> None:
    from .mapping_schema import ResolvedTaskObject

    resolved = ResolvedTaskObject.model_validate(resolved_task)
    plan = dict((approval or {}).get("payload", {}).get("plan") or {})
    assert_approval(
        profile,
        plan,
        approval,
        resolved.model_dump(mode="json"),
        approval_context,
    )


def save_plan(path: Path, plan: Mapping[str, Any]) -> None:
    atomic_json(path, dict(plan))


def compile_experiment_spec(
    template: Mapping[str, Any], profile: Mapping[str, Any], plan: Mapping[str, Any],
    parameters: Mapping[str, UIParameter], metrics: Mapping[str, MetricCandidate], endpoint: str,
) -> ExperimentSpec:
    raw = json.loads(json.dumps(template))
    connection = dict(raw.get("connection") or {})
    kwargs = dict(connection.get("connect_kwargs") or {})
    kwargs["case_file_name"] = profile["case_file"]
    kwargs["dimension"] = profile["dimension"]
    if profile.get("product_version"):
        kwargs["product_version"] = profile["product_version"]
    connection.update(endpoint=endpoint, connect_kwargs=kwargs, baseline_sha256=profile["case_sha256"],
                      reuse_existing_session=False, disconnect_on_exit=True)
    raw["connection"] = connection

    # A reused template must not retain constraints that reference parameters
    # outside the newly approved dynamic search space.
    raw["parameter_constraints"] = []
    raw["parameters"] = [
        parameters[item["parameter_key"]].spec_dict(item["range_min"], item["range_max"])
        for item in plan["parameters"]
    ]
    raw["design_points"] = [{"name": "baseline", "values": {
        item["parameter_key"]: parameters[item["parameter_key"]].native_value(
            min(max(parameters[item["parameter_key"]].default_value, item["range_min"]), item["range_max"])
        ) for item in plan["parameters"]
    }}]

    needed = {plan["objective"]["metric_key"], *(item["metric_key"] for item in plan["constraints"])}
    inlet = next((m for m in metrics.values() if m.kind == "flux" and "mass-in" in m.key), None)
    outlet = next((m for m in metrics.values() if m.kind == "flux" and "mass-out" in m.key), None)
    if plan["checks"]["mass_balance"]:
        needed.update((inlet.key, outlet.key))
    raw["reports"] = [metrics[key].report_spec() for key in sorted(needed)]
    raw["objective"] = {
        "report": metrics[plan["objective"]["metric_key"]].report_name,
        "direction": plan["objective"]["direction"],
    }
    raw["constraints"] = [{
        "report": metrics[item["metric_key"]].report_name,
        "operator": item["operator"], "value": item["value"],
    } for item in plan["constraints"]]
    raw["solver"] = {**(raw.get("solver") or {}), "iterations": plan["budget"]["iterations"],
                     "residual_thresholds": plan["checks"]["residual_thresholds"]}
    optimization = dict(raw.get("optimization") or {})
    optimization.update(
        target_trials=plan["budget"]["target_trials"],
        n_startup_trials=min(int(optimization.get("n_startup_trials", 2)), plan["budget"]["target_trials"]),
        enable_legacy_mass_balance=plan["checks"]["mass_balance"],
        mass_balance_relative_tolerance=plan["checks"]["mass_balance_relative_tolerance"],
    )
    if plan["checks"]["mass_balance"]:
        optimization["mass_flow_in_report"] = inlet.report_name
        optimization["mass_flow_out_report"] = outlet.report_name
    optimization["conservation_checks"] = []
    optimization["numerical_monitors"] = []
    raw["optimization"] = optimization
    raw["task_name"] = "VegapunkFluentAdaptiveExperiment"
    return ExperimentSpec.from_dict(raw)
