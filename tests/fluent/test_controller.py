import asyncio
from dataclasses import asdict, replace

import pytest

from vegapunk.fluent.controller import FluentJobController
from vegapunk.fluent.history import TrialLedger
from vegapunk.fluent.runner import FluentStateUncertainError
from vegapunk.fluent.spec import ConnectionSpec

from .test_validity import optimization_spec


class FakeJobClient:
    def __init__(self):
        self.submissions = []

    async def open_session(self):
        return None

    async def submit_point(self, job_spec):
        self.submissions.append(job_spec)
        return {"job_id": f"job-{job_spec['attempt_id']}"}

    async def job_status(self, job_id):
        if job_id == "job-0":
            return {
                "job_id": job_id,
                "state": "ORPHANED",
                "error": "restart",
                "retry_safe": True,
            }
        return {"job_id": job_id, "state": "SUCCEEDED", "error": None}

    async def job_result(self, job_id):
        return {
            "job_id": job_id,
            "state": "SUCCEEDED",
            "result": {
                "status": "completed",
                "objective_value": 295.0,
                "mass_flow_in": 0.004,
                "mass_flow_out": 0.004,
                "reports": {},
                "constraints": [],
            },
        }

    async def cancel_job(self, job_id):
        return {"job_id": job_id, "cancelled": False}

    async def close_session(self):
        return None


def test_controller_retries_orphaned_job_and_persists_told_state(tmp_path):
    spec = optimization_spec()
    connection = ConnectionSpec.from_dict(
        {
            **asdict(spec.connection),
            "job_endpoint": "http://127.0.0.1:18001/mcp",
        }
    )
    spec = replace(spec, connection=connection)

    async def scenario():
        controller = FluentJobController(spec, tmp_path)
        fake = FakeJobClient()
        controller.client = fake
        await controller.open_session()
        result = await controller.evaluate_point(
            {"velocity": 1.0}, name="trial-0000"
        )
        assert result["objective_value"] == 295.0
        assert [item["attempt_id"] for item in fake.submissions] == [0, 1]
        controller.mark_gated("trial-0000", {"status": "PASS"})
        controller.mark_told("trial-0000")
        ledger = TrialLedger(
            tmp_path, controller.manifest.campaign_id, "trial-0000"
        ).load()
        assert ledger["state"] == "TOLD"
        await controller.close_session()

    asyncio.run(scenario())


def test_controller_refuses_unsafe_orphan_retry(tmp_path):
    spec = optimization_spec()
    connection = ConnectionSpec.from_dict(
        {
            **asdict(spec.connection),
            "job_endpoint": "http://127.0.0.1:18001/mcp",
        }
    )
    spec = replace(spec, connection=connection)

    class UnsafeOrphanClient(FakeJobClient):
        async def job_status(self, job_id):
            return {
                "job_id": job_id,
                "state": "ORPHANED",
                "error": "stop not confirmed",
                "retry_safe": False,
            }

    async def scenario():
        controller = FluentJobController(spec, tmp_path)
        fake = UnsafeOrphanClient()
        controller.client = fake
        await controller.open_session()
        with pytest.raises(FluentStateUncertainError, match="manual recovery"):
            await controller.evaluate_point({"velocity": 1.0}, name="trial-0000")
        assert len(fake.submissions) == 1

    asyncio.run(scenario())
