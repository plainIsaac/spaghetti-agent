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
                    PRIMARY KEY (owner, name)
                )
                """
            )

    def publish(self, owner: str, name: str, value: Any, presenter: str = "json") -> ObservableValue:
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
                INSERT INTO observable_state (owner, name, value_json, revision, updated_at, presenter)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner, name) DO UPDATE SET
                    value_json = excluded.value_json,
                    revision = excluded.revision,
                    updated_at = excluded.updated_at,
                    presenter = excluded.presenter
                """,
                (owner, name, encoded, revision, updated_at.isoformat(), presenter),
            )
        return ObservableValue(owner, name, value, revision, updated_at, presenter)

    def get(self, owner: str, name: str) -> ObservableValue | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT owner, name, value_json, revision, updated_at, presenter FROM observable_state "
                "WHERE owner = ? AND name = ?",
                (owner, name),
            ).fetchone()
        return None if row is None else self._to_value(row)

    def list(self, owner: str | None = None) -> list[ObservableValue]:
        query = "SELECT owner, name, value_json, revision, updated_at, presenter FROM observable_state"
        parameters: tuple[str, ...] = ()
        if owner is not None:
            query += " WHERE owner = ?"
            parameters = (owner,)
        query += " ORDER BY owner, name"
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
        )

    def close(self) -> None:
        self._connection.close()
