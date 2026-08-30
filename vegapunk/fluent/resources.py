"""Minimal single-worker resource guard for unattended Fluent Campaigns."""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class ResourceUnavailableError(RuntimeError):
    """Raised before a Trial starts when its resource budget is unavailable."""


@dataclass(frozen=True)
class ResourcePolicy:
    max_workers: int = 1
    minimum_free_disk_gb: float = 2.0
    minimum_available_memory_gb: float = 2.0
    trial_timeout_seconds: float = 3600.0
    campaign_timeout_seconds: float = 86400.0
    trial_budget: int = 50

    def __post_init__(self) -> None:
        if self.max_workers != 1:
            raise ValueError("V1 requires max_workers=1")
        if self.minimum_free_disk_gb < 0 or self.minimum_available_memory_gb < 0:
            raise ValueError("resource minimums cannot be negative")
        if self.trial_timeout_seconds <= 0 or self.campaign_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if self.trial_budget < 1:
            raise ValueError("trial_budget must be at least one")


class SingleWorkerResourceGuard:
    """Check license, memory, disk, elapsed time, and Trial budget.

    This is intentionally not a scheduler.  It is a fail-fast protection layer for
    exactly one Fluent worker.
    """

    def __init__(
        self,
        root: str | Path,
        policy: ResourcePolicy,
        *,
        license_check: Callable[[], bool] | None = None,
        memory_available_bytes: Callable[[], int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.root = Path(root)
        self.policy = policy
        self.license_check = license_check or (lambda: True)
        self.memory_available_bytes = memory_available_bytes or _available_memory
        self.monotonic = monotonic

    def timestamp(self) -> float:
        return self.monotonic()

    def check(
        self, completed_or_started_trials: int, *, campaign_started_at: float
    ) -> None:
        if completed_or_started_trials >= self.policy.trial_budget:
            raise ResourceUnavailableError("Campaign Trial budget exhausted")
        if (
            self.monotonic() - campaign_started_at
            > self.policy.campaign_timeout_seconds
        ):
            raise ResourceUnavailableError("Campaign time budget exhausted")
        self.root.mkdir(parents=True, exist_ok=True)
        free_disk = shutil.disk_usage(self.root).free
        required_disk = self.policy.minimum_free_disk_gb * 1024**3
        if free_disk < required_disk:
            raise ResourceUnavailableError(
                f"free disk {free_disk / 1024**3:.2f} GiB is below "
                f"{self.policy.minimum_free_disk_gb:.2f} GiB"
            )
        available_memory = self.memory_available_bytes()
        required_memory = self.policy.minimum_available_memory_gb * 1024**3
        if available_memory < required_memory:
            raise ResourceUnavailableError(
                f"available memory {available_memory / 1024**3:.2f} GiB is below "
                f"{self.policy.minimum_available_memory_gb:.2f} GiB"
            )
        if not self.license_check():
            raise ResourceUnavailableError("Fluent license is not available")


def _available_memory() -> int:
    try:
        import psutil
    except ImportError:
        return 2**63 - 1
    return int(psutil.virtual_memory().available)
