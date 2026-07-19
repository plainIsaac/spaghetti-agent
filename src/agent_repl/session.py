"""A usable single-agent flow built on the supervisor and persistent kernel."""

from __future__ import annotations

from .journal import InboxJournal, Message
from .kernel import KernelResult, PersistentKernel
from .observable_state import ObservableStateRegistry, ObservableValue
from .supervisor import RestartReport, Supervisor


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

    def __init__(self, supervisor: Supervisor, agent: str = "agent") -> None:
        self.supervisor = supervisor
        self.agent = agent
        self.supervisor.create_repl(agent)
        self.kernel = self.supervisor.start_agent_kernel(agent)

    @classmethod
    def open(cls, inbox_path: str = ":memory:", observable_state_path: str = ":memory:", agent: str = "agent") -> "SingleAgentSession":
        journal = InboxJournal(inbox_path)
        observable_state = ObservableStateRegistry(observable_state_path)
        return cls(Supervisor(journal, observable_state), agent)

    def send(self, text: str) -> Message:
        """Queue ordinary user text without executing it as agent source code."""
        return self.supervisor.append_user_message(self.agent, text)

    def evaluate(self, source: str, timeout: float = 2) -> KernelResult:
        return self.kernel.evaluate(source, timeout)

    def observe(self) -> list[ObservableValue]:
        return self.supervisor.observable_state.list(self.agent)

    def user_messages(self) -> list[Message]:
        return self.supervisor.journal.pending("user")

    def restart(self) -> RestartReport:
        self.kernel, report = self.supervisor.restart_agent_kernel(self.agent)
        return report

    def run_demo_turn(self) -> int:
        """Exercise the flow without an LLM; useful for manual runtime checks."""
        result = self.evaluate(_DEMO_TURN)
        if result.status != "ok":
            raise RuntimeError(result.error)
        return int(result.value)

    def close(self) -> None:
        self.supervisor.close()
        self.supervisor.journal.close()
        self.supervisor.observable_state.close()
