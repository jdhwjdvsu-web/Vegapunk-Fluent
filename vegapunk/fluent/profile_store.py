"""Local SQLite model library with content identity and atomic profile saves."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .direct_run import _local_case_path
from .model_profile import PROFILE_SCHEMA
from .parameter_rules import RULE_VERSION
from .model_introspection import SCANNER_VERSION


def case_sha256(case_file: str) -> str:
    path = _local_case_path(case_file)
    if not path.name.lower().endswith((".cas", ".cas.h5")):
        raise ValueError("请选择 .cas 或 .cas.h5 文件")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def profile_key(digest: str, runtime_identity: str) -> str:
    identity = [digest, runtime_identity, RULE_VERSION, SCANNER_VERSION, PROFILE_SCHEMA]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


class ProfileStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "models.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def get(self, model_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM profiles WHERE id=?", (model_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, profile: dict) -> None:
        payload = json.dumps(profile, ensure_ascii=False, allow_nan=False)
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO profiles VALUES (?, ?)", (profile["model_id"], payload))

    def list(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM profiles").fetchall()
        return sorted((json.loads(row[0]) for row in rows), key=lambda item: item.get("updated_at", ""), reverse=True)
