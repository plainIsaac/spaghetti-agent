"""Durable inbox storage. The inbox is state; notifications are separate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import sqlite3
import threading
import time


@dataclass(frozen=True)
class Message:
    id: int
    recipient: str
    sender: str
    text: str
    created_at: datetime


@dataclass(frozen=True)
class AgentAssertion:
    id: int
    requester: str
    responder: str
    claim: str
    context: object
    mode: str
    status: str
    passed: bool | None
    evidence: object
    created_at: datetime
    resolved_at: datetime | None


class InboxJournal:
    """A small SQLite-backed journal for durable, explicitly shared messages."""

    def __init__(self, path: str = ":memory:", debug_log_path: str | None = None) -> None:
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._assertion_changed = threading.Condition(self._lock)
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
                CREATE TABLE IF NOT EXISTS agent_assertions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    requester TEXT NOT NULL,
                    responder TEXT NOT NULL,
                    claim TEXT NOT NULL,
                    context TEXT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    passed INTEGER,
                    evidence TEXT,
                    created_at TEXT NOT NULL,
                    resolved_at TEXT
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

    def create_assertion(self, requester: str, responder: str, claim: str, context: object, mode: str) -> AgentAssertion:
        created_at = datetime.now(UTC)
        with self._assertion_changed, self._connection:
            cursor = self._connection.execute(
                "INSERT INTO agent_assertions(requester,responder,claim,context,mode,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                (requester, responder, claim, json.dumps(context), mode, created_at.isoformat()),
            )
            assertion_id = int(cursor.lastrowid)
        return self.get_assertion(assertion_id)  # type: ignore[return-value]

    def get_assertion(self, assertion_id: int) -> AgentAssertion | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM agent_assertions WHERE id=?", (assertion_id,)).fetchone()
        return None if row is None else self._to_assertion(row)

    def pending_assertions(self, responder: str) -> list[AgentAssertion]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM agent_assertions WHERE responder=? AND status='pending' ORDER BY id", (responder,)
            ).fetchall()
        return [self._to_assertion(row) for row in rows]

    def resolve_assertion(self, assertion_id: int, responder: str, passed: bool, evidence: object) -> AgentAssertion:
        resolved_at = datetime.now(UTC)
        with self._assertion_changed, self._connection:
            cursor = self._connection.execute(
                "UPDATE agent_assertions SET status='resolved',passed=?,evidence=?,resolved_at=? "
                "WHERE id=? AND responder=? AND status='pending'",
                (int(passed), json.dumps(evidence), resolved_at.isoformat(), assertion_id, responder),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"pending assertion {assertion_id} is not assigned to {responder}")
            row = self._connection.execute("SELECT * FROM agent_assertions WHERE id=?", (assertion_id,)).fetchone()
            self._assertion_changed.notify_all()
        return self._to_assertion(row)

    def wait_for_assertion(self, assertion_id: int, requester: str, timeout: float) -> AgentAssertion:
        deadline = time.monotonic() + timeout
        with self._assertion_changed:
            while True:
                row = self._connection.execute("SELECT * FROM agent_assertions WHERE id=? AND requester=?", (assertion_id, requester)).fetchone()
                if row is None:
                    raise KeyError(assertion_id)
                assertion = self._to_assertion(row)
                if assertion.status == "resolved":
                    return assertion
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"assertion {assertion_id} was not resolved within {timeout:g} seconds")
                self._assertion_changed.wait(remaining)

    @staticmethod
    def _to_assertion(row: sqlite3.Row) -> AgentAssertion:
        return AgentAssertion(
            id=int(row["id"]), requester=str(row["requester"]), responder=str(row["responder"]),
            claim=str(row["claim"]), context=json.loads(row["context"]) if row["context"] is not None else None,
            mode=str(row["mode"]), status=str(row["status"]),
            passed=None if row["passed"] is None else bool(row["passed"]),
            evidence=json.loads(row["evidence"]) if row["evidence"] is not None else None,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            resolved_at=datetime.fromisoformat(str(row["resolved_at"])) if row["resolved_at"] else None,
        )

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
