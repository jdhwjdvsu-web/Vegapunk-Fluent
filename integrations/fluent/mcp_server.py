"""FastMCP facade for the persistent single-worker Fluent Job service."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from vegapunk.fluent.resources import ResourcePolicy, SingleWorkerResourceGuard

from .fluent_worker import FluentJobExecutor
from .job_service import SingleWorkerJobService
from .job_store import JobStore


def create_server(jobs_root: str | Path) -> FastMCP:
    root = Path(jobs_root)
    guard = SingleWorkerResourceGuard(
        root,
        ResourcePolicy(
            max_workers=1,
            minimum_free_disk_gb=float(os.getenv("VEGAPUNK_MIN_FREE_DISK_GB", "2")),
            minimum_available_memory_gb=float(
                os.getenv("VEGAPUNK_MIN_AVAILABLE_MEMORY_GB", "2")
            ),
            trial_timeout_seconds=float(
                os.getenv("VEGAPUNK_TRIAL_TIMEOUT_SECONDS", "3600")
            ),
            campaign_timeout_seconds=float(
                os.getenv("VEGAPUNK_CAMPAIGN_TIMEOUT_SECONDS", "86400")
            ),
            trial_budget=int(os.getenv("VEGAPUNK_TRIAL_BUDGET", "50")),
        ),
    )
    service = SingleWorkerJobService(
        JobStore(root), FluentJobExecutor(), resource_guard=guard
    )
    mcp = FastMCP("Vegapunk Fluent Job Service")

    @mcp.tool
    async def submit_job(job_spec: dict[str, Any]) -> dict[str, Any]:
        """Persist a validated Job and return its stable id immediately."""

        return await service.submit_job(job_spec)

    @mcp.tool
    def job_status(job_id: str) -> dict[str, Any]:
        """Return the persisted Job state after reconnect or restart."""

        return service.job_status(job_id)

    @mcp.tool
    def job_result(job_id: str) -> dict[str, Any]:
        """Return the terminal result when it is available."""

        return service.job_result(job_id)

    @mcp.tool
    async def cancel_job(job_id: str) -> dict[str, Any]:
        """Return cancelled=true only after the worker confirms it stopped."""

        return await service.cancel_job(job_id)

    @mcp.tool
    def worker_health() -> dict[str, Any]:
        """Report the single worker, queue, persistence, and startup recovery state."""

        return service.worker_health()

    return mcp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Vegapunk Fluent Job MCP")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18001)
    parser.add_argument(
        "--jobs-root",
        default=os.getenv(
            "VEGAPUNK_FLUENT_JOBS",
            str(Path(os.getenv("LOCALAPPDATA", ".")) / "Vegapunk" / "fluent-jobs"),
        ),
    )
    args = parser.parse_args(argv)
    create_server(args.jobs_root).run(
        transport="http", host=args.host, port=args.port, path="/mcp"
    )


if __name__ == "__main__":
    main()
