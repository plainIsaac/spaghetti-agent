"""OpenAI Responses API adapter for a transcript-free Agent REPL turn."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
from typing import Any, Callable, Protocol

from .kernel import KernelResult
from .supervisor import Supervisor


# Default to the current cost-sensitive tier for early runtime experiments.
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
DEFAULT_OPENROUTER_MODEL = "openrouter/free"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0

_INSTRUCTIONS = """You are the agent inside a persistent Python REPL.
Return only Python source code to evaluate in that REPL; do not use Markdown
or ``` code fences. Do not offer multiple alternative programs.
Your entire output must compile under Python exec. Never return placeholders,
signatures, ellipses, prose, or an empty program.
Never return safety classifications or policy commentary; your output is always
executable Python source, even when a request is innocuous or needs no action.
The user messages in the input are durable inbox entries, not chat history.
If `model_feedback` is present, your previous program was rejected. Correct the
specific error and return one replacement Python program; do not discuss it.
For a concise response to the latest message, prefer inbox.reply_to_latest(text):
it queues the user reply and acknowledges that message in one runtime operation.
Use explicit inbox.ack(message_id) only when you intentionally do not reply.
Use observable.publish(...) for state worth showing by default, and
user.inbox.add(...) only for concise messages that need the user's attention.
You can use ordinary persistent Python variables and the granted runtime
capabilities. Do not explain the code outside the Python source.
Use `tasks.announce(title)`, `tasks.take(id)`, `tasks.complete(id)`, and
`tasks.wait_for(id, observable_name, expected_value)` to express durable work;
do not use a long-running polling loop to wait for observable state.
Record failures with `tasks.report_error(id, error)`; when work is materially
hard, announce a follow-up with `tasks.challenge(id, description)`.

A valid minimal program looks like:
inbox.reply_to_latest("Handled your request.")"""

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
    resolved_model: str | None = None


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
        request = {
            "inbox": inbox,
            "presentable": presentable,
            "model_feedback": presentable.get("model_error"),
        }
        response = self._get_client().responses.create(
            model=self.model,
            instructions=_INSTRUCTIONS,
            input=json.dumps(request),
            stream=True,
        )
        raw_output, resolved_model = self._read_stream(response, on_delta)
        source = self._strip_code_fence(raw_output)
        if not source.strip():
            raise RuntimeError("OpenAI returned an empty agent program")
        return PlannedTurn(source, request, raw_output, resolved_model or self.model)

    @staticmethod
    def _read_stream(response: Any, on_delta: Callable[[str], None] | None) -> tuple[str, str | None]:
        # The small compatibility path keeps injected test clients usable.
        if hasattr(response, "output_text"):
            text = str(response.output_text)
            if on_delta is not None and text:
                on_delta(text)
            return text, getattr(response, "model", None)
        parts: list[str] = []
        resolved_model: str | None = None
        for event in response:
            completed_response = getattr(event, "response", None)
            if completed_response is not None:
                resolved_model = getattr(completed_response, "model", resolved_model)
            if getattr(event, "type", None) != "response.output_text.delta":
                continue
            delta = getattr(event, "delta", "")
            if delta:
                text = str(delta)
                parts.append(text)
                if on_delta is not None:
                    on_delta(text)
        return "".join(parts), resolved_model

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
        blocks = re.findall(r"```(?:python)?[ \t]*\r?\n(.*?)```", stripped, flags=re.IGNORECASE | re.DOTALL)
        if blocks:
            # Cheap models sometimes emit alternatives. Execute only the final
            # complete block, which is their most recent proposed program.
            return blocks[-1].strip()
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
        planned: PlannedTurn | None = None
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
            feedback: dict[str, Any] = {
                "error": f"{type(error).__name__}: {error}",
                "instruction": "Your previous output was rejected. Return one valid Python program only.",
            }
            if planned is not None:
                feedback["rejected_output"] = planned.raw_output
            self.supervisor.publish_state(
                self.agent,
                "model_error",
                feedback,
                presenter="error",
                label="Model planning error",
                show_by_default=False,
                priority=100,
            )
            return KernelResult("error", error=f"{type(error).__name__}: {error}")
        if on_phase is not None:
            on_phase("executing")
        return self.supervisor.agent_kernel(self.agent).evaluate(planned.source)
