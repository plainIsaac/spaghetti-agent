"""Supervisor-owned, task-scoped workspace operations for coordinated agents."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import os
import sqlite3
from tempfile import NamedTemporaryFile
from threading import RLock


class Workspace:
    def __init__(self, root: str | Path, path: str = ":memory:") -> None:
        self.root = Path(root).resolve()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._lock = RLock()
        self._connection.execute("""CREATE TABLE IF NOT EXISTS workspace_claims (
            path TEXT PRIMARY KEY, agent TEXT NOT NULL, task_id INTEGER NOT NULL, claimed_at TEXT NOT NULL)""")
        self._connection.execute("""CREATE TABLE IF NOT EXISTS workspace_changes (
            id INTEGER PRIMARY KEY, path TEXT NOT NULL, agent TEXT NOT NULL, task_id INTEGER NOT NULL,
            previous_revision TEXT, revision TEXT NOT NULL, created_at TEXT NOT NULL)""")
        self._connection.execute("""CREATE TABLE IF NOT EXISTS workspace_observations (
            path TEXT NOT NULL, agent TEXT NOT NULL, task_id INTEGER NOT NULL, revision TEXT NOT NULL,
            PRIMARY KEY(path, agent, task_id))""")
        self._connection.commit()

    def list(self, relative: str = ".") -> list[str]:
        directory = self._resolve(relative)
        if not directory.exists():
            return []
        if not directory.is_dir():
            raise ValueError(f"not a directory: {relative}")
        return [str(path.relative_to(self.root)).replace("\\", "/") for path in sorted(directory.rglob("*")) if path.is_file()]

    def read_text(self, relative: str, agent: str | None = None, task_id: int | None = None) -> dict[str, str]:
        path = self._resolve(relative)
        text = path.read_text(encoding="utf-8")
        revision = self._revision(text)
        if agent is not None and task_id is not None:
            with self._lock:
                self._connection.execute(
                    "INSERT OR REPLACE INTO workspace_observations(path,agent,task_id,revision) VALUES(?,?,?,?)",
                    (relative, agent, task_id, revision),
                )
                self._connection.commit()
        return {"text": text, "revision": revision}

    def claim(self, agent: str, task_id: int, relative: str) -> dict[str, object]:
        self._resolve(relative)
        with self._lock:
            existing = self._connection.execute("SELECT agent,task_id FROM workspace_claims WHERE path=?", (relative,)).fetchone()
            if existing is not None and tuple(existing) != (agent, task_id):
                raise RuntimeError(f"workspace conflict: {relative} is claimed by {existing[0]} task {existing[1]}")
            self._connection.execute(
                "INSERT OR REPLACE INTO workspace_claims(path,agent,task_id,claimed_at) VALUES(?,?,?,?)",
                (relative, agent, task_id, datetime.now(UTC).isoformat()),
            )
            self._connection.commit()
        return {"path": relative, "task_id": task_id, "agent": agent}

    def write_text(self, agent: str, task_id: int, relative: str, text: str, expected_revision: str | None = None) -> dict[str, str]:
        path = self._resolve(relative)
        with self._lock:
            claim = self._connection.execute("SELECT agent,task_id FROM workspace_claims WHERE path=?", (relative,)).fetchone()
            if claim is None:
                self._connection.execute(
                    "INSERT INTO workspace_claims(path,agent,task_id,claimed_at) VALUES(?,?,?,?)",
                    (relative, agent, task_id, datetime.now(UTC).isoformat()),
                )
            elif tuple(claim) != (agent, task_id):
                raise RuntimeError(f"workspace conflict: {relative} is claimed by {claim[0]} task {claim[1]}")
            exists = path.exists()
            previous_text = path.read_text(encoding="utf-8") if exists else ""
            previous = self._revision(previous_text)
            if expected_revision is None and exists:
                observed = self._connection.execute(
                    "SELECT revision FROM workspace_observations WHERE path=? AND agent=? AND task_id=?",
                    (relative, agent, task_id),
                ).fetchone()
                if observed is None:
                    raise RuntimeError(f"workspace write requires reading existing file first: {relative}")
                expected_revision = str(observed[0])
            if expected_revision is not None and expected_revision != previous:
                raise RuntimeError(f"workspace conflict: {relative} changed (expected {expected_revision}, found {previous})")
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
                temporary.write(text)
                temporary_name = temporary.name
            os.replace(temporary_name, path)
            revision = self._revision(text)
            self._connection.execute(
                "INSERT INTO workspace_changes(path,agent,task_id,previous_revision,revision,created_at) VALUES(?,?,?,?,?,?)",
                (relative, agent, task_id, previous, revision, datetime.now(UTC).isoformat()),
            )
            self._connection.execute(
                "INSERT OR REPLACE INTO workspace_observations(path,agent,task_id,revision) VALUES(?,?,?,?)",
                (relative, agent, task_id, revision),
            )
            self._connection.commit()
        return {"path": relative, "previous_revision": previous, "revision": revision}

    def changes(self, task_id: int) -> list[dict[str, object]]:
        return [
            {"path": row[0], "agent": row[1], "task_id": row[2], "previous_revision": row[3], "revision": row[4], "created_at": row[5]}
            for row in self._connection.execute("SELECT path,agent,task_id,previous_revision,revision,created_at FROM workspace_changes WHERE task_id=? ORDER BY id", (task_id,))
        ]

    def release_task(self, agent: str, task_id: int) -> int:
        with self._lock:
            cursor = self._connection.execute("DELETE FROM workspace_claims WHERE agent=? AND task_id=?", (agent, task_id))
            self._connection.commit()
            return cursor.rowcount

    def close(self) -> None:
        self._connection.close()

    def _resolve(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("workspace path must stay within its root")
        return candidate

    @staticmethod
    def _revision(text: str) -> str:
        return sha256(text.encode("utf-8")).hexdigest()
