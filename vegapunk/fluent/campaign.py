"""Campaign identity, inheritance, and immutable scientific fingerprints."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .history import atomic_json, utc_now
from .spec import ExperimentSpec


CAMPAIGN_SCHEMA_VERSION = 1
GATE_VERSION = "v1"


class CampaignMismatchError(RuntimeError):
    """Raised when an output directory is reused for a different experiment."""


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _file_identity(value: str | None, expected_sha256: str | None) -> dict[str, Any]:
    if not value:
        return {"path": None, "sha256": None}
    # The controller and worker run on different OSes.  Never auto-hash a path on
    # only one side; use an explicit digest captured during Stage 0 when available.
    return {"path": value, "sha256": expected_sha256}


def campaign_payload(spec: ExperimentSpec) -> dict[str, Any]:
    """Return only fields that define scientific compatibility.

    Trial budget and endpoint are deliberately excluded, so an interrupted study can
    move between WSL gateway addresses or receive a larger budget without changing
    Campaign identity.
    """

    raw = spec.to_dict()
    connection = dict(raw["connection"])
    endpoint = connection.pop("endpoint", None)
    del endpoint
    connection.pop("client_timeout_seconds", None)
    connection.pop("job_endpoint", None)
    baseline_sha256 = connection.pop("baseline_sha256", None)
    connection.pop("allow_remote_endpoint", None)
    connection.pop("reuse_existing_session", None)
    connection.pop("disconnect_on_exit", None)
    optimization = dict(raw.get("optimization") or {})
    optimization.pop("target_trials", None)
    optimization.pop("study_name", None)
    case_value = connection.get("connect_kwargs", {}).get("case_file_name")
    return {
        "schema_version": CAMPAIGN_SCHEMA_VERSION,
        "task_name": raw["task_name"],
        "baseline": _file_identity(case_value, baseline_sha256),
        "connection_scientific_settings": connection,
        "solver": raw["solver"],
        "parameters": raw["parameters"],
        "parameter_constraints": raw["parameter_constraints"],
        "reports": raw["reports"],
        "objective": raw["objective"],
        "constraints": raw["constraints"],
        "optimization": optimization,
        "gate_version": GATE_VERSION,
        "optuna_version": _package_version("optuna"),
    }


def campaign_fingerprint(spec: ExperimentSpec) -> str:
    canonical = json.dumps(
        campaign_payload(spec), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def baseline_fingerprint(spec: ExperimentSpec) -> str:
    baseline = campaign_payload(spec)["baseline"]
    canonical = json.dumps(
        baseline, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_baseline_file(spec: ExperimentSpec) -> None:
    """Verify the configured case digest on the OS that can read the case file."""

    expected = spec.connection.baseline_sha256
    if expected is None:
        return
    value = spec.connection.connect_kwargs.get("case_file_name")
    if not isinstance(value, str) or not value:
        raise ValueError("case_file_name is required when baseline_sha256 is configured")
    path = Path(value)
    if not path.is_file():
        raise ValueError(f"baseline case is not readable: {value}")
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    actual = hasher.hexdigest()
    if actual != expected:
        raise ValueError(
            f"baseline case SHA-256 mismatch: expected {expected}, got {actual}"
        )


def _slug(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return result[:40] or "campaign"


@dataclass(frozen=True)
class CampaignManifest:
    campaign_id: str
    fingerprint: str
    parent_campaign_id: str | None
    target_trials: int | None
    created_at: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAMPAIGN_SCHEMA_VERSION,
            "campaign_id": self.campaign_id,
            "campaign_fingerprint": self.fingerprint,
            "parent_campaign_id": self.parent_campaign_id,
            "target_trials": self.target_trials,
            "created_at": self.created_at,
            "payload": self.payload,
        }


def load_or_create_campaign(
    output_dir: str | Path,
    spec: ExperimentSpec,
    *,
    parent_campaign_id: str | None = None,
) -> CampaignManifest:
    output = Path(output_dir)
    path = output / "campaign.json"
    fingerprint = campaign_fingerprint(spec)
    target = spec.optimization.target_trials if spec.optimization else None
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        existing = raw.get("campaign_fingerprint")
        if existing != fingerprint:
            raise CampaignMismatchError(
                "the output directory belongs to a different Campaign; create a "
                "child Campaign when the model, objective, search space, constraints, "
                "solver, or Gate changes"
            )
        if target is not None and target > int(raw.get("target_trials") or 0):
            raw["target_trials"] = target
            atomic_json(path, raw)
        return CampaignManifest(
            campaign_id=raw["campaign_id"],
            fingerprint=existing,
            parent_campaign_id=raw.get("parent_campaign_id"),
            target_trials=raw.get("target_trials"),
            created_at=raw["created_at"],
            payload=raw["payload"],
        )
    manifest = CampaignManifest(
        campaign_id=f"{_slug(spec.task_name)}-{fingerprint[:12]}",
        fingerprint=fingerprint,
        parent_campaign_id=parent_campaign_id,
        target_trials=target,
        created_at=utc_now(),
        payload=campaign_payload(spec),
    )
    atomic_json(path, manifest.to_dict())
    return manifest
