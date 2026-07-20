"""Supervisor and serialized REPL queues for the first runtime spike."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import queue
import threading
from typing import Any

from .journal import InboxJournal, Message
from .kernel import ExecutionState, PersistentKernel
from .observable_state import ObservableStateRegistry, ObservableValue
from .tasks import TaskRegistry


_STOP = object()


def _task_id(value: Any) -> int:
    if isinstance(value, dict):
        value = value["id"]
    return int(value)


@dataclass(frozen=True)
class RestartReport:
    """What a kernel restart restored and what it intentionally did not."""

    agent: str
    restored_inbox_messages: int
    restored_observable_values: int
    lost_ephemeral_kernel_state: bool = True
    forced_termination: bool = False


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

    def __init__(self, journal: InboxJournal, observable_state: ObservableStateRegistry | None = None, tasks: TaskRegistry | None = None) -> None:
        self.journal = journal
        self._owns_observable_state = observable_state is None
        self.observable_state = observable_state or ObservableStateRegistry()
        self.tasks = tasks or TaskRegistry()
        self._owns_tasks = tasks is None
        self._repls: dict[str, ReplQueue] = {}
        self._handlers: dict[str, Callable[[Message], None]] = {}
        self._kernels: dict[str, PersistentKernel] = {}
        self._active_tasks: dict[str, int] = {}
        self._scheduler_running = threading.Event()
        self._scheduler_running.set()
        self._scheduler_thread = threading.Thread(target=self._schedule_due_tasks, name="task-scheduler", daemon=True)
        self._scheduler_thread.start()

    def _schedule_due_tasks(self) -> None:
        while self._scheduler_running.wait(0.1):
            for task in self.tasks.due(datetime.now(UTC)):
                message = self.journal.append(recipient=task.owner, sender="supervisor", text=f"Task {task.id} is due: {task.title}")
                kernel = self._kernels.get(task.owner)
                if kernel is not None:
                    kernel.deliver(message)

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
        kernel = PersistentKernel(
            agent,
            lambda kind, payload: self._handle_kernel_capability(agent, kind, payload),
            execution_observer=lambda state: self._publish_execution_state(agent, state),
        )
        kernel.start()
        for message in self.journal.pending(agent):
            kernel.deliver(message)
        self._kernels[agent] = kernel
        self.publish_state(agent, "runtime", {"status": "idle"}, presenter="runtime")
        return kernel

    def agent_kernel(self, agent: str) -> PersistentKernel:
        try:
            return self._kernels[agent]
        except KeyError as error:
            raise KeyError(f"No running agent kernel: {agent}") from error

    def start_user_kernel(self, user: str = "user", agent: str = "agent") -> PersistentKernel:
        """Start the user's persistent Python REPL with explicit read/write capabilities."""
        if user in self._kernels:
            raise ValueError(f"User kernel already exists: {user}")
        if user not in self._repls:
            self.create_repl(user)
        kernel = PersistentKernel(
            user,
            lambda kind, payload: self._handle_user_capability(user, agent, kind, payload),
            role="user",
        )
        kernel.start()
        for message in self.journal.pending(user):
            kernel.deliver(message)
        self._kernels[user] = kernel
        return kernel

    def restart_agent_kernel(self, agent: str) -> tuple[PersistentKernel, RestartReport]:
        """Replace a kernel and explicitly report the durable state rehydrated."""
        existing = self._kernels.pop(agent, None)
        if existing is not None:
            existing.stop()
        report = RestartReport(
            agent=agent,
            restored_inbox_messages=len(self.journal.pending(agent)),
            restored_observable_values=len(self.observable_state.list(agent)),
        )
        return self.start_agent_kernel(agent), report

    def recover_agent_kernel(self, agent: str) -> tuple[PersistentKernel, RestartReport]:
        """Force-stop an unresponsive agent kernel and rehydrate durable state."""
        existing = self._kernels.pop(agent, None)
        if existing is None:
            raise KeyError(f"No running agent kernel: {agent}")
        existing.terminate()
        report = RestartReport(
            agent=agent,
            restored_inbox_messages=len(self.journal.pending(agent)),
            restored_observable_values=len(self.observable_state.list(agent)),
            forced_termination=True,
        )
        return self.start_agent_kernel(agent), report

    def publish_state(
        self,
        owner: str,
        name: str,
        value: Any,
        presenter: str = "json",
        show_by_default: bool = True,
        label: str | None = None,
        priority: int = 0,
    ) -> ObservableValue:
        published = self.observable_state.publish(owner, name, value, presenter, show_by_default, label, priority)
        for task in self.tasks.observe(owner, name, value):
            message = self.journal.append(recipient=owner, sender="supervisor", text=f"Task {task.id} is ready: {task.title}")
            kernel = self._kernels.get(owner)
            if kernel is not None:
                kernel.deliver(message)
        return published

    def _publish_execution_state(self, agent: str, state: ExecutionState) -> None:
        self.publish_state(
            agent,
            "runtime",
            {
                "status": state.status,
                "request_id": state.request_id,
                "started_at": state.started_at.isoformat() if state.started_at else None,
                "finished_at": state.finished_at.isoformat() if state.finished_at else None,
                "error": state.error,
            },
            presenter="runtime",
        )
        if state.status == "failed" and state.error is not None:
            task_id = self._active_tasks.get(agent)
            if task_id is not None:
                self.tasks.report_error(agent, task_id, state.error)

    def _handle_kernel_capability(self, agent: str, kind: str, payload: dict[str, Any]) -> Any:
        if kind == "inbox.ack":
            return self.journal.acknowledge(agent, int(payload["message_id"]))
        if kind == "inbox.reply_to_latest":
            message_id = int(payload["message_id"])
            if not any(message.id == message_id for message in self.journal.pending(agent)):
                return False
            message = self.journal.append(recipient="user", sender=agent, text=str(payload["text"]))
            user_kernel = self._kernels.get("user")
            if user_kernel is not None:
                user_kernel.deliver(message)
            return self.journal.acknowledge(agent, message_id)
        if kind == "tasks.announce":
            task = self.tasks.announce(agent, str(payload["title"]), payload.get("details"))
            return {"id": task.id, "state": task.state, "title": task.title}
        if kind == "tasks.take":
            task = self.tasks.transition(agent, _task_id(payload["task_id"]), "working")
            self._active_tasks[agent] = task.id
            return {"id": task.id, "state": task.state}
        if kind == "tasks.complete":
            task = self.tasks.transition(agent, _task_id(payload["task_id"]), "completed")
            if self._active_tasks.get(agent) == task.id:
                self._active_tasks.pop(agent, None)
            return {"id": task.id, "state": task.state}
        if kind == "tasks.wait_for":
            task = self.tasks.wait_for(agent, _task_id(payload["task_id"]), str(payload["name"]), payload.get("equals"))
            return {"id": task.id, "state": task.state}
        if kind == "tasks.report_error":
            return self.tasks.report_error(agent, _task_id(payload["task_id"]), str(payload["error"]))
        if kind == "tasks.challenge":
            task = self.tasks.challenge(agent, _task_id(payload["task_id"]), str(payload["description"]))
            return {"id": task.id, "state": task.state, "title": task.title}
        if kind == "tasks.schedule_after":
            seconds = float(payload["seconds"])
            if seconds < 0:
                raise ValueError("seconds must be non-negative")
            task = self.tasks.schedule(agent, _task_id(payload["task_id"]), datetime.now(UTC) + timedelta(seconds=seconds))
            return {"id": task.id, "state": task.state, "due_at": task.due_at}
        if kind == "tasks.list":
            return [
                {"id": task.id, "title": task.title, "state": task.state, "details": task.details,
                 "taken_by": task.taken_by, "taken_at": task.taken_at, "announced_at": task.announced_at}
                for task in self.tasks.list(agent)
            ]
        if kind == "context.tasks.get":
            task = self.tasks.get(int(payload["task_id"]))
            if task is None:
                return None
            return {"id": task.id, "owner": task.owner, "title": task.title, "state": task.state, "details": task.details,
                    "taken_by": task.taken_by, "taken_at": task.taken_at, "announced_at": task.announced_at}
        if kind == "context.tasks.events":
            return self.tasks.events(int(payload["task_id"]))
        if kind == "context.errors.for_task":
            return self.tasks.errors(int(payload["task_id"]))
        if kind == "context.errors.search":
            return self.tasks.search_errors(str(payload["text"]))
        if kind == "context.observations.get":
            value = self.observable_state.get(agent, str(payload["name"]))
            if value is None:
                return None
            return {"value": value.value, "revision": value.revision, "updated_at": value.updated_at.isoformat(), "presenter": value.presenter}
        if kind == "context.messages.with_party":
            party = str(payload["party"])
            return [self._conversation_data(message) for message in self.journal.conversation(agent, party)]
        if kind == "context.agents.list":
            return sorted(name for name in self._repls if name != "user")
        if kind == "context.conflicts.related":
            return self.tasks.related_conflicts(str(payload["resource"]))
        if kind == "agents.message":
            recipient = str(payload["recipient"])
            if recipient not in self._repls:
                raise KeyError(f"Unknown agent: {recipient}")
            message = self.journal.append(recipient=recipient, sender=agent, text=str(payload["text"]))
            kernel = self._kernels.get(recipient)
            if kernel is not None:
                kernel.deliver(message)
            return {"id": message.id, "recipient": recipient}
        if kind == "conflicts.announce":
            return self.tasks.announce_conflict(agent, str(payload["resource"]), str(payload["summary"]), list(payload.get("related_tasks", [])))
        if kind == "inbox.handler_failed":
            self.publish_state(
                agent,
                "inbox_error",
                {"message_id": payload["message_id"], "error": payload["error"]},
                presenter="error",
                label="Inbox handler error",
                priority=100,
            )
            return None
        if kind == "observable.publish":
            value = self.publish_state(
                agent,
                str(payload["name"]),
                payload["value"],
                str(payload["presenter"]),
                bool(payload["show_by_default"]),
                payload["label"],
                int(payload["priority"]),
            )
            return {"name": value.name, "revision": value.revision, "presenter": value.presenter}
        if kind == "user_inbox.add":
            message = self.journal.append(recipient="user", sender=agent, text=str(payload["text"]))
            user_kernel = self._kernels.get("user")
            if user_kernel is not None:
                user_kernel.deliver(message)
            return {"id": message.id, "recipient": message.recipient}
        raise ValueError(f"Capability is not granted: {kind}")

    def _handle_user_capability(self, user: str, agent: str, kind: str, payload: dict[str, Any]) -> Any:
        if kind == "presentable.list":
            return {value.name: value.value for value in self.observable_state.list(agent, default_only=True)}
        if kind == "presentable.get":
            value = self.observable_state.get(agent, str(payload["name"]))
            if value is None:
                return None
            return {"value": value.value, "revision": value.revision, "presenter": value.presenter}
        if kind == "agent_inbox.pending":
            return [self._message_data(message) for message in self.journal.pending(agent)]
        if kind == "agent_inbox.add":
            message = self.append_user_message(agent, str(payload["text"]))
            return self._message_data(message)
        if kind == "conversation.messages":
            return [self._conversation_data(message) for message in self.journal.conversation(user, agent)]
        raise ValueError(f"Capability is not granted: {kind}")

    @staticmethod
    def _message_data(message: Message) -> dict[str, Any]:
        return {"id": message.id, "sender": message.sender, "text": message.text}

    @staticmethod
    def _conversation_data(message: Message) -> dict[str, Any]:
        return {
            "id": message.id,
            "sender": message.sender,
            "recipient": message.recipient,
            "text": message.text,
            "created_at": message.created_at.isoformat(),
        }

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
        self.journal.acknowledge(message.recipient, message.id)

    def close(self) -> None:
        self._scheduler_running.clear()
        self._scheduler_thread.join(timeout=1)
        for kernel in self._kernels.values():
            kernel.stop()
        for repl in self._repls.values():
            repl.close()
        if self._owns_observable_state:
            self.observable_state.close()
        if self._owns_tasks:
            self.tasks.close()
