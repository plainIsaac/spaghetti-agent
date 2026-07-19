"""Durable inbox storage. The inbox is state; notifications are separate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import sqlite3
import threading


@dataclass(frozen=True)
class Message:
    id: int
    recipient: str
    sender: str
    text: str
    created_at: datetime


class InboxJournal:
    """A small SQLite-backed journal for durable, explicitly shared messages."""

    def __init__(self, path: str = ":memory:") -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS inbox_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recipient TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def append(self, recipient: str, sender: str, text: str) -> Message:
        """Atomically add a message. This operation never invokes a handler."""
        created_at = datetime.now(UTC)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO inbox_messages (recipient, sender, text, created_at) VALUES (?, ?, ?, ?)",
                (recipient, sender, text, created_at.isoformat()),
            )
            message_id = int(cursor.lastrowid)
            self._connection.execute(
                "INSERT INTO events (kind, subject, created_at) VALUES (?, ?, ?)",
                ("inbox.message_added", f"{recipient}:{message_id}", created_at.isoformat()),
            )
        return Message(message_id, recipient, sender, text, created_at)

    def pending(self, recipient: str) -> list[Message]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, recipient, sender, text, created_at FROM inbox_messages "
                "WHERE recipient = ? AND consumed_at IS NULL ORDER BY id",
                (recipient,),
            ).fetchall()
        return [self._to_message(row) for row in rows]

    def acknowledge(self, message_id: int) -> None:
        consumed_at = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE inbox_messages SET consumed_at = ? WHERE id = ?",
                (consumed_at, message_id),
            )
            self._connection.execute(
                "INSERT INTO events (kind, subject, created_at) VALUES (?, ?, ?)",
                ("inbox.message_acknowledged", str(message_id), consumed_at),
            )

    def event_kinds(self) -> list[str]:
        with self._lock:
            rows = self._connection.execute("SELECT kind FROM events ORDER BY id").fetchall()
        return [str(row["kind"]) for row in rows]

    @staticmethod
    def _to_message(row: sqlite3.Row) -> Message:
        return Message(
            id=int(row["id"]),
            recipient=str(row["recipient"]),
            sender=str(row["sender"]),
            text=str(row["text"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def close(self) -> None:
        self._connection.close()
