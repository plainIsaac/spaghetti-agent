"""Registered static workers: fixed runtime code with durable configuration."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch


@dataclass(frozen=True)
class WorkspaceWatcher:
    id: int
    paths: tuple[str, ...]
    recipient: str
    message: str

    def matches(self, path: str) -> bool:
        return any(fnmatch(path, pattern) for pattern in self.paths)
