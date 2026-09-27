"""Phase 2 lifecycle and strict read-only guarantees, without Fluent."""

from __future__ import annotations

import ast
import asyncio

import pytest

from vegapunk.fluent.model_introspection import build_introspection_code
from vegapunk.fluent.pre_simulation.model_inspector import (
    InspectionMode,
    InspectionUnavailable,
    McpReadOnlyInspectionSource,
    ModelInspector,
    StateConsistencySnapshot,
)
from vegapunk.fluent.pre_simulation.schema import VerificationStatus


def snapshot(case="case-a", mesh="mesh-a", local="local-a", updates=None, status=VerificationStatus.VERIFIED):
    return StateConsistencySnapshot(
        case_fingerprint=case,
        structural_digest={
            "case_file": case, "mesh": mesh, "zones": "zones-a",
            "physics": "physics-a", "materials": "materials-a", "catalog": "catalog-a",
        },
        local_digest=local,
        local_updates=updates,
        verification=status,
        reason="test read-only status",
    )


def signature():
    return {
        "solver_type": "pressure-based",
        "physics": {"energy": True, "viscous_model": "k-omega"},
        "materials": [{"name": "air", "type": "fluid"}],
        "cell_zones": [{"name": "air", "type": "fluid"}, {"name": "sink", "type": "solid"}],
        "boundaries": [{"name": "inlet", "type": "velocity_inlet"}],
        "observations": [{"rule_id": "velocity", "value": 1.0}],
        "reports": [{"name": "temperature_max"}],
        "solver_readbacks": {"pseudo_time_courant_number": 1.0},
        "warnings": ["inlet_direction:UNVERIFIED:read-only probe unavailable"],
    }


class FakeSource:
    def __init__(self):
        self.current = snapshot()
        self.full_calls = 0
        self.state_calls = 0
        self.model = signature()

    async def check_state(self, case_file, endpoint, connect_kwargs):
        self.state_calls += 1
        return self.current

    async def read_full(self, case_file, endpoint, connect_kwargs, audit_dir):
        self.full_calls += 1
        return self.model


def inspect(inspector):
    return asyncio.run(inspector.inspect("case.cas.h5", "http://localhost/mcp", {}))


def test_strict_scanner_has_no_fluent_mutation_or_solve_calls():
    strict_code = build_introspection_code(strict_read_only=True)
    legacy_code = build_introspection_code()
    assert "__vp_momentum.flow_direction =" in legacy_code
    assert "__vp_momentum.flow_direction =" not in strict_code
    assert "UNVERIFIED:direction mode not active" in strict_code
    tree = ast.parse(strict_code)
    forbidden_calls = {"solve", "iterate", "initialize", "set_state"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_calls
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    assert not ast.unparse(target).startswith("solver.")


def test_profile_creation_cache_partial_full_and_fingerprint_invalidation(tmp_path):
    source = FakeSource()
    inspector = ModelInspector(tmp_path, source=source)
    first = inspect(inspector)
    assert first.mode == InspectionMode.FULL_REINTROSPECTION
    assert source.full_calls == 1
    assert first.profile.case_fingerprint == "case-a"
    assert first.profile.profile_version.startswith("ps-")
    assert first.profile.generated_at.tzinfo is not None
    assert first.profile.dependency_versions["scanner"]
    assert first.profile.physical_models["energy"] is True
    assert first.profile.fluid_zones[0]["name"] == "air"
    assert first.profile.solid_zones[0]["name"] == "sink"
    second = inspect(inspector)
    assert second.mode == InspectionMode.CACHE_HIT
    assert second.profile.profile_version == first.profile.profile_version
    assert source.full_calls == 1
    assert source.state_calls >= 3  # checked on entry and after the first full scan

    source.current = snapshot(local="local-b", updates={"solver_settings": {"courant": 2.0}})
    partial = inspect(inspector)
    assert partial.mode == InspectionMode.PARTIAL_REFRESH
    assert partial.profile.profile_version != first.profile.profile_version
    assert partial.profile.solver_settings == {"courant": 2.0}
    assert source.full_calls == 1
    assert first.profile.solver_settings != partial.profile.solver_settings

    source.current = snapshot(mesh="mesh-b", local="local-b")
    structural = inspect(inspector)
    assert structural.mode == InspectionMode.FULL_REINTROSPECTION
    assert source.full_calls == 2
    assert structural.profile.profile_version != partial.profile.profile_version

    source.current = snapshot(case="case-b", mesh="mesh-c", local="local-c")
    replacement = inspect(inspector)
    assert replacement.mode == InspectionMode.FULL_REINTROSPECTION
    assert replacement.profile.case_fingerprint == "case-b"
    assert source.full_calls == 3


def test_unlocatable_local_change_causes_full_refresh_and_unknown_never_reuses(tmp_path):
    source = FakeSource()
    inspector = ModelInspector(tmp_path, source=source)
    first = inspect(inspector)
    source.current = snapshot(local="local-b")
    assert inspect(inspector).mode == InspectionMode.FULL_REINTROSPECTION
    assert source.full_calls == 2
    source.current = snapshot(status=VerificationStatus.UNKNOWN)
    with pytest.raises(InspectionUnavailable, match="read-only status"):
        inspect(inspector)
    assert source.full_calls == 2
    assert first.profile.profile_version != inspector._current.profile_version


def test_incomplete_signature_records_unknowns_without_guessing(tmp_path):
    source = FakeSource()
    source.model = {"solver_type": "pressure-based", "boundaries": [{"name": "inlet"}], "warnings": []}
    result = inspect(ModelInspector(tmp_path, source=source))
    assert "mesh_summary" in result.profile.unknown_fields
    assert "materials" in result.profile.unknown_fields
    assert result.profile.mesh_summary == {}
    assert result.profile.materials == []
    assert result.profile.verification == VerificationStatus.UNVERIFIED


def test_profile_store_reuse_still_runs_consistency_check(tmp_path):
    source = FakeSource()
    first = inspect(ModelInspector(tmp_path, source=source))
    second = inspect(ModelInspector(tmp_path, source=source))
    assert second.mode == InspectionMode.CACHE_HIT
    assert second.profile.profile_version == first.profile.profile_version
    assert source.full_calls == 1
    assert source.state_calls >= 3


def test_partial_refresh_rejects_unapproved_fields_and_rechecks_after_full(tmp_path):
    source = FakeSource()
    inspector = ModelInspector(tmp_path, source=source)
    inspect(inspector)
    source.current = snapshot(local="local-b", updates={"materials": [{"name": "other"}]})
    assert inspect(inspector).mode == InspectionMode.FULL_REINTROSPECTION
    assert source.full_calls == 2


def test_default_source_refuses_unidentified_active_session(tmp_path, monkeypatch):
    case_file = tmp_path / "sample.cas.h5"
    case_file.write_bytes(b"fake case bytes for fingerprint only")
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def call(client, name, args):
        calls.append(name)
        return {"connected": True}

    monkeypatch.setattr("fastmcp.Client", Client)
    monkeypatch.setattr("vegapunk.fluent.direct_run._call", call)
    with pytest.raises(InspectionUnavailable, match="active Fluent session"):
        asyncio.run(McpReadOnlyInspectionSource().check_state(str(case_file), "http://localhost", {}))
    assert calls == ["session_status"]


def test_default_consistency_digest_tracks_paired_data_and_connection_configuration(tmp_path, monkeypatch):
    case_file = tmp_path / "sample.cas.h5"
    data_file = tmp_path / "sample.dat.h5"
    case_file.write_bytes(b"fixture case")
    data_file.write_bytes(b"fixture data v1")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    async def call(*args):
        return {"connected": False}

    monkeypatch.setattr("fastmcp.Client", Client)
    monkeypatch.setattr("vegapunk.fluent.direct_run._call", call)
    source = McpReadOnlyInspectionSource()
    def check(kwargs):
        return asyncio.run(source.check_state(str(case_file), "http://localhost", kwargs))
    first = check({"product_version": "25.1"})
    assert check({"product_version": "25.1"}) == first
    data_file.write_bytes(b"fixture data v2")
    data_changed = check({"product_version": "25.1"})
    assert data_changed.case_fingerprint == first.case_fingerprint
    assert data_changed.structural_digest != first.structural_digest
    assert check({"product_version": "26.1"}).structural_digest != data_changed.structural_digest
    assert "mapping_registry" in ModelInspector._dependencies()
    assert "pyfluent_package" in ModelInspector._dependencies()
