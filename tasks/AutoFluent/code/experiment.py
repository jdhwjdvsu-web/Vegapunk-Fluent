"""Thin task launcher for the repository-level Fluent integration."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def find_repository_root() -> Path:
    configured = os.environ.get("VEGAPUNK_ROOT")
    candidates = [Path(configured)] if configured else []
    candidates.extend(Path(__file__).resolve().parents)
    for candidate in candidates:
        if (candidate / "vegapunk" / "fluent" / "cli.py").is_file():
            return candidate
    raise RuntimeError("Cannot locate the Vegapunk repository; set VEGAPUNK_ROOT")


repository_root = find_repository_root()
sys.path.insert(0, str(repository_root))

from vegapunk.fluent.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
