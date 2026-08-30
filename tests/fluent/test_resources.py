import pytest

from vegapunk.fluent.resources import (
    ResourcePolicy,
    ResourceUnavailableError,
    SingleWorkerResourceGuard,
)


def test_resource_guard_enforces_single_worker_budget_and_license(tmp_path):
    policy = ResourcePolicy(
        max_workers=1,
        minimum_free_disk_gb=0,
        minimum_available_memory_gb=0,
        campaign_timeout_seconds=10,
        trial_budget=1,
    )
    guard = SingleWorkerResourceGuard(
        tmp_path,
        policy,
        license_check=lambda: True,
        memory_available_bytes=lambda: 1024**3,
        monotonic=lambda: 1.0,
    )
    started = guard.timestamp()
    guard.check(0, campaign_started_at=started)
    with pytest.raises(ResourceUnavailableError, match="budget"):
        guard.check(1, campaign_started_at=started)


def test_resource_guard_rejects_unavailable_license(tmp_path):
    guard = SingleWorkerResourceGuard(
        tmp_path,
        ResourcePolicy(minimum_free_disk_gb=0, minimum_available_memory_gb=0),
        license_check=lambda: False,
        monotonic=lambda: 1.0,
    )
    with pytest.raises(ResourceUnavailableError, match="license"):
        guard.check(0, campaign_started_at=guard.timestamp())
