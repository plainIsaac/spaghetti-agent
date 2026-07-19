"""A persistent, process-backed agent kernel experiment."""

from __future__ import annotations

from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.queues import Queue
import queue
import threading
from typing import Any

from .journal import Message


@dataclass(frozen=True)
class KernelResult:
    status: str
    value: Any = None
    error: str | None = None


class KernelInbox:
    """The agent-visible cache of supervisor-owned durable inbox messages."""

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []

    def add_message(self, sender: str, text: str, message_id: int | None = None) -> None:
        self._messages.append({"id": message_id, "sender": sender, "text": text})

    def pending(self) -> list[dict[str, Any]]:
        return list(self._messages)


def _kernel_main(commands: Queue[Any], responses: Queue[Any]) -> None:
    inbox = KernelInbox()
    namespace: dict[str, Any] = {
        "__name__": "__agent_repl_kernel__",
        "inbox": inbox,
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

    def __init__(self, name: str) -> None:
        context = get_context("spawn")
        self.name = name
        self._commands: Queue[Any] = context.Queue()
        self._responses: Queue[Any] = context.Queue()
        self._process = context.Process(target=_kernel_main, args=(self._commands, self._responses), name=f"agent:{name}")
        self._lock = threading.Lock()
        self._next_request_id = 0

    def start(self) -> None:
        self._process.start()

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
        if self._process.is_alive():
            self._commands.put({"kind": "stop"})
            self._process.join(timeout=1)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=1)

    @property
    def alive(self) -> bool:
        return self._process.is_alive()
