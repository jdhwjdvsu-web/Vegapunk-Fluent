"""Crash-safe JSON persistence and the V1 trial state ledger."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    """Write one complete JSON document and atomically replace the old file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class TrialState(StrEnum):
    CREATED = "CREATED"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    RESULT_READY = "RESULT_READY"
    GATED = "GATED"
    TOLD = "TOLD"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ORPHANED = "ORPHANED"


_ALLOWED_TRANSITIONS = {
    TrialState.CREATED: {TrialState.SUBMITTED, TrialState.FAILED},
    TrialState.SUBMITTED: {
        TrialState.RUNNING,
        TrialState.RESULT_READY,
        TrialState.RETRYING,
        TrialState.FAILED,
        TrialState.CANCELLED,
        TrialState.ORPHANED,
    },
    TrialState.RUNNING: {
        TrialState.RESULT_READY,
        TrialState.RETRYING,
        TrialState.FAILED,
        TrialState.CANCELLED,
        TrialState.ORPHANED,
    },
    TrialState.RETRYING: {TrialState.SUBMITTED, TrialState.FAILED},
    TrialState.RESULT_READY: {TrialState.GATED, TrialState.FAILED},
    TrialState.GATED: {TrialState.TOLD},
    TrialState.ORPHANED: {TrialState.RETRYING, TrialState.FAILED},
    TrialState.FAILED: set(),
    TrialState.CANCELLED: set(),
    TrialState.TOLD: set(),
}


class TrialLedger:
    """One crash-safe record per Campaign Trial.

    The ledger intentionally stores both the current state and an append-only event
    stream.  The state file is convenient for restart logic; the event stream is the
    audit trail.
    """

    def __init__(self, root: str | Path, campaign_id: str, trial_id: str):
        self.root = Path(root)
        safe_trial = trial_id.replace("/", "-").replace("\\", "-")
        self.path = self.root / "trial_ledger" / campaign_id / f"{safe_trial}.json"
        self.events_path = self.root / "trial_ledger" / campaign_id / "events.jsonl"
        self.campaign_id = campaign_id
        self.trial_id = trial_id

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        return json.loads(self.path.read_text(encoding="utf-8"))

    def create(self, parameters: dict[str, float]) -> dict[str, Any]:
        existing = self.load()
        if existing is not None:
            if existing.get("parameters") != parameters:
                raise ValueError("trial_id already exists with different parameters")
            return existing
        now = utc_now()
        document = {
            "schema_version": 1,
            "campaign_id": self.campaign_id,
            "trial_id": self.trial_id,
            "state": TrialState.CREATED.value,
            "attempt": 0,
            "job_id": None,
            "parameters": parameters,
            "result": None,
            "gate": None,
            "created_at": now,
            "updated_at": now,
        }
        atomic_json(self.path, document)
        append_jsonl(
            self.events_path,
            {
                "timestamp": now,
                "campaign_id": self.campaign_id,
                "trial_id": self.trial_id,
                "from": None,
                "to": TrialState.CREATED.value,
            },
        )
        return document

    def transition(self, state: TrialState, **updates: Any) -> dict[str, Any]:
        document = self.load()
        if document is None:
            raise RuntimeError("trial ledger must be created before transition")
        current = TrialState(document["state"])
        if current != state and state not in _ALLOWED_TRANSITIONS[current]:
            raise ValueError(f"invalid trial transition: {current.value} -> {state.value}")
        now = utc_now()
        document.update(updates)
        document["state"] = state.value
        document["updated_at"] = now
        atomic_json(self.path, document)
        append_jsonl(
            self.events_path,
            {
                "timestamp": now,
                "campaign_id": self.campaign_id,
                "trial_id": self.trial_id,
                "from": current.value,
                "to": state.value,
                **updates,
            },
        )
        return document
