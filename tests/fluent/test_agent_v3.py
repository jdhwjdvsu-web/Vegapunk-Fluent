import asyncio
import copy
import json
import math
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from vegapunk.fluent.agent_graph import FluentAgentGraph, _without_duplicate_solver_gate_constraints
from vegapunk.fluent.capability_registry import CapabilityRegistry
from vegapunk.fluent.geometry_handoff import (
    GeometryCandidate,
    GeometryVerificationRequest,
    SuggestionOnlyGeometryHandoff,
)
from vegapunk.fluent.mapping_runtime import (
    MappingEvaluationError,
    apply_mapping,
    evaluate_expression,
    reverse_mapping,
    validate_mapping_spec,
)
from vegapunk.fluent.mapping_contract import ALLOWED_OPERATORS, mapping_dsl_contract
from vegapunk.fluent.mapping_schema import MappingExpression, MappingSpec
from vegapunk.fluent.experiment_orchestrator import compile_resolved_experiment
from vegapunk.fluent.model_profile import UIParameter
from vegapunk.fluent.planning_attempts import PlanningAttemptStore
from vegapunk.fluent.task_schema import SimulationTaskObject
from vegapunk.fluent.planning import (
    approve_resolved_task,
    assert_resolved_approval,
)
from vegapunk.fluent.metric_catalog import resolve_metrics
from vegapunk.fluent.web import create_app

from .test_planning import SPEC, model_signature, profile


def wait_for_attempt(client, attempt_id, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/agent/attempts/{attempt_id}")
        assert response.status_code == 200
        record = response.json()
        if record["status"] not in {"queued", "running", "cancelling"}:
            return record
        time.sleep(0.02)
    raise AssertionError(f"attempt {attempt_id} did not finish")


def direction_parameter(key, rule_id):
    return UIParameter(
        key=key,
        label=key,
        category="边界条件",
        zone="fan-inlet",
        collection_path="setup.boundary_conditions.velocity_inlet",
        object_name="fan-inlet",
        property_path=rule_id,
        unit="dimensionless",
        hard_min=-1,
        hard_max=1,
        recommended_min=-1,
        recommended_max=1,
        default_value=0,
        step=0,
        description="test direction component",
        rule_id=rule_id,
    )


def fan_registry():
    parameters = {
        item.key: item for item in (
            direction_parameter("flow-x-id", "flow_direction_x"),
            direction_parameter("flow-y-id", "flow_direction_y"),
            direction_parameter("flow-z-id", "flow_direction_z"),
        )
    }
    metrics = {item.key: item for item in resolve_metrics(model_signature())}
    return CapabilityRegistry(profile(), parameters, metrics)


def fan_registry_with_speed(default_value=30.0):
    base = fan_registry()
    parameters = dict(base.parameters)
    parameters["speed-id"] = UIParameter(
        key="speed-id",
        label="入口速度",
        category="边界条件",
        zone="fan-inlet",
        collection_path="setup.boundary_conditions.velocity_inlet",
        object_name="fan-inlet",
        property_path="velocity_magnitude",
        unit="m/s",
        hard_min=0.1,
        hard_max=100.0,
        recommended_min=1.0,
        recommended_max=60.0,
        default_value=default_value,
        step=0.1,
        description="fixed inlet speed",
        rule_id="velocity_magnitude",
    )
    return CapabilityRegistry(profile(), parameters, dict(base.metrics))


def test_registry_exposes_safe_paired_data_and_existing_reports(tmp_path):
    import hashlib

    case_path = tmp_path / "thermal.cas.h5"
    data_path = tmp_path / "thermal.dat.h5"
    case_path.write_bytes(b"case")
    data_path.write_bytes(b"paired data")
    current = profile()
    current["case_file"] = str(case_path)
    current["signature"]["solver_readbacks"] = {
        "pseudo_time_formulation": "local-time-step",
        "pseudo_time_courant_number": 1.0,
    }
    current["signature"]["reports"] = {
        "flux": {
            "diag_heat_net": {
                "name": "diag_heat_net",
                "report_type": "flux-heattransfer",
                "boundaries": ["inlet", "outlet"],
            }
        }
    }
    base = fan_registry()
    context = CapabilityRegistry(current, base.parameters, base.metrics).llm_context()
    evidence = context["case_evidence"]
    assert evidence["paired_data_artifact"] == {
        "artifact_reference": "paired-case-data",
        "sha256": hashlib.sha256(b"paired data").hexdigest(),
        "same_stem_as_selected_case": True,
    }
    assert evidence["existing_reports"][0]["name"] == "diag_heat_net"
    assert evidence["solver_readbacks"] == {
        "pseudo_time_formulation": "local-time-step",
        "pseudo_time_courant_number": 1.0,
    }
    assert str(case_path) not in json.dumps(evidence)


def test_fixed_condition_can_use_existing_report_readback():
    task = fan_task()
    task["fixed_conditions"] = [{
        "id": "heat_source_power",
        "name": "热源功率",
        "value": 100,
        "unit": "W",
        "source": "user",
        "report_id": "diag_heat_net",
        "verification": "run_readback",
    }]
    parsed = SimulationTaskObject.model_validate(task)
    assert parsed.fixed_conditions[0].report_id == "diag_heat_net"


def test_fixed_condition_can_use_system_case_readback():
    task = fan_task()
    task["fixed_conditions"] = [{
        "id": "pseudo_time_courant",
        "name": "伪瞬态 Courant 数",
        "value": 1,
        "unit": "dimensionless",
        "source": "case_scan",
        "evidence_key": "pseudo_time_courant_number",
        "verification": "run_readback",
    }]
    parsed = SimulationTaskObject.model_validate(task)
    assert parsed.fixed_conditions[0].evidence_key == "pseudo_time_courant_number"


def test_duplicate_solver_gate_constraints_are_not_compiled_as_report_constraints():
    task = fan_task()
    task["constraints"] = [
        {"semantic_metric": "连续性残差", "metric_key": None, "operator": "<=", "value": 1e-3, "unit": "dimensionless"},
        {"semantic_metric": "相对质量不平衡", "metric_key": None, "operator": "<=", "value": 1e-3, "unit": "relative"},
        {"semantic_metric": "最后201步最高温度波动范围", "metric_key": None, "operator": "<=", "value": .1, "unit": "K"},
        {"semantic_metric": "实际工程约束", "metric_key": None, "operator": "<=", "value": 3, "unit": "Pa"},
    ]
    task["solver_requirements"] = {
        "residual_thresholds": {"continuity": 1e-3},
        "mass_balance_required": True,
        "mass_balance_relative_tolerance": 1e-3,
        "thermal_guard": {
            "data_file": "paired-case-data",
            "data_sha256": "a" * 64,
            "heat_source_W": 100,
            "heat_net_report": "heat",
            "mass_in_report": "mass-in",
            "mass_out_report": "mass-out",
            "temperature_reports": ["tmax", "tavg"],
            "window": 201,
            "temperature_span_K": .1,
            "heat_relative_tolerance": .01,
            "mass_relative_tolerance": .001,
            "pseudo_courant": 1,
        },
    }
    normalized = _without_duplicate_solver_gate_constraints(SimulationTaskObject.model_validate(task))
    assert [item.semantic_metric for item in normalized.constraints] == ["实际工程约束"]


def fan_task(*, complete=True):
    metric_key = next(
        item.key for item in resolve_metrics(model_signature())
        if "product-velocity-avg" in item.key
    )
    return {
        "schema_version": 3,
        "research_question": "在 wind_direction=30 deg 下优化风扇安装角",
        "variables": [{
            "id": "fan_angle",
            "name": "风扇安装角",
            "semantic_key": "fan_installation_angle",
            "minimum": -30 if complete else None,
            "maximum": 60 if complete else None,
            "initial_value": 0,
            "unit": "deg" if complete else None,
            "description": "固定几何代理变量",
        }],
        "objective": {
            "semantic_metric": "outlet_velocity",
            "metric_key": metric_key,
            "direction": "maximize",
            "aggregation": "area_average",
            "location": "outlet",
            "unit": "m/s",
        },
        "constraints": [],
        "solver_requirements": {"iterations": 100},
        "termination": {
            "max_trials": 12,
            "max_wall_time_seconds": 1800,
            "no_improvement_trials": 5,
            "target_objective": None,
            "maximum_failed_trials": 4,
        },
        "assumptions": ["XY 笛卡尔坐标系，+X 为 0 deg，逆时针为正", "wind_direction=30 deg"],
        "questions": [],
        "needs_information": not complete,
    }


def ref(name):
    return {"ref": name}


def op(name, *args):
    return {"op": name, "args": list(args)}


def fan_mapping():
    relative = op("subtract", ref("wind_direction"), ref("fan_angle"))
    return {
        "mapping_id": "fan-angle-proxy",
        "schema_version": 1,
        "mapping_version": "1.0",
        "source_variables": ["fan_angle"],
        "context_variables": ["wind_direction"],
        "proxy_variables": ["relative_incidence_angle"],
        "selected_capability_id": "mapped.fan_incidence_2d.v1",
        "selected_fluent_parameter_ids": ["flow-x-id", "flow-y-id", "flow-z-id"],
        "forward_expression": {
            "relative_incidence_angle": relative,
            "flow-x-id": op("cos_deg", ref("relative_incidence_angle")),
            "flow-y-id": op("sin_deg", ref("relative_incidence_angle")),
            "flow-z-id": {"value": 0},
        },
        "reverse_expression": {
            "fan_angle": op("subtract", ref("wind_direction"), ref("relative_incidence_angle")),
        },
        "source_units": {"fan_angle": "deg", "wind_direction": "deg"},
        "context_values": {"wind_direction": 30},
        "output_units": {
            "relative_incidence_angle": "deg",
            "flow-x-id": "dimensionless",
            "flow-y-id": "dimensionless",
            "flow-z-id": "dimensionless",
            "fan_angle": "deg",
        },
        "coordinate_convention": "XY Cartesian; +X is zero; Z is zero",
        "angle_convention": "degrees; counter-clockwise positive; wrap [-180, 180)",
        "assumptions": ["fixed geometry proxy"],
        "validity_conditions": ["steady uniform inlet direction"],
        "verification_route": "FUTURE_WORKBENCH_MCP",
        "confidence": 0.82,
        "automatic_geometry_execution": False,
    }


def direct_task():
    data = fan_task()
    data["research_question"] = "优化入口速度"
    data["variables"] = [{
        "id": "velocity",
        "name": "入口速度",
        "semantic_key": "velocity",
        "minimum": 0.5,
        "maximum": 1.5,
        "initial_value": 1.0,
        "unit": "m/s",
        "description": "入口速度",
    }]
    data["assumptions"] = []
    return data


def test_task_object_returns_questions_for_missing_range_and_unit():
    task = SimulationTaskObject.model_validate(fan_task(complete=False))
    gaps = task.information_gaps()
    assert any("最小值和最大值" in item for item in gaps)
    assert any("单位" in item for item in gaps)


@pytest.mark.parametrize(
    ("field", "value"),
    [("minimum", True), ("maximum", float("inf"))],
)
def test_task_object_rejects_bool_and_nonfinite_ranges(field, value):
    raw = fan_task()
    raw["variables"][0][field] = value
    with pytest.raises(ValueError):
        SimulationTaskObject.model_validate(raw)


def test_multiturn_completes_an_initially_partial_task(tmp_path):
    async def scenario():
        incomplete = fan_task(complete=False)
        incomplete["research_question"] = "优化风扇安装角"
        fake = FakeRuntime(incomplete, mapping=fan_mapping())
        graph, first = await run_graph(tmp_path, fake, fan_registry())
        assert first["status"] == "needs_information"
        original = first["research_question"]
        fake.task = fan_task()
        second = await graph.message(
            "范围 -30 到 60 deg，wind_direction=30 deg，XY 坐标逆时针为正",
            task_id="task-1", conversation_revision=0, model_id="model-1",
            case_sha256="a" * 64, model_signature_sha256="b" * 64,
        )
        assert second["status"] == "awaiting_approval"
        assert second["research_question"] == original
        assert len(second["messages"]) >= 2
        await graph.close()
    asyncio.run(scenario())


def test_fan_mapping_forward_reverse_normalization_and_boundaries():
    task = SimulationTaskObject.model_validate(fan_task())
    mapping = MappingSpec.model_validate(fan_mapping())
    assert validate_mapping_spec(mapping, task, fan_registry()) == []
    native = apply_mapping(mapping, {"fan_angle": 10}, {"wind_direction": 30})
    assert native["flow-x-id"] == pytest.approx(math.cos(math.radians(20)))
    assert native["flow-y-id"] == pytest.approx(math.sin(math.radians(20)))
    assert math.sqrt(sum(value * value for value in native.values())) == pytest.approx(1)
    geometry = reverse_mapping(mapping, {"relative_incidence_angle": 20}, {"wind_direction": 30})
    assert geometry["fan_angle"] == pytest.approx(10)
    for angle in (-30, 15, 60):
        values = apply_mapping(mapping, {"fan_angle": angle}, {"wind_direction": 30})
        assert all(math.isfinite(value) for value in values.values())


def test_mapping_runtime_rejects_nonfinite_unknown_operator_and_zero_vector():
    with pytest.raises(ValueError):
        MappingExpression.model_validate({"value": float("inf")})
    with pytest.raises(ValueError, match="Input should be"):
        MappingExpression.model_validate({"op": "python", "args": [{"value": 1}]})
    with pytest.raises(MappingEvaluationError, match="无法归一化"):
        evaluate_expression(MappingExpression.model_validate({"op": "normalize_vector", "args": [{"value": 0}, {"value": 0}]}), {})


def test_mapping_contract_is_versioned_and_matches_expression_enum():
    contract = mapping_dsl_contract()
    assert contract["schema_version"] == 1
    assert set(contract["operators"]) == set(ALLOWED_OPERATORS)
    assert contract["operators"]["sin_deg"]["input"] == "angle_deg"
    assert contract["expression_forms"]["reference"] == {"ref": "declared_variable_name"}
    assert "mapping_spec_validator" in contract["node_responsibilities"]


def test_mapping_validator_rejects_catalog_escape_and_code_or_fluent_path():
    raw = fan_mapping()
    raw["selected_fluent_parameter_ids"] = ["solver.settings.bad"]
    raw["forward_expression"] = {"relative_incidence_angle": op("subtract", ref("wind_direction"), ref("fan_angle")), "solver.settings.bad": {"value": 1}}
    raw["output_units"]["solver.settings.bad"] = "dimensionless"
    errors = validate_mapping_spec(
        MappingSpec.model_validate(raw),
        SimulationTaskObject.model_validate(fan_task()),
        fan_registry(),
    )
    assert any("Fluent Path" in item for item in errors)
    assert any("当前扫描目录" in item for item in errors)


def test_mapping_validator_rejects_code_unit_mismatch_and_missing_conventions():
    raw = fan_mapping()
    raw["assumptions"].append("use EVAL ( x )")
    raw["source_units"]["fan_angle"] = "rad"
    raw["coordinate_convention"] = " "
    raw["angle_convention"] = " "
    errors = validate_mapping_spec(
        MappingSpec.model_validate(raw),
        SimulationTaskObject.model_validate(fan_task()),
        fan_registry(),
    )
    assert any("Python" in item for item in errors)
    assert any("单位" in item for item in errors)
    assert any("坐标系" in item for item in errors)
    assert any("角度零点" in item for item in errors)


def test_mapping_validator_rejects_cross_namespace_name_collision():
    raw = fan_mapping()
    raw["proxy_variables"] = ["flow-x-id"]
    errors = validate_mapping_spec(
        MappingSpec.model_validate(raw),
        SimulationTaskObject.model_validate(fan_task()),
        fan_registry(),
    )
    assert any("命名空间冲突" in item for item in errors)


def test_mapping_validator_rejects_bad_arity_cycles_and_expression_form_conflicts():
    with pytest.raises(ValueError, match="exactly one"):
        MappingExpression.model_validate({"ref": "a", "op": "negate", "args": [{"ref": "a"}]})

    bad_arity = fan_mapping()
    bad_arity["forward_expression"]["flow-x-id"] = op("add", ref("fan_angle"))
    errors = validate_mapping_spec(
        MappingSpec.model_validate(bad_arity),
        SimulationTaskObject.model_validate(fan_task()),
        fan_registry(),
    )
    assert any("参数数量错误" in item for item in errors)

    cyclic = fan_mapping()
    cyclic["forward_expression"]["relative_incidence_angle"] = ref("cycle_value")
    cyclic["forward_expression"]["cycle_value"] = ref("relative_incidence_angle")
    errors = validate_mapping_spec(
        MappingSpec.model_validate(cyclic),
        SimulationTaskObject.model_validate(fan_task()),
        fan_registry(),
    )
    assert any("循环引用" in item for item in errors)


def test_task_validator_separates_semantic_id_from_native_parameter_id(tmp_path):
    async def scenario():
        registry = fan_registry()
        raw = fan_task()
        raw["variables"][0]["id"] = "flow-x-id"
        graph = FluentAgentGraph(
            None,
            model_id=None,
            database_path=tmp_path / "unused.sqlite3",
            output_dir_provider=lambda: tmp_path,
        )
        graph.bind_registry(registry)
        result = await graph.task_schema_validator({"task_object": raw})
        variable_id = result["task_object"]["variables"][0]["id"]
        assert variable_id.startswith("semantic_")
        assert variable_id not in registry.parameters

    asyncio.run(scenario())


class FakeRuntime:
    def __init__(self, task, classification="MAPPED_PROXY", mapping=None, recommendation=None):
        self.task = task
        self.classification = classification
        self.mapping = mapping
        self.recommendation = recommendation
        self.calls = []

    async def generate_json(self, prompt, **_kwargs):
        data = json.loads(prompt)
        self.calls.append(data["stage"])
        if data["stage"] == "task_parser_agent":
            return {"message": "任务已解析", "task": self.task}
        if data["stage"] == "variable_classifier_agent":
            variable = self.task["variables"][0]
            if self.classification == "DIRECT":
                capability = next(item["capability_id"] for item in data["capability_registry"]["capabilities"] if item["capability_id"].startswith("direct."))
                parameter = data["capability_registry"]["capabilities"][0]["available_parameter_ids"][0]
                parameter_ids = [parameter]
            elif self.classification == "MAPPED_PROXY":
                capability = "mapped.fan_incidence_2d.v1"
                parameter_ids = ["flow-x-id", "flow-y-id", "flow-z-id"]
            else:
                capability = None
                parameter_ids = []
            return {"bindings": [{
                "variable_id": variable["id"],
                "classification": self.classification,
                "classification_reason": "fake LLM classification",
                "selected_capability_id": capability,
                "candidate_parameter_ids": parameter_ids,
                "candidate_metric_ids": [self.task["objective"]["metric_key"]] if self.task["objective"]["metric_key"] else [],
                "confidence": 0.9,
                "missing_information": [],
            }]}
        if data["stage"] in {"mapping_designer_agent", "mapping_reviewer_agent"}:
            return {"mappings": [self.mapping] if self.mapping else []}
        if data["stage"] == "result_interpreter_agent":
            return self.recommendation
        raise AssertionError(data["stage"])


class ReviewingRuntime(FakeRuntime):
    def __init__(self, task, *, always_invalid=False):
        super().__init__(task, mapping=fan_mapping())
        self.always_invalid = always_invalid

    async def generate_json(self, prompt, **kwargs):
        data = json.loads(prompt)
        if data["stage"] in {"mapping_designer_agent", "mapping_reviewer_agent"}:
            self.calls.append(data["stage"])
            if data["stage"] == "mapping_reviewer_agent" and not self.always_invalid:
                return {"mappings": [fan_mapping()]}
            invalid = fan_mapping()
            invalid["forward_expression"]["flow-y-id"] = {"value": 0}
            return {"mappings": [invalid]}
        return await super().generate_json(prompt, **kwargs)


class InvalidClassificationRuntime(FakeRuntime):
    async def generate_json(self, prompt, **kwargs):
        data = json.loads(prompt)
        if data["stage"] == "variable_classifier_agent":
            self.calls.append(data["stage"])
            return {"bindings": [{
                "variable_id": self.task["variables"][0]["id"],
                "classification": "DIRECT",
                "classification_reason": "invalid catalog escape",
                "selected_capability_id": "outside.registry.v1",
                "candidate_parameter_ids": ["outside-parameter"],
                "candidate_metric_ids": [],
                "confidence": 0.5,
                "missing_information": [],
            }]}
        return await super().generate_json(prompt, **kwargs)


async def run_graph(tmp_path, fake, registry, *, task_id="task-1", revision=0, checker=None):
    graph = FluentAgentGraph(
        fake,
        model_id="fake/model",
        database_path=tmp_path / "checkpoints.sqlite3",
        output_dir_provider=lambda: tmp_path,
        identity_checker=checker,
        timeout_seconds=2,
    )
    graph.bind_registry(registry)
    await graph.start()
    try:
        result = await graph.message(
            fake.task["research_question"],
            task_id=task_id,
            conversation_revision=revision,
            model_id="model-1",
            case_sha256="a" * 64,
            model_signature_sha256="b" * 64,
        )
        return graph, result
    except Exception:
        await graph.close()
        raise


def test_langgraph_direct_and_checkpoint_recovery(tmp_path):
    async def scenario():
        signature = model_signature()
        from vegapunk.fluent.parameter_rules import resolve_parameters
        parameters = {item.key: item for item in resolve_parameters(signature)}
        metrics = {item.key: item for item in resolve_metrics(signature)}
        registry = CapabilityRegistry(profile(), parameters, metrics)
        fake = FakeRuntime(direct_task(), classification="DIRECT")
        graph, first = await run_graph(tmp_path, fake, registry)
        assert first["resolved_task"]["variables"][0]["binding"]["classification"] == "DIRECT"
        assert first["status"] == "awaiting_approval"
        await graph.close()

        restored = FluentAgentGraph(fake, model_id="fake/model", database_path=tmp_path / "checkpoints.sqlite3", output_dir_provider=lambda: tmp_path)
        restored.bind_registry(registry)
        await restored.start()
        try:
            state = await restored.get_state("task-1")
            assert state["research_question"] == "优化入口速度"
            assert state["resolved_task"]["executable"] is True
        finally:
            await restored.close()
    asyncio.run(scenario())


def test_multiturn_keeps_research_question_and_new_task_isolated(tmp_path):
    async def scenario():
        fake = FakeRuntime(fan_task(), mapping=fan_mapping())
        graph, first = await run_graph(tmp_path, fake, fan_registry())
        original = first["research_question"]
        fake.task = {**fan_task(), "research_question": "好的"}
        second = await graph.message(
            "好的", task_id="task-1", conversation_revision=0, model_id="model-1",
            case_sha256="a" * 64, model_signature_sha256="b" * 64,
        )
        assert second["research_question"] == original
        assert second["plan_revision"] == 1
        fake.task = {**fan_task(), "research_question": "把目标改为新的攻角研究"}
        revised = await graph.message(
            "把目标改为新的攻角研究",
            task_id="task-1",
            conversation_revision=0,
            model_id="model-1",
            case_sha256="a" * 64,
            model_signature_sha256="b" * 64,
            input_category="plan_revision",
        )
        assert revised["research_question"] == "把目标改为新的攻角研究"
        assert revised["original_request"] == original
        assert revised["plan_revision"] == 2
        third = await graph.message(
            "新任务", task_id="task-2", conversation_revision=0, model_id="model-1",
            case_sha256="a" * 64, model_signature_sha256="b" * 64,
        )
        assert third["thread_id"] == "task-2"
        assert len(third["messages"]) < len(second["messages"])
        await graph.close()
    asyncio.run(scenario())


def test_geometry_unsupported_stops_before_approval(tmp_path):
    async def scenario():
        fake = FakeRuntime(fan_task(), classification="GEOMETRY_UNSUPPORTED")
        graph, result = await run_graph(tmp_path, fake, fan_registry())
        assert result["status"] == "geometry_unsupported"
        assert result["resolved_task"]["executable"] is False
        assert result["approval_status"] == "required"
        await graph.close()
    asyncio.run(scenario())


def test_missing_wind_direction_stops_with_structured_question(tmp_path):
    async def scenario():
        task = fan_task()
        task["research_question"] = "优化风扇安装角"
        task["assumptions"] = ["XY 笛卡尔坐标系，+X 为 0 deg，逆时针为正"]
        fake = FakeRuntime(task, mapping=fan_mapping())
        graph, result = await run_graph(tmp_path, fake, fan_registry())
        assert result["status"] == "needs_information"
        assert any("wind_direction" in item for item in result["clarification_questions"])
        assert fake.calls == ["task_parser_agent"]
        await graph.close()
    asyncio.run(scenario())


def test_mapping_reviewer_repairs_deterministic_validation_errors(tmp_path):
    async def scenario():
        fake = ReviewingRuntime(fan_task())
        graph, result = await run_graph(tmp_path, fake, fan_registry())
        assert result["status"] == "awaiting_approval"
        assert result["mapping_revision_count"] == 1
        assert fake.calls.count("mapping_reviewer_agent") == 1
        assert result["mapping_review_issues"] == []
        assert result["issue_history"]
        assert result["active_issues"] == []
        assert all(issue["status"] == "resolved" for issue in result["issue_history"])
        assert result["validation_errors"] == []
        await graph.close()
    asyncio.run(scenario())


def test_fixed_condition_mismatch_blocks_and_run_readback_executes(tmp_path):
    async def scenario():
        task = fan_task()
        task["fixed_conditions"] = [{
            "id": "inlet_speed",
            "name": "入口速度",
            "value": 40.0,
            "unit": "m/s",
            "source": "user",
            "parameter_id": "speed-id",
            "verification": "case_parameter",
        }]
        fake = FakeRuntime(task, mapping=fan_mapping())
        graph, blocked = await run_graph(tmp_path / "blocked", fake, fan_registry_with_speed(30.0))
        assert blocked["status"] == "human_review_required"
        assert any("Case 扫描值" in item for item in blocked["validation_errors"])
        await graph.close()

        task["fixed_conditions"][0]["verification"] = "run_readback"
        fake = FakeRuntime(task, mapping=fan_mapping())
        registry = fan_registry_with_speed(30.0)
        graph, resolved = await run_graph(tmp_path / "executable", fake, registry)
        assert resolved["status"] == "awaiting_approval"
        template = json.loads(Path(SPEC).read_text(encoding="utf-8"))
        experiment = compile_resolved_experiment(
            template,
            profile(),
            resolved["resolved_task"],
            registry,
            "http://localhost:18000/mcp",
        )
        assert experiment.native_spec.design_points[0].values["speed-id"] == pytest.approx(40.0)
        await graph.close()

    asyncio.run(scenario())


def test_mapping_revision_limit_routes_to_human_review(tmp_path):
    async def scenario():
        fake = ReviewingRuntime(fan_task(), always_invalid=True)
        graph, result = await run_graph(tmp_path, fake, fan_registry())
        assert result["status"] == "human_review_required"
        assert result["mapping_revision_count"] == 3
        assert fake.calls.count("mapping_reviewer_agent") == 2
        await graph.close()
    asyncio.run(scenario())


def test_classification_revision_limit_routes_to_human_review(tmp_path):
    async def scenario():
        signature = model_signature()
        from vegapunk.fluent.parameter_rules import resolve_parameters
        parameters = {item.key: item for item in resolve_parameters(signature)}
        fake = InvalidClassificationRuntime(direct_task(), classification="DIRECT")
        graph, result = await run_graph(tmp_path, fake, CapabilityRegistry(profile(), parameters, {}))
        assert result["status"] == "human_review_required"
        assert result["classification_revision_count"] == 3
        assert fake.calls.count("variable_classifier_agent") == 3
        await graph.close()
    asyncio.run(scenario())


def test_identity_guard_discards_stale_model_or_revision_response(tmp_path):
    async def scenario():
        fake = FakeRuntime(fan_task(), mapping=fan_mapping())
        graph, result = await run_graph(
            tmp_path, fake, fan_registry(), checker=lambda _identity: False
        )
        assert result["status"] == "stale_response_discarded"
        assert result["resolved_task"] is None
        await graph.close()
    asyncio.run(scenario())


def test_agent_call_timeout_is_enforced(tmp_path):
    class SlowRuntime:
        async def generate_json(self, *_args, **_kwargs):
            await asyncio.sleep(0.1)

    async def scenario():
        graph = FluentAgentGraph(
            SlowRuntime(), model_id="slow", database_path=tmp_path / "slow.sqlite3",
            output_dir_provider=lambda: tmp_path, timeout_seconds=0.01,
        )
        graph.bind_registry(fan_registry())
        await graph.start()
        try:
            result = await graph.message(
                "任务", task_id="slow-task", conversation_revision=0,
                model_id="model-1", case_sha256="a" * 64,
                model_signature_sha256="b" * 64,
            )
            assert result["status"] == "timed_out"
            assert result["resolved_task"] is None
            assert (await graph.get_state("slow-task"))["status"] == "timed_out"
        finally:
            await graph.close()
    asyncio.run(scenario())


def test_transient_model_error_retries_once_within_node_budget(tmp_path):
    class TransientRuntime(FakeRuntime):
        attempts = 0

        async def generate_json(self, prompt, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise ConnectionError("connection reset")
            return await super().generate_json(prompt, **kwargs)

    async def scenario():
        fake = TransientRuntime(fan_task(), mapping=fan_mapping())
        graph, result = await run_graph(tmp_path, fake, fan_registry())
        assert result["status"] == "awaiting_approval"
        assert fake.attempts >= 4
        audit = tmp_path / "planning_audit" / "untracked.jsonl"
        events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        assert events[0]["network_retry_count"] == 1
        await graph.close()

    asyncio.run(scenario())


def test_approval_fingerprint_binds_mapping_and_registry_versions(tmp_path):
    async def scenario():
        fake = FakeRuntime(fan_task(), mapping=fan_mapping())
        graph, result = await run_graph(tmp_path, fake, fan_registry())
        context = {"task_id": "task-1", "conversation_revision": 0, "plan_revision": 1, "planning_attempt_id": "attempt-a"}
        approval = approve_resolved_task(profile(), result["resolved_task"], "researcher", context)
        assert_resolved_approval(profile(), result["resolved_task"], approval, context)
        with pytest.raises(PermissionError, match="失效"):
            assert_resolved_approval(
                profile(),
                result["resolved_task"],
                approval,
                {**context, "plan_revision": 2},
            )
        for mutate in (
            lambda value: value["mappings"][0].__setitem__("mapping_version", "2.0"),
            lambda value: value.__setitem__("capability_registry_version", "4.0"),
            lambda value: value["mappings"][0]["assumptions"].append("changed"),
        ):
            changed = copy.deepcopy(result["resolved_task"])
            mutate(changed)
            with pytest.raises(PermissionError, match="失效"):
                assert_resolved_approval(profile(), changed, approval, context)
        await graph.close()
    asyncio.run(scenario())


def test_proxy_result_generates_persisted_geometry_recommendation(tmp_path):
    recommendation = {
        "message": "建议进行真实几何复核",
        "recommendation": {
            "source_campaign_id": "campaign-1",
            "source_trial_id": "trial-0002",
            "capability_id": "mapped.fan_incidence_2d.v1",
            "mapping_id": "fan-angle-proxy",
            "mapping_version": "1.0",
            "proxy_variable": "relative_incidence_angle",
            "optimal_proxy_value": 20,
            "recommended_geometry_parameter": "fan_angle",
            "recommended_geometry_value": 10,
            "unit": "deg",
            "reference_conditions": {"wind_direction": 30},
            "assumptions": ["fixed geometry proxy"],
            "confidence": 0.8,
            "verification_required": True,
            "verification_route": "FUTURE_WORKBENCH_MCP",
            "automatic_execution": False,
            "future_handoff_request": {"delta": 1},
        },
    }

    async def scenario():
        fake = FakeRuntime(fan_task(), mapping=fan_mapping(), recommendation=recommendation)
        graph, result = await run_graph(tmp_path, fake, fan_registry())
        approval = approve_resolved_task(profile(), result["resolved_task"], "researcher")
        approved = await graph.approve("task-1", approval["fingerprint"])
        assert approved["status"] == "ready_to_run"
        final = await graph.record_experiment_result("task-1", {
            "campaign_id": "campaign-1", "completed_trials": 3,
            "feasible_trials": 2, "failed_trials": 0,
            "best_result": {
                "trial_id": "trial-0002", "parameters": {"fan_angle": 10},
                "proxy_values": {"relative_incidence_angle": 20},
                "objective_value": 12.5,
            },
            "termination_reason": "target_completed",
        })
        assert final["geometry_recommendations"][0]["automatic_execution"] is False
        assert (tmp_path / "geometry_recommendation.json").is_file()
        request = json.loads((tmp_path / "geometry_verification_request.json").read_text(encoding="utf-8"))
        assert [item["value"] for item in request["candidates"]] == [9.0, 10.0, 11.0]
        await graph.close()
    asyncio.run(scenario())


def test_no_feasible_result_never_generates_geometry_recommendation(tmp_path):
    async def scenario():
        fake = FakeRuntime(fan_task(), mapping=fan_mapping())
        graph, result = await run_graph(tmp_path, fake, fan_registry())
        approval = approve_resolved_task(profile(), result["resolved_task"], "researcher")
        await graph.approve("task-1", approval["fingerprint"])
        final = await graph.record_experiment_result("task-1", {
            "completed_trials": 3, "feasible_trials": 0, "failed_trials": 0,
            "best_result": None,
            "termination_reason": "budget_exhausted_without_feasible_solution",
        })
        assert final["geometry_recommendations"] == []
        assert not (tmp_path / "geometry_recommendation.json").exists()
        await graph.close()
    asyncio.run(scenario())


def test_suggestion_only_handoff_never_submits(tmp_path):
    request = GeometryVerificationRequest(
        source_campaign_id="campaign-1",
        source_trial_id="trial-2",
        capability_id="mapped.fan_incidence_2d.v1",
        mapping_id="fan-angle-proxy",
        geometry_parameter="fan_angle",
        candidates=[
            GeometryCandidate(value=9, unit="deg", label="optimum-minus-delta"),
            GeometryCandidate(value=10, unit="deg", label="optimum"),
            GeometryCandidate(value=11, unit="deg", label="optimum-plus-delta"),
        ],
        reference_conditions={"wind_direction": 30},
    )
    result = asyncio.run(SuggestionOnlyGeometryHandoff(tmp_path).submit(request))
    assert result.mode == "suggestion_only"
    assert result.submitted is False
    assert result.requires_manual_geometry_update is True
    assert (tmp_path / "geometry_verification_request.json").is_file()


def test_web_api_exposes_graph_state_resolved_task_and_reset(tmp_path, monkeypatch):
    case = tmp_path / "generic.cas.h5"
    case.write_bytes(b"generic")

    async def scan(*_args):
        return model_signature()

    monkeypatch.setattr("vegapunk.fluent.adaptive.scan_model", scan)
    task = direct_task()
    task["objective"]["metric_key"] = next(
        item.key for item in resolve_metrics(model_signature())
        if "product-velocity-avg" in item.key
    )
    fake = FakeRuntime(task, classification="DIRECT")
    app = create_app(
        spec_path=SPEC,
        output_dir=tmp_path / "runs",
        allow_remote_control=True,
        agent_runtime=fake,
        agent_model_id="fake/model",
    )
    with TestClient(app) as client:
        analyzed = client.post("/api/models/analyze", json={
            "case_file": str(case),
            "endpoint": "http://localhost:18000/mcp",
            "product_version": "26.1.0",
            "question": "优化入口速度",
        })
        assert analyzed.status_code == 200
        response = client.post("/api/agent/messages", json={"message": "优化入口速度"})
        assert response.status_code == 202
        attempt = wait_for_attempt(client, response.json()["attempt_id"])
        assert attempt["status"] == "awaiting_approval"
        assert client.get("/api/state").json()["task"]["planning_mode"] == "agent_v3"
        state = client.get("/api/agent/state").json()
        assert state["agent"]["framework"] == "langgraph"
        assert state["state"]["variable_bindings"][0]["classification"] == "DIRECT"
        assert client.get("/api/tasks/current/plan").json()["task_object"]["schema_version"] == 3
        assert client.get("/api/tasks/current/resolved-task").json()["resolved_task"]["executable"] is True
        assert client.get("/api/tasks/current/geometry-recommendation").json()["geometry_recommendations"] == []
        prior_state = client.get("/api/state").json()
        app.state.fluent_runtime.planning_mode = None
        app.state.fluent_runtime._persist_task_state()
        legacy_switch = client.post("/api/plans", json=prior_state["plan"])
        assert legacy_switch.status_code == 409
        assert "历史任务" in legacy_switch.json()["detail"]
        revision = client.get("/api/state").json()["task"]["conversation_revision"]
        reset = client.post("/api/agent/reset")
        assert reset.status_code == 200
        assert reset.json()["conversation_revision"] == revision + 1
        assert client.get("/api/agent/state").json()["state"]["research_question"] == ""


def test_v3_incomplete_task_cannot_fall_back_to_manual_approval(tmp_path, monkeypatch):
    case = tmp_path / "generic.cas.h5"
    case.write_bytes(b"generic")

    async def scan(*_args):
        return model_signature()

    monkeypatch.setattr("vegapunk.fluent.adaptive.scan_model", scan)
    fake = FakeRuntime(fan_task(complete=False), mapping=fan_mapping())
    app = create_app(
        spec_path=SPEC,
        output_dir=tmp_path / "runs",
        allow_remote_control=True,
        agent_runtime=fake,
        agent_model_id="fake/model",
    )
    with TestClient(app) as client:
        analyzed = client.post("/api/models/analyze", json={
            "case_file": str(case),
            "endpoint": "http://localhost:18000/mcp",
            "product_version": "26.1.0",
            "question": "优化风扇安装角",
        })
        assert analyzed.status_code == 200
        response = client.post("/api/agent/messages", json={"message": "优化风扇安装角"})
        assert response.status_code == 202
        attempt = wait_for_attempt(client, response.json()["attempt_id"])
        assert attempt["status"] == "needs_information"
        state = client.get("/api/state").json()
        assert state["task"]["planning_mode"] == "agent_v3"
        assert state["plan"] is not None
        blocked = client.post("/api/plans/approve", json={"approved_by": "researcher"})
        assert blocked.status_code == 409
        assert "V3 规划尚未" in blocked.json()["detail"]


def test_attempt_store_marks_active_records_interrupted_on_restart(tmp_path):
    store = PlanningAttemptStore(tmp_path / "attempts", timeout_seconds=30)
    record = store.create(
        task_id="task-1",
        message="test",
        input_category="initial_request",
        identity={"conversation_revision": 0},
        model_id="fake",
        effective_reasoning="medium",
    )
    restored = PlanningAttemptStore(tmp_path / "attempts", timeout_seconds=30)
    assert restored.get(record["attempt_id"])["status"] == "interrupted"


def test_background_attempt_rejects_concurrency_and_can_cancel(tmp_path, monkeypatch):
    case = tmp_path / "generic.cas.h5"
    case.write_bytes(b"generic")

    async def scan(*_args):
        return model_signature()

    class SlowRuntime(FakeRuntime):
        async def generate_json(self, prompt, **kwargs):
            await asyncio.sleep(5)
            return await super().generate_json(prompt, **kwargs)

    monkeypatch.setattr("vegapunk.fluent.adaptive.scan_model", scan)
    app = create_app(
        spec_path=SPEC,
        output_dir=tmp_path / "runs",
        allow_remote_control=True,
        agent_runtime=SlowRuntime(direct_task(), classification="DIRECT"),
        agent_model_id="fake/model",
    )
    with TestClient(app) as client:
        assert client.post("/api/models/analyze", json={
            "case_file": str(case), "endpoint": "http://localhost:18000/mcp",
            "product_version": "26.1.0", "question": "优化入口速度",
        }).status_code == 200
        submitted = client.post("/api/agent/messages", json={"message": "优化入口速度"})
        assert submitted.status_code == 202
        duplicate = client.post("/api/agent/messages", json={"message": "另一条消息"})
        assert duplicate.status_code == 409
        attempt_id = submitted.json()["attempt_id"]
        cancelled = client.post(f"/api/agent/attempts/{attempt_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert client.get("/api/agent/state").json()["state"]["status"] == "cancelled"


def test_failed_attempt_can_retry_and_transient_call_is_audited(tmp_path, monkeypatch):
    case = tmp_path / "generic.cas.h5"
    case.write_bytes(b"generic")

    async def scan(*_args):
        return model_signature()

    class ToggleRuntime(FakeRuntime):
        fail = True

        async def generate_json(self, prompt, **kwargs):
            if self.fail:
                raise ValueError("deterministic provider failure")
            return await super().generate_json(prompt, **kwargs)

    monkeypatch.setattr("vegapunk.fluent.adaptive.scan_model", scan)
    runtime = ToggleRuntime(direct_task(), classification="DIRECT")
    app = create_app(
        spec_path=SPEC,
        output_dir=tmp_path / "runs",
        allow_remote_control=True,
        agent_runtime=runtime,
        agent_model_id="fake/model",
    )
    with TestClient(app) as client:
        assert client.post("/api/models/analyze", json={
            "case_file": str(case), "endpoint": "http://localhost:18000/mcp",
            "product_version": "26.1.0", "question": "优化入口速度",
        }).status_code == 200
        submitted = client.post("/api/agent/messages", json={"message": "优化入口速度"})
        first = wait_for_attempt(client, submitted.json()["attempt_id"])
        assert first["status"] == "failed"
        runtime.fail = False
        retried = client.post(f"/api/agent/attempts/{first['attempt_id']}/retry")
        assert retried.status_code == 202
        second = wait_for_attempt(client, retried.json()["attempt_id"])
        assert second["status"] == "awaiting_approval"
        audit = Path(app.state.fluent_runtime.output_dir) / "planning_audit" / f"{second['attempt_id']}.jsonl"
        events = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
        assert events
        assert all(event["model_id"] == "fake/model" for event in events)
        assert all("token_usage" in event for event in events)


def test_v3_start_uses_revision_gate_and_idempotency_key(tmp_path, monkeypatch):
    case = tmp_path / "generic.cas.h5"
    case.write_bytes(b"generic")

    async def scan(*_args):
        return model_signature()

    monkeypatch.setattr("vegapunk.fluent.adaptive.scan_model", scan)
    task = direct_task()
    task["objective"]["metric_key"] = next(
        item.key for item in resolve_metrics(model_signature())
        if "product-velocity-avg" in item.key
    )
    app = create_app(
        spec_path=SPEC,
        output_dir=tmp_path / "runs",
        allow_remote_control=True,
        agent_runtime=FakeRuntime(task, classification="DIRECT"),
        agent_model_id="fake/model",
    )
    with TestClient(app) as client:
        assert client.post("/api/models/analyze", json={
            "case_file": str(case), "endpoint": "http://localhost:18000/mcp",
            "product_version": "26.1.0", "question": "优化入口速度",
        }).status_code == 200
        submitted = client.post("/api/agent/messages", json={"message": "优化入口速度", "input_category": "initial_request"})
        attempt = wait_for_attempt(client, submitted.json()["attempt_id"])
        assert attempt["status"] == "awaiting_approval"
        graph_state = client.get("/api/agent/state").json()["state"]
        stale = client.post("/api/plans/approve", json={
            "approved_by": "researcher",
            "plan_revision": graph_state["plan_revision"] + 1,
            "planning_attempt_id": graph_state["planning_attempt_id"],
        })
        assert stale.status_code == 409
        approved = client.post("/api/plans/approve", json={
            "approved_by": "researcher",
            "plan_revision": graph_state["plan_revision"],
            "planning_attempt_id": graph_state["planning_attempt_id"],
        })
        assert approved.status_code == 200
        assert approved.json()["execution_mode"] == "V3_APPROVED"
        fingerprint = approved.json()["fingerprint"]
        app.state.fluent_runtime.start_resolved = AsyncMock(return_value=None)
        run = {
            "case_file": str(case),
            "target_trials": task["termination"]["max_trials"],
            "iterations": task["solver_requirements"]["iterations"],
            "endpoint": "http://localhost:18000/mcp",
            "start_request_id": fingerprint,
        }
        approval_file = app.state.fluent_runtime.output_dir / "plan_approval.json"
        record = json.loads(approval_file.read_text(encoding="utf-8"))
        assert record["execution_approval"]["mode"] == "V3_APPROVED"
        prior_only = {key: value for key, value in record.items() if key != "execution_approval"}
        approval_file.write_text(json.dumps(prior_only), encoding="utf-8")
        rejected = client.post("/api/runs", json=run)
        assert rejected.status_code == 409
        assert app.state.fluent_runtime.start_resolved.await_count == 0
        approval_file.write_text(json.dumps(record), encoding="utf-8")
        first = client.post("/api/runs", json=run)
        assert first.status_code == 202
        second = client.post("/api/runs", json=run)
        assert second.status_code == 202
        assert second.json()["idempotent_replay"] is True
        assert app.state.fluent_runtime.start_resolved.await_count == 1
        autonomy = client.get("/api/tasks/current/autonomy-report").json()
        assert autonomy["model_calls"]["coverage_complete"] is True
        assert autonomy["human_intervention"]["coverage_complete"] is True
        assert autonomy["human_intervention"]["category_counts"]["initial_request"] == 1
