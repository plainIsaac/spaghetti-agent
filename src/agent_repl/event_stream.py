"""Structured, live runtime events with an optional durable JSONL mirror."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import queue
from threading import RLock
from typing import Any


_SENSITIVE_KEYS = {"api_key", "authorization", "cookie", "password", "secret", "access_token", "refresh_token"}


@dataclass(frozen=True)
class RuntimeEvent:
    id: int
    kind: str
    timestamp: str
    agent: str | None
    turn_id: str | None
    task_id: int | None
    assertion_id: int | None
    payload: Any

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class RuntimeEventStream:
    """Thread-safe event history and fan-out for UI and test subscribers."""

    def __init__(self, path: str | Path | None = None, history_limit: int = 2_000) -> None:
        self.path = Path(path) if path is not None else None
        self._history: deque[RuntimeEvent] = deque(maxlen=history_limit)
        self._subscribers: set[queue.Queue[RuntimeEvent]] = set()
        self._lock = RLock()
        self._next_id = 0
        if self.path is not None and self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines()[-history_limit:]:
                try:
                    event = RuntimeEvent(**json.loads(line))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                self._history.append(event)
                self._next_id = max(self._next_id, event.id)

    def emit(self, kind: str, *, agent: str | None = None, turn_id: str | None = None,
             task_id: int | None = None, assertion_id: int | None = None, payload: Any = None) -> RuntimeEvent:
        with self._lock:
            self._next_id += 1
            event = RuntimeEvent(
                self._next_id, str(kind), datetime.now(UTC).isoformat(), agent, turn_id,
                task_id, assertion_id, self._redact(payload),
            )
            self._history.append(event)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as output:
                    output.write(json.dumps(event.as_dict(), ensure_ascii=False, default=str) + "\n")
            for subscriber in tuple(self._subscribers):
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    pass
            return event

    def recent(self, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            return [event.as_dict() for event in self._history if event.id > after][-max(1, min(limit, 1_000)):]

    def subscribe(self, max_pending: int = 500) -> queue.Queue[RuntimeEvent]:
        subscriber: queue.Queue[RuntimeEvent] = queue.Queue(maxsize=max_pending)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[RuntimeEvent]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): "[redacted]" if str(key).lower() in _SENSITIVE_KEYS else cls._redact(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._redact(item) for item in value]
        if isinstance(value, str) and len(value) > 8_000:
            return value[:8_000] + "…[truncated]"
        return value
