"""Optional frozen-prior adapter to the existing V3 Optuna/Runner chain."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from ..capability_registry import CapabilityRegistry
from ..experiment_orchestrator import (
    ResolvedExperiment,
    compile_resolved_experiment,
    run_resolved_optimization,
)
from ..mapping_runtime import validate_mapping_spec
from ..mapping_schema import ResolvedTaskObject, ResolvedVariable, VariableBinding, VariableClassification
from ..profile_store import case_sha256
from ..spec import ExperimentSpec
from ..task_schema import SimulationTaskObject, VariableIntent
from .schema import (
    CapabilityRegistrySnapshot,
    ModelProfile,
    OptimizationPrior,
    OptimizationTask,
    PriorValidation,
    SolverPreset,
    VariableRoute,
    VariableRouteBinding,
)
from .validation import FrozenPrior, PriorValidator
from .variables import MAPPING_REGISTRY_VERSION, _VARIABLES, build_registered_mapping_spec, registry_snapshot
from .execution_inputs import FrozenExperimentInputs, snapshot_execution_inputs
from ..execution_approval import seal_execution


class FrozenTaskError(ValueError):
    pass


@dataclass(frozen=True)
class CompiledPreSimulationExperiment:
    task: OptimizationTask
    experiment: ResolvedExperiment
    frozen_prior_version: str
    case_file: str


def _frozen_contract(frozen: FrozenPrior):
    payload = frozen.payload()
    return (
        OptimizationPrior.model_validate(payload["prior"]),
        SolverPreset.model_validate(payload["solver_preset"]),
        PriorValidation.model_validate(payload["validation"]),
        [VariableRouteBinding.model_validate(item) for item in payload["routes"]],
        CapabilityRegistrySnapshot.model_validate(payload["capability_registry"]),
    )


def _legacy_profile(model: ModelProfile, case_file: str) -> dict:
    if model.dimension not in (2, 3):
        raise FrozenTaskError("ModelProfile dimension is not read-only verified")
    signature = {
        "dimension": model.dimension,
        "fluent_version": model.fluent_version,
        "product_version": model.product_version,
        "physics": model.physical_models,
        "boundaries": model.boundary_zones,
        "cell_zones": [*model.fluid_zones, *model.solid_zones],
        "materials": model.materials,
        "observations": model.parameters,
        "reports": model.reports,
        "solver_readbacks": model.solver_settings,
    }
    return {
        "case_file": case_file,
        "case_sha256": model.case_fingerprint,
        "dimension": model.dimension,
        "product_version": model.product_version,
        "fluent_version": model.fluent_version,
        "signature": signature,
    }


def compile_frozen_experiment(
    frozen: FrozenPrior,
    model: ModelProfile,
    case_file: str,
    v3_task: SimulationTaskObject,
    template: Mapping[str, Any],
    registry: CapabilityRegistry,
    endpoint: str,
) -> CompiledPreSimulationExperiment:
    """Compile, never execute. The V3 task supplies objective and Gate semantics."""
    if frozen.approval_status != "APPROVED" or not frozen.approval_id:
        raise PermissionError("Frozen prior is not approved")
    actual_fingerprint = case_sha256(case_file)
    if actual_fingerprint != frozen.case_fingerprint or model.case_fingerprint != actual_fingerprint:
        raise FrozenTaskError("Case fingerprint differs from approved frozen prior")
    if model.profile_version != frozen.profile_version:
        raise FrozenTaskError("ModelProfile version differs from approved frozen prior")
    if registry.version != frozen.capability_registry_version or MAPPING_REGISTRY_VERSION != frozen.mapping_registry_version:
        raise FrozenTaskError("Capability or Mapping Registry version changed")
    raw_inputs = frozen.payload().get("execution_inputs")
    if raw_inputs is None:
        raise PermissionError("Legacy prior-only approval requires a new full-experiment approval")
    approved_inputs = FrozenExperimentInputs.model_validate(raw_inputs)
    current_inputs = snapshot_execution_inputs(v3_task, template, endpoint, case_file)
    if current_inputs != approved_inputs:
        raise PermissionError("Frozen experiment inputs changed; a new approval is required")
    prior, preset, stored_validation, routes, frozen_registry = _frozen_contract(frozen)
    current_registry = registry_snapshot(registry, model)
    if current_registry != frozen_registry:
        raise FrozenTaskError("Current Capability Registry differs from frozen snapshot")
    validation = PriorValidator().validate(
        prior, preset, model, current_registry, routes,
        current_case_fingerprint=actual_fingerprint,
        runner_supports_early_stop=False,
    )
    if not validation.valid or not stored_validation.valid:
        raise FrozenTaskError("PriorValidator rejected frozen prior at compilation")
    if preset.iteration_policy.early_stop_enabled:
        raise FrozenTaskError("Early stop is not verified by the current Runner")
    if v3_task.needs_information or v3_task.information_gaps():
        raise FrozenTaskError("Simulation Agent V3 task has unresolved information")
    original = {item.id: item for item in v3_task.variables}
    proposals = {item.variable: item for item in prior.proposals}
    if set(original) != set(proposals):
        raise FrozenTaskError("V3 task variables do not match frozen prior variables")
    route_by_variable = {item.variable: item for item in routes}
    variable_intents = []
    resolved_variables = []
    mappings = []
    metric_ids = [v3_task.objective.metric_key, *(item.metric_key for item in v3_task.constraints)]
    metric_ids = [str(item) for item in metric_ids if item]
    for variable, original_intent in original.items():
        proposal = proposals[variable]
        route = route_by_variable[variable]
        expected_semantic = _VARIABLES.get(variable, (None,))[0]
        if original_intent.semantic_key != expected_semantic or original_intent.unit != proposal.unit:
            raise FrozenTaskError(f"{variable}: V3 semantic variable or unit differs from frozen prior")
        if route.route == VariableRoute.GEOMETRY:
            raise FrozenTaskError("GEOMETRY_REQUIRED cannot compile to Fluent")
        search = proposal.recommended_range
        initial = original_intent.initial_value
        if initial is not None and not search.lower <= initial <= search.upper:
            initial = min(max(initial, search.lower), search.upper)
        updated = VariableIntent.model_validate({
            **original_intent.model_dump(mode="json"),
            "minimum": search.lower,
            "maximum": search.upper,
            "initial_value": initial,
        })
        variable_intents.append(updated)
        matches = [item for item in registry.available() if item["executable"]
                   and expected_semantic in item["semantic_keys"]
                   and set(route.parameter_ids).issubset(set(item["available_parameter_ids"]))]
        if len(matches) != 1:
            raise FrozenTaskError(f"{variable}: no unique existing Registry capability")
        capability_id = matches[0]["capability_id"]
        if route.route == VariableRoute.MAPPED:
            mapping = build_registered_mapping_spec(route.mapping_id or "", route.parameter_ids, registry.parameters)
            if mapping.selected_capability_id != capability_id:
                raise FrozenTaskError("Registered mapping capability changed")
            mappings.append(mapping)
            classification = VariableClassification.MAPPED_PROXY
        else:
            classification = VariableClassification.DIRECT
        binding = VariableBinding(
            variable_id=variable,
            classification=classification,
            classification_reason="Frozen, validated Pre-Simulation route",
            selected_capability_id=capability_id,
            candidate_parameter_ids=route.parameter_ids,
            candidate_metric_ids=metric_ids,
            confidence=proposal.confidence,
            missing_information=proposal.missing_information,
        )
        resolved_variables.append(ResolvedVariable(
            intent=updated, binding=binding,
            mapping_id=route.mapping_id, executable=True,
        ))
    solver_raw = v3_task.solver_requirements.model_dump(mode="json")
    solver_raw["iterations"] = preset.iteration_policy.hard_limit
    solver_raw["residual_thresholds"] = preset.convergence_policy.residual_thresholds
    task_raw = v3_task.model_dump(mode="json")
    task_raw["variables"] = [item.model_dump(mode="json") for item in variable_intents]
    task_raw["solver_requirements"] = solver_raw
    adapted_task = SimulationTaskObject.model_validate(task_raw)
    for mapping in mappings:
        errors = validate_mapping_spec(mapping, adapted_task, registry)
        if errors:
            raise FrozenTaskError("Registered mapping failed existing validator: " + "; ".join(errors))
    resolved = ResolvedTaskObject(
        task=adapted_task, variables=resolved_variables, mappings=mappings,
        executable=True, geometry_unsupported=[], validation_errors=[],
        capability_registry_version=registry.version,
    )
    legacy = _legacy_profile(model, case_file)
    experiment = compile_resolved_experiment(template, legacy, resolved, registry, endpoint)
    metadata = {
        "frozen_prior_version": frozen.prior_version,
        "frozen_prior_sha256": frozen.payload_sha256,
        "profile_version": model.profile_version,
        "case_fingerprint": actual_fingerprint,
        "capability_registry_version": registry.version,
        "mapping_registry_version": MAPPING_REGISTRY_VERSION,
        "approval_id": frozen.approval_id,
    }

    def with_metadata(spec: ExperimentSpec) -> ExperimentSpec:
        raw = spec.to_dict()
        raw["execution_contract"] = {**(raw["execution_contract"] or {}), "pre_simulation": metadata}
        if spec.solver.thermal_guard is None:
            raw["solver"]["iteration_chunk_size"] = preset.iteration_policy.check_interval
        return ExperimentSpec.from_dict(raw)

    experiment = ResolvedExperiment(
        semantic_spec=with_metadata(experiment.semantic_spec),
        native_spec=with_metadata(experiment.native_spec),
        resolved_task=resolved,
    )
    experiment = replace(experiment, execution_approval=seal_execution(
        experiment, registry, approval_id=frozen.approval_id,
        approved_by=frozen.approved_by, mode="FROZEN_PRIOR",
    ))
    task = OptimizationTask(
        task_id="opt-" + frozen.prior_version,
        approval_id=frozen.approval_id,
        case_fingerprint=actual_fingerprint,
        profile_version=model.profile_version,
        prior=prior,
        solver_preset=preset,
        prior_validation=validation,
        runner_contract_version="existing-v3-1",
    )
    return CompiledPreSimulationExperiment(task, experiment, frozen.prior_version, case_file)


async def run_frozen_optimization(
    compiled: CompiledPreSimulationExperiment,
    output_dir: str | Path,
    registry: CapabilityRegistry,
    *, native_runner_factory=None,
) -> dict[str, Any]:
    """Delegate all ask/tell, Gate, verification and MCP work to existing code."""
    if case_sha256(compiled.case_file) != compiled.task.case_fingerprint:
        raise FrozenTaskError("Case changed after frozen task compilation")
    return await run_resolved_optimization(
        compiled.experiment, output_dir, registry,
        native_runner_factory=native_runner_factory,
    )
