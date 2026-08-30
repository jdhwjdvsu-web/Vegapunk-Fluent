"""Thin MCP client for the Windows Fluent Job service."""

from __future__ import annotations

import json
from typing import Any

from .runner import FluentExperimentError
from .spec import ExperimentSpec


def _structured(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return structured
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


class FluentJobClient:
    """Expose only submit/status/result/cancel/health to the Controller."""

    def __init__(self, spec: ExperimentSpec, client_factory=None):
        if not spec.connection.job_endpoint:
            raise ValueError("connection.job_endpoint is required for Job mode")
        self.spec = spec
        self.client_factory = client_factory
        self._context = None
        self._client = None

    def _new_client(self):
        endpoint = self.spec.connection.job_endpoint
        timeout = self.spec.connection.client_timeout_seconds
        if self.client_factory is not None:
            return self.client_factory(endpoint, timeout)
        try:
            from fastmcp import Client
        except ImportError as exc:
            raise FluentExperimentError("fastmcp is required for Fluent Job mode") from exc
        return Client(endpoint, timeout=timeout)

    async def open_session(self) -> None:
        if self._client is not None:
            return
        self._context = self._new_client()
        self._client = await self._context.__aenter__()
        await self.health()

    async def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise FluentExperimentError("open_session() must be called first")
        result = await self._client.call_tool(
            name,
            arguments,
            timeout=self.spec.connection.client_timeout_seconds,
            raise_on_error=False,
        )
        if getattr(result, "is_error", False):
            raise FluentExperimentError(f"Fluent Job MCP tool failed: {name}")
        data = _structured(result)
        if data.get("status") == "error":
            raise FluentExperimentError(
                f"{name} failed: {data.get('message') or data.get('error')}"
            )
        return data

    async def submit_point(self, job_spec: dict[str, Any]) -> dict[str, Any]:
        return await self._call("submit_job", {"job_spec": job_spec})

    async def job_status(self, job_id: str) -> dict[str, Any]:
        return await self._call("job_status", {"job_id": job_id})

    async def job_result(self, job_id: str) -> dict[str, Any]:
        return await self._call("job_result", {"job_id": job_id})

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        return await self._call("cancel_job", {"job_id": job_id})

    async def health(self) -> dict[str, Any]:
        return await self._call("worker_health", {})

    async def close_session(self) -> None:
        if self._context is not None:
            await self._context.__aexit__(None, None, None)
        self._context = None
        self._client = None
