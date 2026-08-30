"""Crash-safe, idempotent Windows-side Fluent Job persistence."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from vegapunk.fluent.history import append_jsonl, atomic_json, utc_now


class JobState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ORPHANED = "ORPHANED"


TERMINAL_JOB_STATES = {
    JobState.SUCCEEDED,
    JobState.FAILED,
    JobState.CANCELLED,
    JobState.ORPHANED,
}


@dataclass(frozen=True)
class JobSpec:
    campaign_id: str
    trial_id: str
    attempt_id: int
    idempotency_key: str
    session_id: str
    parameters: dict[str, float]
    baseline_fingerprint: str
    timeout: float
    experiment_spec: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> JobSpec:
        strings = {}
        for key in (
            "campaign_id",
            "trial_id",
            "idempotency_key",
            "session_id",
            "baseline_fingerprint",
        ):
            value = raw.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"job_spec.{key} must be a non-empty string")
            strings[key] = value.strip()
        attempt = int(raw.get("attempt_id", -1))
        if attempt < 0:
            raise ValueError("job_spec.attempt_id must be zero or greater")
        timeout = float(raw.get("timeout", 0))
        if timeout <= 0:
            raise ValueError("job_spec.timeout must be positive")
        parameters_raw = raw.get("parameters")
        if not isinstance(parameters_raw, dict) or not parameters_raw:
            raise ValueError("job_spec.parameters must be a non-empty object")
        parameters = {}
        for name, value in parameters_raw.items():
            if isinstance(value, bool):
                raise ValueError(f"job_spec.parameters.{name} must be numeric")
            parameters[str(name)] = float(value)
        experiment_spec = raw.get("experiment_spec", {})
        if not isinstance(experiment_spec, dict):
            raise ValueError("job_spec.experiment_spec must be an object")
        return cls(
            campaign_id=strings["campaign_id"],
            trial_id=strings["trial_id"],
            attempt_id=attempt,
            idempotency_key=strings["idempotency_key"],
            session_id=strings["session_id"],
            parameters=parameters,
            baseline_fingerprint=strings["baseline_fingerprint"],
            timeout=timeout,
            experiment_spec=experiment_spec,
        )

    @property
    def identity(self) -> str:
        return f"{self.campaign_id}:{self.trial_id}:{self.attempt_id}"

    @property
    def payload_sha256(self) -> str:
        canonical = json.dumps(
            asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @property
    def job_id(self) -> str:
        digest = hashlib.sha256(self.identity.encode("utf-8")).hexdigest()[:20]
        return f"job-{digest}"


class JobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.jobs_root = self.root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def create_or_get(self, spec: JobSpec) -> tuple[dict[str, Any], bool]:
        """Create a Job or return the exact previous Job for an idempotent submit."""

        with self._lock:
            path = self.job_dir(spec.job_id) / "job.json"
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                if existing["identity"] != spec.identity:
                    raise RuntimeError("deterministic Job id collision")
                if existing["payload_sha256"] != spec.payload_sha256:
                    raise ValueError(
                        "the same campaign_id + trial_id + attempt_id was submitted "
                        "with a different payload"
                    )
                return existing, False
            now = utc_now()
            document = {
                "schema_version": 1,
                "job_id": spec.job_id,
                "identity": spec.identity,
                "payload_sha256": spec.payload_sha256,
                "state": JobState.CREATED.value,
                "job_spec": asdict(spec),
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
                "error": None,
                "retry_safe": False,
            }
            atomic_json(path, document)
            self.append_event(spec.job_id, "job_created", state=JobState.CREATED.value)
            return document, True

    def load(self, job_id: str) -> dict[str, Any]:
        path = self.job_dir(job_id) / "job.json"
        if not path.exists():
            raise KeyError(f"unknown job_id: {job_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def transition(
        self,
        job_id: str,
        state: JobState,
        *,
        error: str | None = None,
        retry_safe: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            document = self.load(job_id)
            current = JobState(document["state"])
            if current in TERMINAL_JOB_STATES and current != state:
                raise ValueError(
                    f"terminal Job cannot transition: {current.value} -> {state.value}"
                )
            now = utc_now()
            document["state"] = state.value
            document["updated_at"] = now
            document["error"] = error
            if retry_safe is not None:
                document["retry_safe"] = retry_safe
            if state == JobState.RUNNING and document["started_at"] is None:
                document["started_at"] = now
            if state in TERMINAL_JOB_STATES:
                document["finished_at"] = now
            atomic_json(self.job_dir(job_id) / "job.json", document)
            self.append_event(
                job_id,
                "job_state_changed",
                previous_state=current.value,
                state=state.value,
                error=error,
                retry_safe=document.get("retry_safe", False),
            )
            return document

    def save_result(self, job_id: str, result: dict[str, Any]) -> None:
        atomic_json(self.job_dir(job_id) / "result.json", result)

    def result(self, job_id: str) -> dict[str, Any] | None:
        path = self.job_dir(job_id) / "result.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def append_event(self, job_id: str, event: str, **details: Any) -> None:
        append_jsonl(
            self.job_dir(job_id) / "events.jsonl",
            {"timestamp": utc_now(), "job_id": job_id, "event": event, **details},
        )

    def transcript_path(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "transcript.log"

    def recover_orphaned(self) -> list[str]:
        """Mark work that belonged to a previous server process as ORPHANED."""

        orphaned = []
        for path in sorted(self.jobs_root.glob("job-*/job.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            state = JobState(document["state"])
            if state in {JobState.CREATED, JobState.RUNNING}:
                job_id = document["job_id"]
                self.transition(
                    job_id,
                    JobState.ORPHANED,
                    error="Job service restarted before terminal state was persisted",
                    retry_safe=False,
                )
                orphaned.append(job_id)
        return orphaned

    def counts(self) -> dict[str, int]:
        counts = {state.value: 0 for state in JobState}
        for path in self.jobs_root.glob("job-*/job.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))["state"]
            except (OSError, json.JSONDecodeError, KeyError):
                continue
            counts[state] = counts.get(state, 0) + 1
        return counts
