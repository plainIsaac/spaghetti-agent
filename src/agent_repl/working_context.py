"""Scoped working context, distinct from the queryable durable context graph."""

from __future__ import annotations

import json
import sqlite3
from threading import RLock
from typing import Any


LIFETIMES = frozenset({"line", "message", "task", "error", "session"})

class WorkingContext:
    def __init__(self, path: str = ":memory:") -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._lock = RLock()
        self._connection.execute("""CREATE TABLE IF NOT EXISTS working_context (
            owner TEXT NOT NULL, key TEXT NOT NULL, lifetime TEXT NOT NULL, scope_id TEXT NOT NULL,
            value TEXT NOT NULL, model_visible INTEGER NOT NULL, revision INTEGER NOT NULL,
            PRIMARY KEY(owner,key,lifetime,scope_id))""")
        self._connection.commit()

    def set(self, owner: str, key: str, value: Any, lifetime: str = "session", scope_id: str = "", model_visible: bool = False) -> dict[str, Any]:
        self._validate(lifetime, scope_id)
        try:
            encoded = json.dumps(value)
        except (TypeError, ValueError) as error:
            raise ValueError("working-context values must be JSON serializable") from error
        with self._lock:
            row = self._connection.execute("SELECT revision FROM working_context WHERE owner=? AND key=? AND lifetime=? AND scope_id=?", (owner, key, lifetime, scope_id)).fetchone()
            revision = 1 if row is None else int(row[0]) + 1
            self._connection.execute("INSERT OR REPLACE INTO working_context VALUES(?,?,?,?,?,?,?)", (owner, key, lifetime, scope_id, encoded, int(model_visible), revision))
            self._connection.commit()
        return {"key": key, "revision": revision, "lifetime": lifetime, "scope_id": scope_id}

    def active(self, owner: str, scopes: list[tuple[str, str]]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        with self._lock:
            for lifetime, scope_id in scopes:
                for row in self._connection.execute("SELECT key,value,revision,lifetime,scope_id FROM working_context WHERE owner=? AND lifetime=? AND scope_id=? AND model_visible=1 ORDER BY key", (owner, lifetime, scope_id)):
                    entries.append({"key": row[0], "value": json.loads(row[1]), "revision": row[2], "lifetime": row[3], "scope_id": row[4]})
        return entries

    def get(self, owner: str, key: str, lifetime: str, scope_id: str = "") -> Any:
        self._validate(lifetime, scope_id)
        with self._lock:
            row = self._connection.execute("SELECT value FROM working_context WHERE owner=? AND key=? AND lifetime=? AND scope_id=?", (owner, key, lifetime, scope_id)).fetchone()
        return None if row is None else json.loads(row[0])

    def clear(self, owner: str, lifetime: str, scope_id: str = "", key: str | None = None) -> int:
        self._validate(lifetime, scope_id)
        with self._lock:
            if key is None:
                cursor = self._connection.execute("DELETE FROM working_context WHERE owner=? AND lifetime=? AND scope_id=?", (owner, lifetime, scope_id))
            else:
                cursor = self._connection.execute("DELETE FROM working_context WHERE owner=? AND key=? AND lifetime=? AND scope_id=?", (owner, key, lifetime, scope_id))
            self._connection.commit()
            return cursor.rowcount

    def clear_lifetime(self, owner: str, lifetime: str) -> int:
        if lifetime not in LIFETIMES:
            raise ValueError(f"unknown working-context lifetime: {lifetime}")
        with self._lock:
            cursor = self._connection.execute("DELETE FROM working_context WHERE owner=? AND lifetime=?", (owner, lifetime))
            self._connection.commit()
            return cursor.rowcount

    @staticmethod
    def _validate(lifetime: str, scope_id: str) -> None:
        if lifetime not in LIFETIMES:
            raise ValueError(f"unknown working-context lifetime: {lifetime}")
        if lifetime != "session" and not str(scope_id):
            raise ValueError(f"{lifetime} working context requires a scope_id")

    def close(self) -> None:
        with self._lock:
            self._connection.close()
