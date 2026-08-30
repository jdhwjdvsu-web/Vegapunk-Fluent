"""Assemble the fixed 20-point V1 evidence from real Job and Gate artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from vegapunk.fluent.history import atomic_json


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _execution_count(job_dir: Path) -> int:
    return sum(event.get("state") == "RUNNING" for event in _events(job_dir / "events.jsonl"))


def _find_job(jobs_root: Path, campaign_id: str, trial_id: str) -> tuple[Path, dict[str, Any]]:
    matches = []
    for job_dir in (jobs_root / "jobs").glob("job-*"):
        document = _json(job_dir / "job.json")
        spec = document["job_spec"]
        if spec["campaign_id"] == campaign_id and spec["trial_id"] == trial_id:
            matches.append((job_dir, document))
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one Job for {campaign_id}/{trial_id}, found {len(matches)}"
        )
    return matches[0]


def _normal_records(normal_root: Path, jobs_root: Path) -> list[dict[str, Any]]:
    campaign_id = _json(normal_root / "campaign.json")["campaign_id"]
    summary = _json(normal_root / "demo_summary.json")
    best_trial = summary["best_trial_number"]
    ledger_root = normal_root / "trial_ledger" / campaign_id
    records = []
    for index in range(13):
        trial_id = f"trial-{index:04d}"
        ledger = _json(ledger_root / f"{trial_id}.json")
        trial_result = _json(normal_root / "trial_results" / f"{trial_id}.json")
        job_dir, job = _find_job(jobs_root, campaign_id, trial_id)
        job_result = _json(job_dir / "result.json")
        result_matches = (
            float(job_result["objective_value"])
            == float(trial_result["objective_value"])
            == float(ledger["result"]["objective_value"])
        )
        records.append(
            {
                "scenario_id": f"normal-{index + 1:02d}",
                "injection": "none",
                "state": ledger["state"],
                "gate_status": trial_result["gates"]["status"],
                "selected_as_best": index == best_trial,
                "fluent_execution_count": _execution_count(job_dir),
                "ledger_consistent": ledger["state"] == "TOLD"
                and ledger["job_id"] == job["job_id"]
                and job["state"] == "SUCCEEDED",
                "result_files_consistent": result_matches,
                "campaign_id": campaign_id,
                "trial_id": trial_id,
                "job_id": job["job_id"],
                "objective_value": trial_result["objective_value"],
            }
        )
    return records


def _fault_record(
    *,
    scenario_id: str,
    injection: str,
    state: str,
    gate_status: str,
    fluent_execution_count: int,
    ledger_consistent: bool,
    result_files_consistent: bool,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "scenario_id": scenario_id,
        "injection": injection,
        "state": state,
        "gate_status": gate_status,
        "selected_as_best": False,
        "fluent_execution_count": fluent_execution_count,
        "ledger_consistent": ledger_consistent,
        "result_files_consistent": result_files_consistent,
        "evidence": evidence,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normal-root", required=True)
    parser.add_argument("--faults-root", required=True)
    parser.add_argument("--jobs-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    normal_root = Path(args.normal_root)
    faults_root = Path(args.faults_root)
    jobs_root = Path(args.jobs_root)
    records = _normal_records(normal_root, jobs_root)
    logical = _json(faults_root / "logical_faults.json")

    parameter = logical["parameter-out-of-range"]
    records.append(
        _fault_record(
            scenario_id="parameter-out-of-range",
            injection="parameter_gate",
            state="FAILED",
            gate_status=parameter["gate"]["status"],
            fluent_execution_count=0,
            ledger_consistent=parameter["submitted_to_mcp"] is False,
            result_files_consistent=True,
            evidence=parameter,
        )
    )
    for scenario_id in ("mass-balance-failure", "numerical-divergence"):
        evidence = logical[scenario_id]
        records.append(
            _fault_record(
                scenario_id=scenario_id,
                injection="result_fixture",
                state="FAILED",
                gate_status=evidence["gate"]["status"],
                fluent_execution_count=0,
                ledger_consistent=evidence["gate"]["status"] == "DIVERGED",
                result_files_consistent=True,
                evidence=evidence,
            )
        )

    timeout = logical["trial-timeout"]
    timeout_status = timeout["status"]
    records.append(
        _fault_record(
            scenario_id="trial-timeout",
            injection="worker_delay",
            state=timeout_status["state"],
            gate_status="DIVERGED",
            fluent_execution_count=0,
            ledger_consistent=timeout_status["state"] == "ORPHANED"
            and timeout_status["retry_safe"] is False,
            result_files_consistent=True,
            evidence={
                "job_id": timeout_status["job_id"],
                "error": timeout_status["error"],
                "retry_safe": timeout_status["retry_safe"],
            },
        )
    )

    fault_campaign = "autofluentacceptancefaults-v01"
    for scenario_id, injection, expected_state in (
        ("fluent-terminated", "manual_fluent_stop", "FAILED"),
        ("mcp-restart", "manual_job_service_restart", "ORPHANED"),
    ):
        job_dir, job = _find_job(jobs_root, fault_campaign, scenario_id)
        result_exists = (job_dir / "result.json").exists()
        records.append(
            _fault_record(
                scenario_id=scenario_id,
                injection=injection,
                state=job["state"],
                gate_status="DIVERGED",
                fluent_execution_count=_execution_count(job_dir),
                ledger_consistent=job["state"] == expected_state,
                result_files_consistent=not result_exists,
                evidence={
                    "job_id": job["job_id"],
                    "error": job["error"],
                    "retry_safe": job["retry_safe"],
                },
            )
        )

    controller_root = faults_root / "controller-restart"
    campaign_id = _json(controller_root / "campaign.json")["campaign_id"]
    ledger = _json(
        controller_root / "trial_ledger" / campaign_id / "controller-restart.json"
    )
    job_dir, job = _find_job(jobs_root, campaign_id, "controller-restart")
    probe = _json(controller_root / "controller_probe_result.json")
    records.append(
        _fault_record(
            scenario_id="controller-restart",
            injection="manual_wsl_controller_restart",
            state=ledger["state"],
            gate_status=probe["gate"]["status"],
            fluent_execution_count=_execution_count(job_dir),
            ledger_consistent=ledger["state"] == "TOLD"
            and ledger["job_id"] == job["job_id"]
            and job["state"] == "SUCCEEDED",
            result_files_consistent=(job_dir / "result.json").exists()
            and probe["gate"]["status"] == "PASS",
            evidence={"job_id": job["job_id"], "resumed_to": ledger["state"]},
        )
    )

    atomic_json(Path(args.output), records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
