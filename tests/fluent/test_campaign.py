import hashlib

import pytest

from vegapunk.fluent.campaign import (
    CampaignMismatchError,
    campaign_fingerprint,
    load_or_create_campaign,
    verify_baseline_file,
)
from vegapunk.fluent.spec import ExperimentSpec

from .test_validity import optimization_spec


def test_campaign_allows_budget_increase_but_rejects_search_space_change(tmp_path):
    original = optimization_spec()
    first = load_or_create_campaign(tmp_path, original)

    raw = original.to_dict()
    raw["optimization"]["target_trials"] = 12
    increased = ExperimentSpec.from_dict(raw)
    resumed = load_or_create_campaign(tmp_path, increased)
    assert resumed.campaign_id == first.campaign_id
    assert campaign_fingerprint(increased) == campaign_fingerprint(original)
    assert resumed.target_trials == 12

    changed_raw = increased.to_dict()
    changed_raw["parameters"][0]["maximum"] = 3.0
    changed = ExperimentSpec.from_dict(changed_raw)
    with pytest.raises(CampaignMismatchError, match="different Campaign"):
        load_or_create_campaign(tmp_path, changed)


def test_campaign_ignores_network_endpoint_changes():
    first = optimization_spec()
    raw = first.to_dict()
    raw["connection"]["endpoint"] = "http://127.0.0.1:19000/mcp"
    second = ExperimentSpec.from_dict(raw)
    assert campaign_fingerprint(first) == campaign_fingerprint(second)


def test_windows_worker_verifies_explicit_baseline_digest(tmp_path):
    case = tmp_path / "baseline.cas.h5"
    case.write_bytes(b"fixed case")
    digest = hashlib.sha256(b"fixed case").hexdigest()
    raw = optimization_spec().to_dict()
    raw["connection"]["connect_kwargs"] = {"case_file_name": str(case)}
    raw["connection"]["baseline_sha256"] = digest
    spec = ExperimentSpec.from_dict(raw)
    verify_baseline_file(spec)

    raw["connection"]["baseline_sha256"] = "0" * 64
    changed = ExperimentSpec.from_dict(raw)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_baseline_file(changed)
