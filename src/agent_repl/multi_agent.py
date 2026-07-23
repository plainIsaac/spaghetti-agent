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
from .token_budget import TokenBudget


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
            self.supervisor.set_agent_role(agent, "coordinator" if agent == coordinator else agent)
        self.supervisor.start_user_kernel(agent=coordinator)
        self._workers: dict[str, ModelTurnWorker] = {}
        self._logs: dict[str, list[dict]] = {agent: [] for agent in self.agents}
        self._drivers: dict[str, OpenAICompatibleAgentDriver] = {}

    @classmethod
    def open(cls, data_dir: str, coordinator: str = "coordinator", specialists: list[str] | None = None, workspace_root: str | None = None) -> "MultiAgentSession":
        root = Path(data_dir); root.mkdir(parents=True, exist_ok=True)
        workspace = Path(workspace_root) if workspace_root is not None else Path.cwd()
        workspace.mkdir(parents=True, exist_ok=True)
        supervisor = Supervisor(InboxJournal(str(root / "inbox.sqlite"), str(root / "conversation.jsonl")), ObservableStateRegistry(str(root / "observable-state.sqlite")), TaskRegistry(str(root / "tasks.sqlite")), WorkingContext(str(root / "working-context.sqlite")), Workspace(workspace, str(root / "workspace.sqlite")), TokenBudget(str(root / "token-budget.sqlite")))
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
        driver.output_token_reserve = parent_driver.output_token_reserve
        self.agents.append(agent)
        self.supervisor.create_repl(agent); self.supervisor.start_agent_kernel(agent)
        self.supervisor.set_agent_role(agent, role)
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

    def state_snapshot(self) -> dict:
        """Compact, inspection-first operational state for the user-facing UI."""
        statuses = self.agent_status()
        agents = []
        active_tasks = []
        recent_errors = []
        for agent in self.agents:
            phase, elapsed = statuses.get(agent, ("idle", 0.0))
            tasks = self.supervisor.tasks.list(agent)
            active = [task for task in tasks if task.state != "completed"]
            agents.append({"name": agent, "role": self.supervisor.agent_role(agent), "phase": phase, "elapsed_seconds": round(elapsed, 1), "pending_messages": len(self.supervisor.journal.pending(agent))})
            active_tasks.extend({"id": task.id, "owner": agent, "state": task.state, "title": task.title} for task in active)
            for task in tasks:
                for error in self.supervisor.tasks.errors(task.id):
                    recent_errors.append({"task_id": task.id, "owner": agent, "error": error["error"], "count": error["count"]})
        return {"agents": agents, "active_tasks": active_tasks, "recent_errors": recent_errors[-6:], "branches": self.supervisor.workspace.branches(), "token_budget": self.supervisor.token_budget.snapshot()}

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

    def agent_status(self) -> dict[str, tuple[str, float]]:
        """Current worker state for all agents, including dynamically spawned ones."""
        return {agent: worker.status() for agent, worker in self._workers.items()}

    def pending_agent_messages(self) -> int:
        return sum(len(self.supervisor.journal.pending(agent)) for agent in self.agents)

    def close(self) -> None:
        for worker in self._workers.values(): worker.close()
        self.supervisor.close(); self.supervisor.journal.close(); self.supervisor.observable_state.close(); self.supervisor.tasks.close(); self.supervisor.working_context.close(); self.supervisor.workspace.close(); self.supervisor.token_budget.close()
