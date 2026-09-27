"""LangGraph V3 orchestration for stateful Fluent experiment planning."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from vegapunk.mas.models.runtime import ReasoningConfig

from .agent_state import FluentAgentState, initial_agent_state
from .capability_registry import CAPABILITY_REGISTRY_VERSION, CapabilityRegistry
from .geometry_handoff import (
    GeometryCandidate,
    GeometryVerificationRequest,
    SuggestionOnlyGeometryHandoff,
)
from .history import atomic_json
from .mapping_runtime import reverse_mapping, validate_mapping_spec
from .mapping_contract import mapping_dsl_contract
from .mapping_schema import (
    ClassificationEnvelope,
    GeometryRecommendationEnvelope,
    MappingEnvelope,
    MappingSpec,
    ResolvedTaskObject,
    ResolvedVariable,
    VariableBinding,
    VariableClassification,
)
from .task_schema import SimulationTaskObject, TaskParseEnvelope, schema_for


MAX_CLASSIFICATION_REVISIONS = 3
MAX_MAPPING_REVISIONS = 3
DEFAULT_AGENT_TIMEOUT_SECONDS = 120.0


class StaleAgentResponse(RuntimeError):
    """Raised before a late LLM response can write into a changed task identity."""


IdentityChecker = Callable[[Mapping[str, Any]], bool | Awaitable[bool]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity(state: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "task_id": state.get("task_id"),
        "conversation_revision": state.get("conversation_revision"),
        "model_id": state.get("model_id"),
        "case_sha256": state.get("case_sha256"),
        "model_signature_sha256": state.get("model_signature_sha256"),
        "planning_attempt_id": state.get("planning_attempt_id"),
    }


def _without_duplicate_solver_gate_constraints(task: SimulationTaskObject) -> SimulationTaskObject:
    """Remove report constraints already represented by executable solver gates.

    A language model may faithfully repeat residual, balance, and stabilization
    requirements in ``constraints`` as well as ``solver_requirements``.  Those
    values are not Case report metrics and must not be compiled twice.
    Unrecognized or catalog-backed engineering constraints remain untouched.
    """

    solver = task.solver_requirements
    thermal = solver.thermal_guard

    def same(left: float, right: float) -> bool:
        return abs(left - right) <= max(1e-12, abs(right) * 1e-9)

    def residual_limit(name: str) -> float | None:
        normalized = name.lower().replace("_", "-").replace(" ", "")
        aliases = {
            "continuity": ("continuity", "连续性"),
            "x-velocity": ("x-velocity", "x方向速度", "x速度"),
            "y-velocity": ("y-velocity", "y方向速度", "y速度"),
            "z-velocity": ("z-velocity", "z方向速度", "z速度"),
            "k": ("k残差", "turbulentkineticenergy", "湍流动能"),
            "omega": ("omega", "ω", "比耗散率"),
            "energy": ("energy", "能量"),
        }
        for equation, words in aliases.items():
            if any(word in normalized for word in words):
                value = solver.residual_thresholds.get(equation)
                return float(value) if value is not None else None
        return None

    kept = []
    for constraint in task.constraints:
        if constraint.metric_key:
            kept.append(constraint)
            continue
        name = constraint.semantic_metric.lower()
        limit = residual_limit(name) if ("残差" in name or "residual" in name) else None
        duplicated = limit is not None and same(constraint.value, limit)
        if not duplicated and ("质量" in name or "mass" in name) and (
            "不平衡" in name or "守恒" in name or "balance" in name
        ):
            duplicated = solver.mass_balance_required and same(
                constraint.value, solver.mass_balance_relative_tolerance
            )
        if not duplicated and thermal is not None and ("热量" in name or "heat" in name) and (
            "误差" in name or "守恒" in name or "error" in name or "balance" in name
        ):
            duplicated = same(constraint.value, thermal.heat_relative_tolerance)
        if not duplicated and thermal is not None and ("温度" in name or "temperature" in name) and (
            "波动" in name or "稳定" in name or "span" in name or "stability" in name
        ):
            duplicated = same(constraint.value, thermal.temperature_span_K)
        if not duplicated:
            kept.append(constraint)
    return task if len(kept) == len(task.constraints) else task.model_copy(update={"constraints": kept})


def _issue_records(
    messages: list[str],
    *,
    node: str,
    code: str,
    repairable: bool = True,
) -> list[dict[str, Any]]:
    return [
        {
            "code": code,
            "node": node,
            "path": "",
            "severity": "blocking",
            "expected": message,
            "repairable": repairable,
            "status": "open",
        }
        for message in dict.fromkeys(messages)
    ]


def _resolved_issue_history(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    history = [dict(item) for item in state.get("issue_history", [])]
    for issue in state.get("active_issues", []):
        resolved = {**dict(issue), "status": "resolved"}
        if resolved not in history:
            history.append(resolved)
    return history


def _mapping_issue_records(messages: list[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for message in dict.fromkeys(messages):
        lowered = message.lower()
        if "exactly one" in lowered:
            code = "EXPRESSION_FORM_CONFLICT"
        elif "参数数量" in message or "arity" in lowered:
            code = "OPERATOR_ARITY_INVALID"
        elif "循环引用" in message:
            code = "EXPRESSION_CYCLE"
        elif "引用未声明" in message or "引用不存在" in message:
            code = "REFERENCE_INVALID"
        elif "单位" in message:
            code = "UNIT_MISMATCH"
        elif "capability" in lowered or "扫描目录" in message:
            code = "CAPABILITY_OR_CATALOG_INVALID"
        elif "命名空间" in message or "冲突" in message:
            code = "VARIABLE_NAMESPACE_CONFLICT"
        else:
            code = "MAPPING_VALIDATION_FAILED"
        records.extend(_issue_records([message], node="mapping_designer_agent", code=code))
    return records


class FluentAgentGraph:
    def __init__(
        self,
        runtime: Any,
        *,
        model_id: str | None,
        database_path: str | Path,
        output_dir_provider: Callable[[], Path],
        identity_checker: IdentityChecker | None = None,
        timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS,
        max_classification_revisions: int = MAX_CLASSIFICATION_REVISIONS,
        max_mapping_revisions: int = MAX_MAPPING_REVISIONS,
        network_retries: int = 1,
        unavailable_reason: str | None = None,
    ) -> None:
        self.runtime = runtime
        self.model_id = model_id
        self.database_path = Path(database_path)
        self.output_dir_provider = output_dir_provider
        self.identity_checker = identity_checker
        self.timeout_seconds = timeout_seconds
        self.max_classification_revisions = max(1, int(max_classification_revisions))
        self.max_mapping_revisions = max(1, int(max_mapping_revisions))
        self.network_retries = max(0, int(network_retries))
        self.unavailable_reason = unavailable_reason
        self.registry: CapabilityRegistry | None = None
        self._checkpointer_context = None
        self.checkpointer = None
        self.graph = None

    @property
    def configured(self) -> bool:
        return self.runtime is not None

    async def start(self) -> None:
        if self.graph is not None:
            return
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpointer_context = AsyncSqliteSaver.from_conn_string(str(self.database_path))
        self.checkpointer = await self._checkpointer_context.__aenter__()
        await self.checkpointer.setup()
        self.graph = self._builder().compile(checkpointer=self.checkpointer)

    async def close(self) -> None:
        if self._checkpointer_context is not None:
            await self._checkpointer_context.__aexit__(None, None, None)
        self._checkpointer_context = None
        self.checkpointer = None
        self.graph = None

    def bind_registry(self, registry: CapabilityRegistry) -> None:
        self.registry = registry

    def state_info(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "model_id": self.model_id,
            "mode": "langgraph_v3",
            "framework": "langgraph",
            "checkpoint": "sqlite",
            "timeout_seconds": self.timeout_seconds,
            "max_classification_revisions": self.max_classification_revisions,
            "max_mapping_revisions": self.max_mapping_revisions,
            "network_retries": self.network_retries,
            "stream_mode": "updates",
            "reason": self.unavailable_reason,
        }

    async def _guard(self, state: Mapping[str, Any]) -> None:
        if self.identity_checker is None:
            return
        result = self.identity_checker(_identity(state))
        if inspect.isawaitable(result):
            result = await result
        if not result:
            raise StaleAgentResponse("Agent 响应身份已过期，结果已丢弃")

    async def _generate_raw(
        self,
        state: Mapping[str, Any],
        stage: str,
        payload: Mapping[str, Any],
        model_type,
    ) -> Any:
        if self.runtime is None:
            raise RuntimeError("未配置统一模型运行时")
        prompt = json.dumps(
            {
                "stage": stage,
                "identity": _identity(state),
                "conversation": state.get("messages", []),
                "stable_research_question": state.get("research_question", ""),
                **payload,
            },
            ensure_ascii=False,
        )
        stage_rules = {
            "task_parser_agent": (
                "语义变量 id 是内部稳定标识，不能使用 capability registry 中的 Fluent parameter ID；"
                "semantic_key 表示物理含义，二者必须分离。"
                "case_evidence 是服务端扫描与指纹计算得到的权威事实：如果其中已提供配对 data、"
                "现有报告或基线不变性证明，必须直接使用这些事实，不得再向用户索要报告 ID、文件路径、"
                "SHA256 或未参数化固定设置的虚构绑定。配对 data 只能引用 paired-case-data；"
                "由现有 Fluent 报告回读的固定条件使用 verification=run_readback 和 report_id；"
                "由 case_evidence.solver_readbacks 回读的固定条件使用 verification=run_readback 和 evidence_key。"
                "残差阈值、质量平衡、热平衡和迭代窗口温度稳定性只属于 solver_requirements/thermal_guard，"
                "不得同时复制到需要 Case metric_key 的工程 constraints。"
            ),
            "variable_classifier_agent": (
                "必须逐个变量选择 DIRECT、MAPPED_PROXY 或 GEOMETRY_UNSUPPORTED；"
                "参数与指标 ID 只能从当前 capability registry 中选择。"
            ),
            "mapping_designer_agent": (
                "必须按 mapping_dsl_contract 独立生成完整 MappingSpec，"
                "同时给出正向与反向表达式、单位、坐标/角度约定和验证路线。"
            ),
            "mapping_reviewer_agent": (
                "依据 structured_validation_errors 修复并输出完整 MappingSpec；"
                "不得绕过校验器或把错误隐藏到文本字段中。"
            ),
        }.get(stage, "严格完成当前节点职责，不得替代其他节点或宣称已审批。")
        started = time.perf_counter()
        started_at = _now()
        outcome = "completed"
        error: str | None = None
        retry_count = 0
        deadline = time.monotonic() + self.timeout_seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                try:
                    raw = await asyncio.wait_for(
                        self.runtime.generate_json(
                            prompt,
                            schema=schema_for(model_type),
                            system_prompt=(
                                "你是 Fluent 实验规划 Agent V3 的一个受限节点。只能输出给定 JSON Schema；"
                                "分类、能力选择、参数 ID 选择及映射公式由你判断，但只能引用提供的目录 ID。"
                                "禁止 Python、eval、exec、Fluent Setting Path、文件路径、MCP/网络命令和 approved=true。"
                                "信息不足必须明确提问，不得猜测单位、范围、参考条件或坐标约定。"
                                f"当前节点规则：{stage_rules}"
                            ),
                            model_id=self.model_id,
                            reasoning=ReasoningConfig(
                                effort=str(state.get("effective_reasoning") or "medium"),
                                context="current_turn",
                                mode="standard",
                            ),
                        ),
                        timeout=remaining,
                    )
                    break
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    raise
                except Exception as exc:
                    text = str(exc).lower()
                    transient = isinstance(exc, (ConnectionError, OSError)) or any(
                        token in text
                        for token in ("429", "rate limit", "temporarily unavailable", "connection reset", "timeout")
                    )
                    if not transient or retry_count >= self.network_retries:
                        raise
                    retry_count += 1
                    await asyncio.sleep(min(0.5 * retry_count, max(0.0, deadline - time.monotonic())))
        except asyncio.TimeoutError:
            outcome = "timed_out"
            error = "model_call_timeout"
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            error = "local_cancellation"
            raise
        except Exception as exc:
            outcome = "failed"
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self._append_call_audit(
                state,
                {
                    "stage": stage,
                    "started_at": started_at,
                    "finished_at": _now(),
                    "duration_seconds": round(time.perf_counter() - started, 6),
                    "outcome": outcome,
                    "error": error,
                    "model_id": self.model_id,
                    "effective_reasoning": state.get("effective_reasoning", "medium"),
                    "runtime": type(self.runtime).__name__,
                    "schema_enforcement": "provider_requested_and_local_validated",
                    "provider_request_id": None,
                    "token_usage": None,
                    "network_retry_count": retry_count,
                    "contract_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                },
            )
        await self._guard(state)
        return raw

    def _append_call_audit(self, state: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        attempt_id = str(state.get("planning_attempt_id") or "untracked")
        directory = self.output_dir_provider() / "planning_audit"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{attempt_id}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(event), ensure_ascii=False, sort_keys=True) + "\n")

    async def _generate(self, state: Mapping[str, Any], stage: str, payload: Mapping[str, Any], model_type):
        raw = await self._generate_raw(state, stage, payload, model_type)
        return model_type.model_validate(raw)

    async def task_parser_agent(self, state: FluentAgentState) -> dict[str, Any]:
        envelope = await self._generate(
            state,
            "task_parser_agent",
            {
                "latest_user_message": state.get("latest_user_message", ""),
                "previous_task": state.get("task_object"),
                "capability_registry": self._require_registry().llm_context(),
                "semantic_variable_contract": {
                    "id": "仅用于任务内引用的稳定语义 ID，禁止等于任何 Fluent parameter ID",
                    "semantic_key": "物理语义，用于匹配 capability",
                    "native_parameter_id": "仅允许在分类后的 candidate_parameter_ids 中出现",
                },
                "fixed_condition_rules": mapping_dsl_contract()["fixed_condition_contract"],
                "system_evidence_rules": [
                    "case_evidence 是服务端从当前 Case 直接扫描或校验得到的权威事实，必须优先使用",
                    "需要伪瞬态设置时读取 case_evidence.solver_readbacks；不得要求用户提供 Case 内部数值",
                    "不得要求用户提供报告 ID、文件路径、SHA256 或参数 ID",
                ],
            },
            TaskParseEnvelope,
        )
        task = envelope.task
        task = _without_duplicate_solver_gate_constraints(task)
        stable_question = str(state.get("research_question") or "").strip()
        allow_intent_revision = state.get("latest_input_category") == "plan_revision"
        if stable_question and not allow_intent_revision:
            task = task.model_copy(update={"research_question": stable_question})
        return {
            "research_question": task.research_question if allow_intent_revision else stable_question or task.research_question,
            "task_object": task.model_dump(mode="json"),
            "agent_message": envelope.message,
            "status": "parsing",
            "approval_status": "required",
            "approval_fingerprint": None,
            "resolved_task": None,
            "geometry_recommendations": [],
            "best_result": None,
            "current_node": "task_parser_agent",
        }

    async def task_schema_validator(self, state: FluentAgentState) -> dict[str, Any]:
        try:
            task = SimulationTaskObject.model_validate(state.get("task_object"))
            registry_parameter_ids = set(self._require_registry().parameters)
            used_ids = {item.id for item in task.variables if item.id not in registry_parameter_ids}
            normalized_variables = []
            for variable in task.variables:
                if variable.id not in registry_parameter_ids:
                    normalized_variables.append(variable)
                    continue
                base = re.sub(r"[^A-Za-z0-9_-]+", "_", f"semantic_{variable.semantic_key}").strip("_")
                if not base or not base[0].isalpha():
                    base = f"semantic_{base}"
                candidate = base[:120]
                suffix = 2
                while candidate in used_ids or candidate in registry_parameter_ids:
                    tail = f"_{suffix}"
                    candidate = f"{base[:120 - len(tail)]}{tail}"
                    suffix += 1
                used_ids.add(candidate)
                normalized_variables.append(variable.model_copy(update={"id": candidate}))
            if normalized_variables != task.variables:
                task = task.model_copy(update={"variables": normalized_variables})
            gaps = task.information_gaps()
        except ValueError as exc:
            errors = [str(exc)]
            return {
                "validation_errors": errors,
                "active_issues": _issue_records(errors, node="task_parser_agent", code="TASK_SCHEMA_INVALID"),
                "clarification_questions": ["请补充能够形成合法任务对象的变量、范围、单位、目标和终止条件。"],
                "status": "needs_information",
                "current_node": "task_schema_validator",
            }
        if task.needs_information and not gaps:
            gaps = task.questions or ["请补充完成该仿真任务所需的信息。"]
        return {
            "task_object": task.model_dump(mode="json"),
            "validation_errors": [],
            "clarification_questions": gaps,
            "active_issues": _issue_records(gaps, node="task_parser_agent", code="PHYSICAL_INFORMATION_MISSING", repairable=False),
            "issue_history": _resolved_issue_history(state),
            "status": "needs_information" if gaps else "classifying",
            "current_node": "task_schema_validator",
        }

    async def clarification_router(self, state: FluentAgentState) -> dict[str, Any]:
        return {
            "status": "human_review_required" if state.get("status") == "human_review_required" else "needs_information",
            "agent_message": state.get("agent_message") or "需要补充信息后继续。",
            "current_node": "clarification_router",
        }

    async def variable_classifier_agent(self, state: FluentAgentState) -> dict[str, Any]:
        registry = self._require_registry()
        raw = await self._generate_raw(
            state,
            "variable_classifier_agent",
            {
                "task": state.get("task_object"),
                "capability_registry": registry.llm_context(),
                "validation_errors": state.get("validation_errors", []),
                "allowed_classifications": [item.value for item in VariableClassification],
            },
            ClassificationEnvelope,
        )
        bindings = raw.get("bindings", []) if isinstance(raw, Mapping) else []
        return {"variable_bindings": bindings if isinstance(bindings, list) else [], "status": "classifying", "current_node": "variable_classifier_agent"}

    async def classification_validator(self, state: FluentAgentState) -> dict[str, Any]:
        registry = self._require_registry()
        task = SimulationTaskObject.model_validate(state.get("task_object"))
        errors: list[str] = []
        try:
            bindings = [VariableBinding.model_validate(item) for item in state.get("variable_bindings", [])]
        except ValueError as exc:
            bindings = []
            errors.append(str(exc))
        by_id = {item.variable_id: item for item in bindings}
        if set(by_id) != {item.id for item in task.variables} or len(by_id) != len(bindings):
            errors.append("分类结果必须与 Task Object 变量一一对应且不能重复")
        collisions = {item.id for item in task.variables} & set(registry.parameters)
        if collisions:
            errors.append(f"语义变量 ID 不得与 Fluent 参数 ID 冲突：{', '.join(sorted(collisions))}")
        unsupported: list[str] = []
        selected_metric_ids: set[str] = set()
        for binding in bindings:
            if binding.classification == VariableClassification.GEOMETRY_UNSUPPORTED:
                unsupported.append(binding.variable_id)
                if binding.selected_capability_id or binding.candidate_parameter_ids:
                    errors.append("GEOMETRY_UNSUPPORTED 不得伪造可执行能力或参数")
                continue
            if not binding.selected_capability_id:
                errors.append(f"变量 {binding.variable_id} 缺少 selected_capability_id")
                continue
            capability = registry.get(binding.selected_capability_id)
            if capability is None:
                errors.append(f"capability_id 不属于服务端 Registry：{binding.selected_capability_id}")
                continue
            intent = next((item for item in task.variables if item.id == binding.variable_id), None)
            if intent and intent.semantic_key not in capability.semantic_keys:
                errors.append(f"变量 {binding.variable_id} semantic_key 与所选 Capability 不兼容")
            errors.extend(registry.validate_parameter_ids(binding.selected_capability_id, binding.candidate_parameter_ids))
            errors.extend(registry.validate_metric_ids(binding.selected_capability_id, binding.candidate_metric_ids))
            selected_metric_ids.update(binding.candidate_metric_ids)
            if binding.classification == VariableClassification.DIRECT and len(binding.candidate_parameter_ids) != 1:
                errors.append(f"DIRECT 变量 {binding.variable_id} 必须选择且只选择一个参数 ID")
            if binding.classification == VariableClassification.DIRECT and len(binding.candidate_parameter_ids) == 1:
                parameter = registry.parameters.get(binding.candidate_parameter_ids[0])
                if intent and parameter and intent.unit != parameter.unit:
                    errors.append(f"DIRECT 变量 {binding.variable_id} 单位与 Fluent 参数不兼容")
            if binding.missing_information:
                errors.append(f"变量 {binding.variable_id} 仍有未解决信息：{'；'.join(binding.missing_information)}")
        if len(unsupported) != len(bindings):
            required_metric_ids = {
                key
                for key in [task.objective.metric_key, *(item.metric_key for item in task.constraints)]
                if key
            }
            for metric_id in sorted(required_metric_ids - selected_metric_ids):
                errors.append(f"LLM 分类结果未选择任务所需 metric_id：{metric_id}")
        increment = 1 if errors else 0
        count = int(state.get("classification_revision_count", 0)) + increment
        total = int(state.get("classification_revision_total", 0)) + increment
        status = "human_review_required" if errors and count >= self.max_classification_revisions else "classifying"
        return {
            "validation_errors": list(dict.fromkeys(errors)),
            "active_issues": _issue_records(errors, node="variable_classifier_agent", code="CLASSIFICATION_INVALID"),
            "issue_history": _resolved_issue_history(state),
            "geometry_unsupported": unsupported,
            "classification_revision_count": count,
            "classification_revision_total": total,
            "status": status,
            "current_node": "classification_validator",
        }

    async def mapping_designer_agent(self, state: FluentAgentState) -> dict[str, Any]:
        bindings = [VariableBinding.model_validate(item) for item in state.get("variable_bindings", [])]
        mapped = [item for item in bindings if item.classification == VariableClassification.MAPPED_PROXY]
        if not mapped:
            return {"mapping_specs": [], "status": "designing_mapping", "current_node": "mapping_designer_agent"}
        raw = await self._generate_raw(
            state,
            "mapping_designer_agent",
            {
                "task": state.get("task_object"),
                "mapped_proxy_bindings": [item.model_dump(mode="json") for item in mapped],
                "capability_registry": self._require_registry().llm_context(),
                "mapping_dsl_contract": mapping_dsl_contract(),
            },
            MappingEnvelope,
        )
        mappings = raw.get("mappings", []) if isinstance(raw, Mapping) else []
        return {"mapping_specs": mappings if isinstance(mappings, list) else [], "status": "designing_mapping", "current_node": "mapping_designer_agent"}

    async def mapping_spec_validator(self, state: FluentAgentState) -> dict[str, Any]:
        task = SimulationTaskObject.model_validate(state.get("task_object"))
        bindings = [VariableBinding.model_validate(item) for item in state.get("variable_bindings", [])]
        mapped_ids = {item.variable_id for item in bindings if item.classification == VariableClassification.MAPPED_PROXY}
        errors: list[str] = []
        mappings: list[MappingSpec] = []
        try:
            mappings = [MappingSpec.model_validate(item) for item in state.get("mapping_specs", [])]
        except ValueError as exc:
            errors.append(str(exc))
        coverage = [source for mapping in mappings for source in mapping.source_variables]
        covered = set(coverage)
        if covered != mapped_ids:
            errors.append("每个 MAPPED_PROXY 变量必须且只能由已验证 MappingSpec 覆盖")
        if len(coverage) != len(covered):
            errors.append("MAPPED_PROXY 变量不能被多个 MappingSpec 重复覆盖")
        for mapping in mappings:
            errors.extend(validate_mapping_spec(mapping, task, self._require_registry()))
        proxy_owners: dict[str, str] = {}
        parameter_owners: dict[str, str] = {}
        for mapping in mappings:
            for proxy in mapping.proxy_variables:
                owner = proxy_owners.setdefault(proxy, mapping.mapping_id)
                if owner != mapping.mapping_id:
                    errors.append(f"跨映射代理变量冲突：{proxy} 同时属于 {owner} 和 {mapping.mapping_id}")
            for parameter_id in mapping.selected_fluent_parameter_ids:
                owner = parameter_owners.setdefault(parameter_id, mapping.mapping_id)
                if owner != mapping.mapping_id:
                    errors.append(f"跨映射 Fluent 参数冲突：{parameter_id} 同时属于 {owner} 和 {mapping.mapping_id}")
        increment = 1 if errors else 0
        count = int(state.get("mapping_revision_count", 0)) + increment
        total = int(state.get("mapping_revision_total", 0)) + increment
        status = "human_review_required" if errors and count >= self.max_mapping_revisions else "designing_mapping"
        issue_history = _resolved_issue_history(state)
        return {
            "validation_errors": list(dict.fromkeys(errors)),
            "mapping_review_issues": list(dict.fromkeys(errors)),
            "active_issues": _mapping_issue_records(errors),
            "issue_history": issue_history,
            "mapping_revision_count": count,
            "mapping_revision_total": total,
            "status": status,
            "current_node": "mapping_spec_validator",
        }

    async def mapping_reviewer_agent(self, state: FluentAgentState) -> dict[str, Any]:
        raw = await self._generate_raw(
            state,
            "mapping_reviewer_agent",
            {
                "task": state.get("task_object"),
                "bindings": state.get("variable_bindings", []),
                "invalid_mappings": state.get("mapping_specs", []),
                "structured_validation_errors": state.get("validation_errors", []),
                "capability_registry": self._require_registry().llm_context(),
                "mapping_dsl_contract": mapping_dsl_contract(),
            },
            MappingEnvelope,
        )
        mappings = raw.get("mappings", []) if isinstance(raw, Mapping) else []
        return {"mapping_specs": mappings if isinstance(mappings, list) else [], "status": "reviewing_mapping", "current_node": "mapping_reviewer_agent"}

    async def resolved_task_compiler(self, state: FluentAgentState) -> dict[str, Any]:
        task = SimulationTaskObject.model_validate(state.get("task_object"))
        bindings = [VariableBinding.model_validate(item) for item in state.get("variable_bindings", [])]
        mappings = [MappingSpec.model_validate(item) for item in state.get("mapping_specs", [])]
        mapping_by_source = {source: mapping.mapping_id for mapping in mappings for source in mapping.source_variables}
        unsupported = [item.variable_id for item in bindings if item.classification == VariableClassification.GEOMETRY_UNSUPPORTED]
        errors = list(state.get("validation_errors", []))
        registry = self._require_registry()
        objective = task.objective
        if not objective.metric_key or objective.metric_key not in registry.metrics:
            errors.append("目标 metric_key 必须属于当前 Case 指标目录")
        elif objective.unit != registry.metrics[objective.metric_key].unit:
            errors.append("目标单位与当前 Case 指标目录不一致")
        for constraint in task.constraints:
            if not constraint.metric_key or constraint.metric_key not in registry.metrics:
                errors.append(f"约束 metric_key 不属于当前 Case 指标目录：{constraint.semantic_metric}")
            elif constraint.unit != registry.metrics[constraint.metric_key].unit:
                errors.append(f"约束单位与当前 Case 指标目录不一致：{constraint.semantic_metric}")
        optimized_parameter_ids = {
            parameter_id
            for binding in bindings
            for parameter_id in binding.candidate_parameter_ids
        } | {
            parameter_id
            for mapping in mappings
            for parameter_id in mapping.selected_fluent_parameter_ids
        }
        for condition in task.fixed_conditions:
            if condition.parameter_id is None:
                if condition.verification == "run_readback" and condition.report_id:
                    existing_reports = {
                        item["name"]
                        for item in registry.llm_context()["case_evidence"]["existing_reports"]
                    }
                    if condition.report_id not in existing_reports:
                        errors.append(f"固定条件 {condition.id} 的 report_id 不属于当前 Case 现有报告")
                elif condition.verification == "run_readback" and condition.evidence_key:
                    readbacks = registry.llm_context()["case_evidence"]["solver_readbacks"]
                    if condition.evidence_key not in readbacks:
                        errors.append(f"固定条件 {condition.id} 的 evidence_key 不属于当前 Case 系统回读")
                    else:
                        try:
                            readback_value = float(readbacks[condition.evidence_key])
                        except (TypeError, ValueError):
                            errors.append(f"固定条件 {condition.id} 的系统回读不是数值")
                        else:
                            if abs(condition.value - readback_value) > 1e-12:
                                errors.append(f"固定条件 {condition.id} 与当前 Case 系统回读值不一致")
                elif condition.verification != "documented_assumption":
                    errors.append(f"固定条件 {condition.id} 缺少可执行 parameter_id、report_id 或 evidence_key")
                continue
            parameter = registry.parameters.get(condition.parameter_id)
            if parameter is None:
                errors.append(f"固定条件 {condition.id} 的 parameter_id 不属于当前扫描目录")
                continue
            if condition.parameter_id in optimized_parameter_ids:
                errors.append(f"固定条件 {condition.id} 与优化变量共用 Fluent 参数 {condition.parameter_id}")
            if condition.unit != parameter.unit:
                errors.append(f"固定条件 {condition.id} 单位与 Fluent 参数不一致")
                continue
            try:
                parameter.validate_display_value(condition.value, condition.id)
            except ValueError as exc:
                errors.append(str(exc))
            if condition.verification == "case_parameter" and abs(condition.value - parameter.default_value) > 1e-12:
                errors.append(
                    f"固定条件 {condition.id}={condition.value:g} {condition.unit} 与 Case 扫描值 "
                    f"{parameter.default_value:g} {parameter.unit} 不一致；请改为 run_readback 或修正条件"
                )
        resolved = ResolvedTaskObject(
            task=task,
            variables=[
                ResolvedVariable(
                    intent=next(variable for variable in task.variables if variable.id == binding.variable_id),
                    binding=binding,
                    mapping_id=mapping_by_source.get(binding.variable_id),
                    executable=binding.classification != VariableClassification.GEOMETRY_UNSUPPORTED,
                ) for binding in bindings
            ],
            mappings=mappings,
            executable=not unsupported and not errors,
            geometry_unsupported=unsupported,
            validation_errors=list(dict.fromkeys(errors)),
            capability_registry_version=CAPABILITY_REGISTRY_VERSION,
        )
        return {
            "resolved_task": resolved.model_dump(mode="json"),
            "geometry_unsupported": unsupported,
            "validation_errors": list(dict.fromkeys(errors)),
            "active_issues": _issue_records(errors, node="resolved_task_compiler", code="RESOLVED_TASK_INVALID"),
            "status": "geometry_unsupported" if unsupported else "human_review_required" if errors else "awaiting_approval",
            "current_node": "resolved_task_compiler",
        }

    async def human_approval_gate(self, state: FluentAgentState) -> dict[str, Any]:
        resolved = ResolvedTaskObject.model_validate(state.get("resolved_task"))
        if not resolved.executable:
            return {"approval_status": "rejected", "status": "geometry_unsupported", "current_node": "human_approval_gate"}
        decision = interrupt({
            "kind": "fluent_experiment_approval",
            "task_id": state.get("task_id"),
            "resolved_task": resolved.model_dump(mode="json"),
            "message": "LLM 无权批准；请由用户审查最终 ResolvedTaskObject。",
        })
        if not isinstance(decision, Mapping) or decision.get("approved") is not True:
            return {"approval_status": "rejected", "approval_fingerprint": None, "status": "awaiting_approval", "current_node": "human_approval_gate"}
        await self._guard(state)
        fingerprint = str(decision.get("approval_fingerprint") or "").strip()
        if not fingerprint:
            raise PermissionError("人工批准必须包含服务端审批指纹")
        return {"approval_status": "approved", "approval_fingerprint": fingerprint, "status": "ready_to_run", "current_node": "human_approval_gate"}

    async def experiment_orchestrator(self, state: FluentAgentState) -> dict[str, Any]:
        if state.get("approval_status") != "approved":
            return {"status": "awaiting_approval"}
        # Execution remains in the existing WebRuntime -> Optuna -> Runner path.
        # This node is the durable coordination boundary and is resumed with the
        # result after that path completes; it never replaces Job idempotency.
        return {"status": "completed" if state.get("best_result") is not None or state.get("termination_reason") else "ready_to_run"}

    async def result_interpreter_agent(self, state: FluentAgentState) -> dict[str, Any]:
        resolved = ResolvedTaskObject.model_validate(state.get("resolved_task"))
        if not resolved.mappings or not state.get("best_result") or int(state.get("feasible_trials", 0)) < 1:
            return {"pending_geometry_recommendation": None, "status": "completed"}
        envelope = await self._generate(
            state,
            "result_interpreter_agent",
            {
                "approved_resolved_task": resolved.model_dump(mode="json"),
                "best_result": state.get("best_result"),
                "campaign_id": state.get("campaign_id"),
                "constraints": resolved.task.constraints,
            },
            GeometryRecommendationEnvelope,
        )
        return {
            "agent_message": envelope.message,
            "pending_geometry_recommendation": envelope.recommendation.model_dump(mode="json"),
            "status": "completed",
        }

    async def geometry_recommendation_builder(self, state: FluentAgentState) -> dict[str, Any]:
        raw = state.get("pending_geometry_recommendation")
        if not raw:
            return {"geometry_recommendations": [], "status": "completed"}
        envelope = GeometryRecommendationEnvelope.model_validate({"message": state.get("agent_message") or "几何建议", "recommendation": raw})
        recommendation = envelope.recommendation
        resolved = ResolvedTaskObject.model_validate(state.get("resolved_task"))
        mapping = next((item for item in resolved.mappings if item.mapping_id == recommendation.mapping_id), None)
        if mapping is None or mapping.mapping_version != recommendation.mapping_version:
            raise ValueError("GeometryRecommendation 未绑定已批准 MappingSpec")
        reversed_values = reverse_mapping(
            mapping,
            {recommendation.proxy_variable: recommendation.optimal_proxy_value},
            recommendation.reference_conditions,
        )
        expected = reversed_values.get(recommendation.recommended_geometry_parameter)
        if expected is None or abs((((expected - recommendation.recommended_geometry_value) + 180) % 360) - 180) > 1e-6:
            raise ValueError("GeometryRecommendation 与已批准 reverse expression 不一致")
        delta = recommendation.future_handoff_request.delta
        request = GeometryVerificationRequest(
            source_campaign_id=recommendation.source_campaign_id,
            source_trial_id=recommendation.source_trial_id,
            capability_id=recommendation.capability_id,
            mapping_id=recommendation.mapping_id,
            geometry_parameter=recommendation.recommended_geometry_parameter,
            candidates=[
                GeometryCandidate(value=recommendation.recommended_geometry_value - delta, unit=recommendation.unit, label="optimum-minus-delta"),
                GeometryCandidate(value=recommendation.recommended_geometry_value, unit=recommendation.unit, label="optimum"),
                GeometryCandidate(value=recommendation.recommended_geometry_value + delta, unit=recommendation.unit, label="optimum-plus-delta"),
            ],
            reference_conditions=recommendation.reference_conditions,
            verification_route=recommendation.verification_route,
        )
        output = self.output_dir_provider()
        output.mkdir(parents=True, exist_ok=True)
        atomic_json(output / "geometry_recommendation.json", recommendation.model_dump(mode="json"))
        handoff = await SuggestionOnlyGeometryHandoff(output).submit(request)
        return {
            "geometry_recommendations": [recommendation.model_dump(mode="json")],
            "geometry_handoff_result": handoff.model_dump(mode="json"),
            "status": "completed",
        }

    def _require_registry(self) -> CapabilityRegistry:
        if self.registry is None:
            raise RuntimeError("请先扫描 Fluent 模型并绑定 Capability Registry")
        return self.registry

    def _builder(self):
        graph = StateGraph(FluentAgentState)
        for name in (
            "task_parser_agent", "task_schema_validator", "clarification_router",
            "variable_classifier_agent", "classification_validator", "mapping_designer_agent",
            "mapping_spec_validator", "mapping_reviewer_agent", "resolved_task_compiler",
            "human_approval_gate", "experiment_orchestrator", "result_interpreter_agent",
            "geometry_recommendation_builder",
        ):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "task_parser_agent")
        graph.add_edge("task_parser_agent", "task_schema_validator")
        graph.add_conditional_edges("task_schema_validator", lambda s: "clarify" if s.get("clarification_questions") else "classify", {"clarify": "clarification_router", "classify": "variable_classifier_agent"})
        graph.add_edge("clarification_router", END)
        graph.add_edge("variable_classifier_agent", "classification_validator")
        graph.add_conditional_edges(
            "classification_validator",
            lambda s: "review" if s.get("status") == "human_review_required" else "retry" if s.get("validation_errors") else "map",
            {"review": "clarification_router", "retry": "variable_classifier_agent", "map": "mapping_designer_agent"},
        )
        graph.add_edge("mapping_designer_agent", "mapping_spec_validator")
        graph.add_conditional_edges(
            "mapping_spec_validator",
            lambda s: "review" if s.get("status") == "human_review_required" else "retry" if s.get("validation_errors") else "resolve",
            {"review": "clarification_router", "retry": "mapping_reviewer_agent", "resolve": "resolved_task_compiler"},
        )
        graph.add_edge("mapping_reviewer_agent", "mapping_spec_validator")
        graph.add_conditional_edges(
            "resolved_task_compiler",
            lambda s: (
                "blocked" if s.get("geometry_unsupported")
                else "review" if s.get("status") == "human_review_required"
                else "approve"
            ),
            {"blocked": END, "review": "clarification_router", "approve": "human_approval_gate"},
        )
        graph.add_edge("human_approval_gate", "experiment_orchestrator")
        graph.add_conditional_edges("experiment_orchestrator", lambda s: "interpret" if s.get("best_result") is not None else "done", {"interpret": "result_interpreter_agent", "done": END})
        graph.add_edge("result_interpreter_agent", "geometry_recommendation_builder")
        graph.add_edge("geometry_recommendation_builder", END)
        return graph

    def _config(self, task_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": task_id}}

    async def get_state(self, task_id: str) -> dict[str, Any] | None:
        await self.start()
        snapshot = await self.graph.aget_state(self._config(task_id))
        return dict(snapshot.values) if snapshot and snapshot.values else None

    async def _cancel_pending_approval(self, task_id: str) -> None:
        snapshot = await self.graph.aget_state(self._config(task_id))
        if snapshot.next and "human_approval_gate" in snapshot.next:
            await self.graph.ainvoke(Command(resume={"approved": False}), config=self._config(task_id))

    async def message(
        self,
        message: str,
        *,
        task_id: str,
        conversation_revision: int,
        model_id: str,
        case_sha256: str,
        model_signature_sha256: str,
        planning_attempt_id: str | None = None,
        input_category: str = "user_message",
        effective_reasoning: str = "medium",
    ) -> dict[str, Any]:
        await self.start()
        await self._cancel_pending_approval(task_id)
        previous = await self.get_state(task_id)
        expected = {
            "model_id": model_id,
            "case_sha256": case_sha256,
            "model_signature_sha256": model_signature_sha256,
        }
        if previous is None or any(previous.get(key) != value for key, value in expected.items()) or previous.get("conversation_revision") != conversation_revision:
            state = initial_agent_state(
                task_id=task_id,
                conversation_revision=conversation_revision,
                model_id=model_id,
                case_sha256=case_sha256,
                model_signature_sha256=model_signature_sha256,
            )
        else:
            state = FluentAgentState(**previous)
        if not state.get("original_request"):
            state["original_request"] = message
            state["current_intent"] = message
            state["plan_revision"] = max(1, int(state.get("plan_revision", 0)))
        elif input_category == "plan_revision":
            state["current_intent"] = message
            state["plan_revision"] = int(state.get("plan_revision", 1)) + 1
        state["planning_attempt_id"] = planning_attempt_id
        state["latest_input_category"] = input_category
        state["effective_reasoning"] = effective_reasoning
        input_record = {
            "attempt_id": planning_attempt_id,
            "category": input_category,
            "content": message,
            "at": _now(),
        }
        state["input_history"] = [*state.get("input_history", []), input_record]
        state["messages"] = [
            *state.get("messages", []),
            {"role": "user", "content": message, "at": input_record["at"], "category": input_category},
        ]
        state["latest_user_message"] = message
        state["status"] = "parsing"
        state["current_node"] = "task_parser_agent"
        state["classification_revision_count"] = 0
        state["mapping_revision_count"] = 0
        try:
            async for _update in self.graph.astream(
                state,
                config=self._config(task_id),
                stream_mode="updates",
            ):
                pass
            result = await self.get_state(task_id) or state
        except StaleAgentResponse:
            return {**state, "status": "stale_response_discarded", "agent_message": "任务身份已变化，旧响应未写回。"}
        except asyncio.TimeoutError:
            update = {
                "status": "timed_out",
                "agent_message": f"规划模型调用超过 {self.timeout_seconds:g} 秒，已安全终止本次规划。请重试或缩小任务范围。",
                "validation_errors": ["规划模型调用超时"],
                "approval_status": "required",
                "approval_fingerprint": None,
                "resolved_task": None,
                "current_node": state.get("current_node"),
            }
            await self.graph.aupdate_state(self._config(task_id), update)
            result = {**state, **update}
        except asyncio.CancelledError:
            update = {
                "status": "cancelled",
                "agent_message": "本次规划已取消。",
                "approval_status": "required",
                "approval_fingerprint": None,
                "current_node": state.get("current_node"),
            }
            await self.graph.aupdate_state(self._config(task_id), update)
            raise
        except Exception as exc:
            update = {
                "status": "failed",
                "agent_message": f"规划失败：{exc}",
                "validation_errors": [str(exc)],
                "approval_status": "required",
                "approval_fingerprint": None,
                "resolved_task": None,
                "current_node": state.get("current_node"),
            }
            await self.graph.aupdate_state(self._config(task_id), update)
            raise
        if result.get("agent_message"):
            result["messages"] = [*result.get("messages", []), {"role": "assistant", "content": result["agent_message"], "at": _now()}]
            await self.graph.aupdate_state(self._config(task_id), {"messages": result["messages"]})
        return dict(result)

    async def approve(self, task_id: str, approval_fingerprint: str) -> dict[str, Any]:
        await self.start()
        result = await self.graph.ainvoke(
            Command(resume={"approved": True, "approval_fingerprint": approval_fingerprint}),
            config=self._config(task_id),
        )
        return dict(result)

    async def set_terminal_state(
        self,
        task_id: str,
        *,
        status: str,
        message: str,
        error: str | None = None,
    ) -> dict[str, Any]:
        await self.start()
        update: dict[str, Any] = {
            "status": status,
            "agent_message": message,
            "approval_status": "required",
            "approval_fingerprint": None,
            "current_node": None,
        }
        if error:
            update["validation_errors"] = [error]
        # A cancellation can arrive before the first graph node commits its
        # checkpoint.  Name the logical writer so LangGraph can still apply
        # the terminal update deterministically in that race window.
        await self.graph.aupdate_state(
            self._config(task_id), update, as_node="clarification_router"
        )
        return dict(await self.get_state(task_id) or update)

    async def reset(
        self,
        *,
        task_id: str,
        conversation_revision: int,
        model_id: str,
        case_sha256: str,
        model_signature_sha256: str,
    ) -> dict[str, Any]:
        await self.start()
        await self._cancel_pending_approval(task_id)
        state = initial_agent_state(
            task_id=task_id,
            conversation_revision=conversation_revision,
            model_id=model_id,
            case_sha256=case_sha256,
            model_signature_sha256=model_signature_sha256,
        )
        await self.graph.aupdate_state(self._config(task_id), state, as_node="clarification_router")
        return dict(state)

    async def record_experiment_result(self, task_id: str, result: Mapping[str, Any]) -> dict[str, Any]:
        await self.start()
        update = {
            "campaign_id": result.get("campaign_id"),
            "active_trial_id": result.get("active_trial_id"),
            "active_job_id": result.get("active_job_id"),
            "solved_trials": int(result.get("solved_trials", result.get("completed_trials", 0))),
            "completed_trials": int(result.get("completed_trials", 0)),
            "feasible_trials": int(result.get("feasible_trials", 0)),
            "failed_trials": int(result.get("failed_trials", 0)),
            "best_result": result.get("best_result"),
            "termination_reason": result.get("termination_reason"),
            "status": "completed",
        }
        await self.graph.aupdate_state(self._config(task_id), update, as_node="experiment_orchestrator")
        try:
            final = await self.graph.ainvoke(None, config=self._config(task_id))
            return dict(final)
        except Exception as exc:
            recovery = {
                "status": "completed",
                "result_interpretation_error": f"{type(exc).__name__}: {exc}",
                "agent_message": "Fluent 求解与验证已完成，但自动结果解读失败；计算产物仍然有效。",
            }
            await self.graph.aupdate_state(self._config(task_id), recovery)
            return dict(await self.get_state(task_id) or {**update, **recovery})
