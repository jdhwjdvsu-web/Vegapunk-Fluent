"""One-worker asynchronous Job service used by the Windows MCP facade."""

from __future__ import annotations

import asyncio
import inspect
from collections import Counter
from pathlib import Path
from typing import Any, Protocol

from vegapunk.fluent.resources import SingleWorkerResourceGuard
from vegapunk.fluent.runner import FluentStateUncertainError

from .job_store import JobSpec, JobState, JobStore, TERMINAL_JOB_STATES


class JobExecutor(Protocol):
    async def __call__(self, spec: JobSpec, transcript: Path) -> dict[str, Any]: ...


class SingleWorkerJobService:
    """Persist first, return ``job_id`` immediately, and execute one Job at a time."""

    def __init__(
        self,
        store: JobStore,
        executor: JobExecutor,
        *,
        resource_guard: SingleWorkerResourceGuard | None = None,
    ):
        self.store = store
        self.executor = executor
        self.resource_guard = resource_guard
        self._worker_lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started_jobs: Counter[str] = Counter()
        self._campaign_started_at: dict[str, float] = {}
        self.orphaned_on_startup = self.store.recover_orphaned()

    async def submit_job(self, raw_spec: dict[str, Any]) -> dict[str, Any]:
        spec = JobSpec.from_dict(raw_spec)
        document, created = self.store.create_or_get(spec)
        state = JobState(document["state"])
        if created:
            self._tasks[spec.job_id] = asyncio.create_task(self._run_job(spec))
        return {
            "job_id": spec.job_id,
            "state": state.value,
            "created": created,
            "idempotent": not created,
        }

    async def _run_job(self, spec: JobSpec) -> None:
        try:
            async with self._worker_lock:
                if JobState(self.store.load(spec.job_id)["state"]) == JobState.CANCELLED:
                    return
                if self.resource_guard is not None:
                    started_at = self._campaign_started_at.setdefault(
                        spec.campaign_id, self.resource_guard.timestamp()
                    )
                    self.resource_guard.check(
                        self._started_jobs[spec.campaign_id],
                        campaign_started_at=started_at,
                    )
                self._started_jobs[spec.campaign_id] += 1
                self.store.transition(spec.job_id, JobState.RUNNING)
                try:
                    result = await asyncio.wait_for(
                        self.executor(spec, self.store.transcript_path(spec.job_id)),
                        timeout=spec.timeout,
                    )
                except TimeoutError:
                    self.store.transition(
                        spec.job_id,
                        JobState.ORPHANED,
                        error=f"Job exceeded timeout of {spec.timeout:g} seconds",
                        retry_safe=False,
                    )
                    return
                self.store.save_result(spec.job_id, result)
                self.store.transition(spec.job_id, JobState.SUCCEEDED)
        except asyncio.CancelledError:
            current = JobState(self.store.load(spec.job_id)["state"])
            if current not in TERMINAL_JOB_STATES:
                self.store.transition(spec.job_id, JobState.CANCELLED)
            raise
        except FluentStateUncertainError as exc:
            current = JobState(self.store.load(spec.job_id)["state"])
            if current not in TERMINAL_JOB_STATES:
                self.store.transition(
                    spec.job_id,
                    JobState.ORPHANED,
                    error=str(exc),
                    retry_safe=False,
                )
        except Exception as exc:  # noqa: BLE001 - persist the worker failure
            current = JobState(self.store.load(spec.job_id)["state"])
            if current not in TERMINAL_JOB_STATES:
                self.store.transition(
                    spec.job_id, JobState.FAILED, error=str(exc), retry_safe=True
                )
        finally:
            self._tasks.pop(spec.job_id, None)

    def job_status(self, job_id: str) -> dict[str, Any]:
        return self.store.load(job_id)

    def job_result(self, job_id: str) -> dict[str, Any]:
        document = self.store.load(job_id)
        state = JobState(document["state"])
        return {
            "job_id": job_id,
            "state": state.value,
            "result": self.store.result(job_id),
            "error": document.get("error"),
        }

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        document = self.store.load(job_id)
        state = JobState(document["state"])
        if state in TERMINAL_JOB_STATES:
            return {
                "job_id": job_id,
                "cancelled": state == JobState.CANCELLED,
                "state": state.value,
            }
        task = self._tasks.get(job_id)
        if state == JobState.CREATED and task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if JobState(self.store.load(job_id)["state"]) != JobState.CANCELLED:
                self.store.transition(job_id, JobState.CANCELLED)
            return {"job_id": job_id, "cancelled": True, "state": "CANCELLED"}

        cancel = getattr(self.executor, "cancel", None)
        confirmed = False
        if cancel is not None:
            outcome = cancel(job_id)
            confirmed = bool(await outcome) if inspect.isawaitable(outcome) else bool(outcome)
        if not confirmed:
            return {
                "job_id": job_id,
                "cancelled": False,
                "state": state.value,
                "reason": "worker stop was not confirmed; Job remains active",
            }
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if JobState(self.store.load(job_id)["state"]) != JobState.CANCELLED:
            self.store.transition(job_id, JobState.CANCELLED)
        return {"job_id": job_id, "cancelled": True, "state": "CANCELLED"}

    def worker_health(self) -> dict[str, Any]:
        active = [
            job_id
            for job_id, task in self._tasks.items()
            if not task.done()
            and JobState(self.store.load(job_id)["state"]) == JobState.RUNNING
        ]
        queued = [
            job_id
            for job_id, task in self._tasks.items()
            if not task.done()
            and JobState(self.store.load(job_id)["state"]) == JobState.CREATED
        ]
        return {
            "healthy": True,
            "max_workers": 1,
            "active_job_id": active[0] if active else None,
            "queued_jobs": queued,
            "counts": self.store.counts(),
            "orphaned_on_startup": self.orphaned_on_startup,
        }
