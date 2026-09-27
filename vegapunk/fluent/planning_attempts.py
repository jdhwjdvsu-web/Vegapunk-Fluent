"""Persistent lifecycle records for background Agent V3 planning attempts."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


ACTIVE_ATTEMPT_STATUSES = {"queued", "running", "cancelling"}
TERMINAL_ATTEMPT_STATUSES = {
    "awaiting_approval",
    "needs_information",
    "human_review_required",
    "geometry_unsupported",
    "timed_out",
    "cancelled",
    "failed",
    "interrupted",
    "stale_response_discarded",
    "completed",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


class PlanningAttemptStore:
    def __init__(self, root: str | Path, *, timeout_seconds: float) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = float(timeout_seconds)
        self.records: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task[Any]] = {}
        self._restore()

    def _path(self, attempt_id: str) -> Path:
        return self.root / f"{attempt_id}.json"

    def _persist(self, record: Mapping[str, Any]) -> None:
        path = self._path(str(record["attempt_id"]))
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(dict(record), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    def _restore(self) -> None:
        for path in sorted(self.root.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or not record.get("attempt_id"):
                continue
            if record.get("status") in ACTIVE_ATTEMPT_STATUSES:
                record.update(
                    status="interrupted",
                    graph_status="interrupted",
                    finished_at=_iso(),
                    error="服务进程重启，本次规划已中断；可以显式重试。",
                )
                self._persist(record)
            self.records[str(record["attempt_id"])] = record

    def create(
        self,
        *,
        task_id: str,
        message: str,
        input_category: str,
        identity: Mapping[str, Any],
        model_id: str | None,
        effective_reasoning: str,
    ) -> dict[str, Any]:
        if self.active_for_task(task_id) is not None:
            raise RuntimeError("当前任务已有活动规划 attempt")
        attempt_id = f"attempt-{uuid.uuid4().hex}"
        now = _now()
        record = {
            "attempt_id": attempt_id,
            "task_id": task_id,
            "status": "queued",
            "graph_status": "parsing",
            "current_node": "task_parser_agent",
            "message": message,
            "input_category": input_category,
            "created_at": _iso(now),
            "started_at": None,
            "finished_at": None,
            "deadline_at": _iso(now + timedelta(seconds=self.timeout_seconds)),
            "timeout_seconds": self.timeout_seconds,
            "model_id": model_id,
            "effective_reasoning": effective_reasoning,
            "identity": dict(identity),
            "cancel_requested": False,
            "error": None,
            "response": None,
        }
        self.records[attempt_id] = record
        self._persist(record)
        return dict(record)

    def update(self, attempt_id: str, **changes: Any) -> dict[str, Any]:
        if attempt_id not in self.records:
            raise KeyError(attempt_id)
        record = self.records[attempt_id]
        record.update(changes)
        self._persist(record)
        return dict(record)

    def attach(self, attempt_id: str, task: asyncio.Task[Any]) -> None:
        self.tasks[attempt_id] = task
        task.add_done_callback(lambda _task: self.tasks.pop(attempt_id, None))

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        record = self.records.get(attempt_id)
        return dict(record) if record else None

    def latest_for_task(self, task_id: str) -> dict[str, Any] | None:
        matches = [record for record in self.records.values() if record.get("task_id") == task_id]
        if not matches:
            return None
        return dict(max(matches, key=lambda item: str(item.get("created_at") or "")))

    def active_for_task(self, task_id: str) -> dict[str, Any] | None:
        matches = [
            record
            for record in self.records.values()
            if record.get("task_id") == task_id and record.get("status") in ACTIVE_ATTEMPT_STATUSES
        ]
        if not matches:
            return None
        return dict(max(matches, key=lambda item: str(item.get("created_at") or "")))

    def is_current(self, task_id: str, attempt_id: str | None) -> bool:
        latest = self.latest_for_task(task_id)
        return bool(
            latest
            and latest.get("attempt_id") == attempt_id
            and latest.get("status") not in {"cancelled", "interrupted"}
        )

    def cancel(self, attempt_id: str) -> dict[str, Any]:
        record = self.records.get(attempt_id)
        if record is None:
            raise KeyError(attempt_id)
        if record.get("status") not in ACTIVE_ATTEMPT_STATUSES:
            return dict(record)
        task = self.tasks.get(attempt_id)
        if task is not None and not task.done():
            task.cancel()
        return self.update(
            attempt_id,
            status="cancelled",
            graph_status="cancelled",
            cancel_requested=True,
            finished_at=_iso(),
            error="本地已取消；若上游不支持取消，其远端状态可能未知。",
        )
