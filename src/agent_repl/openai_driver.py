"""OpenAI Responses API adapter for a transcript-free Spaghetti Agent turn."""

from __future__ import annotations

from dataclasses import dataclass
import ast
import json
import os
import re
import uuid
from pathlib import Path
from datetime import datetime, timezone
from threading import Event, Lock, Timer
from typing import Any, Callable, Protocol

from .kernel import KernelResult
from .supervisor import Supervisor
from .token_budget import TokenBudgetExceeded, TokenReservation, estimate_tokens


# Default to the current cost-sensitive tier for early runtime experiments.
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
DEFAULT_OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30.0
DEFAULT_CONTEXT_WINDOW_MESSAGES = 8
DEFAULT_CONTEXT_WINDOW_CHARS = 4_000
DEFAULT_PENDING_WORK_ITEMS = 6

_INSTRUCTIONS = """You are the agent inside a persistent Python REPL.
Return only Python source code to evaluate in that REPL; do not use Markdown
or ``` code fences. Do not offer multiple alternative programs.
Your entire output must compile under Python exec. Never return placeholders,
signatures, ellipses, prose, or an empty program.
Never return safety classifications or policy commentary; your output is always
executable Python source, even when a request is innocuous or needs no action.
When writing program code inside a Python string, use a raw string literal
(for example `r"..."`) whenever it contains backslashes. This preserves the
generated program's escapes and intended functionality.
The activation identifies why you were invoked. Durable inbox entries, task
history, errors, observations, and prior messages are pulled through Python:
use `inbox.pending()` and `context`, not assumed prompt snapshots.
Before guessing missing requirements, pull them: use `context.user.messages()`
for the shared user conversation (available to subagents too), `context.tasks`
for task records and delegation state, and `context.messages.with_party(name)`
for a specific agent conversation. If the context still does not resolve an
important ambiguity, send a concise question to the user rather than inventing it.
When the default context window feature is enabled, activation also carries a
small recent conversation and pending-work window for continuity. Treat it as a
convenience, not the whole state; do not ask the user to repeat information
already in that window or obtainable through `context.messages.with_party("user")`.
Activation task summaries are authoritative working context: `active_tasks`
contains your active task details, and coordinators also receive
`delegated_tasks` with child ownership, state, and details. Use these directly
before asking the user or creating replacement work.
Activation is input to the model, not a Python REPL global: never reference an
`activation` variable in generated code. In the REPL, obtain work through
`tasks.list()`, `context.tasks.list()`, task-assignment inbox messages, and
other `context` APIs.
For an inbox activation, read the actual message from `inbox.pending()` before
replying. Do not give a canned acknowledgement or claim you lack its contents.
`inbox`, `tasks`, `workspace`, `context`, `observable`, `user`, `agents`, and `conflicts`
are already injected REPL globals. Never import them as Python modules.
`runtime.on_shutdown(handler)` optionally registers a short best-effort hook to
publish final presentable state before a normal kernel shutdown; never depend on
it for durable task completion or forced termination.
`context.local` stores small JSON values scoped to a session, message, task,
error, or current line. Only entries explicitly set with model_visible=True are
included in a later relevant activation; use it sparingly.
It is an API, not a dictionary: use `context.local.set(key, value, model_visible=True)`,
`context.local.get(key)`, and `context.local.clear(key=key)`; never use brackets,
assignment, `.pop()`, or `.get()` on inbox messages. Inbox messages support both
`message["id"]` and `message["message_id"]` as aliases.
If `model_feedback` is present, your previous program was rejected. Correct the
specific error and return one replacement Python program; do not discuss it.
`model_feedback.failed_program` is the exact Python source that failed. Inspect
that source and the reported exception; preserve valid work and make the
smallest correction instead of reconstructing the program from memory.
For a concise response to the latest message, prefer inbox.reply_to_latest(text):
it queues the user reply and acknowledges that message in one runtime operation.
Use explicit inbox.ack(message_id) only when you intentionally do not reply.
Never bulk-ack user messages merely to clear the inbox. Leave unrelated
messages pending, or create/take a task that records why they are handled.
When more than one user message is pending, inspect the queue before acting.
Create durable tasks for distinct work; if an older message is a duplicate that
the current completed work fully satisfies, explicitly acknowledge that one
message rather than leaving it to trigger duplicate work later.
Use observable.publish(...) for state worth showing by default, and
user.inbox.add(...) only for concise messages that need the user's attention.
Call `observable.publish(name, value)` normally; a single dictionary is also
accepted to publish multiple named values.
You can use ordinary persistent Python variables and the granted runtime
capabilities. Do not explain the code outside the Python source.
Use `tasks.announce(title)`, `tasks.take(task_or_id)`, `tasks.complete(task_or_id)`, and
`tasks.wait_for(id, observable_name, expected_value)` to express durable work;
do not use a long-running polling loop to wait for observable state.
Before announcing work, inspect `tasks.list()`. If a matching task is already
active, take or continue that task—never create a duplicate task after a retry.
Take a task before performing fallible work: kernel exceptions are then
automatically recorded against that task by the supervisor.
Record failures with `tasks.report_error(id, error)`; when work is materially
hard, announce a follow-up with `tasks.challenge(id, description)`.
For a known existing specialist, coordinators use `tasks.delegate(agent_name, title, details)`.
`agents` is an API object, not a mapping: never call `.get()`, `.items()`, or
iterate it. When no suitable specialist exists, create one with
`agents.spawn(name, role, task, details=None)`. `task` must be a plain task
title string: do not call `tasks.announce` first and do not pass a task dict.
That call creates the child REPL and gives it the requested task; its result
contains `agent` and `task_id`.
The preferred messaging API is `agents.message(recipient, text)` for
agent-to-agent messages and `inbox.reply_to_latest(text)` for user replies.
For a focused verification request, prefer `agents.assert_async(recipient, claim, context=None)`;
the receiving agent inspects `agents.pending_assertions()` and answers with
`agents.resolve_assertion(id, passed, evidence)`. Use `agents.assert_sync(...)`
only when the receiving agent is already running concurrently; otherwise the
serialized model-turn scheduler cannot produce the response until this turn yields.
Compatible aliases `context.send(recipient, text)`,
`context.send_message(recipient, text)`, `agents.send(recipient, text)`, and
`agents.send_message(recipient, text)` are also supported. For compatibility,
`agents.spawn(name, task_title, details)` is accepted when no role is needed.
Do not delegate the same work again after spawning. The child will send the
delegator a durable `Task <id> completed ...` inbox message. Do not poll or
wait for an invented observable status. Never use `while`, polling, or sleeping
to wait for a child: acknowledge or leave the current message after spawning;
the supervisor will wake you with the completion event on a later turn. Then
inspect the child's workspace result and complete the parent task.
Before every spawn, call `context.tasks.delegated(active_only=True)`. On a
retry, reuse and wait for any related active child rather than spawning a
replacement. Use this same API to identify which completion to review.
When coordinating a deliberate conflict, assign each child the exact same
target path in task details, let the managed workspace report the conflict, and
wait for child messages before writing any resolution yourself. Do not preempt
the child work by writing the contested file in the coordinator's first turn.
Specialists should publish concise results and message the coordinator, not the
user, unless specifically granted responsibility for user communication.
If your role is coordinator and `available_agents` lists suitable specialists,
delegate substantial project work before directly editing shared files: send
research/discovery to a researcher and implementation to a builder where
available. Create/take the coordination task, send precise task details, then
wait for durable child completion messages; do not solve every independent
workstream yourself.
After delegation, keep the coordination task open. Complete it only after the
delegated tasks have reported results and you have reviewed or integrated them.
Never call `tasks.take()` on a child-owned delegated task. Wait for its durable
completion/failure inbox message, inspect the submitted branch with
`workspace.diff(child_task_id)`, then merge it with `workspace.merge(child_task_id)`
only after review.
For managed workspace conflicts, inspect `context.errors.for_task(child_id)`,
message the affected agent or assign a resolver with the exact path and both
task details, then verify the resulting managed write before completing the
coordination task.
For a build or change request, first inspect the relevant inbox, recent user
context, active tasks, and existing workspace before writing files. Record the
work in a task, verify changed files after writing them, publish concise
observable completion state, and send a concise completion reply. `print()` is
debug output only and never a user-facing completion signal.
For verification, use `workspace.run(["program", "arg"], timeout_seconds=30)`
inside your active task. It accepts an argument list only (never a shell string),
runs from the managed workspace, caps execution at 60 seconds, and returns
durable output. The result supports `result["output"]` and `result.stdout`.
When writing a short Python verifier, use `import sys` and
`workspace.run([sys.executable, "verify.py"])`, not a platform-specific
`python` command. Inspect `workspace.command_runs()` when reviewing prior checks.
For coordinated project files, use `workspace.list()`, `workspace.read_text()`,
and `workspace.write_text(path, text)` while a task is active. Reads remember
the revision; writes claim new paths automatically and protect existing files
with that revision. Use `workspace.claim(path, task_id=...)` or explicit
`task_id=` / `expected_revision=` only for handoffs or conflict resolution.
`workspace.read_text(path)` returns `{"text": "...", "revision": "..."}`:
use `workspace.read_text(path)["text"]` when verifying or editing file text.
These managed writes are task-scoped and conflict-aware;
ordinary Python filesystem I/O is unmanaged and must not be used for shared
multi-agent files.
Use `workspace.exists(path)` when existence is needed. Before overwriting an
existing file, call `workspace.read_text(path)` in the same active task; the
returned revision authorizes the later managed write.

A valid minimal program looks like:
message = inbox.pending()[-1]
task = tasks.announce("Respond to latest inbox message")
tasks.take(task)
inbox.reply_to_latest(f"Received: {message['text']}")
tasks.complete(task)"""

_ROLE_POLICIES = {
    "coordinator": """\nRole policy: You coordinate rather than independently implementing every workstream. For substantial work, delegate suitable research and implementation to available agents, track delegated tasks, and integrate/review their results. Only modify a shared file yourself when coordinating a resolution or when no suitable specialist exists.""",
    "researcher": """\nRole policy: You investigate requirements, constraints, and relevant project context. Pull user/task context first, publish concise findings, and message the coordinator with actionable conclusions. Do not edit shared implementation files unless your assigned task explicitly requires it.""",
    "builder": """\nRole policy: You implement the assigned change using managed workspace APIs. Read existing files before overwriting, verify your result, then complete your task and report the changed paths and verification to the coordinator.""",
    "reviewer": """\nRole policy: You inspect assigned changes, workspace state, and task context. Report specific defects or approval to the coordinator; do not silently replace another agent's implementation.""",
}

_LEGACY_HARNESS_COMMANDS = {":state", ":help", ":log", ":model-log", ":python", ":restart", ":quit"}


class ResponsesClient(Protocol):
    class responses(Protocol):
        @staticmethod
        def create(**kwargs: Any) -> Any: ...


class OpenAIConfigurationError(RuntimeError):
    pass


class ProviderFallbackError(RuntimeError):
    pass


@dataclass(frozen=True)
class PlannedTurn:
    source: str
    request: dict[str, Any]
    raw_output: str
    resolved_model: str | None = None
    usage: dict[str, int] | None = None


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
        transport: str = "responses",
        output_token_reserve: int = 1_024,
    ) -> None:
        self.model = model
        self._client = client
        self._client_is_external = client is not None
        self._client_lock = Lock()
        self.provider_name = provider_name
        self.api_key_environment = api_key_environment
        self.base_url = base_url
        self.request_timeout = request_timeout
        self.transport = transport
        self.output_token_reserve = output_token_reserve
        self._http_log_path: Path | None = None

    def set_http_log_path(self, path: str) -> None:
        """Enable a local, redacted provider HTTP trace for debugging."""
        self._http_log_path = Path(path)

    def close(self) -> None:
        """Close an in-flight or cached provider client during shutdown."""
        with self._client_lock:
            client, self._client = self._client, None
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def clone(self) -> "OpenAICompatibleAgentDriver":
        clone = type(self)(self.model, request_timeout=self.request_timeout)
        clone.output_token_reserve = self.output_token_reserve
        return clone

    def _append_http_log(self, entry: dict[str, Any]) -> None:
        if self._http_log_path is None:
            return
        self._http_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._http_log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, default=str) + "\n")

    def plan(
        self,
        activation: list[dict[str, Any]],
        model_feedback: dict[str, Any] | None,
        on_delta: Callable[[str], None] | None = None,
        role: str = "agent",
    ) -> PlannedTurn:
        request = {
            "activation": activation,
            "model_feedback": model_feedback,
        }
        opened = datetime.now(timezone.utc)
        self._append_http_log({"timestamp": opened.isoformat(), "event": "stream_opened", "provider": self.provider_name, "model": self.model})
        timed_out = Event()
        client = self._get_client()
        deadline_lock = Lock()
        deadline: Timer | None = None
        deadline_generation = 0

        def close_overdue_client(generation: int) -> None:
            with deadline_lock:
                if generation != deadline_generation:
                    return
            timed_out.set()
            close = getattr(client, "close", None)
            if callable(close):
                close()
            self._discard_timed_out_client(client)

        def arm_idle_deadline() -> None:
            nonlocal deadline, deadline_generation
            with deadline_lock:
                deadline_generation += 1
                if deadline is not None:
                    deadline.cancel()
                deadline = Timer(self.request_timeout, close_overdue_client, args=(deadline_generation,))
                deadline.daemon = True
                deadline.start()

        def disarm_idle_deadline() -> None:
            nonlocal deadline_generation
            with deadline_lock:
                deadline_generation += 1
                if deadline is not None:
                    deadline.cancel()

        def received_delta(text: str) -> None:
            # A healthy stream may run longer than the timeout; only silence is
            # a failure. This first arm also acts as the first-token deadline.
            arm_idle_deadline()
            if on_delta is not None:
                on_delta(text)

        arm_idle_deadline()
        try:
            instructions = _INSTRUCTIONS + _ROLE_POLICIES.get(role, "\nRole policy: Complete only your assigned task and communicate durable results.")
            if self.transport == "responses":
                response = client.responses.create(model=self.model, instructions=instructions, input=json.dumps(request), stream=True)
                raw_output, resolved_model, usage = self._read_stream(response, received_delta)
            elif self.transport == "chat_completions":
                response = client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": instructions}, {"role": "user", "content": json.dumps(request)}],
                    stream=True,
                )
                raw_output, resolved_model, usage = self._read_chat_stream(response, received_delta)
            else:
                raise ValueError(f"unsupported model transport: {self.transport}")
        except Exception as error:
            closed = datetime.now(timezone.utc)
            if timed_out.is_set():
                timeout = TimeoutError(f"provider stream was idle for {self.request_timeout:.1f}s before the first or next token")
                self._append_http_log({"timestamp": closed.isoformat(), "event": "stream_closed", "opened_at": opened.isoformat(), "closed_at": closed.isoformat(), "duration_seconds": (closed - opened).total_seconds(), "error": f"{type(timeout).__name__}: {timeout}"})
                raise timeout from error
            self._append_http_log({"timestamp": closed.isoformat(), "event": "stream_closed", "opened_at": opened.isoformat(), "closed_at": closed.isoformat(), "duration_seconds": (closed - opened).total_seconds(), "error": f"{type(error).__name__}: {error}"})
            raise
        finally:
            disarm_idle_deadline()
        if timed_out.is_set():
            raise TimeoutError(f"provider stream was idle for {self.request_timeout:.1f}s before the first or next token")
        closed = datetime.now(timezone.utc)
        self._append_http_log({"timestamp": closed.isoformat(), "event": "stream_closed", "opened_at": opened.isoformat(), "closed_at": closed.isoformat(), "duration_seconds": (closed - opened).total_seconds()})
        source = self._strip_code_fence(raw_output)
        if not source.strip():
            raise RuntimeError("OpenAI returned an empty agent program")
        return PlannedTurn(source, request, raw_output, resolved_model or self.model, usage)

    def _discard_timed_out_client(self, client: ResponsesClient) -> None:
        """Do not reuse a socket force-closed to stop a timed-out stream."""
        if self._client_is_external:
            return
        with self._client_lock:
            if self._client is client:
                self._client = None

    @staticmethod
    def _read_stream(response: Any, on_delta: Callable[[str], None] | None) -> tuple[str, str | None, dict[str, int] | None]:
        # The small compatibility path keeps injected test clients usable.
        if hasattr(response, "output_text"):
            text = str(response.output_text)
            if on_delta is not None and text:
                on_delta(text)
            return text, getattr(response, "model", None), OpenAICompatibleAgentDriver._usage_dict(getattr(response, "usage", None))
        parts: list[str] = []
        resolved_model: str | None = None
        usage: dict[str, int] | None = None
        for event in response:
            completed_response = getattr(event, "response", None)
            if completed_response is not None:
                resolved_model = getattr(completed_response, "model", resolved_model)
                usage = OpenAICompatibleAgentDriver._usage_dict(getattr(completed_response, "usage", None)) or usage
            usage = OpenAICompatibleAgentDriver._usage_dict(getattr(event, "usage", None)) or usage
            if getattr(event, "type", None) != "response.output_text.delta":
                continue
            delta = getattr(event, "delta", "")
            if delta:
                text = str(delta)
                parts.append(text)
                if on_delta is not None:
                    on_delta(text)
        return "".join(parts), resolved_model, usage

    @staticmethod
    def _read_chat_stream(response: Any, on_delta: Callable[[str], None] | None) -> tuple[str, str | None, dict[str, int] | None]:
        parts: list[str] = []
        resolved_model: str | None = None
        usage: dict[str, int] | None = None
        for chunk in response:
            resolved_model = getattr(chunk, "model", resolved_model)
            usage = OpenAICompatibleAgentDriver._usage_dict(getattr(chunk, "usage", None)) or usage
            choices = getattr(chunk, "choices", [])
            if not choices:
                continue
            delta = getattr(choices[0], "delta", None)
            text = getattr(delta, "content", None)
            if text:
                text = str(text); parts.append(text)
                if on_delta is not None:
                    on_delta(text)
        return "".join(parts), resolved_model, usage

    @staticmethod
    def _usage_dict(usage: Any) -> dict[str, int] | None:
        """Normalize OpenAI-style usage without depending on a provider SDK type."""
        if usage is None:
            return None
        values = {
            "input_tokens": getattr(usage, "input_tokens", None) or getattr(usage, "prompt_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None) or getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }
        values = {key: int(value) for key, value in values.items() if value is not None}
        if not values:
            return None
        if "total_tokens" not in values:
            values["total_tokens"] = values.get("input_tokens", 0) + values.get("output_tokens", 0)
        return values

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
        if self._http_log_path is not None:
            try:
                import httpx
            except ImportError:
                pass
            else:
                def log_request(request: Any) -> None:
                    headers = {key: ("[redacted]" if key.lower() == "authorization" else value) for key, value in request.headers.items()}
                    self._append_http_log({
                        "timestamp": datetime.now(timezone.utc).isoformat(), "event": "request",
                        "method": request.method, "url": str(request.url), "headers": headers,
                        "body": request.content.decode("utf-8", errors="replace"),
                    })

                def log_response(response: Any) -> None:
                    self._append_http_log({
                        "timestamp": datetime.now(timezone.utc).isoformat(), "event": "response",
                        "method": response.request.method, "url": str(response.request.url), "status_code": response.status_code,
                    })

                options["http_client"] = httpx.Client(event_hooks={"request": [log_request], "response": [log_response]})
        with self._client_lock:
            if self._client is None:
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


class GroqAgentDriver(OpenAICompatibleAgentDriver):
    """Groq Responses API; useful for fast, free-tier smoke tests."""

    def __init__(self, model: str = DEFAULT_GROQ_MODEL, client: ResponsesClient | None = None, request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> None:
        super().__init__(model, client, provider_name="Groq", api_key_environment="GROQ_API_KEY", base_url="https://api.groq.com/openai/v1", request_timeout=request_timeout)


class GeminiAgentDriver(OpenAICompatibleAgentDriver):
    """Gemini's OpenAI-compatible streaming chat-completions endpoint."""

    def __init__(self, model: str = DEFAULT_GEMINI_MODEL, client: ResponsesClient | None = None, request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> None:
        super().__init__(model, client, provider_name="Google Gemini", api_key_environment="GEMINI_API_KEY", base_url="https://generativelanguage.googleapis.com/v1beta/openai/", request_timeout=request_timeout, transport="chat_completions")


class FallbackAgentDriver:
    """Tries configured providers in order, retaining the one that served a turn."""

    def __init__(self, drivers: list[OpenAICompatibleAgentDriver]) -> None:
        if not drivers:
            raise ValueError("at least one provider driver is required")
        self.drivers = drivers
        self._selected = drivers[0]
        self.last_failures: list[dict[str, str]] = []

    @property
    def model(self) -> str:
        return self._selected.model

    @property
    def provider_name(self) -> str:
        return self._selected.provider_name

    @property
    def request_timeout(self) -> float:
        return self._selected.request_timeout

    @property
    def output_token_reserve(self) -> int:
        return self._selected.output_token_reserve

    @output_token_reserve.setter
    def output_token_reserve(self, value: int) -> None:
        for driver in self.drivers:
            driver.output_token_reserve = value

    def set_http_log_path(self, path: str) -> None:
        for driver in self.drivers:
            driver.set_http_log_path(path)

    def close(self) -> None:
        for driver in self.drivers:
            driver.close()

    def clone(self) -> "FallbackAgentDriver":
        return FallbackAgentDriver([driver.clone() for driver in self.drivers])

    def plan(self, activation: list[dict[str, Any]], model_feedback: dict[str, Any] | None, on_delta: Callable[[str], None] | None = None, role: str = "agent") -> PlannedTurn:
        self.last_failures = []
        for driver in self.drivers:
            try:
                planned = driver.plan(activation, model_feedback, on_delta, role)
            except Exception as error:
                self.last_failures.append({"provider": driver.provider_name, "model": driver.model, "error": f"{type(error).__name__}: {error}"})
                continue
            self._selected = driver
            return planned
        details = "; ".join(f"{item['provider']}: {item['error']}" for item in self.last_failures)
        raise ProviderFallbackError(f"all configured providers failed: {details}")


def driver_from_policy(specification: dict[str, Any], request_timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS) -> OpenAICompatibleAgentDriver:
    """Construct a provider driver from the durable, user-editable policy form."""
    provider = str(specification.get("provider", "")).strip().lower().replace(" ", "")
    model = specification.get("model")
    factories = {
        "openai": (OpenAIAgentDriver, DEFAULT_OPENAI_MODEL),
        "openrouter": (OpenRouterAgentDriver, DEFAULT_OPENROUTER_MODEL),
        "groq": (GroqAgentDriver, DEFAULT_GROQ_MODEL),
        "googlegemini": (GeminiAgentDriver, DEFAULT_GEMINI_MODEL),
        "gemini": (GeminiAgentDriver, DEFAULT_GEMINI_MODEL),
    }
    try:
        factory, default_model = factories[provider]
    except KeyError as error:
        raise ValueError(f"unknown inference provider: {specification.get('provider')!r}") from error
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("provider model must be a non-empty string")
    return factory(model or default_model, request_timeout=request_timeout)


class OpenAIAgentController:
    """Connects durable runtime state to a single OpenAI-planned agent turn."""

    def __init__(
        self,
        supervisor: Supervisor,
        driver: OpenAICompatibleAgentDriver,
        agent: str = "agent",
        *,
        default_context_window: bool = True,
    ) -> None:
        self.supervisor = supervisor
        self.driver = driver
        self.agent = agent
        self.default_context_window = default_context_window

    def run_turn(
        self,
        on_delta: Callable[[str], None] | None = None,
        on_program: Callable[[PlannedTurn], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
    ) -> KernelResult | None:
        turn_id = uuid.uuid4().hex
        self.supervisor.set_turn_id(self.agent, turn_id)
        self.supervisor.events.emit("model.turn_started", agent=self.agent, turn_id=turn_id,
                                    payload={"provider": self.driver.provider_name, "model": self.driver.model})
        pending = self.supervisor.journal.pending(self.agent)
        relevant = [message for message in pending if message.text.strip() not in _LEGACY_HARNESS_COMMANDS]
        activation = [] if not relevant else [{
            "kind": "inbox_message",
            "message_id": relevant[-1].id,
            "sender": relevant[-1].sender,
            "text": relevant[-1].text,
        }]
        if not activation:
            self.supervisor.events.emit("model.turn_skipped", agent=self.agent, turn_id=turn_id, payload={"reason": "no pending activation"})
            self.supervisor.set_turn_id(self.agent, None)
            return None
        if self.default_context_window:
            activation[0]["context_window"] = self._context_window()
            activation[0]["pending_work"] = self._pending_work(relevant)
            active_tasks = self._active_task_summary()
            if active_tasks:
                activation[0]["active_tasks"] = active_tasks
            delegated_tasks = self._delegated_task_summary()
            if delegated_tasks:
                activation[0]["delegated_tasks"] = delegated_tasks
            available_agents = self.supervisor.agent_roles(exclude=self.agent)
            if available_agents:
                activation[0]["available_agents"] = available_agents
        feedback_value = self.supervisor.observable_state.get(self.agent, "model_error")
        model_feedback = feedback_value.value if feedback_value is not None and feedback_value.value.get("active") else None
        scopes = [("session", "")]
        if relevant:
            scopes.append(("message", str(relevant[-1].id)))
        active_task = self.supervisor._active_tasks.get(self.agent)
        if active_task is not None:
            scopes.append(("task", str(active_task)))
        if model_feedback is not None:
            scopes.append(("error", str(relevant[-1].id)))
        working_context = self.supervisor.working_context.active(self.agent, scopes)
        if working_context:
            activation[0]["working_context"] = working_context
        planned: PlannedTurn | None = None
        reservation: TokenReservation | None = None
        try:
            if on_phase is not None:
                on_phase("planning")
            activation[0]["agent_role"] = self.supervisor.agent_role(self.agent)
            estimated_input = estimate_tokens(json.dumps(activation)) + estimate_tokens(_INSTRUCTIONS)
            reservation = self.supervisor.token_budget.reserve(estimated_input, self.driver.output_token_reserve)
            self._publish_budget()
            def stream_delta(delta: str) -> None:
                self.supervisor.events.emit("model.delta", agent=self.agent, turn_id=turn_id, payload={"text": delta})
                if on_delta is not None:
                    on_delta(delta)
            planned = self.driver.plan(activation, model_feedback, stream_delta, role=self.supervisor.agent_role(self.agent))
            actual_tokens = (planned.usage or {}).get("total_tokens", estimated_input + estimate_tokens(planned.raw_output))
            self.supervisor.token_budget.settle(reservation, actual_tokens)
            reservation = None
            self._publish_budget(planned.usage)
            if on_program is not None:
                on_program(planned)
            self.supervisor.events.emit("model.program", agent=self.agent, turn_id=turn_id,
                                        payload={"source": planned.source, "resolved_model": planned.resolved_model, "usage": planned.usage})
            self._validate_program(planned.source)
        except OpenAIConfigurationError:
            if reservation is not None:
                self.supervisor.token_budget.settle(reservation, reservation.estimated_input_tokens)
                self._publish_budget()
            self.supervisor.events.emit("model.turn_failed", agent=self.agent, turn_id=turn_id, payload={"error": "provider configuration unavailable"})
            self.supervisor.set_turn_id(self.agent, None)
            raise
        except Exception as error:
            if reservation is not None:
                self.supervisor.token_budget.settle(reservation, reservation.estimated_input_tokens)
                self._publish_budget()
            error_text = f"{type(error).__name__}: {error}"
            self.supervisor.events.emit("model.turn_failed", agent=self.agent, turn_id=turn_id, payload={"error": error_text})
            instruction = "Your previous output was rejected. Return one valid Python program only."
            if isinstance(error, SyntaxError) and "escape" in str(error).lower():
                instruction = "Your program has an invalid escape sequence. Use a raw regex string such as r'\\s+' or double each backslash, then return corrected Python only."
            feedback: dict[str, Any] = {
                "active": True,
                "error": error_text,
                "instruction": instruction,
            }
            if planned is not None:
                feedback["rejected_output"] = planned.raw_output
                feedback["failed_program"] = planned.source
            self.supervisor.publish_state(
                self.agent,
                "model_error",
                feedback,
                presenter="error",
                label="Model planning error",
                show_by_default=False,
                priority=100,
            )
            if planned is None:
                self.supervisor.publish_state(
                    self.agent,
                    "provider",
                    {
                        "status": "rate_limited" if "rate limit" in error_text.lower() or "429" in error_text else "unavailable",
                        "provider": self.driver.provider_name,
                        "model": self.driver.model,
                        "retry_after_seconds": self._retry_after_seconds(error_text) if "rate limit" in error_text.lower() or "429" in error_text else 60,
                        "error": error_text,
                        "fallback_failures": getattr(self.driver, "last_failures", []),
                    },
                    presenter="error",
                    label="Provider",
                    show_by_default=True,
                    priority=100,
                )
            lowered = error_text.lower()
            if "ratelimit" in lowered or "rate limit" in lowered or "status code: 429" in lowered or " 429" in lowered:
                retry_after = self._retry_after_seconds(error_text)
                self.supervisor.publish_state(
                    self.agent,
                    "provider",
                    {"status": "rate_limited", "provider": self.driver.provider_name, "model": self.driver.model, "retry_after_seconds": retry_after, "error": error_text},
                    presenter="error",
                    label="Provider",
                    show_by_default=True,
                    priority=100,
                )
            self.supervisor.set_turn_id(self.agent, None)
            return KernelResult("error", error=error_text)
        if on_phase is not None:
            on_phase("executing")
        self.supervisor.set_turn_messages(self.agent, [relevant[-1].id])
        try:
            result = self.supervisor.agent_kernel(self.agent).evaluate(planned.source)
        finally:
            self.supervisor.clear_turn_messages(self.agent)
        if result.status == "ok":
            self.supervisor.publish_state(
                self.agent,
                "model_error",
                {"active": False, "status": "resolved"},
                presenter="error",
                show_by_default=False,
            )
            self.supervisor.publish_state(
                self.agent,
                "provider",
                {
                    "status": "available",
                    "provider": self.driver.provider_name,
                    "model": self.driver.model,
                    "fallback_failures": getattr(self.driver, "last_failures", []),
                },
                presenter="json",
                label="Provider",
                show_by_default=False,
            )
        else:
            error_text = result.error or "model program evaluation failed"
            repair_instruction = "Your program ran but failed. Inspect the error, preserve completed work, and return one corrected Python program."
            if "workspace write requires reading existing file first" in error_text:
                repair_instruction = "The target file already exists. Reuse the active task, call workspace.read_text(path), then call workspace.write_text(path, text); do not create another task."
            elif "has no attribute 'exists'" in error_text:
                repair_instruction = "Use workspace.exists(path) for an existence check, then correct the failed program without creating duplicate tasks."
            elif "is claimed by" in error_text:
                repair_instruction = "This is your existing active task's workspace claim. Continue that task and do not announce a duplicate task or claim."
            self.supervisor.publish_state(
                self.agent,
                "model_error",
                {
                    "active": True,
                    "error": error_text,
                    "instruction": repair_instruction,
                    "rejected_output": planned.raw_output,
                    "failed_program": planned.source,
                },
                presenter="error",
                label="Model evaluation error",
                show_by_default=False,
                priority=100,
            )
        self.supervisor.events.emit("model.turn_completed", agent=self.agent, turn_id=turn_id,
                                    payload={"status": result.status, "error": result.error})
        self.supervisor.set_turn_id(self.agent, None)
        return result

    def _publish_budget(self, last_turn_usage: dict[str, int] | None = None) -> None:
        budget = self.supervisor.token_budget.snapshot()
        if last_turn_usage is not None:
            budget["last_turn_usage"] = last_turn_usage
            budget["last_turn_accounting"] = "provider_reported"
        self.supervisor.publish_state(
            self.agent, "token_budget", budget, presenter="json",
            label="Token budget", show_by_default=True, priority=90,
        )

    def _context_window(self) -> list[dict[str, Any]]:
        """A bounded continuity aid; deeper history remains explicitly pullable."""
        remaining = DEFAULT_CONTEXT_WINDOW_CHARS
        selected: list[dict[str, Any]] = []
        messages = [
            message for message in self.supervisor.journal.conversation("user", self.agent)
            if message.text.strip() not in _LEGACY_HARNESS_COMMANDS
        ][-DEFAULT_CONTEXT_WINDOW_MESSAGES:]
        for message in reversed(messages):
            if remaining <= 0:
                break
            text = message.text[:remaining]
            selected.append({"message_id": message.id, "sender": message.sender, "text": text})
            remaining -= len(text)
        return list(reversed(selected))

    @staticmethod
    def _pending_work(messages: list[Any]) -> list[dict[str, Any]]:
        return [
            {"message_id": message.id, "sender": message.sender, "text": message.text[:800]}
            for message in messages[-DEFAULT_PENDING_WORK_ITEMS:]
        ]

    def _active_task_summary(self) -> list[dict[str, Any]]:
        return [
            {"id": task.id, "title": task.title, "state": task.state, "details": task.details}
            for task in self.supervisor.tasks.list(self.agent)
            if task.state != "completed"
        ][-DEFAULT_PENDING_WORK_ITEMS:]

    def _delegated_task_summary(self) -> list[dict[str, Any]]:
        return [
            {"id": task.id, "owner": task.owner, "title": task.title, "state": task.state, "details": task.details}
            for task in self.supervisor.tasks.delegated(self.agent)
        ][-DEFAULT_PENDING_WORK_ITEMS:]

    @staticmethod
    def _retry_after_seconds(error: str) -> float:
        match = re.search(r"retry[- ]after[^0-9]*(\d+(?:\.\d+)?)", error, re.IGNORECASE)
        return min(300.0, max(1.0, float(match.group(1)))) if match else 30.0

    @staticmethod
    def _validate_program(source: str) -> None:
        tree = ast.parse(source, "<spaghetti-agent-model-output>", "exec")
        runtime_globals = {"inbox", "tasks", "workspace", "context", "observable", "user", "agents", "conflicts"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name.split(".")[0] in runtime_globals for alias in node.names):
                raise ValueError("runtime APIs are injected globals; do not import them")
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] in runtime_globals:
                raise ValueError("runtime APIs are injected globals; do not import them")
