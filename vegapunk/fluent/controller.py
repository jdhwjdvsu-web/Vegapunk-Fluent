"""Heartbeat, retry, timeout, and crash recovery for Job-backed Fluent Trials."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from .campaign import baseline_fingerprint, load_or_create_campaign
from .history import TrialLedger, TrialState
from .job_client import FluentJobClient
from .runner import FluentExperimentError, FluentStateUncertainError
from .spec import ExperimentSpec


_JOB_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "ORPHANED"}


class FluentJobController:
    """Runner-compatible facade that delegates durable work to Windows Job MCP."""

    def __init__(
        self,
        spec: ExperimentSpec,
        output_dir: str | Path,
        client_factory=None,
    ):
        if spec.optimization is None:
            raise ValueError("Job Controller requires an optimization block")
        self.spec = spec
        self.output_dir = Path(output_dir)
        self.manifest = load_or_create_campaign(self.output_dir, spec)
        self.client = FluentJobClient(spec, client_factory=client_factory)

    async def open_session(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        await self.client.open_session()

    async def evaluate_point(
        self,
        parameters: dict[str, float],
        *,
        name: str,
        artifact_stem: str | None = None,
    ) -> dict[str, Any]:
        del artifact_stem
        optimization = self.spec.optimization
        assert optimization is not None
        ledger = TrialLedger(self.output_dir, self.manifest.campaign_id, name)
        document = ledger.create(parameters)
        if document["state"] in {
            TrialState.RESULT_READY.value,
            TrialState.GATED.value,
            TrialState.TOLD.value,
        }:
            result = document.get("result")
            if isinstance(result, dict):
                return result

        attempt = int(document.get("attempt", 0))
        while attempt < optimization.max_attempts_per_trial:
            document = ledger.load() or document
            job_id = document.get("job_id")
            if document["state"] not in {
                TrialState.SUBMITTED.value,
                TrialState.RUNNING.value,
            }:
                job_spec = {
                    "campaign_id": self.manifest.campaign_id,
                    "trial_id": name,
                    "attempt_id": attempt,
                    "idempotency_key": f"{self.manifest.campaign_id}:{name}:{attempt}",
                    "session_id": self.manifest.campaign_id,
                    "parameters": parameters,
                    "baseline_fingerprint": baseline_fingerprint(self.spec),
                    "timeout": optimization.trial_timeout_seconds,
                    "experiment_spec": self.spec.to_dict(),
                }
                submitted = await self.client.submit_point(job_spec)
                job_id = submitted["job_id"]
                ledger.transition(
                    TrialState.SUBMITTED, attempt=attempt, job_id=job_id
                )

            started = time.monotonic()
            while True:
                status = await self.client.job_status(str(job_id))
                job_state = status["state"]
                if job_state == "RUNNING":
                    current = (ledger.load() or {})["state"]
                    if current != TrialState.RUNNING.value:
                        ledger.transition(TrialState.RUNNING)
                if job_state in _JOB_TERMINAL:
                    break
                elapsed = time.monotonic() - started
                if elapsed > optimization.trial_timeout_seconds:
                    cancellation = await self.client.cancel_job(str(job_id))
                    if not cancellation.get("cancelled"):
                        ledger.transition(
                            TrialState.ORPHANED,
                            error="timeout elapsed but Fluent stop was not confirmed",
                        )
                        raise FluentStateUncertainError(
                            "Job timed out and worker stop was not confirmed; refusing "
                            "an automatic retry that could duplicate Fluent work"
                        )
                    job_state = "CANCELLED"
                    break
                await asyncio.sleep(optimization.heartbeat_seconds)

            if job_state == "SUCCEEDED":
                response = await self.client.job_result(str(job_id))
                result = response.get("result")
                if not isinstance(result, dict):
                    raise FluentExperimentError("successful Job has no persisted result")
                ledger.transition(TrialState.RESULT_READY, result=result)
                return result

            if job_state == "ORPHANED" and not status.get("retry_safe", False):
                current = TrialState((ledger.load() or {})["state"])
                if current != TrialState.ORPHANED:
                    ledger.transition(
                        TrialState.ORPHANED,
                        error=status.get("error")
                        or "worker state is uncertain after service interruption",
                    )
                raise FluentStateUncertainError(
                    "Fluent Job is ORPHANED and worker termination was not confirmed; "
                    "manual recovery is required before retry"
                )

            retryable = job_state in {"FAILED", "CANCELLED", "ORPHANED"}
            attempt += 1
            if retryable and attempt < optimization.max_attempts_per_trial:
                current = TrialState((ledger.load() or {})["state"])
                if current not in {TrialState.RETRYING, TrialState.ORPHANED}:
                    if current == TrialState.RUNNING and job_state == "ORPHANED":
                        ledger.transition(TrialState.ORPHANED, error=status.get("error"))
                    ledger.transition(TrialState.RETRYING, attempt=attempt)
                continue
            ledger.transition(
                TrialState.FAILED,
                attempt=attempt,
                error=status.get("error") or f"Job ended as {job_state}",
            )
            raise FluentExperimentError(
                status.get("error") or f"Fluent Job ended as {job_state}"
            )
        raise FluentExperimentError("Fluent Job exhausted its retry budget")

    def mark_gated(self, trial_name: str, gate: dict[str, Any]) -> None:
        ledger = TrialLedger(
            self.output_dir, self.manifest.campaign_id, trial_name
        )
        document = ledger.load()
        if document and document["state"] == TrialState.RESULT_READY.value:
            ledger.transition(TrialState.GATED, gate=gate)

    def mark_told(self, trial_name: str) -> None:
        ledger = TrialLedger(
            self.output_dir, self.manifest.campaign_id, trial_name
        )
        document = ledger.load()
        if document and document["state"] == TrialState.GATED.value:
            ledger.transition(TrialState.TOLD)

    async def close_session(self) -> None:
        await self.client.close_session()
