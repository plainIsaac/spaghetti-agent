"""Durable supervisor-owned task records and observable-state waits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import sqlite3
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


class TaskRegistry:
    def __init__(self, path: str = ":memory:") -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                state TEXT NOT NULL, details TEXT NOT NULL, wait_name TEXT,
                wait_equals TEXT, updated_at TEXT NOT NULL)"""
        )
        self._connection.commit()

    def announce(self, owner: str, title: str, details: Any = None) -> Task:
        cursor = self._connection.execute(
            "INSERT INTO tasks(owner,title,state,details,updated_at) VALUES(?,?,?,?,?)",
            (owner, title, "announced", json.dumps(details), datetime.now(UTC).isoformat()),
        )
        self._connection.commit()
        return self.get(int(cursor.lastrowid))  # type: ignore[return-value]

    def get(self, task_id: int) -> Task | None:
        row = self._connection.execute("SELECT id,owner,title,state,details,wait_name,wait_equals FROM tasks WHERE id=?", (task_id,)).fetchone()
        return None if row is None else Task(*row[:4], json.loads(row[4]), row[5], json.loads(row[6]) if row[6] is not None else None)

    def list(self, owner: str) -> list[Task]:
        return [self.get(int(row[0])) for row in self._connection.execute("SELECT id FROM tasks WHERE owner=? ORDER BY id", (owner,)).fetchall()]  # type: ignore[list-item]

    def transition(self, owner: str, task_id: int, state: str) -> Task:
        task = self.get(task_id)
        if task is None or task.owner != owner:
            raise KeyError(task_id)
        self._connection.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?", (state, datetime.now(UTC).isoformat(), task_id))
        self._connection.commit()
        return self.get(task_id)  # type: ignore[return-value]

    def wait_for(self, owner: str, task_id: int, name: str, equals: Any) -> Task:
        task = self.transition(owner, task_id, "waiting")
        self._connection.execute("UPDATE tasks SET wait_name=?, wait_equals=? WHERE id=?", (name, json.dumps(equals), task.id))
        self._connection.commit()
        return self.get(task.id)  # type: ignore[return-value]

    def observe(self, owner: str, name: str, value: Any) -> list[Task]:
        rows = self._connection.execute("SELECT id,wait_equals FROM tasks WHERE owner=? AND state='waiting' AND wait_name=?", (owner, name)).fetchall()
        ready: list[Task] = []
        for task_id, expected in rows:
            if json.loads(expected) == value:
                ready.append(self.transition(owner, int(task_id), "ready"))
        return ready

    def close(self) -> None:
        self._connection.close()
