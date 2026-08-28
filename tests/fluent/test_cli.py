import pytest

from vegapunk.fluent.cli import build_parser
from vegapunk.fluent.spec import ConnectionSpec, SpecError


def test_cli_accepts_explicit_wsl_endpoint_override():
    args = build_parser().parse_args(
        [
            "--spec",
            "experiment.json",
            "--endpoint",
            "http://192.0.2.1:18000/mcp",
            "--allow-remote-endpoint",
        ]
    )
    connection = ConnectionSpec.from_dict(
        {
            "endpoint": args.endpoint,
            "allow_remote_endpoint": args.allow_remote_endpoint,
        }
    )
    assert connection.endpoint == "http://192.0.2.1:18000/mcp"


def test_cli_remote_override_still_requires_explicit_permission():
    with pytest.raises(SpecError, match="allow_remote_endpoint"):
        ConnectionSpec.from_dict({"endpoint": "http://192.0.2.1:18000/mcp"})
