"""Resume the same named Trial after a WSL Controller process interruption."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from vegapunk.fluent.controller import FluentJobController
from vegapunk.fluent.history import atomic_json
from vegapunk.fluent.spec import ExperimentSpec, load_experiment_spec
from vegapunk.fluent.validity import evaluate_result_gate


async def _run(args: argparse.Namespace) -> dict:
    spec = load_experiment_spec(args.spec)
    raw_spec = spec.to_dict()
    raw_spec["connection"]["job_endpoint"] = args.job_endpoint
    raw_spec["connection"]["allow_remote_endpoint"] = True
    spec = ExperimentSpec.from_dict(raw_spec)
    controller = FluentJobController(spec, args.output_dir)
    await controller.open_session()
    try:
        result = await controller.evaluate_point(
            {spec.parameters[0].name: args.value}, name=args.trial_id
        )
        gate = evaluate_result_gate(spec, result)
        controller.mark_gated(args.trial_id, gate.to_dict())
        controller.mark_told(args.trial_id)
        document = {"trial_id": args.trial_id, "result": result, "gate": gate.to_dict()}
        atomic_json(Path(args.output_dir) / "controller_probe_result.json", document)
        return document
    finally:
        await controller.close_session()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--job-endpoint", required=True)
    parser.add_argument("--trial-id", default="controller-restart")
    parser.add_argument("--value", type=float, default=0.75)
    args = parser.parse_args(argv)
    print(json.dumps(asyncio.run(_run(args)), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
