"""Read-only cross-check of Optuna, semantic/native ledgers and result evidence.

Unknown runtime state outranks apparently completed files. Only a complete,
matched gated result permits recovery of tell; reconciliation never solves.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .history import TrialLedger, TrialState, atomic_json, utc_now
from .runtime_reliability import inspect_completed_trial


UNCERTAIN_STATES = {"ORPHANED", "UNKNOWN", "UNKNOWN_RUNTIME_STATE"}


def reconcile_runtime(study, spec, output: Path) -> dict:
    trials = {f"trial-{item.number:04d}": item for item in study.get_trials(deepcopy=False)}
    issues, ledgers, records = [], {}, []
    campaign_id = None
    campaign_path = output / "campaign.json"
    if campaign_path.exists():
        try:
            campaign_id = json.loads(campaign_path.read_text(encoding="utf-8"))["campaign_id"]
        except (OSError, ValueError, KeyError, TypeError):
            issues.append("Campaign identity cannot be read for ledger reconciliation")
    verification_dir = output / "verification"
    verification_path = verification_dir / "result.json"
    if verification_path.exists():
        try:
            verification = json.loads(verification_path.read_text(encoding="utf-8"))
            if not isinstance(verification, dict):
                raise ValueError("invalid verification result")
            if verification.get("runtime_state") in UNCERTAIN_STATES or verification.get("status") in {"orphaned", "blocked"}:
                issues.append("Independent verification has unresolved runtime state; cannot resume Campaign")
        except (OSError, ValueError):
            issues.append("Independent verification result is unreadable; manual diagnosis required")
    elif (verification_dir / "request.json").exists() or (verification_dir / "generated_code" / "best-verification.py").exists():
        issues.append("Independent verification was reserved/submitted without a final result; runtime state UNKNOWN")
    log = output / "run_log.jsonl"
    if log.exists():
        for number, line in enumerate(log.read_text(encoding="utf-8").splitlines(), 1):
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                issues.append(f"Unreadable run event at line {number}")
                continue
            if not isinstance(event, dict):
                issues.append(f"Invalid run event at line {number}")
            elif event.get("event") in {"trial_state_uncertain", "trial_orphaned_on_recovery", "duplicate_trial_blocked"}:
                issues.append("Campaign contains an uncertain/orphaned Trial and cannot be resumed")
    for root in (output, output / "native_execution"):
        for path in sorted((root / "trial_ledger").glob("*/trial-*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                identity = document["trial_id"]
                if identity != path.stem or document.get("campaign_id") != path.parent.name:
                    raise ValueError("ledger identity mismatch")
                if campaign_id and document.get("campaign_id") != campaign_id:
                    raise ValueError("ledger belongs to a different Campaign")
                state = TrialState(document["state"]).value
            except (OSError, ValueError, KeyError, TypeError):
                issues.append(f"Unreadable or invalid ledger: {path}")
                continue
            ledgers.setdefault(identity, []).append({"path": str(path), "state": state,
                                                     "native": root != output, "document": document})
            if identity not in trials:
                issues.append(f"Ledger has no matching Optuna trial: {identity}")
            if state == "ORPHANED":
                issues.append(f"ORPHANED ledger blocks recovery: {identity}")
    for identity, trial in trials.items():
        complete, reason, document = inspect_completed_trial(
            output / "trial_results" / f"{identity}.json", trial.number, trial.params,
            expected_constraints=len(spec.constraints))
        state = trial.state.name
        row_issues = []
        runtime = trial.user_attrs.get("runtime_state")
        if runtime in UNCERTAIN_STATES:
            row_issues.append(f"{identity} has unresolved runtime state {runtime}")
        related = ledgers.get(identity, [])
        for ledger in related:
            if not ledger["native"] and ledger["document"].get("parameters") != trial.params:
                row_issues.append(f"{identity} semantic ledger parameters differ from Optuna")
            if ledger["native"] and complete and document.get("fluent_parameters") != ledger["document"].get("parameters"):
                row_issues.append(f"{identity} native ledger parameters differ from persisted mapping result")
            if ledger["state"] == "TOLD" and state in {"RUNNING", "WAITING"}:
                row_issues.append(f"{identity} ledger TOLD conflicts with Optuna {state}")
            if ledger["state"] in {"FAILED", "CANCELLED", "CREATED", "RETRYING"} and state in {"RUNNING", "COMPLETE"}:
                row_issues.append(f"{identity} ledger {ledger['state']} conflicts with Optuna {state}")
        if state == "COMPLETE":
            if not complete:
                row_issues.append(f"{identity} Optuna COMPLETE lacks trusted result ({reason})")
            elif document["gates"]["status"] not in {"PASS", "CONSTRAINT"}:
                row_issues.append(f"{identity} completed with a nontellable Gate")
            elif (not isinstance(trial.value, (int, float)) or not math.isfinite(trial.value)
                  or trial.value != document["objective_value"]
                  or trial.user_attrs.get("constraint_vector") != document["gates"]["constraint_vector"]):
                row_issues.append(f"{identity} Optuna value/constraint vector differs from gated result")
        if state == "RUNNING" and not complete:
            row_issues.append(f"RUNNING {identity} has no complete gated result ({reason}); Campaign is blocked")
        if state == "FAIL" and any(item["state"] in {"RUNNING", "SUBMITTED", "RETRYING"} for item in related):
            row_issues.append(f"{identity} Optuna FAIL does not confirm native work termination")
        if state == "FAIL" and not complete and any(item["state"] in {"RESULT_READY", "GATED", "TOLD"} for item in related):
            row_issues.append(f"{identity} finalized failure ledger lacks complete Gate evidence")
        if state == "FAIL" and complete and document["gates"]["status"] in {"PASS", "CONSTRAINT"}:
            row_issues.append(f"{identity} failed despite a tellable result; manual reconciliation required")
        issues.extend(row_issues)
        records.append({"trial_id": identity, "optuna_state": state, "runtime_state": runtime,
                        "result_integrity": reason, "ledgers": [{key: item[key] for key in ("path", "state", "native")} for item in related],
                        "action": "BLOCKED" if row_issues or any(item["state"] == "ORPHANED" for item in related)
                        else "RECOVER_TELL_ONLY" if state == "RUNNING" and complete
                        else "SYNC_LEDGER_ONLY" if complete and state in {"COMPLETE", "FAIL"}
                        and any(item["state"] != "TOLD" for item in related) else "NONE"})
    report = {"schema_version": 1, "checked_at": utc_now(), "status": "BLOCKED" if issues else "CONSISTENT",
              "automatic_submission_allowed": not issues, "issues": list(dict.fromkeys(issues)), "trials": records,
              "source_of_truth": "Optuna tell plus matched complete Gate evidence; unresolved runtime state vetoes recovery"}
    atomic_json(output / "runtime_reconciliation.json", report)
    return report


def sync_finalized_ledgers(study, spec, output: Path) -> int:
    """Finish ledger persistence after tell, never change an uncertain state."""
    report = reconcile_runtime(study, spec, output)
    if report["status"] != "CONSISTENT":
        raise RuntimeError("Cannot synchronize inconsistent runtime ledgers")
    synchronized = 0
    for row in report["trials"]:
        if row["optuna_state"] not in {"COMPLETE", "FAIL"} or row["result_integrity"] != "COMPLETE":
            continue
        document = json.loads((output / "trial_results" / f"{row['trial_id']}.json").read_text(encoding="utf-8"))
        for item in row["ledgers"]:
            if item["state"] in {"TOLD", "FAILED", "CANCELLED"}:
                continue
            path = Path(item["path"])
            ledger = TrialLedger(path.parent.parent.parent, path.parent.name, path.stem)
            state = TrialState(item["state"])
            if state in {TrialState.SUBMITTED, TrialState.RUNNING}:
                ledger.transition(TrialState.RESULT_READY, recovered_result_file=str(output / "trial_results" / path.name))
                state = TrialState.RESULT_READY
            if state == TrialState.RESULT_READY:
                ledger.transition(TrialState.GATED, gate=document["gates"])
            ledger.transition(TrialState.TOLD, recovery="PERSISTENCE_ONLY_NO_SOLVE")
            synchronized += 1
    reconcile_runtime(study, spec, output)
    return synchronized
