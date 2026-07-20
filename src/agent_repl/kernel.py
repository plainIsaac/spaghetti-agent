"""A persistent, process-backed agent kernel experiment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing import get_context
from multiprocessing.queues import Queue
import queue
import threading
from typing import Any, Callable

from .journal import Message


@dataclass(frozen=True)
class KernelResult:
    status: str
    value: Any = None
    error: str | None = None


@dataclass(frozen=True)
class ExecutionState:
    request_id: int | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None = None
    error: str | None = None


class KernelInbox:
    """The agent-visible cache of supervisor-owned durable inbox messages."""

    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._messages: list[dict[str, Any]] = []
        self._call_supervisor = call_supervisor
        self._message_handler: Callable[[dict[str, Any]], None] | None = None

    def add_message(self, sender: str, text: str, message_id: int | None = None) -> None:
        message = {"id": message_id, "sender": sender, "text": text}
        self._messages.append(message)
        if self._message_handler is not None:
            try:
                self._message_handler(message)
            except BaseException as error:
                self._call_supervisor(
                    "inbox.handler_failed",
                    {"message_id": message_id, "error": f"{type(error).__name__}: {error}"},
                )

    def on_message(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Handle future deliveries in a later, serialized kernel execution unit."""
        if not callable(handler):
            raise TypeError("inbox.on_message expects a callable")
        self._message_handler = handler

    def pending(self) -> list[dict[str, Any]]:
        return list(self._messages)

    def ack(self, message_id: int) -> bool:
        """Acknowledge a processed message through the supervisor."""
        acknowledged = bool(self._call_supervisor("inbox.ack", {"message_id": message_id}))
        if acknowledged:
            self._messages = [message for message in self._messages if message["id"] != message_id]
        return acknowledged


class Observable:
    """The agent's explicit, presentable-state publishing capability."""

    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def publish(
        self,
        name: str,
        value: Any,
        presenter: str = "json",
        show_by_default: bool = True,
        label: str | None = None,
        priority: int = 0,
    ) -> dict[str, Any]:
        return self._call_supervisor(
            "observable.publish",
            {
                "name": name,
                "value": value,
                "presenter": presenter,
                "show_by_default": show_by_default,
                "label": label,
                "priority": priority,
            },
        )


class UserInbox:
    """The agent's channel for concise, intentional messages to the user."""

    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def add(self, text: str) -> dict[str, Any]:
        return self._call_supervisor("user_inbox.add", {"text": text})


class User:
    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self.inbox = UserInbox(call_supervisor)


class PresentableState:
    """Read-only presentable state granted to the user's REPL."""

    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def list(self) -> dict[str, Any]:
        return self._call_supervisor("presentable.list", {})

    def __getitem__(self, name: str) -> Any:
        entry = self._call_supervisor("presentable.get", {"name": name})
        if entry is None:
            raise KeyError(name)
        return entry["value"]


class AgentInbox:
    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def pending(self) -> list[dict[str, Any]]:
        return self._call_supervisor("agent_inbox.pending", {})


class Agent:
    """The user REPL's intentionally narrow capability for one agent."""

    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor
        self.inbox = AgentInbox(call_supervisor)

    def send(self, text: str) -> dict[str, Any]:
        return self._call_supervisor("agent_inbox.add", {"text": text})


class Conversation:
    """The user REPL's optional raw-message debugging capability."""

    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def messages(self) -> list[dict[str, Any]]:
        return self._call_supervisor("conversation.messages", {})


def _kernel_main(
    commands: Queue[Any],
    responses: Queue[Any],
    capability_requests: Queue[Any],
    capability_responses: Queue[Any],
    role: str,
) -> None:
    next_capability_request_id = 0

    def call_supervisor(kind: str, payload: dict[str, Any]) -> Any:
        nonlocal next_capability_request_id
        next_capability_request_id += 1
        request_id = next_capability_request_id
        capability_requests.put({"request_id": request_id, "kind": kind, "payload": payload})
        while True:
            response = capability_responses.get()
            if response["request_id"] != request_id:
                continue
            if response["status"] == "error":
                raise RuntimeError(response["error"])
            return response["value"]

    inbox = KernelInbox(call_supervisor)
    namespace: dict[str, Any] = {"__name__": "__agent_repl_kernel__", "inbox": inbox}
    if role == "agent":
        namespace["observable"] = Observable(call_supervisor)
        namespace["user"] = User(call_supervisor)
    elif role == "user":
        namespace["presentable"] = PresentableState(call_supervisor)
        namespace["agent"] = Agent(call_supervisor)
        namespace["conversation"] = Conversation(call_supervisor)
    else:
        raise ValueError(f"Unknown kernel role: {role}")
    while True:
        command = commands.get()
        kind = command["kind"]
        if kind == "stop":
            return
        if kind == "deliver":
            message = command["message"]
            inbox.add_message(message["sender"], message["text"], message["id"])
            continue
        if kind != "evaluate":
            continue

        request_id = command["request_id"]
        try:
            namespace.pop("_result", None)
            exec(compile(command["source"], "<agent-repl-kernel>", "exec"), namespace, namespace)
            responses.put({"request_id": request_id, "status": "ok", "value": namespace.get("_result")})
        except BaseException as error:
            responses.put(
                {
                    "request_id": request_id,
                    "status": "error",
                    "error": f"{type(error).__name__}: {error}",
                }
            )


class PersistentKernel:
    """A process with a persistent Python namespace and a serialized command queue.

    Delivery commands wait behind a running evaluation by design: an inbox is a
    durable event source, not an asynchronous interruption of Python code.
    """

    def __init__(
        self,
        name: str,
        capability_handler: Callable[[str, dict[str, Any]], Any],
        role: str = "agent",
        execution_observer: Callable[[ExecutionState], None] | None = None,
    ) -> None:
        context = get_context("spawn")
        self.name = name
        self.role = role
        self._commands: Queue[Any] = context.Queue()
        self._responses: Queue[Any] = context.Queue()
        self._capability_requests: Queue[Any] = context.Queue()
        self._capability_responses: Queue[Any] = context.Queue()
        self._process = context.Process(
            target=_kernel_main,
            args=(self._commands, self._responses, self._capability_requests, self._capability_responses, role),
            name=f"{role}:{name}",
        )
        self._capability_handler = capability_handler
        self._execution_observer = execution_observer
        self._serving_capabilities = threading.Event()
        self._capability_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._next_request_id = 0
        self._execution = ExecutionState(None, "idle", None)

    def start(self) -> None:
        self._process.start()
        self._serving_capabilities.set()
        self._capability_thread = threading.Thread(
            target=self._serve_capabilities,
            name=f"kernel-capabilities:{self.name}",
            daemon=True,
        )
        self._capability_thread.start()

    def _serve_capabilities(self) -> None:
        while self._serving_capabilities.is_set():
            try:
                request = self._capability_requests.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                value = self._capability_handler(request["kind"], request["payload"])
                response = {"request_id": request["request_id"], "status": "ok", "value": value}
            except BaseException as error:
                response = {
                    "request_id": request["request_id"],
                    "status": "error",
                    "error": f"{type(error).__name__}: {error}",
                }
            self._capability_responses.put(response)

    def deliver(self, message: Message) -> None:
        self._commands.put(
            {
                "kind": "deliver",
                "message": {"id": message.id, "sender": message.sender, "text": message.text},
            }
        )

    def evaluate(self, source: str, timeout: float = 2) -> KernelResult:
        """Evaluate source in the persistent namespace and return its `_result`."""
        with self._lock:
            if not self._process.is_alive():
                raise RuntimeError("Kernel is not running")
            self._next_request_id += 1
            request_id = self._next_request_id
            started_at = datetime.now(UTC)
            self._set_execution(ExecutionState(request_id, "running", started_at))
            self._commands.put({"kind": "evaluate", "request_id": request_id, "source": source})
            try:
                response = self._responses.get(timeout=timeout)
            except queue.Empty as error:
                self._set_execution(ExecutionState(request_id, "unresponsive", started_at, error="evaluation timed out"))
                raise TimeoutError("Evaluation did not yield a result") from error
        if response["request_id"] != request_id:
            raise RuntimeError("Received a response for another evaluation")
        result = KernelResult(response["status"], response.get("value"), response.get("error"))
        status = "completed" if result.status == "ok" else "failed"
        self._set_execution(ExecutionState(request_id, status, started_at, datetime.now(UTC), result.error))
        return result

    def _set_execution(self, state: ExecutionState) -> None:
        self._execution = state
        if self._execution_observer is not None:
            self._execution_observer(state)

    def stop(self) -> None:
        self._serving_capabilities.clear()
        if self._process.is_alive():
            self._commands.put({"kind": "stop"})
            self._process.join(timeout=1)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=1)
        if self._capability_thread is not None:
            self._capability_thread.join(timeout=1)

    def terminate(self) -> bool:
        """Immediately stop a wedged kernel; durable state is recovered by its supervisor."""
        was_alive = self._process.is_alive()
        self._serving_capabilities.clear()
        if was_alive:
            self._process.terminate()
            self._process.join(timeout=1)
        if self._capability_thread is not None:
            self._capability_thread.join(timeout=1)
        self._set_execution(
            ExecutionState(
                self._execution.request_id,
                "terminated",
                self._execution.started_at,
                datetime.now(UTC),
                "kernel terminated by supervisor",
            )
        )
        return was_alive

    @property
    def execution(self) -> ExecutionState:
        return self._execution

    @property
    def alive(self) -> bool:
        return self._process.is_alive()
