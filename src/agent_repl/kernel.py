"""A persistent, process-backed agent kernel experiment."""

from __future__ import annotations

from dataclasses import dataclass
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


class KernelInbox:
    """The agent-visible cache of supervisor-owned durable inbox messages."""

    def __init__(self, call_supervisor: Callable[[str, dict[str, Any]], Any]) -> None:
        self._messages: list[dict[str, Any]] = []
        self._call_supervisor = call_supervisor

    def add_message(self, sender: str, text: str, message_id: int | None = None) -> None:
        self._messages.append({"id": message_id, "sender": sender, "text": text})

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

    def publish(self, name: str, value: Any, presenter: str = "json") -> dict[str, Any]:
        return self._call_supervisor(
            "observable.publish",
            {"name": name, "value": value, "presenter": presenter},
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


def _kernel_main(commands: Queue[Any], responses: Queue[Any], capability_requests: Queue[Any], capability_responses: Queue[Any]) -> None:
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
    namespace: dict[str, Any] = {
        "__name__": "__agent_repl_kernel__",
        "inbox": inbox,
        "observable": Observable(call_supervisor),
        "user": User(call_supervisor),
    }
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

    def __init__(self, name: str, capability_handler: Callable[[str, dict[str, Any]], Any]) -> None:
        context = get_context("spawn")
        self.name = name
        self._commands: Queue[Any] = context.Queue()
        self._responses: Queue[Any] = context.Queue()
        self._capability_requests: Queue[Any] = context.Queue()
        self._capability_responses: Queue[Any] = context.Queue()
        self._process = context.Process(
            target=_kernel_main,
            args=(self._commands, self._responses, self._capability_requests, self._capability_responses),
            name=f"agent:{name}",
        )
        self._capability_handler = capability_handler
        self._serving_capabilities = threading.Event()
        self._capability_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._next_request_id = 0

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
            self._commands.put({"kind": "evaluate", "request_id": request_id, "source": source})
            try:
                response = self._responses.get(timeout=timeout)
            except queue.Empty as error:
                raise TimeoutError("Evaluation did not yield a result") from error
        if response["request_id"] != request_id:
            raise RuntimeError("Received a response for another evaluation")
        return KernelResult(response["status"], response.get("value"), response.get("error"))

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

    @property
    def alive(self) -> bool:
        return self._process.is_alive()
