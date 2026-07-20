"""Durable inbox storage. The inbox is state; notifications are separate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
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

    def __init__(self, path: str = ":memory:", debug_log_path: str | None = None) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._debug_log_path = Path(debug_log_path) if debug_log_path is not None else None
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
        message = Message(message_id, recipient, sender, text, created_at)
        self._append_debug_log(message)
        return message

    def pending(self, recipient: str) -> list[Message]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, recipient, sender, text, created_at FROM inbox_messages "
                "WHERE recipient = ? AND consumed_at IS NULL ORDER BY id",
                (recipient,),
            ).fetchall()
        return [self._to_message(row) for row in rows]

    def conversation(self, first_party: str, second_party: str) -> list[Message]:
        """Return the raw, append-only debug record for two conversation endpoints."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT id, recipient, sender, text, created_at FROM inbox_messages
                WHERE (sender = ? AND recipient = ?) OR (sender = ? AND recipient = ?)
                ORDER BY id
                """,
                (first_party, second_party, second_party, first_party),
            ).fetchall()
        return [self._to_message(row) for row in rows]

    def acknowledge(self, recipient: str, message_id: int) -> bool:
        """Acknowledge a message only from its intended recipient."""
        consumed_at = datetime.now(UTC).isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "UPDATE inbox_messages SET consumed_at = ? WHERE id = ? AND recipient = ? AND consumed_at IS NULL",
                (consumed_at, message_id, recipient),
            )
            if cursor.rowcount != 1:
                return False
            self._connection.execute(
                "INSERT INTO events (kind, subject, created_at) VALUES (?, ?, ?)",
                ("inbox.message_acknowledged", str(message_id), consumed_at),
            )
        return True

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

    def _append_debug_log(self, message: Message) -> None:
        """Write a human-portable debug mirror after durable SQLite persistence."""
        if self._debug_log_path is None:
            return
        self._debug_log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "event": "message",
            "id": message.id,
            "sender": message.sender,
            "recipient": message.recipient,
            "text": message.text,
            "created_at": message.created_at.isoformat(),
        }
        with self._debug_log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(record) + "\n")

    def close(self) -> None:
        self._connection.close()
