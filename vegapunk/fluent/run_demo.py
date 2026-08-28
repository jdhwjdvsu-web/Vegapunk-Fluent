"""Command line entry point for the resumable Optuna/Fluent walking skeleton."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .optimizer import run_optimization
from .runner import FluentExperimentError
from .spec import ConnectionSpec, SpecError, load_experiment_spec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the serial Optuna/Fluent demo")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--output-dir", default="runs/fluent_demo_v01")
    parser.add_argument("--target-trials", type=int)
    parser.add_argument("--endpoint")
    parser.add_argument("--allow-remote-endpoint", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> dict:
    spec = load_experiment_spec(args.spec)
    if args.endpoint:
        connection_data = asdict(spec.connection)
        connection_data["endpoint"] = args.endpoint
        if args.allow_remote_endpoint:
            connection_data["allow_remote_endpoint"] = True
        spec = replace(spec, connection=ConnectionSpec.from_dict(connection_data))
    return await run_optimization(
        spec,
        args.output_dir,
        target_trials=args.target_trials,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = Path(args.output_dir)
    try:
        summary = asyncio.run(_run(args))
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except (SpecError, FluentExperimentError, OSError, RuntimeError, ValueError) as exc:
        output.mkdir(parents=True, exist_ok=True)
        (output / "demo_error.json").write_text(
            json.dumps({"status": "error", "message": str(exc)}, indent=2),
            encoding="utf-8",
        )
        print(f"Fluent optimization demo failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
