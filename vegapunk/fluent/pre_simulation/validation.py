"""Solver proposal, pre-execution validation and immutable approval records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..history import atomic_json
from .prior import profile_evidence_ids
from .schema import (
    CapabilityRegistrySnapshot,
    EvidenceRef,
    EvidenceStatus,
    IterationPolicy,
    ModelProfile,
    OptimizationPrior,
    PriorValidation,
    SolverPreset,
    VariableRoute,
    VariableRouteBinding,
    VerificationStatus,
)
from .variables import MAPPING_REGISTRY_VERSION, MappingRegistry, _catalog
from .execution_inputs import FrozenExperimentInputs
from .evidence import evidence_catalog, validate_fact_citations, validate_range_inference
from .physics import PhysicsFeatureExtractor


VALIDATOR_VERSION = "2"
_CRITICAL_FIELDS = frozenset({"solver_type", "physical_models", "boundary_zones", "parameters"})


@dataclass(frozen=True)
class SolverBudgetPolicy:
    policy_id: str
    min_iterations: int
    initial_budget: int
    hard_limit: int
    check_interval: int
    residual_thresholds: dict[str, float]
    monitor_ids: tuple[str, ...] = ()
    convergence_window: int | None = None
    chunked_runner_verified: bool = False


class SolverPresetPlanner:
    def plan(self, profile: ModelProfile, policy: SolverBudgetPolicy) -> SolverPreset:
        if not policy.policy_id.strip():
            raise ValueError("Solver budget requires an identified policy basis")
        has_checks = bool(policy.residual_thresholds or policy.monitor_ids)
        early_stop = policy.chunked_runner_verified and has_checks and policy.convergence_window is not None
        iterations = IterationPolicy(
            min_iterations=policy.min_iterations,
            initial_budget=policy.initial_budget,
            hard_limit=policy.hard_limit,
            check_interval=policy.check_interval,
            early_stop_enabled=early_stop,
            convergence_window=policy.convergence_window if early_stop else None,
        )
        basis = EvidenceRef(
            source_type="policy", source_id=policy.policy_id,
            fact="Configured deterministic iteration and convergence limits, not a predicted optimum",
            profile_version=None, verification=VerificationStatus.VERIFIED,
        )
        return SolverPreset(
            preset_id="preset-" + uuid4().hex,
            profile_version=profile.profile_version,
            iteration_policy=iterations,
            convergence_policy={
                "monitor_ids": list(policy.monitor_ids),
                "residual_thresholds": policy.residual_thresholds,
            },
            confidence=0.5 if early_stop else 0.25,
            preset_basis=[basis],
            assumptions=[
                "Budget is a configured safety policy, not an optimal iteration count",
                "Early stopping disabled unless chunked execution and monitors are verified",
            ],
            evidence_status=EvidenceStatus.PARTIAL if early_stop else EvidenceStatus.INSUFFICIENT,
            verification=VerificationStatus.VERIFIED,
        )


class PriorValidator:
    def __init__(self, mappings: MappingRegistry | None = None) -> None:
        self.mappings = mappings or MappingRegistry()

    def validate(
        self, prior: OptimizationPrior, preset: SolverPreset, profile: ModelProfile,
        registry: CapabilityRegistrySnapshot, routes: list[VariableRouteBinding],
        *, current_case_fingerprint: str, runner_supports_early_stop: bool = False,
    ) -> PriorValidation:
        errors: list[str] = []
        warnings: list[str] = []
        for check in (
            lambda: validate_fact_citations(prior.proposals, evidence_catalog(
                profile, prior.feasible_ranges, registry, PhysicsFeatureExtractor().extract(profile),
            )),
            lambda: validate_range_inference(prior.proposals),
        ):
            try:
                check()
            except ValueError as exc:
                errors.append(str(exc))
        if current_case_fingerprint != profile.case_fingerprint:
            errors.append("Current Case fingerprint differs from ModelProfile")
        if prior.profile_version != profile.profile_version or preset.profile_version != profile.profile_version:
            errors.append("Prior or SolverPreset references a stale ModelProfile")
        if registry.profile_version != profile.profile_version:
            errors.append("Capability Registry references a stale ModelProfile")
        if _CRITICAL_FIELDS.intersection(profile.unknown_fields):
            errors.append("Critical ModelProfile fields remain UNKNOWN")
        if not preset.preset_basis or preset.verification != VerificationStatus.VERIFIED:
            errors.append("SolverPreset basis or verification is insufficient")
        if preset.iteration_policy.early_stop_enabled and not runner_supports_early_stop:
            errors.append("Runner does not verify early-stop convergence checks")
        catalog = _catalog(profile)
        known_evidence = {
            (ref.source_type, ref.source_id)
            for entry in prior.feasible_ranges for ref in entry.constraint_sources
        } | {
            (ref.source_type, ref.source_id)
            for capability in registry.capabilities for ref in capability.evidence
        }
        known_profile_fields = profile_evidence_ids(profile)
        route_map = {item.variable: item for item in routes}
        if len(route_map) != len(routes):
            errors.append("Duplicate variable routes")
        for proposal in prior.proposals:
            route = route_map.get(proposal.variable)
            feasible = next(item for item in prior.feasible_ranges if item.variable == proposal.variable)
            if feasible.verification != VerificationStatus.VERIFIED:
                errors.append(f"{proposal.variable}: feasible range is not verified")
            if route is None or route.capability_status != VerificationStatus.VERIFIED:
                errors.append(f"{proposal.variable}: executable route is not verified")
                continue
            if proposal.route != route.route:
                errors.append(f"{proposal.variable}: route differs from proposal")
            if route.route == VariableRoute.GEOMETRY:
                errors.append(f"{proposal.variable}: GEOMETRY_REQUIRED cannot execute in V1")
                continue
            if not route.parameter_ids or any(key not in catalog for key in route.parameter_ids):
                errors.append(f"{proposal.variable}: parameter is absent or not exposed")
                continue
            if route.route == VariableRoute.DIRECT:
                if len(route.parameter_ids) != 1 or catalog[route.parameter_ids[0]].unit != proposal.unit:
                    errors.append(f"{proposal.variable}: DIRECT parameter unit/binding invalid")
            elif route.route == VariableRoute.MAPPED:
                mapping = self.mappings.get(route.mapping_id or "")
                if mapping is None:
                    errors.append(f"{proposal.variable}: mapping is not registered")
                elif (proposal.variable not in mapping.input_variables or proposal.unit != mapping.input_unit
                      or {catalog[key].rule_id for key in route.parameter_ids} != set(mapping.output_rule_ids)):
                    errors.append(f"{proposal.variable}: mapping is incompatible")
            if proposal.evidence_status == EvidenceStatus.INSUFFICIENT:
                warnings.append(f"{proposal.variable}: physical prior insufficient; full feasible range retained")
            if proposal.evidence_status == EvidenceStatus.SUPPORTED and not proposal.evidence:
                errors.append(f"{proposal.variable}: SUPPORTED proposal has no evidence")
            for ref in proposal.evidence:
                if ref.verification != VerificationStatus.VERIFIED:
                    errors.append(f"{proposal.variable}: evidence is not verified")
                elif (ref.source_type, ref.source_id) not in known_evidence and not (
                    ref.source_type in {"model_readback", "model_profile"}
                    and ref.source_id in known_profile_fields
                ):
                    errors.append(
                        f"{proposal.variable}: evidence source is not traceable: "
                        f"{ref.source_type}:{ref.source_id}"
                    )
            if proposal.evidence_status == EvidenceStatus.SUPPORTED and not any(
                ref.source_type == "model_readback" for ref in proposal.evidence
            ):
                errors.append(f"{proposal.variable}: SUPPORTED evidence lacks physical readback")
            if not any(item.route == route.route and item.executable and
                       set(route.parameter_ids).issubset(set(item.parameter_ids))
                       for item in registry.capabilities):
                errors.append(f"{proposal.variable}: Capability Registry does not support route")
        return PriorValidation(
            valid=not errors, errors=errors, warnings=warnings,
            profile_version=profile.profile_version, validator_version=VALIDATOR_VERSION,
        )


@dataclass(frozen=True)
class FrozenPrior:
    """Frozen canonical JSON, not mutable model references."""

    prior_version: str
    case_fingerprint: str
    profile_version: str
    capability_registry_version: str
    mapping_registry_version: str
    approval_id: str
    approved_by: str
    approval_status: str
    created_at: str
    payload_json: str
    payload_sha256: str

    def payload(self) -> dict:
        if hashlib.sha256(self.payload_json.encode("utf-8")).hexdigest() != self.payload_sha256:
            raise ValueError("Frozen prior payload digest mismatch")
        return json.loads(self.payload_json)


class PriorFreezeStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def freeze(
        self, prior: OptimizationPrior, preset: SolverPreset,
        profile: ModelProfile, registry: CapabilityRegistrySnapshot,
        routes: list[VariableRouteBinding],
        *, approved_by: str, approval_id: str, current_case_fingerprint: str,
        validator: PriorValidator | None = None, runner_supports_early_stop: bool = False,
        execution_inputs: FrozenExperimentInputs | None = None,
    ) -> FrozenPrior:
        if not approved_by.strip() or not approval_id.strip():
            raise PermissionError("Explicit approval identity is required")
        if current_case_fingerprint != profile.case_fingerprint:
            raise PermissionError("Case fingerprint changed before approval")
        validation = (validator or PriorValidator()).validate(
            prior, preset, profile, registry, routes,
            current_case_fingerprint=current_case_fingerprint,
            runner_supports_early_stop=runner_supports_early_stop,
        )
        if not validation.valid or validation.profile_version != profile.profile_version:
            raise PermissionError("PriorValidator must pass for the current ModelProfile")
        if prior.profile_version != profile.profile_version or preset.profile_version != profile.profile_version:
            raise PermissionError("Prior or preset is stale")
        if registry.profile_version != profile.profile_version:
            raise PermissionError("Capability Registry is stale")
        payload = {
            "approval_scope": "FULL_EXPERIMENT" if execution_inputs is not None else "PRIOR_ONLY",
            "prior": prior.model_dump(mode="json"),
            "solver_preset": preset.model_dump(mode="json"),
            "validation": validation.model_dump(mode="json"),
            "routes": [item.model_dump(mode="json") for item in routes],
            "capability_registry": registry.model_dump(mode="json"),
        }
        if execution_inputs is not None:
            execution_inputs = FrozenExperimentInputs.model_validate(execution_inputs)
            if execution_inputs.case_fingerprint != current_case_fingerprint:
                raise PermissionError("Full experiment inputs refer to a different Case")
            payload["execution_inputs"] = execution_inputs.model_dump(mode="json")
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        frozen = FrozenPrior(
            prior_version="frozen-" + uuid4().hex,
            case_fingerprint=profile.case_fingerprint,
            profile_version=profile.profile_version,
            capability_registry_version=registry.registry_version,
            mapping_registry_version=MAPPING_REGISTRY_VERSION,
            approval_id=approval_id, approved_by=approved_by, approval_status="APPROVED",
            created_at=datetime.now(timezone.utc).isoformat(),
            payload_json=canonical,
            payload_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        target = self.root / (frozen.prior_version + ".json")
        if target.exists():
            raise FileExistsError("Frozen prior version already exists")
        atomic_json(target, frozen.__dict__)
        return frozen

    def load(self, prior_version: str) -> FrozenPrior:
        if (not prior_version.startswith("frozen-") or len(prior_version[7:]) != 32
                or any(char not in "0123456789abcdef" for char in prior_version[7:])):
            raise ValueError("Invalid frozen prior version")
        record = json.loads((self.root / (prior_version + ".json")).read_text(encoding="utf-8"))
        frozen = FrozenPrior(**record)
        frozen.payload()
        return frozen
