"""Read-only session continuity, including missing or changed MCP identities."""
import asyncio
import json

import pytest

from vegapunk.fluent.runner import FluentExperimentRunner, FluentStateUncertainError
from vegapunk.fluent.session_identity import SessionObservation, continuity_error
from vegapunk.fluent.spec import ExperimentSpec

from .test_runtime_reliability import ContextClient
from .test_spec import minimal_spec


def test_identity_is_not_invented_from_connection_or_pid():
    observation = SessionObservation.from_status({"connected": True, "pid": 123})
    assert observation.fluent_session_id is None
    assert observation.identity_status == "UNAVAILABLE"
    assert continuity_error(None, observation) is None
    conflict = SessionObservation.from_status({"connected": True, "session_id": "a", "fluent_session_id": "b"})
    assert continuity_error(None, conflict)


@pytest.mark.parametrize("new_id", ["replacement-session", None])
def test_changed_or_lost_identity_blocks_before_trial_and_cleanup(tmp_path, new_id):
    raw = minimal_spec()
    raw["connection"]["connect_kwargs"] = {"case_file_name": "C:/offline/fixture.cas.h5"}
    client = ContextClient()
    runner = FluentExperimentRunner(ExperimentSpec.from_dict(raw), tmp_path, client_factory=lambda *_: client)

    async def scenario():
        await runner.open_session()
        client.session_id = new_id
        with pytest.raises(FluentStateUncertainError, match="identity changed"):
            await runner.evaluate_point({"velocity": 0.5}, name="trial-0000")
        return await runner.close_session()

    assert "uncertain" in asyncio.run(scenario())
    assert not any(name in {"run_code", "disconnect"} for name, _ in client.calls)
    assert not list((tmp_path / "generated_code").glob("*.py"))


def test_missing_identity_is_explicitly_audited_not_claimed_confirmed(tmp_path):
    client = ContextClient()
    client.session_id = None
    runner = FluentExperimentRunner(ExperimentSpec.from_dict(minimal_spec()), tmp_path, client_factory=lambda *_: client)

    async def scenario():
        await runner.open_session()
        await runner._check_session("before_trial", "trial-0000")
        await runner.close_session()

    asyncio.run(scenario())
    records = [json.loads(line) for line in (tmp_path / "session_audit.jsonl").read_text(encoding="utf-8").splitlines()]
    assert all(item["identity_status"] == "UNAVAILABLE" for item in records)
    assert all(item["continuity"] == "UNVERIFIED_IDENTITY" for item in records)
    assert sum(name == "connect" for name, _ in client.calls) == 1


def test_cleanup_does_not_disconnect_a_replacement_session(tmp_path):
    client = ContextClient()
    runner = FluentExperimentRunner(ExperimentSpec.from_dict(minimal_spec()), tmp_path, client_factory=lambda *_: client)

    async def scenario():
        await runner.open_session()
        client.session_id = "someone-else"
        return await runner.close_session()

    assert "no exit or disconnect" in asyncio.run(scenario())
    assert not any(name == "disconnect" for name, _ in client.calls)
