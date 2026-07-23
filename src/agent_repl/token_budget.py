"""Durable, provider-neutral token budgeting for model turns.

Providers do not all return usage in the same streaming shape, so this ledger
intentionally records a conservative character-based estimate.  A reservation
is made before a request, which keeps concurrent agents from silently spending
past the project limit.
"""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from threading import RLock
from typing import Any


class TokenBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class TokenReservation:
    id: int
    estimated_input_tokens: int
    reserved_output_tokens: int


def estimate_tokens(value: Any) -> int:
    """A deliberately portable estimate when provider usage is unavailable."""
    text = value if isinstance(value, str) else repr(value)
    return max(1, (len(text) + 3) // 4)


class TokenBudget:
    def __init__(self, path: str = ":memory:", limit_tokens: int | None = None) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._lock = RLock()
        with self._connection:
            self._connection.execute("CREATE TABLE IF NOT EXISTS token_budget (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
            self._connection.execute("CREATE TABLE IF NOT EXISTS token_reservations (id INTEGER PRIMARY KEY, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, settled_tokens INTEGER)")
            if limit_tokens is not None:
                self._connection.execute("INSERT OR REPLACE INTO token_budget(key, value) VALUES ('limit_tokens', ?)", (limit_tokens,))

    def set_limit(self, limit_tokens: int | None) -> None:
        if limit_tokens is not None and limit_tokens < 1:
            raise ValueError("token budget must be positive")
        with self._lock, self._connection:
            if limit_tokens is None:
                self._connection.execute("DELETE FROM token_budget WHERE key = 'limit_tokens'")
            else:
                self._connection.execute("INSERT OR REPLACE INTO token_budget(key, value) VALUES ('limit_tokens', ?)", (limit_tokens,))

    def reserve(self, estimated_input_tokens: int, reserved_output_tokens: int) -> TokenReservation:
        estimated_input_tokens, reserved_output_tokens = max(0, estimated_input_tokens), max(0, reserved_output_tokens)
        with self._lock, self._connection:
            snapshot = self.snapshot()
            requested = estimated_input_tokens + reserved_output_tokens
            if snapshot["limit_tokens"] is not None and requested > snapshot["remaining_tokens"]:
                raise TokenBudgetExceeded(
                    f"token budget exhausted: need {requested} estimated tokens, only {snapshot['remaining_tokens']} remain"
                )
            cursor = self._connection.execute(
                "INSERT INTO token_reservations(input_tokens, output_tokens, settled_tokens) VALUES (?, ?, NULL)",
                (estimated_input_tokens, reserved_output_tokens),
            )
            return TokenReservation(int(cursor.lastrowid), estimated_input_tokens, reserved_output_tokens)

    def settle(self, reservation: TokenReservation, actual_tokens: int) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE token_reservations SET settled_tokens = ? WHERE id = ? AND settled_tokens IS NULL",
                (max(0, actual_tokens), reservation.id),
            )

    def snapshot(self) -> dict[str, int | None | str]:
        with self._lock:
            row = self._connection.execute("SELECT value FROM token_budget WHERE key = 'limit_tokens'").fetchone()
            limit = None if row is None else int(row[0])
            used = int(self._connection.execute("SELECT COALESCE(SUM(settled_tokens), 0) FROM token_reservations").fetchone()[0])
            reserved = int(self._connection.execute("SELECT COALESCE(SUM(input_tokens + output_tokens), 0) FROM token_reservations WHERE settled_tokens IS NULL").fetchone()[0])
        return {
            "status": "unlimited" if limit is None else ("exhausted" if limit - used - reserved <= 0 else "available"),
            "limit_tokens": limit,
            "used_tokens": used,
            "reserved_tokens": reserved,
            "remaining_tokens": None if limit is None else max(0, limit - used - reserved),
            "method": "estimated_characters_divided_by_4",
        }

    def close(self) -> None:
        self._connection.close()
