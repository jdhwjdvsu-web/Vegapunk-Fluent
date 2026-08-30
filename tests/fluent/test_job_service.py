import asyncio

import pytest

from integrations.fluent.job_service import SingleWorkerJobService
from integrations.fluent.job_store import JobSpec, JobState, JobStore


def job_spec(trial_id="trial-1", attempt_id=0):
    return {
        "campaign_id": "campaign-a",
        "trial_id": trial_id,
        "attempt_id": attempt_id,
        "idempotency_key": f"campaign-a:{trial_id}:{attempt_id}",
        "session_id": "session-a",
        "parameters": {"velocity": 1.0},
        "baseline_fingerprint": "abc123",
        "timeout": 2,
    }


class RecordingExecutor:
    def __init__(self):
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def __call__(self, spec, transcript):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return {"trial_id": spec.trial_id, "objective_value": 1.0}


async def wait_terminal(service, *job_ids):
    for _ in range(100):
        states = [service.job_status(job_id)["state"] for job_id in job_ids]
        if all(state in {"SUCCEEDED", "FAILED", "CANCELLED", "ORPHANED"} for state in states):
            return states
        await asyncio.sleep(0.01)
    raise AssertionError("Jobs did not reach a terminal state")


def test_job_service_is_idempotent_persistent_and_strictly_serial(tmp_path):
    async def scenario():
        executor = RecordingExecutor()
        service = SingleWorkerJobService(JobStore(tmp_path), executor)
        first = await service.submit_job(job_spec("trial-1"))
        duplicate = await service.submit_job(job_spec("trial-1"))
        second = await service.submit_job(job_spec("trial-2"))
        assert duplicate["job_id"] == first["job_id"]
        assert duplicate["idempotent"] is True
        await wait_terminal(service, first["job_id"], second["job_id"])
        assert executor.calls == 2
        assert executor.max_active == 1
        assert service.job_result(first["job_id"])["result"]["objective_value"] == 1.0
        job_dir = tmp_path / "jobs" / first["job_id"]
        assert (job_dir / "job.json").exists()
        assert (job_dir / "events.jsonl").exists()
        assert (job_dir / "result.json").exists()

    asyncio.run(scenario())


def test_same_trial_attempt_with_different_payload_is_rejected(tmp_path):
    store = JobStore(tmp_path)
    store.create_or_get(JobSpec.from_dict(job_spec()))
    changed = job_spec()
    changed["parameters"] = {"velocity": 1.5}
    with pytest.raises(ValueError, match="different payload"):
        store.create_or_get(JobSpec.from_dict(changed))


def test_restart_marks_nonterminal_jobs_orphaned(tmp_path):
    store = JobStore(tmp_path)
    document, _ = store.create_or_get(JobSpec.from_dict(job_spec()))
    store.transition(document["job_id"], JobState.RUNNING)
    recovered = JobStore(tmp_path)
    assert recovered.recover_orphaned() == [document["job_id"]]
    assert recovered.load(document["job_id"])["state"] == "ORPHANED"
