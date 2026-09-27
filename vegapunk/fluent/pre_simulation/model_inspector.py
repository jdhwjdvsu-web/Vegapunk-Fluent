"""Read-only ModelProfile lifecycle built on the existing Case scanner.

The default source checks the Case file and MCP session status before reuse.
It never takes over an active Fluent session. A source able to prove safe
read-only local changes may provide partial-refresh fields; otherwise changes
cause full re-introspection. No value is inferred for unreadable physics.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import importlib.metadata
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import ValidationError

from ..capability_registry import CAPABILITY_REGISTRY_VERSION
from ..metric_catalog import METRIC_VERSION
from ..model_introspection import SCANNER_VERSION, scan_model
from ..model_profile import PROFILE_SCHEMA
from ..parameter_rules import RULE_VERSION
from ..profile_store import ProfileStore, case_sha256
from .schema import ModelProfile, VerificationStatus
from .evidence import EVIDENCE_POLICY_VERSION
from .variables import MAPPING_REGISTRY_VERSION


class InspectionMode(StrEnum):
    CACHE_HIT = "CACHE_HIT"
    PARTIAL_REFRESH = "PARTIAL_REFRESH"
    FULL_REINTROSPECTION = "FULL_REINTROSPECTION"


class InspectionUnavailable(RuntimeError):
    """Current Case state cannot be checked without taking control or guessing."""


def _digest(value: object) -> str:
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StateConsistencySnapshot:
    case_fingerprint: str
    structural_digest: dict[str, str]
    local_digest: str
    local_updates: dict[str, object] | None = None
    verification: VerificationStatus = VerificationStatus.VERIFIED
    reason: str = ""


@dataclass(frozen=True)
class InspectionResult:
    profile: ModelProfile
    mode: InspectionMode
    reason: str


class ReadOnlyInspectionSource(Protocol):
    async def check_state(
        self, case_file: str, endpoint: str, connect_kwargs: dict
    ) -> StateConsistencySnapshot: ...

    async def read_full(
        self, case_file: str, endpoint: str, connect_kwargs: dict, audit_dir: Path
    ) -> dict: ...


class McpReadOnlyInspectionSource:
    """Use a cheap session check, then the existing strict read-only scanner.

    With no active session, all relevant Fluent state comes from the named Case
    file, so its content hash identifies structural and local state. An active
    session is rejected: session_status does not prove its Case identity, and
    the inspector must not take it over to discover that identity.
    """

    async def check_state(self, case_file: str, endpoint: str, connect_kwargs: dict) -> StateConsistencySnapshot:
        from fastmcp import Client
        from ..direct_run import _call

        fingerprint = await asyncio.to_thread(case_sha256, case_file)
        async with Client(endpoint, timeout=30) as client:
            status = await _call(client, "session_status", {})
        if status.get("connected"):
            raise InspectionUnavailable(
                "MCP has an active Fluent session whose Case identity cannot be read-only verified"
            )
        if status.get("connected") is not False:
            raise InspectionUnavailable("Read-only session status is UNKNOWN; Case state cannot be verified")
        data_path = Path(case_file[:-7] + ".dat.h5") if case_file.endswith(".cas.h5") else None
        def paired_data_digest():
            if data_path is None or not data_path.is_file():
                return "ABSENT"
            with data_path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()
        data_digest = await asyncio.to_thread(paired_data_digest)
        structural = {"case_file": fingerprint, "paired_data": data_digest,
                      "connection_configuration": _digest(connect_kwargs)}
        return StateConsistencySnapshot(
            case_fingerprint=fingerprint,
            structural_digest=structural,
            local_digest=_digest(structural),
            reason="No active session; named Case file fingerprint is current",
        )

    async def read_full(self, case_file: str, endpoint: str, connect_kwargs: dict, audit_dir: Path) -> dict:
        kwargs = dict(connect_kwargs)
        kwargs["case_file_name"] = case_file
        return await scan_model(endpoint, kwargs, audit_dir, strict_read_only=True)


_PARTIAL_FIELDS = frozenset({"boundary_zones", "reports", "residual_settings", "solver_settings", "readbacks"})
_REQUIRED_FULL_FIELDS = {
    "solver_type": "solver_type",
    "physical_models": "physics",
    "materials": "materials",
    "boundary_zones": "boundaries",
    "fluid_solid_zones": "cell_zones",
    "parameters": "observations",
    "time_regime": "time_regime",
    "heat_sources": "heat_sources",
    "mesh_summary": "mesh_summary",
    "mesh_quality": "mesh_quality",
    "residual_settings": "residual_settings",
    "reports": "reports",
}


class ModelInspector:
    """One versioned profile shared by all downstream planning nodes."""

    def __init__(
        self,
        audit_root: Path,
        source: ReadOnlyInspectionSource | None = None,
        store: ProfileStore | None = None,
    ) -> None:
        self.audit_root = audit_root
        self.source = source or McpReadOnlyInspectionSource()
        self.store = store or ProfileStore(audit_root / "model_library")
        self._current: ModelProfile | None = None
        self._lock = asyncio.Lock()

    @staticmethod
    def _dependencies() -> dict[str, str]:
        def package_version(name):
            try:
                return importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                return "NOT_INSTALLED"
        return {
            "scanner": SCANNER_VERSION,
            "parameter_rules": RULE_VERSION,
            "metric_catalog": METRIC_VERSION,
            "capability_registry": CAPABILITY_REGISTRY_VERSION,
            "legacy_profile_schema": str(PROFILE_SCHEMA),
            "pre_simulation_schema": "1",
            "state_consistency_policy": "2",
            "evidence_policy": EVIDENCE_POLICY_VERSION,
            "mapping_registry": MAPPING_REGISTRY_VERSION,
            "pyfluent_package": package_version("ansys-fluent-core"),
            "fastmcp_package": package_version("fastmcp"),
        }

    def _store_id(self, case_fingerprint: str, endpoint: str) -> str:
        return "pre-simulation-" + _digest([case_fingerprint, endpoint, self._dependencies()])

    def _load(self, model_id: str) -> ModelProfile | None:
        record = self.store.get(model_id)
        if not record:
            return None
        try:
            profile = ModelProfile.model_validate(record["profile"])
        except (KeyError, ValidationError):
            return None
        return profile if profile.dependency_versions == self._dependencies() else None

    def _save(self, model_id: str, profile: ModelProfile) -> None:
        self.store.save({"model_id": model_id, "profile": profile.model_dump(mode="json")})

    @staticmethod
    def _from_signature(signature: dict, snapshot: StateConsistencySnapshot) -> ModelProfile:
        unknown = [name for name, key in _REQUIRED_FULL_FIELDS.items() if key not in signature]
        unverified = []
        if any("UNVERIFIED:" in str(item) and "direction" in str(item) for item in signature.get("warnings", [])):
            unverified.append("inlet_direction: read-only capability not verified")
        physics = signature.get("physics") or {}
        cell_zones = signature.get("cell_zones") or []
        reports = signature.get("reports") or {}
        return ModelProfile(
            case_fingerprint=snapshot.case_fingerprint,
            profile_version="ps-" + uuid4().hex,
            generated_at=datetime.now(timezone.utc),
            dependency_versions=ModelInspector._dependencies(),
            state_digest={**snapshot.structural_digest, "local": snapshot.local_digest},
            dimension=signature.get("dimension"),
            fluent_version=signature.get("fluent_version"),
            product_version=signature.get("product_version"),
            solver_type=signature.get("solver_type"),
            time_regime=signature.get("time_regime"),
            physical_models=physics,
            materials=signature.get("materials") or [],
            fluid_zones=[item for item in cell_zones if item.get("type") == "fluid"],
            solid_zones=[item for item in cell_zones if item.get("type") == "solid"],
            boundary_zones=signature.get("boundaries") or [],
            heat_sources=signature.get("heat_sources") or [],
            parameters=signature.get("observations") or [],
            mesh_summary=signature.get("mesh_summary") or {},
            mesh_quality=signature.get("mesh_quality") or {},
            residual_settings=signature.get("residual_settings") or {},
            reports=reports,
            solver_settings=signature.get("solver_readbacks") or {},
            unknown_fields=unknown,
            unverified_fields=unverified,
            verification=VerificationStatus.UNVERIFIED if unknown or unverified else VerificationStatus.VERIFIED,
        )

    async def inspect(self, case_file: str, endpoint: str, connect_kwargs: dict) -> InspectionResult:
        async with self._lock:
            snapshot = await self.source.check_state(case_file, endpoint, connect_kwargs)
            if snapshot.verification != VerificationStatus.VERIFIED:
                raise InspectionUnavailable(snapshot.reason or "State consistency is not verified")
            model_id = self._store_id(snapshot.case_fingerprint, endpoint)
            current = self._current
            if current is None or current.case_fingerprint != snapshot.case_fingerprint:
                current = self._load(model_id)
            if current and current.dependency_versions == self._dependencies():
                old_structural = {k: v for k, v in current.state_digest.items() if k != "local"}
                if old_structural == snapshot.structural_digest:
                    if current.state_digest.get("local") == snapshot.local_digest:
                        self._current = current
                        return InspectionResult(current, InspectionMode.CACHE_HIT, "Case and read-only state unchanged")
                    updates = snapshot.local_updates
                    if updates and set(updates).issubset(_PARTIAL_FIELDS):
                        data = current.model_dump(mode="python")
                        data.update(updates)
                        data["profile_version"] = "ps-" + uuid4().hex
                        data["generated_at"] = datetime.now(timezone.utc)
                        data["state_digest"] = {**snapshot.structural_digest, "local": snapshot.local_digest}
                        refreshed = ModelProfile.model_validate(data)
                        self._save(model_id, refreshed)
                        self._current = refreshed
                        return InspectionResult(refreshed, InspectionMode.PARTIAL_REFRESH, "Read-only local fields changed")
            audit = self.audit_root / "scans" / uuid4().hex
            signature = await self.source.read_full(case_file, endpoint, connect_kwargs, audit)
            if not isinstance(signature, dict):
                raise InspectionUnavailable("Full introspection did not return a signature")
            after = await self.source.check_state(case_file, endpoint, connect_kwargs)
            if after.verification != VerificationStatus.VERIFIED or after != snapshot:
                raise InspectionUnavailable("Case state changed during full introspection")
            profile = self._from_signature(signature, snapshot)
            self._save(model_id, profile)
            self._current = profile
            return InspectionResult(profile, InspectionMode.FULL_REINTROSPECTION, "No safe reusable profile")
