"""Durable registry and runtime factory for independent agent projects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import json
import sqlite3
from threading import RLock
from typing import Callable

from .multi_agent import MultiAgentSession


CURRENT_PROJECT_FORMAT = 2


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    state: str
    created_at: str
    archived_at: str | None
    format_version: int


class ProjectRegistry:
    def __init__(self, path: str = ":memory:") -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._lock = RLock()
        self._connection.execute("""CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
            created_at TEXT NOT NULL, archived_at TEXT, format_version INTEGER NOT NULL DEFAULT 1)""")
        columns = {row[1] for row in self._connection.execute("PRAGMA table_info(projects)")}
        if "format_version" not in columns:
            self._connection.execute("ALTER TABLE projects ADD COLUMN format_version INTEGER NOT NULL DEFAULT 1")
        self._connection.commit()

    def create(self, name: str) -> Project:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("project name must be non-empty")
        now = datetime.now(UTC).isoformat()
        with self._lock:
            try:
                cursor = self._connection.execute("INSERT INTO projects(name,state,created_at,format_version) VALUES(?,?,?,?)", (name.strip(), "active", now, CURRENT_PROJECT_FORMAT))
                self._connection.commit()
            except sqlite3.IntegrityError as error:
                raise ValueError(f"project already exists: {name.strip()}") from error
        return self.get(int(cursor.lastrowid))

    def get(self, project_id: int) -> Project:
        with self._lock:
            row = self._connection.execute("SELECT id,name,state,created_at,archived_at,format_version FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise KeyError(project_id)
        return Project(*row)

    def list(self, include_archived: bool = False) -> list[Project]:
        query = "SELECT id,name,state,created_at,archived_at,format_version FROM projects"
        if not include_archived:
            query += " WHERE state='active'"
        with self._lock:
            return [Project(*row) for row in self._connection.execute(query + " ORDER BY id")]

    def archive(self, project_id: int) -> Project:
        self.get(project_id)
        with self._lock:
            self._connection.execute("UPDATE projects SET state='archived',archived_at=? WHERE id=?", (datetime.now(UTC).isoformat(), project_id))
            self._connection.commit()
        return self.get(project_id)

    def migrate(self, project_id: int) -> Project:
        project = self.get(project_id)
        if project.format_version > CURRENT_PROJECT_FORMAT:
            raise RuntimeError(f"project format {project.format_version} is newer than this runtime")
        if project.format_version < CURRENT_PROJECT_FORMAT:
            with self._lock:
                self._connection.execute("UPDATE projects SET format_version=? WHERE id=?", (CURRENT_PROJECT_FORMAT, project_id))
                self._connection.commit()
        return self.get(project_id)

    def close(self) -> None:
        with self._lock:
            self._connection.close()


class ProjectManager:
    """Owns project metadata; each opened project gets an isolated runtime directory."""

    def __init__(self, root: str | Path, configure_session: Callable[[MultiAgentSession], None] | None = None, default_inference_policy: dict | None = None) -> None:
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.registry = ProjectRegistry(str(self.root / "projects.sqlite"))
        self._configure_session = configure_session
        self._default_inference_policy = default_inference_policy or {}
        self._sessions: dict[int, MultiAgentSession] = {}

    def create(self, name: str) -> Project:
        project = self.registry.create(name)
        root = self.root / f"project-{project.id}"; root.mkdir(parents=True, exist_ok=True)
        (root / "project.json").write_text(json.dumps({"format_version": CURRENT_PROJECT_FORMAT, "project_id": project.id, "name": project.name, "inference_policy": self._default_inference_policy}, indent=2), encoding="utf-8")
        return project

    def inference_policy(self, project_id: int) -> dict:
        self.registry.get(project_id)
        path = self.root / f"project-{project_id}" / "project.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        return dict(payload.get("inference_policy", {}))

    def set_inference_policy(self, project_id: int, policy: dict) -> dict:
        if not isinstance(policy, dict):
            raise ValueError("inference policy must be an object")
        if "token_budget" in policy and policy["token_budget"] is not None and (not isinstance(policy["token_budget"], int) or policy["token_budget"] < 1):
            raise ValueError("token_budget must be a positive integer or null")
        if "turn_token_reserve" in policy and (not isinstance(policy["turn_token_reserve"], int) or policy["turn_token_reserve"] < 0):
            raise ValueError("turn_token_reserve must be a non-negative integer")
        current = self.inference_policy(project_id)
        current.update(policy)
        path = self.root / f"project-{project_id}" / "project.json"
        payload = json.loads(path.read_text(encoding="utf-8")); payload["format_version"] = CURRENT_PROJECT_FORMAT; payload["inference_policy"] = current
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return current

    def open(self, project_id: int) -> MultiAgentSession:
        existing = self._sessions.get(project_id)
        if existing is not None:
            return existing
        project = self.registry.migrate(project_id)
        if project.state != "active":
            raise RuntimeError(f"project is not active: {project.name}")
        runtime_root = self.root / f"project-{project.id}"
        session = MultiAgentSession.open(str(runtime_root), workspace_root=str(runtime_root / "workspace"))
        session.inference_policy = self.inference_policy(project_id)
        if self._configure_session is not None:
            self._configure_session(session)
        self._sessions[project_id] = session
        return session

    def is_open(self, project_id: int) -> bool:
        return project_id in self._sessions

    def close_project(self, project_id: int) -> bool:
        session = self._sessions.pop(project_id, None)
        if session is None:
            return False
        session.close()
        return True

    def close(self) -> None:
        for project_id in list(self._sessions):
            self.close_project(project_id)
        self.registry.close()
