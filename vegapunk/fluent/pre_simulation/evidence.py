"""Server-owned facts; a model may cite them but may not rewrite them."""

from __future__ import annotations

import hashlib
import json

from .schema import EvidenceRef, ModelProfile, VerificationStatus


EVIDENCE_POLICY_VERSION = "2"


def profile_evidence_ids(profile: ModelProfile) -> set[str]:
    return {ref.source_id for ref in profile_facts(profile)}


def profile_facts(profile: ModelProfile) -> list[EvidenceRef]:
    records = [*profile.readbacks.items(), *profile.physical_models.items(), *profile.solver_settings.items()]
    records += [(str(item.get("object_name") or item.get("rule_id")), item)
                for item in profile.parameters if item.get("object_name") or item.get("rule_id")]
    records += [(str(item["name"]), item) for group in (
        profile.boundary_zones, profile.materials, profile.fluid_zones, profile.solid_zones, profile.heat_sources,
    ) for item in group if isinstance(item, dict) and item.get("name")]
    facts = []
    for name, value in records:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded) > 1700:
            encoded = "content_sha256=" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        facts.append(EvidenceRef(
            source_type="model_profile", source_id=name, fact=f"{name}={encoded}",
            profile_version=profile.profile_version,
            # This proves what the profile contains, not a physical trend.
            verification=VerificationStatus.VERIFIED,
        ))
    return facts


def evidence_catalog(profile, feasible_ranges, registry, physics_features) -> list[EvidenceRef]:
    groups = [*(item.constraint_sources for item in feasible_ranges),
              *(item.evidence for item in registry.capabilities),
              *(item.evidence for item in physics_features.features.values()), profile_facts(profile)]
    unique = {}
    for group in groups:
        for ref in group:
            copied = EvidenceRef.model_validate(ref.model_dump(mode="json"))
            key = json.dumps(copied.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
            unique[key] = copied
    return [unique[key] for key in sorted(unique)]


def validate_fact_citations(proposals, catalog) -> None:
    allowed = {(item.source_type, item.source_id) for item in catalog}
    for proposal in proposals:
        for ref in proposal.evidence:
            if (ref.source_type, ref.source_id) not in allowed:
                raise ValueError(f"Proposal cites unknown evidence: {ref.source_type}:{ref.source_id}")
            if ref not in catalog:
                raise ValueError("Evidence fact, verification or profile version differs from server-owned fact")


def validate_range_inference(proposals) -> None:
    """V1 facts are measurements/bounds, not validated variable-objective trends.

    No external provider or correlation has been enabled. Refuse to convert a
    qualitative LLM hypothesis into a smaller search domain just because it
    cites a measured velocity/Reynolds number. Keep proposals visible for audit.
    """
    for proposal in proposals:
        if proposal.recommended_range != proposal.feasible_range or proposal.high_potential_range is not None:
            raise ValueError("No validated variable-objective trend authorizes range narrowing or high-potential range in V1")
