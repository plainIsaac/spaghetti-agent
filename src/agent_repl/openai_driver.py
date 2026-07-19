"""OpenAI Responses API adapter for a transcript-free Agent REPL turn."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Protocol

from .kernel import KernelResult
from .supervisor import Supervisor


DEFAULT_OPENAI_MODEL = "gpt-5.6-terra"

_INSTRUCTIONS = """You are the agent inside a persistent Python REPL.
Return only Python source code to evaluate in that REPL; do not use Markdown.
The user messages in the input are durable inbox entries, not chat history.
Read and deliberately acknowledge them with inbox.ack(message_id) when handled.
Use observable.publish(...) for state worth showing by default, and
user.inbox.add(...) only for concise messages that need the user's attention.
You can use ordinary persistent Python variables and the granted runtime
capabilities. Do not explain the code outside the Python source."""


class ResponsesClient(Protocol):
    class responses(Protocol):
        @staticmethod
        def create(**kwargs: Any) -> Any: ...


class OpenAIConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlannedTurn:
    source: str
    request: dict[str, Any]


class OpenAIAgentDriver:
    """Plans one agent evaluation through the OpenAI Responses API."""

    def __init__(self, model: str = DEFAULT_OPENAI_MODEL, client: ResponsesClient | None = None) -> None:
        self.model = model
        self._client = client

    def plan(self, inbox: list[dict[str, Any]], presentable: dict[str, Any]) -> PlannedTurn:
        request = {"inbox": inbox, "presentable": presentable}
        response = self._get_client().responses.create(
            model=self.model,
            instructions=_INSTRUCTIONS,
            input=json.dumps(request),
        )
        source = self._strip_code_fence(str(response.output_text))
        if not source.strip():
            raise RuntimeError("OpenAI returned an empty agent program")
        return PlannedTurn(source, request)

    def _get_client(self) -> ResponsesClient:
        if self._client is not None:
            return self._client
        if not os.environ.get("OPENAI_API_KEY"):
            raise OpenAIConfigurationError("Set OPENAI_API_KEY before enabling the OpenAI agent driver")
        try:
            from openai import OpenAI
        except ImportError as error:
            raise OpenAIConfigurationError(
                "Install the optional OpenAI dependency: python -m pip install -e '.[openai]'"
            ) from error
        self._client = OpenAI()
        return self._client

    @staticmethod
    def _strip_code_fence(source: str) -> str:
        stripped = source.strip()
        if not stripped.startswith("```"):
            return stripped
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
        return stripped


class OpenAIAgentController:
    """Connects durable runtime state to a single OpenAI-planned agent turn."""

    def __init__(self, supervisor: Supervisor, driver: OpenAIAgentDriver, agent: str = "agent") -> None:
        self.supervisor = supervisor
        self.driver = driver
        self.agent = agent

    def run_turn(self) -> KernelResult | None:
        inbox = [self.supervisor._message_data(message) for message in self.supervisor.journal.pending(self.agent)]
        if not inbox:
            return None
        presentable = {value.name: value.value for value in self.supervisor.observable_state.list(self.agent)}
        planned = self.driver.plan(inbox, presentable)
        return self.supervisor.agent_kernel(self.agent).evaluate(planned.source)
