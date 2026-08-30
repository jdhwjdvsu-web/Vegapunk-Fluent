"""Deterministic Job executor backed by the existing PyFluent-MCP service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vegapunk.fluent.campaign import baseline_fingerprint, verify_baseline_file
from vegapunk.fluent.runner import FluentExperimentRunner
from vegapunk.fluent.spec import ExperimentSpec

from .job_store import JobSpec


class FluentJobExecutor:
    """Execute one validated point; arbitrary Agent-authored code is never accepted."""

    async def __call__(self, job: JobSpec, transcript: Path) -> dict[str, Any]:
        if not job.experiment_spec:
            raise ValueError("job_spec.experiment_spec is required by the Fluent worker")
        spec = ExperimentSpec.from_dict(job.experiment_spec)
        verify_baseline_file(spec)
        actual_baseline = baseline_fingerprint(spec)
        if actual_baseline != job.baseline_fingerprint:
            raise ValueError("baseline_fingerprint does not match experiment_spec")
        output_dir = transcript.parent
        runner = FluentExperimentRunner(spec, output_dir)
        warning = None
        await runner.open_session()
        try:
            result = await runner.evaluate_point(
                job.parameters,
                name=job.trial_id,
                artifact_stem=f"{job.trial_id}-attempt-{job.attempt_id}",
            )
        finally:
            warning = await runner.close_session()
        audit = {
            "campaign_id": job.campaign_id,
            "trial_id": job.trial_id,
            "attempt_id": job.attempt_id,
            "baseline_fingerprint": actual_baseline,
            "disconnect_warning": warning,
            "result": result,
        }
        transcript.write_text(
            json.dumps(audit, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        return result

    async def cancel(self, job_id: str) -> bool:
        """Refuse to claim success until PyFluent exposes verified solver stop.

        Cancelling a blocking ``run_code`` request can leave Fluent running.  Returning
        ``False`` preserves the V1 safety contract instead of reporting a false stop.
        """

        del job_id
        return False
