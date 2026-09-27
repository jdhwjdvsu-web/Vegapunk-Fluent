"""Versioned, machine-readable contract for the safe Fluent Mapping DSL."""

from __future__ import annotations

from enum import StrEnum
from typing import Any


MAPPING_DSL_SCHEMA_VERSION = 1
MAPPING_VERSION = "1.0"


class MappingOperator(StrEnum):
    ADD = "add"
    SUBTRACT = "subtract"
    MULTIPLY = "multiply"
    DIVIDE = "divide"
    NEGATE = "negate"
    SIN_DEG = "sin_deg"
    COS_DEG = "cos_deg"
    TAN_DEG = "tan_deg"
    DEG_TO_RAD = "deg_to_rad"
    RAD_TO_DEG = "rad_to_deg"
    WRAP_ANGLE = "wrap_angle"
    CLAMP = "clamp"
    NORMALIZE_VECTOR = "normalize_vector"
    CONSTANT = "constant"
    REFERENCE = "reference"


ALLOWED_OPERATORS = frozenset(item.value for item in MappingOperator)

OPERATOR_SPECS = {
    MappingOperator.ADD.value: {"arity": [2], "input": "scalar", "output": "scalar"},
    MappingOperator.SUBTRACT.value: {"arity": [2], "input": "scalar", "output": "scalar"},
    MappingOperator.MULTIPLY.value: {"arity": [2], "input": "scalar", "output": "scalar"},
    MappingOperator.DIVIDE.value: {"arity": [2], "input": "scalar", "output": "scalar"},
    MappingOperator.NEGATE.value: {"arity": [1], "input": "scalar", "output": "scalar"},
    MappingOperator.SIN_DEG.value: {"arity": [1], "input": "angle_deg", "output": "scalar"},
    MappingOperator.COS_DEG.value: {"arity": [1], "input": "angle_deg", "output": "scalar"},
    MappingOperator.TAN_DEG.value: {"arity": [1], "input": "angle_deg", "output": "scalar"},
    MappingOperator.DEG_TO_RAD.value: {"arity": [1], "input": "angle_deg", "output": "angle_rad"},
    MappingOperator.RAD_TO_DEG.value: {"arity": [1], "input": "angle_rad", "output": "angle_deg"},
    MappingOperator.WRAP_ANGLE.value: {"arity": [1], "input": "angle_deg", "output": "angle_deg"},
    MappingOperator.CLAMP.value: {"arity": [1, 3], "input": "scalar", "output": "scalar"},
    MappingOperator.NORMALIZE_VECTOR.value: {"arity": [1, 2, 3], "input": "scalar_or_vector", "output": "vector"},
    MappingOperator.CONSTANT.value: {"arity": [1], "input": "scalar", "output": "scalar"},
    MappingOperator.REFERENCE.value: {"arity": [1], "input": "scalar", "output": "scalar"},
}


def mapping_dsl_contract() -> dict[str, Any]:
    """Return the exact contract supplied to designer and reviewer nodes.

    The examples describe syntax only. Physical formulas remain the agent's
    responsibility and must be derived from the task and scanned case catalog.
    """

    return {
        "schema_version": MAPPING_DSL_SCHEMA_VERSION,
        "mapping_version": MAPPING_VERSION,
        "operators": OPERATOR_SPECS,
        "expression_forms": {
            "reference": {"ref": "declared_variable_name"},
            "constant": {"value": 1.0},
            "operation": {
                "op": "multiply",
                "args": [{"ref": "declared_variable_name"}, {"value": 1.0}],
            },
        },
        "expression_rules": [
            "每个表达式节点必须且只能使用 ref、value、op 三种形式之一。",
            "ref 只能引用已声明变量或同一表达式字典中生成的变量。",
            "角度三角函数 sin_deg/cos_deg/tan_deg 的输入单位为 deg。",
            "forward_expression 必须生成全部 proxy_variables 和 selected_fluent_parameter_ids。",
            "reverse_expression 必须恢复全部 source_variables。",
            "禁止 Python、代码、Fluent Setting Path、文件路径、MCP 或网络命令。",
        ],
        "namespace_rules": [
            "source_variables 是语义变量 ID，不能冒充 Fluent 参数 ID。",
            "proxy_variables、source_variables、context_variables、selected_fluent_parameter_ids 必须分属互不冲突的命名空间。",
            "selected_fluent_parameter_ids 只能来自当前 capability registry。",
        ],
        "fixed_condition_contract": {
            "rule": "用户要求保持的数值条件必须进入 fixed_conditions，不得只写入 assumptions。",
            "verified_forms": ["case_parameter", "run_readback"],
            "binding": "case_parameter 使用 parameter_id；run_readback 可使用 parameter_id、case_evidence 中的现有 report_id，或 solver_readbacks 中的 evidence_key。",
            "documentation_only": "documented_assumption 只记录适用边界，不能冒充已写入 Fluent。",
        },
        "required_mapping_fields": [
            "source_variables",
            "context_variables",
            "proxy_variables",
            "selected_capability_id",
            "selected_fluent_parameter_ids",
            "forward_expression",
            "reverse_expression",
            "source_units",
            "output_units",
            "coordinate_convention",
            "angle_convention",
            "validity_conditions",
            "verification_route",
            "confidence",
        ],
        "verification_routes": ["MANUAL_GEOMETRY", "FUTURE_WORKBENCH_MCP"],
        "node_responsibilities": {
            "mapping_designer_agent": "根据任务与目录独立设计物理映射；不得复制不存在的公式或 ID。",
            "mapping_reviewer_agent": "只修复结构化校验指出的问题；保留可证明正确的物理含义并重新输出完整 MappingSpec。",
            "mapping_spec_validator": "以服务端目录、单位、命名空间、往返采样和安全白名单为最终裁决。",
        },
    }
