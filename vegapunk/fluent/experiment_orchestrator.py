"""Compile and execute approved V3 tasks over the existing Optuna/Runner base."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .capability_registry import CapabilityRegistry
from .mapping_runtime import apply_mapping, evaluate_named_expressions
from .mapping_schema import MappingSpec, ResolvedTaskObject, VariableClassification
from .optimizer import run_optimization
from .runner import FluentExperimentRunner
from .spec import ExperimentSpec
from .verification import run_independent_verification, verification_is_trusted
from .execution_approval import ExecutionApproval, require_execution_approval


@dataclass(frozen=True)
class ResolvedExperiment:
    semantic_spec: ExperimentSpec
    native_spec: ExperimentSpec
    resolved_task: ResolvedTaskObject
    execution_approval: ExecutionApproval | None = None


def _base_raw(
    template: Mapping[str, Any],
    profile: Mapping[str, Any],
    resolved: ResolvedTaskObject,
    registry: CapabilityRegistry,
    endpoint: str,
) -> dict[str, Any]:
    task = resolved.task
    raw = json.loads(json.dumps(template))
    connection = dict(raw.get("connection") or {})
    kwargs = dict(connection.get("connect_kwargs") or {})
    kwargs.update(
        case_file_name=profile["case_file"],
        dimension=profile["dimension"],
    )
    product_version = profile.get("product_version") or profile.get("signature", {}).get("product_version")
    if product_version:
        kwargs["product_version"] = product_version
    connection.update(
        endpoint=endpoint,
        connect_kwargs=kwargs,
        baseline_sha256=profile["case_sha256"],
        reuse_existing_session=False,
        disconnect_on_exit=True,
    )
    raw["connection"] = connection
    objective_key = task.objective.metric_key
    if not objective_key or objective_key not in registry.metrics:
        raise ValueError("ResolvedTaskObject 的目标 metric_key 不属于当前 Case 指标目录")
    constraint_keys = [item.metric_key for item in task.constraints]
    if any(not key or key not in registry.metrics for key in constraint_keys):
        raise ValueError("ResolvedTaskObject 存在目录外或未解析的约束 metric_key")
    needed = {objective_key, *(str(key) for key in constraint_keys)}
    inlet = next((item for item in registry.metrics.values() if item.kind == "flux" and "mass-in" in item.key), None)
    outlet = next((item for item in registry.metrics.values() if item.kind == "flux" and "mass-out" in item.key), None)
    mass_balance = task.solver_requirements.mass_balance_required
    if mass_balance:
        if inlet is None or outlet is None:
            raise ValueError("任务要求质量守恒，但当前 Case 缺少完整入口/出口指标")
        needed.update((inlet.key, outlet.key))
    raw["reports"] = [registry.metrics[key].report_spec() for key in sorted(needed)]
    raw["objective"] = {
        "report": registry.metrics[objective_key].report_name,
        "direction": task.objective.direction,
    }
    raw["constraints"] = [
        {
            "report": registry.metrics[str(item.metric_key)].report_name,
            "operator": item.operator,
            "value": item.value,
        }
        for item in task.constraints
    ]
    raw["parameter_constraints"] = []
    raw["solver"] = {
        "initialization": task.solver_requirements.initialization,
        "iterations": task.solver_requirements.iterations,
        "residual_thresholds": task.solver_requirements.residual_thresholds,
        "thermal_guard": task.solver_requirements.thermal_guard.model_dump() if task.solver_requirements.thermal_guard else None,
    }
    if raw['solver']['thermal_guard']:
        case_path = str(profile['case_file'])
        if not case_path.endswith('.cas.h5'):
            raise ValueError('Thermal continuation requires selected .cas.h5 and its paired .dat.h5')
        # Resolve only the selected case's paired data; the LLM cannot choose paths.
        data_path = Path(case_path[:-7] + '.dat.h5')
        if not data_path.is_file():
            raise ValueError('Thermal continuation paired .dat.h5 does not exist')
        digest = hashlib.sha256()
        with data_path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != raw['solver']['thermal_guard']['data_sha256']:
            raise ValueError('Thermal continuation data SHA256 does not match selected paired data')
        signature = profile.get('signature', {})
        existing = signature.get('reports') or signature.get('existing_reports') or {}
        existing_names = {
            str(definition.get('name') or name)
            for definitions in existing.values()
            if isinstance(definitions, Mapping)
            for name, definition in definitions.items()
            if isinstance(definition, Mapping)
        }
        required_reports = {
            raw['solver']['thermal_guard']['heat_net_report'],
            raw['solver']['thermal_guard']['mass_in_report'],
            raw['solver']['thermal_guard']['mass_out_report'],
            *raw['solver']['thermal_guard']['temperature_reports'],
        }
        missing_reports = sorted(required_reports - existing_names)
        if missing_reports:
            raise ValueError(f'Thermal guard references unknown existing reports: {missing_reports}')
        raw['solver']['thermal_guard']['data_file'] = str(data_path)
    termination = task.termination
    optimization = dict(raw.get("optimization") or {})
    optimization.update(
        target_trials=termination.max_trials,
        n_startup_trials=min(int(optimization.get("n_startup_trials", 2)), termination.max_trials),
        enable_legacy_mass_balance=mass_balance,
        mass_balance_relative_tolerance=task.solver_requirements.mass_balance_relative_tolerance,
        max_wall_time_seconds=termination.max_wall_time_seconds,
        no_improvement_trials=termination.no_improvement_trials,
        target_objective=termination.target_objective,
        maximum_failed_trials=termination.maximum_failed_trials,
        conservation_checks=[],
        numerical_monitors=[],
    )
    if mass_balance:
        optimization["mass_flow_in_report"] = inlet.report_name
        optimization["mass_flow_out_report"] = outlet.report_name
    raw["optimization"] = optimization
    raw["task_name"] = "VegapunkFluentAgentV3"
    raw["execution_contract"] = resolved.model_dump(mode="json")
    return raw


def compile_resolved_experiment(
    template: Mapping[str, Any],
    profile: Mapping[str, Any],
    resolved_task: ResolvedTaskObject | Mapping[str, Any],
    registry: CapabilityRegistry,
    endpoint: str,
) -> ResolvedExperiment:
    resolved = (
        resolved_task
        if isinstance(resolved_task, ResolvedTaskObject)
        else ResolvedTaskObject.model_validate(resolved_task)
    )
    if not resolved.executable or resolved.geometry_unsupported or resolved.validation_errors:
        raise PermissionError("ResolvedTaskObject 不可执行；存在几何阻断或验证错误")
    raw = _base_raw(template, profile, resolved, registry, endpoint)
    semantic_parameters = []
    semantic_values = {}
    native_ids: list[str] = []
    for resolved_variable in resolved.variables:
        intent = resolved_variable.intent
        binding = resolved_variable.binding
        if intent.minimum is None or intent.maximum is None or not intent.unit:
            raise ValueError(f"变量 {intent.id} 缺少范围或单位")
        semantic_parameters.append(
            {
                "name": intent.id,
                "collection_path": "semantic.variables",
                "object_name": intent.id,
                "property_path": "value",
                "unit": intent.unit,
                "minimum": intent.minimum,
                "maximum": intent.maximum,
            }
        )
        semantic_values[intent.id] = (
            intent.initial_value
            if intent.initial_value is not None
            else (intent.minimum + intent.maximum) / 2
        )
        native_ids.extend(binding.candidate_parameter_ids)
    for mapping in resolved.mappings:
        native_ids.extend(mapping.selected_fluent_parameter_ids)
    optimized_native_ids = set(native_ids)
    for condition in resolved.task.fixed_conditions:
        if condition.parameter_id is None:
            continue
        if condition.parameter_id not in registry.parameters:
            raise ValueError(f"固定条件 parameter_id 不属于扫描目录：{condition.parameter_id}")
        if condition.parameter_id in optimized_native_ids:
            raise ValueError(f"固定条件与优化变量共用 Fluent 参数：{condition.parameter_id}")
        parameter = registry.parameters[condition.parameter_id]
        if condition.unit != parameter.unit:
            raise ValueError(f"固定条件 {condition.id} 单位与 Fluent 参数不一致")
        parameter.validate_display_value(condition.value, condition.id)
        if condition.verification == "case_parameter" and abs(condition.value - parameter.default_value) > 1e-12:
            raise ValueError(f"固定条件 {condition.id} 与 Case 扫描值不一致")
    native_ids.extend(
        condition.parameter_id
        for condition in resolved.task.fixed_conditions
        if condition.parameter_id is not None
    )
    native_ids = list(dict.fromkeys(native_ids))
    if not native_ids:
        raise ValueError("ResolvedTaskObject 没有可执行 Fluent 参数")

    semantic_raw = json.loads(json.dumps(raw))
    semantic_raw["parameters"] = semantic_parameters
    semantic_raw["design_points"] = [{"name": "baseline", "values": semantic_values}]
    semantic_spec = ExperimentSpec.from_dict(semantic_raw)

    native_raw = json.loads(json.dumps(raw))
    native_raw["parameters"] = []
    native_values = {}
    for parameter_id in native_ids:
        if parameter_id not in registry.parameters:
            raise ValueError(f"Fluent parameter_id 不属于扫描目录：{parameter_id}")
        parameter = registry.parameters[parameter_id]
        native_raw["parameters"].append(
            parameter.spec_dict(parameter.hard_min, parameter.hard_max)
        )
        native_values[parameter_id] = parameter.native_value(parameter.default_value)
    for condition in resolved.task.fixed_conditions:
        if condition.parameter_id is not None:
            parameter = registry.parameters[condition.parameter_id]
            native_values[condition.parameter_id] = parameter.native_value(condition.value)
    native_raw["design_points"] = [{"name": "baseline", "values": native_values}]
    native_spec = ExperimentSpec.from_dict(native_raw)
    return ResolvedExperiment(semantic_spec, native_spec, resolved)


class MappingRunnerAdapter:
    """Map semantic Optuna values once per Trial, then delegate to the existing runner."""

    def __init__(
        self,
        semantic_spec: ExperimentSpec,
        output_dir: str | Path,
        *,
        native_spec: ExperimentSpec,
        resolved_task: ResolvedTaskObject,
        registry: CapabilityRegistry,
        native_runner_factory=None,
    ) -> None:
        self.spec = semantic_spec
        self.native_spec = native_spec
        self.resolved_task = resolved_task
        self.registry = registry
        if native_runner_factory is None:
            if native_spec.connection.job_endpoint:
                from .controller import FluentJobController

                native_runner_factory = lambda spec, path: FluentJobController(
                    spec, path, campaign_spec=semantic_spec
                )
            else:
                native_runner_factory = FluentExperimentRunner
        self.native_runner = native_runner_factory(native_spec, output_dir)

    async def open_session(self):
        return await self.native_runner.open_session()

    async def close_session(self):
        return await self.native_runner.close_session()

    def _native_values(self, semantic_values: Mapping[str, Any]) -> dict[str, float]:
        native: dict[str, float] = {}
        fixed_evidence: list[dict[str, Any]] = []
        for condition in self.resolved_task.task.fixed_conditions:
            if condition.parameter_id is None:
                if condition.report_id:
                    fixed_evidence.append({
                        "condition_id": condition.id,
                        "report_id": condition.report_id,
                        "expected_value": condition.value,
                        "unit": condition.unit,
                        "verification": condition.verification,
                        "evidence_source": "trial_readback",
                    })
                elif condition.evidence_key:
                    fixed_evidence.append({
                        "condition_id": condition.id,
                        "evidence_key": condition.evidence_key,
                        "expected_value": condition.value,
                        "unit": condition.unit,
                        "verification": condition.verification,
                        "evidence_source": "case_scan",
                    })
                continue
            parameter = self.registry.parameters[condition.parameter_id]
            native[condition.parameter_id] = parameter.native_value(condition.value)
            fixed_evidence.append({
                "condition_id": condition.id,
                "parameter_id": condition.parameter_id,
                "display_value": condition.value,
                "native_value": native[condition.parameter_id],
                "unit": condition.unit,
                "verification": condition.verification,
            })
        self._last_fixed_condition_evidence = fixed_evidence
        mappings = {item.mapping_id: item for item in self.resolved_task.mappings}
        for item in self.resolved_task.variables:
            source = item.intent.id
            if source not in semantic_values:
                raise ValueError(f"Trial 缺少语义变量：{source}")
            if item.binding.classification == VariableClassification.DIRECT:
                parameter_id = item.binding.candidate_parameter_ids[0]
                value = self.registry.parameters[parameter_id].native_value(
                    float(semantic_values[source])
                )
                if parameter_id in native and native[parameter_id] != value:
                    raise ValueError(f"多个变量对同一 Fluent 参数产生冲突：{parameter_id}")
                native[parameter_id] = value
            else:
                if item.binding.classification == VariableClassification.GEOMETRY_UNSUPPORTED:
                    raise PermissionError("GEOMETRY_UNSUPPORTED 变量不得进入 Runner")
        for mapping in mappings.values():
            mapped = apply_mapping(
                mapping,
                {source: semantic_values[source] for source in mapping.source_variables},
                mapping.context_values,
            )
            overlap = set(native) & set(mapped)
            if overlap and any(native[key] != mapped[key] for key in overlap):
                raise ValueError(f"多个映射对同一 Fluent 参数产生冲突：{sorted(overlap)}")
            native.update(mapped)
        return native

    async def evaluate_point(self, parameters, *, name, artifact_stem=None):
        native = self._native_values(parameters)
        result = await self.native_runner.evaluate_point(
            native, name=name, artifact_stem=artifact_stem
        )
        return {
            **result,
            "parameters": dict(parameters),
            "semantic_parameters": dict(parameters),
            "fluent_parameters": native,
            "fixed_condition_evidence": list(getattr(self, "_last_fixed_condition_evidence", [])),
        }

    def __getattr__(self, name):
        return getattr(self.native_runner, name)


async def run_resolved_optimization(
    experiment: ResolvedExperiment,
    output_dir: str | Path,
    registry: CapabilityRegistry,
    *,
    native_runner_factory=None,
) -> dict[str, Any]:
    approval = require_execution_approval(experiment, registry)
    from .history import atomic_json
    approval_path = Path(output_dir) / "execution_approval.json"
    if approval_path.exists():
        previous = ExecutionApproval.model_validate(json.loads(approval_path.read_text(encoding="utf-8")))
        if previous != approval:
            raise PermissionError("Campaign execution approval changed; use a new Campaign directory")
    elif (Path(output_dir) / "campaign.json").exists():
        raise PermissionError("Legacy Campaign has no full-experiment approval; use a new Campaign directory")
    else:
        atomic_json(approval_path, approval.model_dump(mode="json"))
    def factory(spec, path):
        return MappingRunnerAdapter(
            spec,
            path,
            native_spec=experiment.native_spec,
            resolved_task=experiment.resolved_task,
            registry=registry,
            native_runner_factory=native_runner_factory,
        )

    summary = await run_optimization(
        experiment.semantic_spec,
        output_dir,
        target_trials=experiment.resolved_task.task.termination.max_trials,
        runner_factory=factory,
    )
    summary["execution_mode"] = approval.mode
    summary["pre_simulation_applied"] = approval.mode == "FROZEN_PRIOR"
    summary["execution_approval_sha256"] = approval.payload_sha256
    campaign_path = Path(output_dir) / "campaign.json"
    if campaign_path.is_file():
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        summary["campaign_id"] = campaign.get("campaign_id")
    if summary.get("best_params"):
        proxy_values: dict[str, Any] = {}
        for mapping in experiment.resolved_task.mappings:
            forward = evaluate_named_expressions(
                mapping.forward_expression,
                {**mapping.context_values, **summary["best_params"]},
            )
            proxy_values.update(
                {name: forward[name] for name in mapping.proxy_variables if name in forward}
            )
        summary["best_result"] = {
            "trial_id": (
                f"trial-{int(summary['best_trial_number']):04d}"
                if summary.get("best_trial_number") is not None
                else None
            ),
            "parameters": summary["best_params"],
            "proxy_values": proxy_values,
            "objective_value": summary.get("best_value"),
        }
    else:
        summary["best_result"] = None
    if summary.get("best_params") and summary.get("best_value") is not None:
        summary["verification"] = await run_independent_verification(
            experiment.semantic_spec,
            Path(output_dir),
            summary["best_params"],
            float(summary["best_value"]),
            relative_tolerance=experiment.resolved_task.task.solver_requirements.verification_relative_tolerance,
            absolute_tolerance=experiment.resolved_task.task.solver_requirements.verification_absolute_tolerance,
            tolerance_mode=experiment.resolved_task.task.solver_requirements.verification_tolerance_mode,
            reference_scale=experiment.resolved_task.task.solver_requirements.verification_reference_scale,
            policy_basis=experiment.resolved_task.task.solver_requirements.verification_policy_basis,
            runner_factory=factory,
        )
    summary["best_status"] = ("BEST_VERIFIED" if summary.get("best_params") and verification_is_trusted(
        summary.get("verification") or {}, summary["best_params"], summary["best_value"])
                              else "BEST_OBSERVED" if summary.get("best_params") else "NO_TRUSTED_RESULT")
    summary["improvement_verified"] = False
    if summary.get("best_result"):
        summary["best_result"]["evidence_status"] = summary["best_status"]
        summary["best_result"]["improvement_verified"] = False
        summary["best_result"]["optimality_claim"] = "BEST_OF_EVALUATED_FEASIBLE_SAMPLES_ONLY"
    return summary
