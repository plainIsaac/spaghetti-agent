"""Explicitly published, JSON-presentable state for the supervisor UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import sqlite3
import threading
from typing import Any


@dataclass(frozen=True)
class ObservableValue:
    owner: str
    name: str
    value: Any
    revision: int
    updated_at: datetime
    presenter: str
    show_by_default: bool
    label: str | None
    priority: int


class ObservableStateRegistry:
    """Durable state deliberately exposed for inspection.

    The first version accepts JSON values only. This makes persistence and safe
    presentation explicit instead of accidentally serializing arbitrary Python
    objects or calling their representations.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS observable_state (
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    presenter TEXT NOT NULL,
                    show_by_default INTEGER NOT NULL DEFAULT 1,
                    label TEXT,
                    priority INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (owner, name)
                )
                """
            )
        self._ensure_columns()

    def _ensure_columns(self) -> None:
        """Keep early on-disk sessions forward-compatible as the registry grows."""
        with self._lock, self._connection:
            columns = {row["name"] for row in self._connection.execute("PRAGMA table_info(observable_state)")}
            if "show_by_default" not in columns:
                self._connection.execute("ALTER TABLE observable_state ADD COLUMN show_by_default INTEGER NOT NULL DEFAULT 1")
            if "label" not in columns:
                self._connection.execute("ALTER TABLE observable_state ADD COLUMN label TEXT")
            if "priority" not in columns:
                self._connection.execute("ALTER TABLE observable_state ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")

    def publish(
        self,
        owner: str,
        name: str,
        value: Any,
        presenter: str = "json",
        show_by_default: bool = True,
        label: str | None = None,
        priority: int = 0,
    ) -> ObservableValue:
        encoded = json.dumps(value)
        updated_at = datetime.now(UTC)
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT revision FROM observable_state WHERE owner = ? AND name = ?",
                (owner, name),
            ).fetchone()
            revision = 1 if row is None else int(row["revision"]) + 1
            self._connection.execute(
                """
                INSERT INTO observable_state
                    (owner, name, value_json, revision, updated_at, presenter, show_by_default, label, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner, name) DO UPDATE SET
                    value_json = excluded.value_json,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at,
                    presenter = excluded.presenter,
                    show_by_default = excluded.show_by_default,
                    label = excluded.label,
                    priority = excluded.priority
                """,
                (owner, name, encoded, revision, updated_at.isoformat(), presenter, int(show_by_default), label, priority),
            )
        return ObservableValue(owner, name, value, revision, updated_at, presenter, show_by_default, label, priority)

    def get(self, owner: str, name: str) -> ObservableValue | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT owner, name, value_json, revision, updated_at, presenter, show_by_default, label, priority FROM observable_state "
                "WHERE owner = ? AND name = ?",
                (owner, name),
            ).fetchone()
        return None if row is None else self._to_value(row)

    def list(self, owner: str | None = None, default_only: bool = False) -> list[ObservableValue]:
        query = "SELECT owner, name, value_json, revision, updated_at, presenter, show_by_default, label, priority FROM observable_state"
        parameters: tuple[str, ...] = ()
        conditions: list[str] = []
        if owner is not None:
            conditions.append("owner = ?")
            parameters += (owner,)
        if default_only:
            conditions.append("show_by_default = 1")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY priority DESC, owner, name"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [self._to_value(row) for row in rows]

    @staticmethod
    def _to_value(row: sqlite3.Row) -> ObservableValue:
        return ObservableValue(
            owner=str(row["owner"]),
            name=str(row["name"]),
            value=json.loads(str(row["value_json"])),
            revision=int(row["revision"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            presenter=str(row["presenter"]),
            show_by_default=bool(row["show_by_default"]),
            label=None if row["label"] is None else str(row["label"]),
            priority=int(row["priority"]),
        )

    def close(self) -> None:
        self._connection.close()
