"""Submit and inspect one deterministic Job during real fault-injection tests."""

from __future__ import annotations

import argparse
import asyncio
import json

from vegapunk.fluent.campaign import baseline_fingerprint
from vegapunk.fluent.job_client import FluentJobClient
from vegapunk.fluent.spec import ExperimentSpec, load_experiment_spec


async def _run(args: argparse.Namespace) -> dict:
    spec = load_experiment_spec(args.spec)
    raw_spec = spec.to_dict()
    raw_spec["connection"]["job_endpoint"] = args.job_endpoint
    spec = ExperimentSpec.from_dict(raw_spec)
    client = FluentJobClient(spec)
    await client.open_session()
    try:
        if args.action == "health":
            return await client.health()
        if args.action == "status":
            return await client.job_status(args.job_id)
        if args.action == "result":
            return await client.job_result(args.job_id)
        job_spec = {
            "campaign_id": args.campaign_id,
            "trial_id": args.trial_id,
            "attempt_id": 0,
            "idempotency_key": f"{args.campaign_id}:{args.trial_id}:0",
            "session_id": args.campaign_id,
            "parameters": {spec.parameters[0].name: args.value},
            "baseline_fingerprint": baseline_fingerprint(spec),
            "timeout": args.timeout,
            "experiment_spec": spec.to_dict(),
        }
        return await client.submit_point(job_spec)
    finally:
        await client.close_session()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("submit", "status", "result", "health"))
    parser.add_argument("--spec", required=True)
    parser.add_argument("--job-endpoint", default="http://127.0.0.1:18001/mcp")
    parser.add_argument("--campaign-id")
    parser.add_argument("--trial-id")
    parser.add_argument("--job-id")
    parser.add_argument("--value", type=float, default=0.75)
    parser.add_argument("--timeout", type=float, default=3600)
    args = parser.parse_args(argv)
    if args.action == "submit" and not (args.campaign_id and args.trial_id):
        parser.error("submit requires --campaign-id and --trial-id")
    if args.action in {"status", "result"} and not args.job_id:
        parser.error(f"{args.action} requires --job-id")
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
