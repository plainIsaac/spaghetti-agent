"""A usable single-agent flow built on the supervisor and persistent kernel."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone
import json
from threading import Event, Lock, Thread
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Callable

from .journal import InboxJournal, Message
from .kernel import KernelResult, PersistentKernel
from .observable_state import ObservableStateRegistry, ObservableValue
from .supervisor import RestartReport, Supervisor
from .tasks import TaskRegistry
from .working_context import WorkingContext

if TYPE_CHECKING:
    from .openai_driver import OpenAICompatibleAgentDriver, PlannedTurn


_DEMO_TURN = """
messages = inbox.pending()
for message in messages:
    observable.publish("latest_input", {"text": message["text"], "message_id": message["id"]})
    inbox.ack(message["id"])
    user.inbox.add("Received your message and recorded it in observable state.")
_result = len(messages)
"""


class SingleAgentSession:
    """The smallest end-to-end user/agent flow.

    `send` accepts ordinary user text. `evaluate` is the developer-facing path
    for agent code. A future model adapter can call `evaluate`; it does not need
    to change message delivery, observation, or restart semantics.
    """

    def __init__(self, supervisor: Supervisor, agent: str = "agent", model_log_path: str | None = None) -> None:
        self.supervisor = supervisor
        self.agent = agent
        self.supervisor.create_repl(agent)
        self.kernel = self.supervisor.start_agent_kernel(agent)
        self.user_kernel = self.supervisor.start_user_kernel(agent=agent)
        self._model_log_path = Path(model_log_path) if model_log_path else None
        self._latest_model_name: str | None = None

    @classmethod
    def open(cls, inbox_path: str = ":memory:", observable_state_path: str = ":memory:", agent: str = "agent") -> "SingleAgentSession":
        if inbox_path != ":memory:":
            Path(inbox_path).parent.mkdir(parents=True, exist_ok=True)
        if observable_state_path != ":memory:":
            Path(observable_state_path).parent.mkdir(parents=True, exist_ok=True)
        debug_log_path = None if inbox_path == ":memory:" else str(Path(inbox_path).with_name("conversation.jsonl"))
        journal = InboxJournal(inbox_path, debug_log_path)
        observable_state = ObservableStateRegistry(observable_state_path)
        model_log_path = None if inbox_path == ":memory:" else str(Path(inbox_path).with_name("model-programs.jsonl"))
        task_path = ":memory:" if inbox_path == ":memory:" else str(Path(inbox_path).with_name("tasks.sqlite"))
        context_path = ":memory:" if inbox_path == ":memory:" else str(Path(inbox_path).with_name("working-context.sqlite"))
        return cls(Supervisor(journal, observable_state, TaskRegistry(task_path), WorkingContext(context_path)), agent, model_log_path)

    def send(self, text: str) -> Message:
        """Queue ordinary user text without executing it as agent source code."""
        return self.supervisor.append_user_message(self.agent, text)

    def evaluate(self, source: str, timeout: float = 2) -> KernelResult:
        return self.kernel.evaluate(source, timeout)

    def user_evaluate(self, source: str, timeout: float = 2) -> KernelResult:
        """Run Python in the user's persistent REPL for inspection/intervention."""
        return self.user_kernel.evaluate(source, timeout)

    def observe(self) -> list[ObservableValue]:
        return self.supervisor.observable_state.list(self.agent)

    def user_messages(self) -> list[Message]:
        return self.supervisor.journal.pending("user")

    def user_message_label(self, message: Message) -> str:
        return self._latest_model_name or message.sender

    def conversation_log(self) -> list[Message]:
        return self.supervisor.journal.conversation("user", self.agent)

    def restart(self) -> RestartReport:
        self.kernel, report = self.supervisor.restart_agent_kernel(self.agent)
        return report

    def run_demo_turn(self) -> int:
        """Exercise the flow without an LLM; useful for manual runtime checks."""
        result = self.evaluate(_DEMO_TURN)
        if result.status != "ok":
            raise RuntimeError(result.error)
        return int(result.value)

    def run_openai_turn(
        self,
        driver: "OpenAICompatibleAgentDriver",
        on_delta=None,
        on_phase=None,
    ) -> KernelResult | None:
        from .openai_driver import OpenAIAgentController
        result = OpenAIAgentController(self.supervisor, driver, self.agent).run_turn(
            on_delta=on_delta,
            on_program=self._append_model_program,
            on_phase=on_phase,
        )
        self._append_repl_result(result)
        return result

    def _append_model_program(self, planned: "PlannedTurn") -> None:
        self._latest_model_name = planned.resolved_model
        self._append_model_log_entry({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "model_program",
            "raw_output": planned.raw_output,
            "source": planned.source,
            "request": planned.request,
            "resolved_model": planned.resolved_model,
        })

    def _append_repl_result(self, result: KernelResult | None) -> None:
        if result is None:
            return
        self._append_model_log_entry({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "repl_result",
            "status": result.status,
            "value": result.value,
            "error": result.error,
        })

    def _append_model_log_entry(self, entry: dict) -> None:
        if self._model_log_path is None:
            return
        self._model_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._model_log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, default=str) + "\n")

    def model_program_log(self) -> list[dict]:
        if self._model_log_path is None or not self._model_log_path.exists():
            return []
        return [json.loads(line) for line in self._model_log_path.read_text(encoding="utf-8").splitlines() if line]

    def repl_log(self) -> list[dict]:
        """Debug trace of model programs followed by their REPL outcomes."""
        return [entry for entry in self.model_program_log() if entry["event"] in {"model_program", "repl_result"}]

    def close(self) -> None:
        self.supervisor.close()
        self.supervisor.journal.close()
        self.supervisor.observable_state.close()
        self.supervisor.tasks.close()
        self.supervisor.working_context.close()


class ModelTurnWorker:
    """Serial background model work so console input is never blocked by planning."""

    def __init__(
        self,
        session: SingleAgentSession,
        driver: "OpenAICompatibleAgentDriver",
        on_complete: Callable[[KernelResult | None], None] | None = None,
    ) -> None:
        self.session = session
        self.driver = driver
        self._on_complete = on_complete
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-model")
        self._future: Future[KernelResult | None] | None = None
        self._phase = "idle"
        self._started_at: datetime | None = None
        self._lock = Lock()
        self._wake_seen: set[int] = set()
        self._wake_stop = Event()
        self._wake_thread = Thread(target=self._watch_task_wakeups, name="agent-task-wakeups", daemon=True)
        self._wake_thread.start()

    def request_turn(self) -> bool:
        with self._lock:
            if self._future is not None and not self._future.done():
                return False
            self._phase = "queued"
            self._started_at = datetime.now(timezone.utc)
            self._future = self._executor.submit(self._run)
            return True

    def _watch_task_wakeups(self) -> None:
        """Dispatch each supervisor task wake-up once, without agent polling."""
        while not self._wake_stop.wait(0.1):
            for message in self.session.supervisor.journal.pending(self.session.agent):
                if message.sender != "supervisor" or not message.text.startswith("Task "):
                    continue
                if message.id in self._wake_seen:
                    continue
                self._wake_seen.add(message.id)
                self.request_turn()

    def _run(self) -> KernelResult | None:
        def phase(value: str) -> None:
            with self._lock:
                self._phase = value
        try:
            result = self.session.run_openai_turn(self.driver, on_phase=phase)
        except Exception as error:
            result = KernelResult("error", error=f"{type(error).__name__}: {error}")
        if self._on_complete is not None:
            self._on_complete(result)
        return result

    def status(self) -> tuple[str, float]:
        with self._lock:
            phase, started = self._phase, self._started_at
            future = self._future
        if future is not None and future.done():
            phase = "completed"
        elapsed = 0.0 if started is None else (datetime.now(timezone.utc) - started).total_seconds()
        return phase, elapsed

    def collect(self) -> KernelResult | None | object:
        with self._lock:
            future = self._future
        if future is None or not future.done():
            return _NOT_READY
        result = future.result()
        with self._lock:
            if self._future is future:
                self._future = None
                self._phase = "completed"
        return result

    def close(self) -> None:
        self._wake_stop.set()
        self._wake_thread.join(timeout=1)
        self._executor.shutdown(wait=False, cancel_futures=False)


_NOT_READY = object()
