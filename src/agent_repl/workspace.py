"""Supervisor-owned, task-scoped workspace operations for coordinated agents."""

from __future__ import annotations

from datetime import UTC, datetime
from difflib import unified_diff
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
        self._connection.execute("""CREATE TABLE IF NOT EXISTS workspace_branches (
            task_id INTEGER PRIMARY KEY, agent TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL)""")
        self._connection.execute("""CREATE TABLE IF NOT EXISTS workspace_branch_files (
            task_id INTEGER NOT NULL, path TEXT NOT NULL, text TEXT NOT NULL, base_revision TEXT NOT NULL,
            PRIMARY KEY(task_id, path))""")
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
        branch = self._branch_text(task_id, relative) if task_id is not None else None
        text = branch if branch is not None else path.read_text(encoding="utf-8")
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
            existing_branch = self._connection.execute("SELECT text,base_revision FROM workspace_branch_files WHERE task_id=? AND path=?", (task_id, relative)).fetchone()
            exists = path.exists()
            previous_text = str(existing_branch[0]) if existing_branch is not None else (path.read_text(encoding="utf-8") if exists else "")
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
            if self._is_branch(task_id):
                self._connection.execute(
                    "INSERT OR REPLACE INTO workspace_branch_files(task_id,path,text,base_revision) VALUES(?,?,?,?)",
                    (task_id, relative, text, str(existing_branch[1]) if existing_branch is not None else self._revision(path.read_text(encoding="utf-8") if exists else "")),
                )
                revision = self._revision(text)
                self._connection.execute(
                    "INSERT OR REPLACE INTO workspace_observations(path,agent,task_id,revision) VALUES(?,?,?,?)",
                    (relative, agent, task_id, revision),
                )
                self._connection.commit()
                return {"path": relative, "previous_revision": previous, "revision": revision}
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

    def branch(self, agent: str, task_id: int) -> dict[str, object]:
        with self._lock:
            self._connection.execute(
                "INSERT OR IGNORE INTO workspace_branches(task_id,agent,state,created_at) VALUES(?,?,?,?)",
                (task_id, agent, "working", datetime.now(UTC).isoformat()),
            )
            self._connection.commit()
        return {"task_id": task_id, "state": "working"}

    def diff(self, task_id: int) -> list[dict[str, str]]:
        rows = self._connection.execute("SELECT path,text FROM workspace_branch_files WHERE task_id=? ORDER BY path", (task_id,)).fetchall()
        result: list[dict[str, str]] = []
        for relative, branch_text in rows:
            path = self._resolve(str(relative))
            main_text = path.read_text(encoding="utf-8") if path.exists() else ""
            diff = "".join(unified_diff(main_text.splitlines(True), str(branch_text).splitlines(True), fromfile=f"main/{relative}", tofile=f"branch/{relative}"))
            result.append({"path": str(relative), "diff": diff})
        return result

    def submit(self, agent: str, task_id: int) -> dict[str, object]:
        with self._lock:
            self._connection.execute("UPDATE workspace_branches SET state='submitted' WHERE task_id=? AND agent=?", (task_id, agent))
            self._connection.commit()
        return {"task_id": task_id, "state": "submitted", "files": len(self.diff(task_id))}

    def merge(self, task_id: int) -> dict[str, object]:
        with self._lock:
            branch = self._connection.execute("SELECT state FROM workspace_branches WHERE task_id=?", (task_id,)).fetchone()
            if branch is None or branch[0] != "submitted":
                raise RuntimeError("workspace merge requires a submitted branch")
            rows = self._connection.execute("SELECT path,text,base_revision FROM workspace_branch_files WHERE task_id=?", (task_id,)).fetchall()
            for relative, _text, base in rows:
                path = self._resolve(str(relative))
                current = self._revision(path.read_text(encoding="utf-8") if path.exists() else "")
                if current != base:
                    raise RuntimeError(f"workspace merge conflict: {relative} changed since branch")
            for relative, text, _base in rows:
                path = self._resolve(str(relative)); path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(text), encoding="utf-8")
            self._connection.execute("UPDATE workspace_branches SET state='merged' WHERE task_id=?", (task_id,))
            self._connection.commit()
        return {"task_id": task_id, "state": "merged", "files": len(rows)}

    def close(self) -> None:
        self._connection.close()

    def _resolve(self, relative: str) -> Path:
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("workspace path must stay within its root")
        return candidate

    def _is_branch(self, task_id: int) -> bool:
        return self._connection.execute("SELECT 1 FROM workspace_branches WHERE task_id=? AND state='working'", (task_id,)).fetchone() is not None

    def _branch_text(self, task_id: int, relative: str) -> str | None:
        row = self._connection.execute("SELECT text FROM workspace_branch_files WHERE task_id=? AND path=?", (task_id, relative)).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def _revision(text: str) -> str:
        return sha256(text.encode("utf-8")).hexdigest()
