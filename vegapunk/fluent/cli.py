"""Command-line interface for deterministic Fluent experiments."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

from .codegen import build_design_point_code, build_report_setup_code
from .runner import FluentExperimentError, run_experiment
from .spec import ConnectionSpec, SpecError, load_experiment_spec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a constrained Fluent MCP experiment"
    )
    parser.add_argument(
        "--spec", required=True, help="JSON or YAML experiment specification"
    )
    parser.add_argument(
        "--output-dir", default=".", help="Directory for result artifacts"
    )
    parser.add_argument(
        "--endpoint",
        help="Override the MCP endpoint, for example with the dynamic WSL host address",
    )
    parser.add_argument(
        "--allow-remote-endpoint",
        action="store_true",
        help="Permit a non-loopback --endpoint (required for WSL-to-Windows access)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the spec and write generated code without contacting Fluent",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir)
    try:
        spec = load_experiment_spec(args.spec)
        if args.endpoint:
            connection_data = asdict(spec.connection)
            connection_data["endpoint"] = args.endpoint
            if args.allow_remote_endpoint:
                connection_data["allow_remote_endpoint"] = True
            spec = replace(
                spec,
                connection=ConnectionSpec.from_dict(connection_data),
            )
        if args.dry_run:
            code_dir = output_dir / "generated_code"
            code_dir.mkdir(parents=True, exist_ok=True)
            (code_dir / "setup_reports.py").write_text(
                build_report_setup_code(spec), encoding="utf-8"
            )
            for index, point in enumerate(spec.design_points):
                (code_dir / f"point-{index + 1}.py").write_text(
                    build_design_point_code(spec, point), encoding="utf-8"
                )
            print(
                json.dumps(
                    {"status": "dry-run-ok", "design_points": len(spec.design_points)}
                )
            )
            return 0
        result = run_experiment(spec, output_dir)
        ok = result.get("best_design_point") is not None
        print(
            json.dumps(
                {
                    "status": "ok" if ok else "no-valid-design-point",
                    "best_design_point": result.get("best_design_point"),
                    "objective": result.get("objective"),
                    "artifact": str(output_dir / "fluent_result.json"),
                },
                ensure_ascii=False,
            )
        )
        return 0 if ok else 2
    except (SpecError, FluentExperimentError, OSError, ValueError) as exc:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "fluent_error.json").write_text(
            json.dumps({"status": "error", "message": str(exc)}, indent=2),
            encoding="utf-8",
        )
        print(f"Fluent experiment failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
