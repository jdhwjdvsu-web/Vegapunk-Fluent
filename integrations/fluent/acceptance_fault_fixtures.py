"""Run the four non-process fault probes from the fixed V1 acceptance plan."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path

from integrations.fluent.job_service import SingleWorkerJobService
from integrations.fluent.job_store import JobStore
from vegapunk.fluent.campaign import baseline_fingerprint
from vegapunk.fluent.history import atomic_json
from vegapunk.fluent.spec import load_experiment_spec
from vegapunk.fluent.validity import evaluate_result_gate, validate_parameter_values


async def _timeout_probe(spec, root: Path) -> dict:
    async def delayed_executor(job, transcript):
        del job, transcript
        await asyncio.sleep(0.25)
        return {"unexpected": True}

    store = JobStore(root)
    service = SingleWorkerJobService(store, delayed_executor)
    raw = {
        "campaign_id": "autofluentacceptancefaults-v01",
        "trial_id": "trial-timeout",
        "attempt_id": 0,
        "idempotency_key": "autofluentacceptancefaults-v01:trial-timeout:0",
        "session_id": "autofluentacceptancefaults-v01",
        "parameters": {spec.parameters[0].name: 0.75},
        "baseline_fingerprint": baseline_fingerprint(spec),
        "timeout": 0.05,
        "experiment_spec": spec.to_dict(),
    }
    submitted = await service.submit_job(raw)
    await asyncio.sleep(0.15)
    return {"submitted": submitted, "status": service.job_status(submitted["job_id"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--normal-result", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)

    spec = load_experiment_spec(args.spec)
    root = Path(args.output_dir)
    normal_document = json.loads(Path(args.normal_result).read_text(encoding="utf-8"))
    normal = normal_document.get("result", normal_document)

    invalid_value = spec.parameters[0].maximum + 0.1
    normalized, parameter_gate = validate_parameter_values(
        spec, {spec.parameters[0].name: invalid_value}
    )
    assert normalized is None and parameter_gate.status == "PARAMETER"

    mass_failure = copy.deepcopy(normal)
    mass_failure["mass_flow_out"] = float(mass_failure["mass_flow_in"]) * 1.2
    mass_gate = evaluate_result_gate(spec, mass_failure)
    assert mass_gate.status == "DIVERGED"

    divergence = copy.deepcopy(normal)
    divergence["status"] = "solver_error"
    divergence_gate = evaluate_result_gate(spec, divergence)
    assert divergence_gate.status == "DIVERGED"

    timeout = asyncio.run(_timeout_probe(spec, root / "trial-timeout-job-store"))
    assert timeout["status"]["state"] == "ORPHANED"
    assert timeout["status"]["retry_safe"] is False

    atomic_json(
        root / "logical_faults.json",
        {
            "parameter-out-of-range": {
                "submitted_to_mcp": False,
                "gate": parameter_gate.to_dict(),
            },
            "mass-balance-failure": {
                "fixture": {
                    "mass_flow_in": mass_failure["mass_flow_in"],
                    "mass_flow_out": mass_failure["mass_flow_out"],
                },
                "gate": mass_gate.to_dict(),
            },
            "numerical-divergence": {
                "fixture": {"status": divergence["status"]},
                "gate": divergence_gate.to_dict(),
            },
            "trial-timeout": timeout,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
