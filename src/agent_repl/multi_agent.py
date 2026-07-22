"""A small coordinator/specialist runtime assembled from the shared supervisor."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

from .journal import InboxJournal, Message
from .kernel import KernelResult
from .observable_state import ObservableStateRegistry
from .openai_driver import OpenAIAgentController, OpenAICompatibleAgentDriver
from .session import ModelTurnWorker
from .supervisor import Supervisor
from .tasks import TaskRegistry
from .working_context import WorkingContext
from .workspace import Workspace


class _AgentTurnSession:
    def __init__(self, supervisor: Supervisor, agent: str, log: list[dict]) -> None:
        self.supervisor, self.agent, self._log = supervisor, agent, log

    def run_openai_turn(self, driver: OpenAICompatibleAgentDriver, on_delta=None, on_phase=None, default_context_window: bool = True) -> KernelResult | None:
        def program(planned) -> None:
            self._log.append({"event": "model_program", "agent": self.agent, "timestamp": datetime.now(timezone.utc).isoformat(), "source": planned.source, "raw_output": planned.raw_output, "resolved_model": planned.resolved_model})
        result = OpenAIAgentController(self.supervisor, driver, self.agent, default_context_window=default_context_window).run_turn(on_delta=on_delta, on_phase=on_phase, on_program=program)
        if result is not None:
            self._log.append({"event": "repl_result", "agent": self.agent, "timestamp": datetime.now(timezone.utc).isoformat(), "status": result.status, "error": result.error})
        return result


class MultiAgentSession:
    """One user-facing coordinator plus task-woken specialist REPLs."""

    def __init__(self, supervisor: Supervisor, coordinator: str, specialists: list[str]) -> None:
        self.supervisor, self.coordinator = supervisor, coordinator
        self.agents = [coordinator, *specialists]
        for agent in self.agents:
            self.supervisor.create_repl(agent)
            self.supervisor.start_agent_kernel(agent)
        self.supervisor.start_user_kernel(agent=coordinator)
        self._workers: dict[str, ModelTurnWorker] = {}
        self._logs: dict[str, list[dict]] = {agent: [] for agent in self.agents}
        self._drivers: dict[str, OpenAICompatibleAgentDriver] = {}

    @classmethod
    def open(cls, data_dir: str, coordinator: str = "coordinator", specialists: list[str] | None = None) -> "MultiAgentSession":
        root = Path(data_dir); root.mkdir(parents=True, exist_ok=True)
        supervisor = Supervisor(InboxJournal(str(root / "inbox.sqlite"), str(root / "conversation.jsonl")), ObservableStateRegistry(str(root / "observable-state.sqlite")), TaskRegistry(str(root / "tasks.sqlite")), WorkingContext(str(root / "working-context.sqlite")), Workspace(Path.cwd(), str(root / "workspace.sqlite")))
        return cls(supervisor, coordinator, specialists or ["researcher", "builder"])

    def start_workers(self, drivers: dict[str, OpenAICompatibleAgentDriver], default_context_window: bool = True) -> None:
        missing = set(self.agents) - set(drivers)
        if missing:
            raise ValueError(f"Missing model drivers for: {', '.join(sorted(missing))}")
        for agent in self.agents:
            self._workers[agent] = ModelTurnWorker(_AgentTurnSession(self.supervisor, agent, self._logs[agent]), drivers[agent], default_context_window=default_context_window)
        self._drivers = drivers
        self.supervisor.set_agent_spawner(self._spawn_agent)

    def _spawn_agent(self, agent: str, role: str) -> None:
        parent_driver = next(iter(self._drivers.values()))
        driver = type(parent_driver)(parent_driver.model, request_timeout=parent_driver.request_timeout)
        self.agents.append(agent)
        self.supervisor.create_repl(agent); self.supervisor.start_agent_kernel(agent)
        self._drivers[agent] = driver
        self._logs[agent] = []
        self._workers[agent] = ModelTurnWorker(_AgentTurnSession(self.supervisor, agent, self._logs[agent]), driver)

    def send(self, text: str) -> Message:
        message = self.supervisor.append_user_message(self.coordinator, text)
        if self.coordinator in self._workers:
            self._workers[self.coordinator].request_turn()
        return message

    @property
    def agent(self) -> str:
        return self.coordinator

    def observe(self):
        return self.supervisor.observable_state.list(self.coordinator)

    def user_messages(self):
        return self.supervisor.journal.pending("user")

    def user_message_label(self, message: Message) -> str:
        return message.sender

    def conversation_log(self):
        return self.supervisor.journal.conversation("user", self.coordinator)

    def repl_log(self) -> list[dict]:
        """Compatibility view for the console; workers gain per-role logs next."""
        return [entry for agent in self.agents for entry in self._logs.get(agent, [])]

    def model_program_log(self) -> list[dict]:
        return [entry for entry in self.repl_log() if entry["event"] == "model_program"]

    def worker(self, agent: str) -> ModelTurnWorker:
        return self._workers[agent]

    def close(self) -> None:
        for worker in self._workers.values(): worker.close()
        self.supervisor.close(); self.supervisor.journal.close(); self.supervisor.observable_state.close(); self.supervisor.tasks.close(); self.supervisor.working_context.close(); self.supervisor.workspace.close()
