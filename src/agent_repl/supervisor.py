"""Supervisor and serialized REPL queues for the first runtime spike."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
import queue
import threading
from typing import Any

from .journal import InboxJournal, Message
from .kernel import PersistentKernel
from .observable_state import ObservableStateRegistry, ObservableValue


_STOP = object()


class ReplQueue:
    """One serialized execution lane. It models a REPL, not process isolation."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._items: queue.Queue[tuple[Callable[[], Any], Future[Any]] | object] = queue.Queue()
        self._thread = threading.Thread(target=self._work, name=f"repl:{name}", daemon=True)
        self._thread.start()

    def submit(self, action: Callable[[], Any]) -> Future[Any]:
        future: Future[Any] = Future()
        self._items.put((action, future))
        return future

    def _work(self) -> None:
        while True:
            item = self._items.get()
            if item is _STOP:
                return
            action, future = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(action())
            except BaseException as error:
                future.set_exception(error)

    def close(self) -> None:
        self._items.put(_STOP)
        self._thread.join(timeout=1)


class Supervisor:
    """Coordinates explicit inbox sharing and schedules opt-in delivery."""

    def __init__(self, journal: InboxJournal, observable_state: ObservableStateRegistry | None = None) -> None:
        self.journal = journal
        self.observable_state = observable_state or ObservableStateRegistry()
        self._repls: dict[str, ReplQueue] = {}
        self._handlers: dict[str, Callable[[Message], None]] = {}
        self._kernels: dict[str, PersistentKernel] = {}

    def create_repl(self, name: str) -> ReplQueue:
        if name in self._repls:
            raise ValueError(f"REPL already exists: {name}")
        repl = ReplQueue(name)
        self._repls[name] = repl
        return repl

    def subscribe_inbox(self, agent: str, handler: Callable[[Message], None]) -> None:
        """Opt in to later, serialized handling of newly appended messages."""
        if agent not in self._repls:
            raise KeyError(f"Unknown agent REPL: {agent}")
        self._handlers[agent] = handler

    def start_agent_kernel(self, agent: str) -> PersistentKernel:
        """Start a persistent agent namespace and hydrate its pending inbox."""
        if agent in self._kernels:
            raise ValueError(f"Agent kernel already exists: {agent}")
        kernel = PersistentKernel(agent)
        kernel.start()
        for message in self.journal.pending(agent):
            kernel.deliver(message)
        self._kernels[agent] = kernel
        return kernel

    def publish_state(self, owner: str, name: str, value: Any, presenter: str = "json") -> ObservableValue:
        return self.observable_state.publish(owner, name, value, presenter)

    def append_user_message(self, agent: str, text: str) -> Message:
        """Durably append first; any handler runs later in the agent REPL."""
        message = self.journal.append(recipient=agent, sender="user", text=text)
        kernel = self._kernels.get(agent)
        if kernel is not None:
            kernel.deliver(message)
        handler = self._handlers.get(agent)
        if handler is not None:
            self._repls[agent].submit(lambda: self._deliver(message, handler))
        return message

    def _deliver(self, message: Message, handler: Callable[[Message], None]) -> None:
        handler(message)
        self.journal.acknowledge(message.id)

    def close(self) -> None:
        for kernel in self._kernels.values():
            kernel.stop()
        for repl in self._repls.values():
            repl.close()
