"""A persistent, process-backed agent kernel experiment."""

from __future__ import annotations

import ast
from collections.abc import Collection, Iterator
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


DEFAULT_NON_COLLECTION_LOOP_LIMIT = 1_000


class LoopLimitExceeded(RuntimeError):
    """A non-collection loop exceeded its supervisor-visible execution budget."""


class LoopBudget:
    """One-shot loop budgets set by `loop_limit` before the next guarded loop."""

    def __init__(self, default_limit: int = DEFAULT_NON_COLLECTION_LOOP_LIMIT) -> None:
        self.default_limit = default_limit
        self.next_limit: int | None = None
        self.counts: dict[int, int] = {}
        self.limits: dict[int, int] = {}

    def set_next(self, limit: int) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("loop_limit expects a positive integer")
        self.next_limit = limit

    def guard_while(self, loop_id: int) -> None:
        self._advance(loop_id)

    def guarded_iterable(self, value: object, loop_id: int):
        if isinstance(value, Collection):
            return iter(value)
        return _GuardedIterator(iter(value), lambda: self._advance(loop_id))

    def _advance(self, loop_id: int) -> None:
        count = self.counts.get(loop_id, 0) + 1
        self.counts[loop_id] = count
        if count == 1:
            self.limits[loop_id] = self.next_limit if self.next_limit is not None else self.default_limit
            self.next_limit = None
        limit = self.limits[loop_id]
        if count > limit:
            raise LoopLimitExceeded(f"non-collection loop exceeded its {limit:,} iteration limit")


class _GuardedIterator(Iterator[Any]):
    def __init__(self, iterator: Iterator[Any], advance: Callable[[], None]) -> None:
        self._iterator = iterator
        self._advance = advance

    def __next__(self) -> Any:
        value = next(self._iterator)
        self._advance()
        return value


class _LoopGuardTransformer(ast.NodeTransformer):
    def __init__(self) -> None:
        self._next_loop_id = 0

    def _loop_id(self) -> int:
        self._next_loop_id += 1
        return self._next_loop_id

    def visit_While(self, node: ast.While) -> ast.While:
        self.generic_visit(node)
        guard = ast.Expr(ast.Call(ast.Name("_agent_repl_guard_while", ast.Load()), [ast.Constant(self._loop_id())], []))
        node.body.insert(0, ast.copy_location(guard, node))
        return node

    def visit_For(self, node: ast.For) -> ast.For:
        self.generic_visit(node)
        node.iter = ast.copy_location(
            ast.Call(ast.Name("_agent_repl_guarded_iterable", ast.Load()), [node.iter, ast.Constant(self._loop_id())], []),
            node.iter,
        )
        return node


def _compile_agent_source(source: str) -> Any:
    tree = ast.parse(source, "<agent-repl-kernel>", "exec")
    tree = _LoopGuardTransformer().visit(tree)
    return compile(ast.fix_missing_locations(tree), "<agent-repl-kernel>", "exec")


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
        message = InboxMessage(id=message_id, sender=sender, text=text)
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

    def reply_to_latest(self, text: str) -> bool:
        """Reply to and acknowledge the newest pending message in one operation."""
        if not self._messages:
            return False
        message = self._messages[-1]
        replied = bool(
            self._call_supervisor(
                "inbox.reply_to_latest",
                {"message_id": message["id"], "text": str(text)},
            )
        )
        if replied:
            self._messages = [item for item in self._messages if item["id"] != message["id"]]
        return replied


class InboxMessage(dict[str, Any]):
    """Inbox data usable with either Python attributes or mapping syntax."""

    @property
    def id(self) -> int | None:
        return self["id"]

    @property
    def message_id(self) -> int | None:
        return self["id"]


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


class Tasks:
    """Supervisor-owned durable task lifecycle for an agent REPL."""

    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def announce(self, title: str, details: Any = None) -> dict[str, Any]:
        return self._call_supervisor("tasks.announce", {"title": title, "details": details})

    def take(self, task_id: Any) -> dict[str, Any]:
        return self._call_supervisor("tasks.take", {"task_id": task_id})

    def complete(self, task_id: Any) -> dict[str, Any]:
        return self._call_supervisor("tasks.complete", {"task_id": task_id})

    def wait_for(self, task_id: Any, name: str, equals: Any) -> dict[str, Any]:
        return self._call_supervisor("tasks.wait_for", {"task_id": task_id, "name": name, "equals": equals})

    def report_error(self, task_id: Any, error: str) -> dict[str, Any]:
        return self._call_supervisor("tasks.report_error", {"task_id": task_id, "error": error})

    def challenge(self, task_id: Any, description: str) -> dict[str, Any]:
        return self._call_supervisor("tasks.challenge", {"task_id": task_id, "description": description})

    def schedule_after(self, task_id: Any, seconds: float) -> dict[str, Any]:
        return self._call_supervisor("tasks.schedule_after", {"task_id": task_id, "seconds": seconds})

    def list(self) -> list[dict[str, Any]]:
        return self._call_supervisor("tasks.list", {})

    def delegate(self, agent: str, title: str, details: Any = None) -> dict[str, Any]:
        return self._call_supervisor("tasks.delegate", {"agent": agent, "title": title, "details": details})


class Workspace:
    """Managed files for task-scoped, conflict-aware coordinated work."""

    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def list(self, path: str = ".") -> list[str]:
        return self._call_supervisor("workspace.list", {"path": path})

    def read_text(self, path: str) -> dict[str, str]:
        return self._call_supervisor("workspace.read_text", {"path": path})

    def claim(self, path: str, task_id: Any = None) -> dict[str, Any]:
        return self._call_supervisor("workspace.claim", {"task_id": task_id, "path": path})

    def write_text(self, path: str, text: str, task_id: Any = None, expected_revision: str | None = None) -> dict[str, str]:
        return self._call_supervisor("workspace.write_text", {"task_id": task_id, "path": path, "text": text, "expected_revision": expected_revision})

    def changes(self, task_id: Any = None) -> list[dict[str, Any]]:
        return self._call_supervisor("workspace.changes", {"task_id": task_id})

    def branch(self, task_id: Any = None) -> dict[str, Any]:
        return self._call_supervisor("workspace.branch", {"task_id": task_id})

    def diff(self, task_id: Any = None) -> list[dict[str, str]]:
        return self._call_supervisor("workspace.diff", {"task_id": task_id})

    def submit(self, task_id: Any = None) -> dict[str, Any]:
        return self._call_supervisor("workspace.submit", {"task_id": task_id})

    def merge(self, task_id: Any) -> dict[str, Any]:
        return self._call_supervisor("workspace.merge", {"task_id": task_id})


class ContextTasks:
    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def get(self, task_id: int) -> dict[str, Any] | None:
        return self._call_supervisor("context.tasks.get", {"task_id": task_id})

    def events(self, task_id: int) -> list[dict[str, Any]]:
        return self._call_supervisor("context.tasks.events", {"task_id": task_id})

    def errors(self, task_id: int) -> list[dict[str, Any]]:
        return self._call_supervisor("context.errors.for_task", {"task_id": task_id})


class ContextErrors:
    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def search(self, text: str) -> list[dict[str, Any]]:
        return self._call_supervisor("context.errors.search", {"text": text})


class ContextObservations:
    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def get(self, name: str) -> dict[str, Any] | None:
        return self._call_supervisor("context.observations.get", {"name": name})


class ContextMessages:
    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def with_party(self, party: str) -> list[dict[str, Any]]:
        return self._call_supervisor("context.messages.with_party", {"party": party})


class ContextAgents:
    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def list(self) -> list[str]:
        return self._call_supervisor("context.agents.list", {})


class ContextConflicts:
    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def related(self, resource: str) -> list[dict[str, Any]]:
        return self._call_supervisor("context.conflicts.related", {"resource": resource})


class LocalContext:
    """Small scoped values an agent can deliberately make available next turn."""

    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def set(self, key: str, value: Any, lifetime: str = "session", scope_id: str = "", *, model_visible: bool = False) -> dict[str, Any]:
        return self._call_supervisor("working_context.set", {"key": key, "value": value, "lifetime": lifetime, "scope_id": scope_id, "model_visible": model_visible})

    def get(self, key: str, lifetime: str = "session", scope_id: str = "") -> Any:
        return self._call_supervisor("working_context.get", {"key": key, "lifetime": lifetime, "scope_id": scope_id})

    def clear(self, lifetime: str = "session", scope_id: str = "", key: str | None = None) -> int:
        return self._call_supervisor("working_context.clear", {"lifetime": lifetime, "scope_id": scope_id, "key": key})


class Context:
    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self.tasks = ContextTasks(call_supervisor)
        self.errors = ContextErrors(call_supervisor)
        self.observations = ContextObservations(call_supervisor)
        self.messages = ContextMessages(call_supervisor)
        self.agents = ContextAgents(call_supervisor)
        self.conflicts = ContextConflicts(call_supervisor)
        self.local = LocalContext(call_supervisor)


class Agents:
    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def message(self, recipient: str, text: str) -> dict[str, Any]:
        return self._call_supervisor("agents.message", {"recipient": recipient, "text": text})


class Conflicts:
    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._call_supervisor = call_supervisor

    def announce(self, resource: str, summary: str, related_tasks: list[int] = []) -> dict[str, Any]:
        return self._call_supervisor("conflicts.announce", {"resource": resource, "summary": summary, "related_tasks": related_tasks})


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
    loop_budget = LoopBudget()
    namespace: dict[str, Any] = {"__name__": "__agent_repl_kernel__", "inbox": inbox}
    namespace["loop_limit"] = loop_budget.set_next
    namespace["_agent_repl_guard_while"] = loop_budget.guard_while
    namespace["_agent_repl_guarded_iterable"] = loop_budget.guarded_iterable
    if role == "agent":
        namespace["observable"] = Observable(call_supervisor)
        namespace["user"] = User(call_supervisor)
        namespace["tasks"] = Tasks(call_supervisor)
        namespace["workspace"] = Workspace(call_supervisor)
        namespace["context"] = Context(call_supervisor)
        namespace["agents"] = Agents(call_supervisor)
        namespace["conflicts"] = Conflicts(call_supervisor)
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
            loop_budget.counts.clear()
            loop_budget.limits.clear()
            exec(_compile_agent_source(command["source"]), namespace, namespace)
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
