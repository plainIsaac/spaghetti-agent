"""Process boundary experiment for non-yielding, dynamically importing code."""

from __future__ import annotations

from multiprocessing import get_context
from multiprocessing.queues import Queue
from typing import Any

from .kernel import _configure_background_processes, _close_process_queue


def _run_source(source: str, results: Queue[Any]) -> None:
    namespace: dict[str, Any] = {"__name__": "__agent_repl_isolated__"}
    try:
        exec(compile(source, "<spaghetti-agent-evaluation>", "exec"), namespace, namespace)
        results.put(("ok", namespace.get("_result")))
    except BaseException as error:
        results.put(("error", f"{type(error).__name__}: {error}"))


class IsolatedExecution:
    """A cancellable process-backed execution unit.

    It deliberately does not preserve Python heap state. That limitation is a
    useful result of the spike: durable state needs explicit representation.
    """

    def __init__(self, source: str) -> None:
        _configure_background_processes()
        context = get_context("spawn")
        self._results: Queue[Any] = context.Queue()
        self._process = context.Process(target=_run_source, args=(source, self._results))
        self._closed = False

    def start(self) -> None:
        self._process.start()

    def cancel(self) -> None:
        if self._process.is_alive():
            self._process.terminate()
        self._process.join(timeout=2)
        self.close()

    def result(self, timeout: float = 2) -> tuple[str, Any]:
        item = self._results.get(timeout=timeout)
        self._process.join(timeout=timeout)
        self.close()
        return item

    def close(self) -> None:
        """Release the child process handle and queue feeder thread."""
        if self._closed:
            return
        if self._process.is_alive():
            raise RuntimeError("cannot close a running isolated execution; cancel it first")
        self._closed = True
        _close_process_queue(self._results)
        self._process.close()

    @property
    def alive(self) -> bool:
        return not self._closed and self._process.is_alive()
