"""Safe evaluator and deterministic validator for LLM-authored Mapping DSL."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from itertools import product
from typing import Any

from .capability_registry import CapabilityRegistry
from .mapping_contract import ALLOWED_OPERATORS, OPERATOR_SPECS
from .mapping_schema import MappingExpression, MappingSpec, VerificationRoute
from .task_schema import SimulationTaskObject


FORBIDDEN_TEXT = (
    "python",
    "setup.",
    "solver.settings",
    "solution.",
    "results.",
    "mcp command",
    "tools/call",
    "execute_code",
    "http://",
    "https://",
    "approved=true",
    '"approved": true',
)
FORBIDDEN_CALL = re.compile(r"\b(?:eval|exec)\s*\(", re.IGNORECASE)
FORBIDDEN_CODE_OR_PATH = re.compile(
    r"(?:\b(?:lambda|import)\b|__|[a-z]:\\|\\\\|\.\./|/(?:tmp|home|mnt|etc)/)",
    re.IGNORECASE,
)


class MappingEvaluationError(ValueError):
    pass


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise MappingEvaluationError(f"{label} 不能是 bool")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MappingEvaluationError(f"{label} 必须是数值") from exc
    if not math.isfinite(result):
        raise MappingEvaluationError(f"{label} 必须是有限数")
    return result


def evaluate_expression(expression: MappingExpression, values: Mapping[str, Any]) -> float | list[float]:
    if expression.ref is not None:
        if expression.ref not in values:
            raise MappingEvaluationError(f"映射引用不存在：{expression.ref}")
        raw = values[expression.ref]
        if isinstance(raw, (list, tuple)):
            vector = [_number(item, expression.ref) for item in raw]
            return vector
        return _number(raw, expression.ref)
    if expression.value is not None:
        return _number(expression.value, "constant")
    op = str(expression.op)
    if op not in ALLOWED_OPERATORS:
        raise MappingEvaluationError(f"映射运算符不在白名单：{op}")
    args = [evaluate_expression(item, values) for item in expression.args]

    def scalar(index: int) -> float:
        if index >= len(args) or isinstance(args[index], list):
            raise MappingEvaluationError(f"{op} 参数数量或类型错误")
        return float(args[index])

    if op == "constant":
        if len(args) != 1:
            raise MappingEvaluationError("constant 需要一个参数")
        return scalar(0)
    if op == "reference":
        if len(args) != 1:
            raise MappingEvaluationError("reference 需要一个参数")
        return scalar(0)
    if op in {"add", "subtract", "multiply", "divide"}:
        if len(args) != 2:
            raise MappingEvaluationError(f"{op} 需要两个参数")
        left, right = scalar(0), scalar(1)
        if op == "add":
            result = left + right
        elif op == "subtract":
            result = left - right
        elif op == "multiply":
            result = left * right
        else:
            if abs(right) <= 1e-15:
                raise MappingEvaluationError("divide 不允许除以零")
            result = left / right
    elif op == "negate":
        if len(args) != 1:
            raise MappingEvaluationError("negate 需要一个参数")
        result = -scalar(0)
    elif op in {"sin_deg", "cos_deg", "tan_deg", "deg_to_rad", "rad_to_deg", "wrap_angle"}:
        if len(args) != 1:
            raise MappingEvaluationError(f"{op} 需要一个参数")
        value = scalar(0)
        result = {
            "sin_deg": lambda: math.sin(math.radians(value)),
            "cos_deg": lambda: math.cos(math.radians(value)),
            "tan_deg": lambda: math.tan(math.radians(value)),
            "deg_to_rad": lambda: math.radians(value),
            "rad_to_deg": lambda: math.degrees(value),
            "wrap_angle": lambda: ((value + 180.0) % 360.0) - 180.0,
        }[op]()
    elif op == "clamp":
        if len(args) == 3:
            result = min(max(scalar(0), scalar(1)), scalar(2))
        elif len(args) == 1 and expression.minimum is not None and expression.maximum is not None:
            result = min(max(scalar(0), expression.minimum), expression.maximum)
        else:
            raise MappingEvaluationError("clamp 需要 value/min/max")
    elif op == "normalize_vector":
        vector: list[float]
        if len(args) == 1 and isinstance(args[0], list):
            vector = [float(item) for item in args[0]]
        else:
            vector = [scalar(index) for index in range(len(args))]
        if len(vector) not in {2, 3}:
            raise MappingEvaluationError("normalize_vector 只支持二维或三维向量")
        norm = math.sqrt(sum(item * item for item in vector))
        if not math.isfinite(norm) or norm <= 1e-12:
            raise MappingEvaluationError("方向向量无法归一化")
        normalized = [item / norm for item in vector]
        if not all(math.isfinite(item) for item in normalized):
            raise MappingEvaluationError("归一化结果不是有限数")
        return normalized
    else:  # pragma: no cover - kept exhaustive for future operators
        raise MappingEvaluationError(f"未实现运算符：{op}")
    return _number(result, op)


def evaluate_named_expressions(expressions: Mapping[str, MappingExpression], values: Mapping[str, Any]) -> dict[str, Any]:
    environment = dict(values)
    pending = dict(expressions)
    output: dict[str, Any] = {}
    for _ in range(len(pending) + 1):
        progressed = False
        for name, expression in list(pending.items()):
            try:
                value = evaluate_expression(expression, environment)
            except MappingEvaluationError as exc:
                if "引用不存在" in str(exc):
                    continue
                raise
            environment[name] = value
            output[name] = value
            pending.pop(name)
            progressed = True
        if not pending:
            return output
        if not progressed:
            break
    raise MappingEvaluationError(f"映射存在未知或循环引用：{', '.join(sorted(pending))}")


def apply_mapping(spec: MappingSpec, source_values: Mapping[str, Any], context_values: Mapping[str, Any]) -> dict[str, float]:
    output = evaluate_named_expressions(spec.forward_expression, {**context_values, **source_values})
    result = {}
    for parameter_id in spec.selected_fluent_parameter_ids:
        if parameter_id not in output:
            raise MappingEvaluationError(f"正向映射没有生成 Fluent 参数：{parameter_id}")
        result[parameter_id] = _number(output[parameter_id], parameter_id)
    return result


def reverse_mapping(spec: MappingSpec, proxy_values: Mapping[str, Any], context_values: Mapping[str, Any]) -> dict[str, float]:
    output = evaluate_named_expressions(spec.reverse_expression, {**context_values, **proxy_values})
    return {name: _number(output[name], name) for name in spec.source_variables if name in output}


def _collect_references(expression: MappingExpression) -> set[str]:
    refs = {expression.ref} if expression.ref else set()
    for item in expression.args:
        refs.update(_collect_references(item))
    return refs


def _collect_operators(expression: MappingExpression) -> set[str]:
    operators = {str(expression.op)} if expression.op else set()
    for item in expression.args:
        operators.update(_collect_operators(item))
    return operators


def _validate_expression_shape(expression: MappingExpression, path: str) -> list[str]:
    errors: list[str] = []
    if expression.op:
        operator = str(expression.op)
        spec = OPERATOR_SPECS.get(operator)
        if spec and len(expression.args) not in spec["arity"]:
            errors.append(
                f"{path} 运算符 {operator} 参数数量错误：期望 {spec['arity']}，实际 {len(expression.args)}"
            )
        if operator == "clamp" and len(expression.args) == 1:
            if expression.minimum is None or expression.maximum is None:
                errors.append(f"{path} clamp 单参数形式必须提供 minimum 和 maximum")
            elif expression.minimum >= expression.maximum:
                errors.append(f"{path} clamp minimum 必须小于 maximum")
    for index, item in enumerate(expression.args):
        errors.extend(_validate_expression_shape(item, f"{path}.args[{index}]"))
    return errors


def _dependency_cycles(expressions: Mapping[str, MappingExpression]) -> list[str]:
    names = set(expressions)
    dependencies = {
        name: _collect_references(expression) & names
        for name, expression in expressions.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()
    cycles: list[str] = []

    def visit(name: str, path: list[str]) -> None:
        if name in visiting:
            start = path.index(name) if name in path else 0
            cycles.append(" -> ".join([*path[start:], name]))
            return
        if name in visited:
            return
        visiting.add(name)
        for dependency in sorted(dependencies[name]):
            visit(dependency, [*path, name])
        visiting.remove(name)
        visited.add(name)

    for name in sorted(names):
        visit(name, [])
    return list(dict.fromkeys(cycles))


def validate_mapping_spec(
    spec: MappingSpec,
    task: SimulationTaskObject,
    registry: CapabilityRegistry,
) -> list[str]:
    errors: list[str] = []
    if spec.mapping_id == "inlet_direction_angle_xy.v1":
        from .pre_simulation.variables import build_registered_mapping_spec

        try:
            canonical = build_registered_mapping_spec(
                spec.mapping_id, spec.selected_fluent_parameter_ids, registry.parameters,
            )
        except ValueError as exc:
            return [f"Registered mapping is not available: {exc}"]
        if spec.model_dump(mode="json") != canonical.model_dump(mode="json"):
            return ["Registered mapping formula or metadata differs from server-owned canonical version"]
    serialized = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False).lower()
    if (
        any(token in serialized for token in FORBIDDEN_TEXT)
        or FORBIDDEN_CALL.search(serialized)
        or FORBIDDEN_CODE_OR_PATH.search(serialized)
    ):
        errors.append("MappingSpec 包含 Python、Fluent Path、文件路径、MCP 或网络内容")
    capability = registry.get(spec.selected_capability_id)
    if capability is None:
        return [*errors, f"capability_id 不属于服务端 Registry：{spec.selected_capability_id}"]
    errors.extend(registry.validate_parameter_ids(spec.selected_capability_id, spec.selected_fluent_parameter_ids))
    if spec.verification_route == VerificationRoute.NONE and spec.mapping_id != "inlet_direction_angle_xy.v1":
        errors.append("MAPPED_PROXY 必须声明几何验证路线")
    if spec.automatic_geometry_execution:
        errors.append("V3 禁止自动执行几何修改")
    if not spec.coordinate_convention.strip():
        errors.append("缺少坐标系约定")
    if not spec.angle_convention.strip():
        errors.append("缺少角度零点和正方向约定")

    variables = {item.id: item for item in task.variables}
    for source in spec.source_variables:
        if source not in variables:
            errors.append(f"source variable 不属于 Task Object：{source}")
    namespaces = {
        "source_variables": set(spec.source_variables),
        "context_variables": set(spec.context_variables),
        "proxy_variables": set(spec.proxy_variables),
        "selected_fluent_parameter_ids": set(spec.selected_fluent_parameter_ids),
    }
    names = list(namespaces)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1:]:
            overlap = namespaces[left_name] & namespaces[right_name]
            if overlap:
                errors.append(
                    f"映射命名空间冲突：{left_name} 与 {right_name} 重复使用 {', '.join(sorted(overlap))}"
                )
    declared = set().union(*namespaces.values())
    generated = set(spec.forward_expression)
    overwritten_inputs = generated & (namespaces["source_variables"] | namespaces["context_variables"])
    if overwritten_inputs:
        errors.append(f"正向映射不得覆写输入变量：{', '.join(sorted(overwritten_inputs))}")
    for name, expression in spec.forward_expression.items():
        errors.extend(_validate_expression_shape(expression, f"forward_expression.{name}"))
        for operator in sorted(_collect_operators(expression) - ALLOWED_OPERATORS):
            errors.append(f"运算符不在白名单：{operator}")
        unknown = _collect_references(expression) - declared - generated
        errors.extend(f"正向映射引用未声明变量：{item}" for item in sorted(unknown))
    reverse_generated = set(spec.reverse_expression)
    for expression in spec.reverse_expression.values():
        errors.extend(_validate_expression_shape(expression, "reverse_expression"))
        for operator in sorted(_collect_operators(expression) - ALLOWED_OPERATORS):
            errors.append(f"反向映射运算符不在白名单：{operator}")
        unknown = _collect_references(expression) - declared - reverse_generated
        errors.extend(f"反向映射引用未声明变量：{item}" for item in sorted(unknown))
    errors.extend(f"正向映射存在循环引用：{cycle}" for cycle in _dependency_cycles(spec.forward_expression))
    errors.extend(f"反向映射存在循环引用：{cycle}" for cycle in _dependency_cycles(spec.reverse_expression))
    if not set(spec.proxy_variables) <= generated:
        errors.append("正向映射必须生成全部代理变量")
    if not set(spec.selected_fluent_parameter_ids) <= generated:
        errors.append("正向映射必须生成全部 Fluent 参数 ID")
    if not set(spec.source_variables) <= set(spec.reverse_expression):
        errors.append("反向映射必须恢复全部源变量")
    required_units = set(spec.source_variables) | set(spec.context_variables)
    if not required_units <= {key for key, value in spec.source_units.items() if value.strip()}:
        errors.append("源变量或上下文变量单位不完整")
    output_names = set(spec.proxy_variables) | set(spec.selected_fluent_parameter_ids) | set(spec.source_variables)
    if not output_names <= {key for key, value in spec.output_units.items() if value.strip()}:
        errors.append("代理、Fluent 输出或反向几何单位不完整")
    for source in spec.source_variables:
        intent = variables.get(source)
        if intent and intent.unit and spec.source_units.get(source) != intent.unit:
            errors.append(f"源变量 {source} 单位与 Task Object 不一致")
        if spec.output_units.get(source) != spec.source_units.get(source):
            errors.append(f"反向几何变量 {source} 单位与源变量不一致")
    for parameter_id in spec.selected_fluent_parameter_ids:
        parameter = registry.parameters.get(parameter_id)
        if parameter and spec.output_units.get(parameter_id) != parameter.unit:
            errors.append(f"Fluent 参数 {parameter_id} 输出单位与扫描目录不一致")
    if capability.source_unit and any(
        spec.source_units.get(source) != capability.source_unit
        for source in spec.source_variables
    ):
        errors.append("源变量单位与所选 Capability 不兼容")

    if errors:
        return list(dict.fromkeys(errors))

    minima: dict[str, float] = {}
    midpoints: dict[str, float] = {}
    maxima: dict[str, float] = {}
    for source in spec.source_variables:
        intent = variables[source]
        if intent.minimum is None or intent.maximum is None:
            errors.append(f"源变量 {source} 缺少范围")
            continue
        minima[source] = intent.minimum
        midpoints[source] = (intent.minimum + intent.maximum) / 2
        maxima[source] = intent.maximum
    ordered_sources = list(spec.source_variables)
    sample_values = [
        dict(zip(ordered_sources, values, strict=True))
        for values in product(*[
            (minima[name], midpoints[name], maxima[name])
            for name in ordered_sources
        ])
    ]
    for fraction in (0.2113248654, 0.7330508076):
        sample_values.append({
            name: minima[name] + (maxima[name] - minima[name]) * fraction
            for name in ordered_sources
        })
    context = dict(spec.context_values)
    for context_name in spec.context_variables:
        if context_name in context:
            continue
        match = next((item for item in task.variables if item.id == context_name), None)
        if match and match.initial_value is not None:
            context[context_name] = match.initial_value
        else:
            # Context may be a declared reference condition in an assumption. The
            # parser should materialize it as a VariableIntent for execution.
            errors.append(f"上下文变量 {context_name} 缺少有限初始值")
    if errors:
        return list(dict.fromkeys(errors))

    parameters = registry.parameters
    for sample in sample_values:
        try:
            forward = evaluate_named_expressions(spec.forward_expression, {**context, **sample})
            for parameter_id in spec.selected_fluent_parameter_ids:
                native = _number(forward[parameter_id], parameter_id)
                parameter = parameters[parameter_id]
                display = native / parameter.scale_to_native
                parameter.validate_display_value(display, parameter_id)
            vector_keys = [
                parameter_id for parameter_id in spec.selected_fluent_parameter_ids
                if parameters[parameter_id].rule_id in {"flow_direction_x", "flow_direction_y", "flow_direction_z"}
            ]
            vector_groups = {}
            for key in vector_keys:
                parameter = parameters[key]
                group = vector_groups.setdefault((parameter.collection_path, parameter.object_name), {})
                group[parameter.rule_id] = key
            for zone, group in vector_groups.items():
                if set(group) != {"flow_direction_x", "flow_direction_y", "flow_direction_z"}:
                    errors.append(f"入口 {zone[1]} 缺少完整XYZ方向分量")
                    continue
                norm = math.sqrt(sum(float(forward[key]) ** 2 for key in group.values()))
                if abs(norm - 1.0) > 1e-6:
                    errors.append(f"方向分量未归一化：{zone[1]}")
                if abs(float(forward[group['flow_direction_z']])) > 1e-9:
                    errors.append(f"XY角度代理的Z方向必须为零：{zone[1]}")
            proxy = {name: forward[name] for name in spec.proxy_variables}
            reversed_values = reverse_mapping(spec, proxy, context)
            for source, expected in sample.items():
                actual = reversed_values.get(source)
                difference = abs((((actual - expected) + 180) % 360) - 180) if actual is not None else math.inf
                if difference > 1e-6:
                    errors.append(f"正向和反向映射不一致：{source}")
        except (KeyError, ValueError, MappingEvaluationError) as exc:
            errors.append(f"映射采样验证失败：{exc}")
    return list(dict.fromkeys(errors))
