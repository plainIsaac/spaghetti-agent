"""Durable supervisor-owned task records and observable-state waits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import sqlite3
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class Task:
    id: int
    owner: str
    title: str
    state: str
    details: Any
    wait_name: str | None = None
    wait_equals: Any = None
    announced_at: str | None = None
    taken_by: str | None = None
    taken_at: str | None = None
    completed_at: str | None = None
    due_at: str | None = None


class TaskRegistry:
    def __init__(self, path: str = ":memory:") -> None:
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                state TEXT NOT NULL, details TEXT NOT NULL, wait_name TEXT,
                wait_equals TEXT, updated_at TEXT NOT NULL, announced_at TEXT,
                taken_by TEXT, taken_at TEXT, completed_at TEXT, due_at TEXT)"""
        )
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS task_errors (
                task_id INTEGER NOT NULL, fingerprint TEXT NOT NULL, error TEXT NOT NULL,
                count INTEGER NOT NULL, trouble_task_id INTEGER, last_at TEXT NOT NULL,
                PRIMARY KEY(task_id, fingerprint))"""
        )
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL, actor TEXT NOT NULL,
                event TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL)"""
        )
        self._add_missing_columns()
        self._connection.commit()

    def _add_missing_columns(self) -> None:
        existing = {row[1] for row in self._connection.execute("PRAGMA table_info(tasks)")}
        for name in ("announced_at", "taken_by", "taken_at", "completed_at", "due_at"):
            if name not in existing:
                self._connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} TEXT")

    def announce(self, owner: str, title: str, details: Any = None) -> Task:
        with self._lock:
            return self._announce(owner, title, details)

    def _announce(self, owner: str, title: str, details: Any = None) -> Task:
        now = datetime.now(UTC).isoformat()
        cursor = self._connection.execute(
            "INSERT INTO tasks(owner,title,state,details,updated_at,announced_at) VALUES(?,?,?,?,?,?)",
            (owner, title, "announced", json.dumps(details), now, now),
        )
        self._event(int(cursor.lastrowid), owner, "announced", {"title": title, "details": details}, now)
        self._connection.commit()
        return self.get(int(cursor.lastrowid))  # type: ignore[return-value]

    def get(self, task_id: int) -> Task | None:
        with self._lock:
            return self._get(task_id)

    def _get(self, task_id: int) -> Task | None:
        row = self._connection.execute("SELECT id,owner,title,state,details,wait_name,wait_equals,announced_at,taken_by,taken_at,completed_at,due_at FROM tasks WHERE id=?", (task_id,)).fetchone()
        return None if row is None else Task(*row[:4], json.loads(row[4]), row[5], json.loads(row[6]) if row[6] is not None else None, *row[7:])

    def list(self, owner: str) -> list[Task]:
        with self._lock:
            return [self._get(int(row[0])) for row in self._connection.execute("SELECT id FROM tasks WHERE owner=? ORDER BY id", (owner,)).fetchall()]  # type: ignore[list-item]

    def transition(self, owner: str, task_id: int, state: str) -> Task:
        with self._lock:
            return self._transition(owner, task_id, state)

    def _transition(self, owner: str, task_id: int, state: str) -> Task:
        task = self._get(task_id)
        if task is None or task.owner != owner:
            raise KeyError(task_id)
        now = datetime.now(UTC).isoformat()
        assignments = ""
        values: list[Any] = [state, now]
        if state == "working":
            assignments = ", taken_by=?, taken_at=?"
            values.extend([owner, now])
        if state == "completed":
            assignments = ", completed_at=?"
            values.append(now)
        values.append(task_id)
        self._connection.execute(f"UPDATE tasks SET state=?, updated_at=?{assignments} WHERE id=?", values)
        self._event(task_id, owner, state, {"from": task.state}, now)
        self._connection.commit()
        return self._get(task_id)  # type: ignore[return-value]

    def wait_for(self, owner: str, task_id: int, name: str, equals: Any) -> Task:
        with self._lock:
            task = self._transition(owner, task_id, "waiting")
            self._connection.execute("UPDATE tasks SET wait_name=?, wait_equals=? WHERE id=?", (name, json.dumps(equals), task.id))
            self._connection.commit()
            return self._get(task.id)  # type: ignore[return-value]

    def events(self, task_id: int) -> list[dict[str, Any]]:
        return [
            {"actor": row[0], "event": row[1], "details": json.loads(row[2]), "created_at": row[3]}
            for row in self._connection.execute("SELECT actor,event,details,created_at FROM task_events WHERE task_id=? ORDER BY id", (task_id,))
        ]

    def errors(self, task_id: int) -> list[dict[str, Any]]:
        return [
            {"error": row[0], "count": row[1], "trouble_task_id": row[2], "last_at": row[3]}
            for row in self._connection.execute("SELECT error,count,trouble_task_id,last_at FROM task_errors WHERE task_id=? ORDER BY last_at DESC", (task_id,))
        ]

    def search_errors(self, text: str, owner: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT e.task_id,e.error,e.count,e.trouble_task_id,e.last_at,t.title,t.owner FROM task_errors e JOIN tasks t ON t.id=e.task_id WHERE e.error LIKE ?"
        parameters: list[Any] = [f"%{text}%"]
        if owner is not None:
            query += " AND t.owner=?"
            parameters.append(owner)
        return [
            {"task_id": row[0], "error": row[1], "count": row[2], "trouble_task_id": row[3], "last_at": row[4], "task_title": row[5], "owner": row[6]}
            for row in self._connection.execute(query + " ORDER BY e.last_at DESC", parameters)
        ]

    def report_error(self, owner: str, task_id: int, error: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None or task.owner != owner:
            raise KeyError(task_id)
        fingerprint = error.strip()[:300]
        now = datetime.now(UTC).isoformat()
        row = self._connection.execute("SELECT count,trouble_task_id FROM task_errors WHERE task_id=? AND fingerprint=?", (task_id, fingerprint)).fetchone()
        count = 1 if row is None else int(row[0]) + 1
        trouble_task_id = None if row is None else row[1]
        self._connection.execute(
            "INSERT INTO task_errors(task_id,fingerprint,error,count,trouble_task_id,last_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(task_id,fingerprint) DO UPDATE SET count=excluded.count,last_at=excluded.last_at",
            (task_id, fingerprint, error, count, trouble_task_id, now),
        )
        self._event(task_id, owner, "error", {"error": error, "count": count}, now)
        if count >= 3 and trouble_task_id is None:
            trouble = self.announce(owner, f"Trouble: recurring error in task {task_id}", {"task_id": task_id, "error": error, "count": count})
            self._connection.execute("UPDATE task_errors SET trouble_task_id=? WHERE task_id=? AND fingerprint=?", (trouble.id, task_id, fingerprint))
            self._event(task_id, owner, "trouble_announced", {"trouble_task_id": trouble.id, "count": count}, now)
            trouble_task_id = trouble.id
        self._connection.commit()
        return {"count": count, "trouble_task_id": trouble_task_id}

    def challenge(self, owner: str, task_id: int, description: str) -> Task:
        task = self.get(task_id)
        if task is None or task.owner != owner:
            raise KeyError(task_id)
        challenge = self.announce(owner, f"Challenge: {task.title}", {"task_id": task_id, "description": description})
        self._event(task_id, owner, "challenge_announced", {"challenge_task_id": challenge.id, "description": description}, datetime.now(UTC).isoformat())
        self._connection.commit()
        return challenge

    def schedule(self, owner: str, task_id: int, due_at: datetime) -> Task:
        with self._lock:
            task = self._transition(owner, task_id, "scheduled")
            due = due_at.astimezone(UTC).isoformat()
            self._connection.execute("UPDATE tasks SET due_at=? WHERE id=?", (due, task.id))
            self._event(task.id, owner, "scheduled", {"due_at": due}, datetime.now(UTC).isoformat())
            self._connection.commit()
            return self._get(task.id)  # type: ignore[return-value]

    def due(self, now: datetime) -> list[Task]:
        with self._lock:
            rows = self._connection.execute("SELECT id FROM tasks WHERE state='scheduled' AND due_at<=?", (now.astimezone(UTC).isoformat(),)).fetchall()
            return [self._transition(self._get(int(row[0])).owner, int(row[0]), "ready") for row in rows]  # type: ignore[union-attr]

    def _event(self, task_id: int, actor: str, event: str, details: Any, created_at: str) -> None:
        self._connection.execute(
            "INSERT INTO task_events(task_id,actor,event,details,created_at) VALUES(?,?,?,?,?)",
            (task_id, actor, event, json.dumps(details), created_at),
        )

    def observe(self, owner: str, name: str, value: Any) -> list[Task]:
        rows = self._connection.execute("SELECT id,wait_equals FROM tasks WHERE owner=? AND state='waiting' AND wait_name=?", (owner, name)).fetchall()
        ready: list[Task] = []
        for task_id, expected in rows:
            if json.loads(expected) == value:
                ready.append(self.transition(owner, int(task_id), "ready"))
        return ready

    def close(self) -> None:
        self._connection.close()
