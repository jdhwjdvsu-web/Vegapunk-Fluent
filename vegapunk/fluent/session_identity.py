"""Read-only MCP session observations. Never invent a solver identity."""
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .history import utc_now


@dataclass(frozen=True)
class SessionObservation:
    connected: bool | None
    fluent_session_id: str | None
    identity_status: str
    observed_at: str

    @classmethod
    def from_status(cls, status: Mapping[str, Any]):
        connected = status.get("connected")
        connected = connected if isinstance(connected, bool) else None
        identities = {status[key].strip() for key in ("fluent_session_id", "session_id")
                      if isinstance(status.get(key), str) and status[key].strip()}
        identity = next(iter(identities)) if len(identities) == 1 else None
        return cls(connected, identity, "CONFLICT" if len(identities) > 1 else
                   "AVAILABLE" if identity else "UNAVAILABLE", utc_now())

    def to_dict(self):
        return asdict(self)


def continuity_error(previous: SessionObservation | None, current: SessionObservation) -> str | None:
    if current.connected is not True:
        return "Fluent session connection is not confirmed"
    if current.identity_status == "CONFLICT":
        return "MCP returned conflicting Fluent session identities"
    if previous and previous.fluent_session_id and current.fluent_session_id != previous.fluent_session_id:
        return "Fluent session identity changed or became unavailable"
    return None
