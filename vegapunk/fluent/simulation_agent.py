"""Legacy compatibility wrapper for the Fluent LangGraph V3 runtime.

The Web application passes ``runtime`` and ``model_id`` from this small adapter to
``FluentAgentGraph``. ``respond`` remains only for older Python callers; it is not
the V3 orchestration path and cannot approve or launch calculations.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .metric_catalog import MetricCandidate
from .model_profile import UIParameter


AGENT_SCHEMA = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "needs_information": {"type": "boolean"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "plan_patch": {
            "type": "object",
            "properties": {
                "parameter_keys": {"type": "array", "items": {"type": "string"}},
                "objective_metric_key": {"type": ["string", "null"]},
                "objective_direction": {"type": ["string", "null"]},
                "target_trials": {"type": ["integer", "null"]},
                "iterations": {"type": ["integer", "null"]},
            },
            "additionalProperties": False,
        },
    },
    "required": ["message", "needs_information", "questions", "plan_patch"],
    "additionalProperties": False,
}


class SimulationAgent:
    def __init__(self, runtime: Any | None, *, model_id: str | None = None, reason: str | None = None):
        self.runtime = runtime
        self.model_id = model_id
        self.reason = reason

    @classmethod
    def from_environment(cls, catalog_path: Path | None = None) -> "SimulationAgent":
        if not os.environ.get("OPENAI_API_KEY"):
            return cls(None, reason="未配置 OPENAI_API_KEY；大模型助手不可用")
        try:
            from vegapunk.mas.models.unified_runtime import UnifiedModelRuntime

            runtime = (
                UnifiedModelRuntime.from_catalog_path(catalog_path)
                if catalog_path else UnifiedModelRuntime.from_default_catalog()
            )
            model_id = runtime.catalog.active_text_model
            return cls(runtime, model_id=model_id)
        except Exception as exc:  # configuration must be visible, never faked
            return cls(None, reason=f"模型运行时配置失败：{exc}")

    def state(self) -> dict[str, Any]:
        return {
            "configured": self.runtime is not None,
            "model_id": self.model_id,
            "reason": self.reason,
            "mode": "legacy_runtime_adapter",
        }

    async def respond(
        self,
        question: str,
        profile: Mapping[str, Any],
        parameters: Mapping[str, UIParameter],
        metrics: Mapping[str, MetricCandidate],
        current_plan: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        if self.runtime is None:
            raise RuntimeError(self.reason or "大模型助手尚未配置")
        prompt = json.dumps(
            {
                "user_request": question,
                "model": {
                    "name": profile.get("name"),
                    "physics": profile.get("signature", {}).get("physics", {}),
                    "boundaries": profile.get("signature", {}).get("boundaries", []),
                },
                "allowed_parameters": [
                    {
                        "key": item.key, "label": item.label, "unit": item.unit,
                        "current": item.default_value,
                        "recommended_range": [item.recommended_min, item.recommended_max],
                    } for item in parameters.values()
                ],
                "allowed_metrics": [
                    {
                        "key": item.key, "label": item.label, "unit": item.unit,
                        "roles": item.roles, "recommended_direction": item.recommended_direction,
                    } for item in metrics.values()
                ],
                "current_plan": current_plan,
            }, ensure_ascii=False,
        )
        raw = await self.runtime.generate_json(
            prompt,
            schema=AGENT_SCHEMA,
            system_prompt=(
                "你是科研仿真方案助手。只能引用 allowed_parameters 与 allowed_metrics 中的 key；"
                "不得输出 Python、Fluent 设置路径、执行命令或声称已经运行。缺少工程范围、目标或"
                "约束时先提问。plan_patch 只是待用户审查的草稿，不是审批。"
            ),
            model_id=self.model_id,
        )
        return self._validate_response(raw, parameters, metrics)

    @staticmethod
    def _validate_response(
        raw: Mapping[str, Any],
        parameters: Mapping[str, UIParameter],
        metrics: Mapping[str, MetricCandidate],
    ) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ValueError("大模型未返回结构化方案")
        message = str(raw.get("message", "")).strip()[:4000]
        if not message:
            raise ValueError("大模型回复缺少说明")
        questions = [str(item).strip()[:500] for item in raw.get("questions", []) if str(item).strip()][:8]
        patch = raw.get("plan_patch") if isinstance(raw.get("plan_patch"), Mapping) else {}
        selected = []
        unknown = []
        for key in patch.get("parameter_keys", []) if isinstance(patch.get("parameter_keys"), list) else []:
            key = str(key)
            if key in parameters and key not in selected and len(selected) < 5:
                selected.append(key)
            elif key not in parameters:
                unknown.append(key)
        metric_key = patch.get("objective_metric_key")
        if metric_key is not None and (
            metric_key not in metrics or "objective" not in metrics[metric_key].roles
        ):
            unknown.append(str(metric_key))
            metric_key = None
        direction = patch.get("objective_direction")
        if direction not in {None, "minimize", "maximize"}:
            direction = None
        target = patch.get("target_trials")
        iterations = patch.get("iterations")
        target = int(target) if isinstance(target, int) and not isinstance(target, bool) and 1 <= target <= 1000 else None
        iterations = int(iterations) if isinstance(iterations, int) and not isinstance(iterations, bool) and 1 <= iterations <= 1_000_000 else None
        if unknown:
            questions.append("模型建议引用了目录外项目，已被服务端拒绝，请重新选择。")
        return {
            "message": message,
            "needs_information": bool(raw.get("needs_information")) or bool(questions),
            "questions": questions,
            "plan_patch": {
                "parameter_keys": selected,
                "objective_metric_key": metric_key,
                "objective_direction": direction,
                "target_trials": target,
                "iterations": iterations,
            },
        }
