"""OpenAI Responses API adapter for a transcript-free Agent REPL turn."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Callable, Protocol

from .kernel import KernelResult
from .supervisor import Supervisor


# Default to the current cost-sensitive tier for early runtime experiments.
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
DEFAULT_OPENROUTER_MODEL = "openrouter/free"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

_INSTRUCTIONS = """You are the agent inside a persistent Python REPL.
Return only Python source code to evaluate in that REPL; do not use Markdown.
Your entire output must compile under Python exec. Never return placeholders,
signatures, ellipses, prose, or an empty program.
Never return safety classifications or policy commentary; your output is always
executable Python source, even when a request is innocuous or needs no action.
The user messages in the input are durable inbox entries, not chat history.
Read and deliberately acknowledge them with inbox.ack(message_id) when handled.
Use observable.publish(...) for state worth showing by default, and
user.inbox.add(...) only for concise messages that need the user's attention.
You can use ordinary persistent Python variables and the granted runtime
capabilities. Do not explain the code outside the Python source.

A valid minimal program looks like:
message = inbox.pending()[0]
observable.publish("status", {"message_id": message["id"], "state": "handled"})
inbox.ack(message["id"])
user.inbox.add("Handled your request.")"""

_LEGACY_HARNESS_COMMANDS = {":state", ":help", ":log", ":model-log", ":python", ":restart", ":quit"}


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
    raw_output: str


class OpenAICompatibleAgentDriver:
    """Plans an agent evaluation through an OpenAI-compatible Responses API."""

    def __init__(
        self,
        model: str,
        client: ResponsesClient | None = None,
        *,
        provider_name: str,
        api_key_environment: str,
        base_url: str | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.model = model
        self._client = client
        self.provider_name = provider_name
        self.api_key_environment = api_key_environment
        self.base_url = base_url
        self.request_timeout = request_timeout

    def plan(
        self,
        inbox: list[dict[str, Any]],
        presentable: dict[str, Any],
        on_delta: Callable[[str], None] | None = None,
    ) -> PlannedTurn:
        request = {"inbox": inbox, "presentable": presentable}
        response = self._get_client().responses.create(
            model=self.model,
            instructions=_INSTRUCTIONS,
            input=json.dumps(request),
            stream=True,
        )
        raw_output = self._read_stream(response, on_delta)
        source = self._strip_code_fence(raw_output)
        if not source.strip():
            raise RuntimeError("OpenAI returned an empty agent program")
        return PlannedTurn(source, request, raw_output)

    @staticmethod
    def _read_stream(response: Any, on_delta: Callable[[str], None] | None) -> str:
        # The small compatibility path keeps injected test clients usable.
        if hasattr(response, "output_text"):
            text = str(response.output_text)
            if on_delta is not None and text:
                on_delta(text)
            return text
        parts: list[str] = []
        for event in response:
            if getattr(event, "type", None) != "response.output_text.delta":
                continue
            delta = getattr(event, "delta", "")
            if delta:
                text = str(delta)
                parts.append(text)
                if on_delta is not None:
                    on_delta(text)
        return "".join(parts)

    def _get_client(self) -> ResponsesClient:
        if self._client is not None:
            return self._client
        api_key = os.environ.get(self.api_key_environment)
        if not api_key:
            raise OpenAIConfigurationError(
                f"Set {self.api_key_environment} before enabling the {self.provider_name} agent driver"
            )
        try:
            from openai import OpenAI
        except ImportError as error:
            raise OpenAIConfigurationError(
                "Install the optional OpenAI-compatible SDK: python -m pip install -e '.[openai]'"
            ) from error
        options: dict[str, Any] = {
            "api_key": api_key,
            "timeout": self.request_timeout,
            "max_retries": 0,
        }
        if self.base_url is not None:
            options["base_url"] = self.base_url
        self._client = OpenAI(**options)
        return self._client

    def validate_configuration(self) -> None:
        """Fail fast for missing credentials without starting a background turn."""
        self._get_client()

    @staticmethod
    def _strip_code_fence(source: str) -> str:
        stripped = source.strip()
        if not stripped.startswith("```"):
            return stripped
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
        return stripped


class OpenAIAgentDriver(OpenAICompatibleAgentDriver):
    """OpenAI's native Responses API adapter."""

    def __init__(
        self,
        model: str = DEFAULT_OPENAI_MODEL,
        client: ResponsesClient | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            model,
            client,
            provider_name="OpenAI",
            api_key_environment="OPENAI_API_KEY",
            request_timeout=request_timeout,
        )


class OpenRouterAgentDriver(OpenAICompatibleAgentDriver):
    """OpenRouter's OpenAI-compatible API, intended for low-cost smoke tests."""

    def __init__(
        self,
        model: str = DEFAULT_OPENROUTER_MODEL,
        client: ResponsesClient | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            model,
            client,
            provider_name="OpenRouter",
            api_key_environment="OPENROUTER_API_KEY",
            base_url="https://openrouter.ai/api/v1",
            request_timeout=request_timeout,
        )


class OpenAIAgentController:
    """Connects durable runtime state to a single OpenAI-planned agent turn."""

    def __init__(self, supervisor: Supervisor, driver: OpenAICompatibleAgentDriver, agent: str = "agent") -> None:
        self.supervisor = supervisor
        self.driver = driver
        self.agent = agent

    def run_turn(
        self,
        on_delta: Callable[[str], None] | None = None,
        on_program: Callable[[PlannedTurn], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
    ) -> KernelResult | None:
        pending = self.supervisor.journal.pending(self.agent)
        inbox = [
            self.supervisor._message_data(message)
            for message in pending
            if message.text.strip() not in _LEGACY_HARNESS_COMMANDS
        ]
        if not inbox:
            return None
        presentable = {value.name: value.value for value in self.supervisor.observable_state.list(self.agent)}
        try:
            if on_phase is not None:
                on_phase("planning")
            planned = self.driver.plan(inbox, presentable, on_delta)
            if on_program is not None:
                on_program(planned)
            compile(planned.source, "<agent-repl-model-output>", "exec")
        except OpenAIConfigurationError:
            raise
        except Exception as error:
            self.supervisor.publish_state(
                self.agent,
                "model_error",
                {"error": f"{type(error).__name__}: {error}"},
                presenter="error",
                label="Model planning error",
                priority=100,
            )
            return KernelResult("error", error=f"{type(error).__name__}: {error}")
        if on_phase is not None:
            on_phase("executing")
        return self.supervisor.agent_kernel(self.agent).evaluate(planned.source)
