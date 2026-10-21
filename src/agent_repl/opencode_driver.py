"""Plan agent turns through a locally installed opencode CLI.

opencode owns provider authentication, so this adapter can use any model that
`opencode models` lists, including subscription-backed logins such as a
ChatGPT account, without Spaghetti Agent handling the credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Callable

from .openai_driver import (
    _INSTRUCTIONS,
    _ROLE_POLICIES,
    OpenAICompatibleAgentDriver,
    OpenAIConfigurationError,
    PlannedTurn,
)


DEFAULT_OPENCODE_MODEL = "openai/gpt-6-luna"
DEFAULT_OPENCODE_TIMEOUT_SECONDS = 180.0
_DEFAULT_ROLE_POLICY = "\nRole policy: Complete only your assigned task and communicate durable results."


class QuotaExceeded(RuntimeError):
    """The provider's usage window is exhausted; retrying before the reset only fails again."""

    def __init__(self, message: str, resets_in_seconds: float | None) -> None:
        detail = f" (resets in {resets_in_seconds:.0f}s)" if resets_in_seconds is not None else ""
        super().__init__(f"opencode provider.quota: {message}{detail}")
        self.resets_in_seconds = resets_in_seconds


def _resets_in(error: dict[str, Any]) -> float | None:
    try:
        body = json.loads((error.get("response") or {}).get("body") or "{}")
        value = (body.get("error") or {}).get("resets_in_seconds")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class OpencodeCompletion:
    text: str
    session_id: str
    usage: dict[str, int]
    duration_seconds: float


class OpencodeClient:
    """Runs single tool-free opencode messages and reports their token usage.

    Each agent is a system prompt written to `<work_dir>/.opencode/agent/`.
    Passing `session_id` continues an opencode session, so opencode replays the
    prior messages; omitting it starts a fresh, history-free session.
    """

    def __init__(self, work_dir: str | Path | None = None, executable: str = "opencode", timeout: float = DEFAULT_OPENCODE_TIMEOUT_SECONDS) -> None:
        self.work_dir = Path(work_dir) if work_dir else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "spaghetti-agent" / "opencode"
        self.executable = executable
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def define_agent(self, name: str, system_prompt: str) -> None:
        agent_dir = self.work_dir / ".opencode" / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        definition = (
            "---\n"
            "description: Spaghetti Agent tool-free planner\n"
            "mode: primary\n"
            "tools:\n  \"*\": false\n"
            "permission:\n  \"*\": deny\n"
            "---\n" + system_prompt.strip() + "\n"
        )
        path = agent_dir / f"{name}.md"
        if not path.exists() or path.read_text(encoding="utf-8") != definition:
            path.write_text(definition, encoding="utf-8")

    def complete(self, model: str, agent: str, message: str, session_id: str | None = None) -> OpencodeCompletion:
        try:
            return self._complete(model, agent, message, session_id)
        except RuntimeError as error:
            if "Agent not found" not in str(error):
                raise
            # The background service may not have noticed a just-written agent definition yet.
            self._run(["reload"])
            return self._complete(model, agent, message, session_id)

    def _complete(self, model: str, agent: str, message: str, session_id: str | None = None) -> OpencodeCompletion:
        command = [self.executable, "run", "--format", "json", "--agent", agent, "-m", model, "--title", f"spaghetti-agent {agent}"]
        if session_id:
            command += ["--session", session_id]
        opened = datetime.now(timezone.utc)
        try:
            # The message goes through stdin: argv caps a single argument at 128 KiB.
            process = subprocess.run(
                command, cwd=self.work_dir, env=self._environment(), input=message, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"opencode did not answer within {self.timeout:.0f}s") from error
        parts: list[str] = []
        resolved_session = session_id
        for line in process.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            resolved_session = event.get("sessionID") or resolved_session
            if event.get("type") == "error":
                error = event.get("error") or {}
                if error.get("type") == "provider.quota":
                    raise QuotaExceeded(error.get("message", "usage limit reached"), _resets_in(error))
                raise RuntimeError(f"opencode {error.get('type', 'error')}: {error.get('message', 'unknown failure')}")
            part = event.get("part") or {}
            if event.get("type") == "text" and part.get("text"):
                parts.append(part["text"])
        if process.returncode != 0 and not parts:
            raise RuntimeError(f"opencode exited with status {process.returncode}: {process.stderr.strip()[-400:]}")
        if not resolved_session:
            raise RuntimeError("opencode did not report a session")
        usage = self.last_usage(resolved_session)
        duration = (datetime.now(timezone.utc) - opened).total_seconds()
        return OpencodeCompletion("".join(parts), resolved_session, usage, duration)

    def last_usage(self, session_id: str) -> dict[str, int]:
        """Usage of the session's most recent assistant message, prompt cache included."""
        exported = self.export_session(session_id)
        assistant = [message for message in exported.get("messages", []) if message.get("type") == "assistant"]
        tokens = (assistant[-1].get("tokens") if assistant else None) or {}
        cache = tokens.get("cache") or {}
        uncached, cache_read, cache_write = int(tokens.get("input", 0)), int(cache.get("read", 0)), int(cache.get("write", 0))
        output, reasoning = int(tokens.get("output", 0)), int(tokens.get("reasoning", 0))
        usage = {
            "input_tokens": uncached + cache_read + cache_write,
            "uncached_input_tokens": uncached,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "output_tokens": output + reasoning,
            "reasoning_tokens": reasoning,
        }
        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        return usage

    def export_session(self, session_id: str) -> dict[str, Any]:
        # opencode truncates large exports written to a pipe, so export through a file.
        with tempfile.TemporaryFile("w+", encoding="utf-8") as output:
            process = subprocess.run(
                [self.executable, "session", "export", session_id], cwd=self.work_dir, env=self._environment(),
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.PIPE, text=True, timeout=60,
            )
            if process.returncode != 0:
                raise RuntimeError(f"opencode session export failed: {process.stderr.strip()[-400:]}")
            output.seek(0)
            return json.loads(output.read())

    def delete_session(self, session_id: str) -> None:
        try:
            self._run(["session", "delete", session_id])
        except RuntimeError:
            pass

    def _environment(self) -> dict[str, str]:
        # opencode resolves its project (and agent definitions) from $PWD, not the process cwd.
        return {**os.environ, "PWD": str(self.work_dir)}

    def _run(self, arguments: list[str]) -> str:
        process = subprocess.run(
            [self.executable, *arguments], cwd=self.work_dir, env=self._environment(), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        if process.returncode != 0:
            raise RuntimeError(f"opencode {' '.join(arguments[:2])} failed: {process.stderr.strip()[-400:]}")
        return process.stdout


class OpencodeAgentDriver(OpenAICompatibleAgentDriver):
    """Plans each activation in a fresh opencode session: no transcript is replayed."""

    def __init__(
        self,
        model: str = DEFAULT_OPENCODE_MODEL,
        client: OpencodeClient | None = None,
        request_timeout: float = DEFAULT_OPENCODE_TIMEOUT_SECONDS,
        *,
        keep_sessions: bool = False,
        instructions: str | None = None,
    ) -> None:
        super().__init__(model, provider_name="opencode", api_key_environment="", request_timeout=request_timeout)
        self.opencode = client or OpencodeClient(timeout=request_timeout)
        self.keep_sessions = keep_sessions
        self.instructions = instructions  # replaces the default instructions and role policy

    def clone(self) -> "OpencodeAgentDriver":
        clone = OpencodeAgentDriver(self.model, self.opencode, self.request_timeout, keep_sessions=self.keep_sessions, instructions=self.instructions)
        clone.output_token_reserve = self.output_token_reserve
        return clone

    def close(self) -> None:
        pass

    def validate_configuration(self) -> None:
        if not self.opencode.available():
            raise OpenAIConfigurationError(f"Install opencode and log in to a provider; `{self.opencode.executable}` was not found")

    def plan(
        self,
        activation: list[dict[str, Any]],
        model_feedback: dict[str, Any] | None,
        on_delta: Callable[[str], None] | None = None,
        role: str = "agent",
    ) -> PlannedTurn:
        self.validate_configuration()
        request = {"activation": activation, "model_feedback": model_feedback}
        if self.instructions is not None:
            agent = f"spaghetti-custom-{hashlib.sha1(self.instructions.encode()).hexdigest()[:8]}"
            self.opencode.define_agent(agent, self.instructions)
        else:
            agent = f"spaghetti-{role}"
            self.opencode.define_agent(agent, _INSTRUCTIONS + _ROLE_POLICIES.get(role, _DEFAULT_ROLE_POLICY))
        self._append_http_log({"timestamp": datetime.now(timezone.utc).isoformat(), "event": "stream_opened", "provider": self.provider_name, "model": self.model})
        try:
            completion = self.opencode.complete(self.model, agent, json.dumps(request))
        except Exception as error:
            self._append_http_log({"timestamp": datetime.now(timezone.utc).isoformat(), "event": "stream_closed", "error": f"{type(error).__name__}: {error}"})
            raise
        self._append_http_log({"timestamp": datetime.now(timezone.utc).isoformat(), "event": "stream_closed", "duration_seconds": completion.duration_seconds, "usage": completion.usage})
        if not self.keep_sessions:
            self.opencode.delete_session(completion.session_id)
        if on_delta is not None and completion.text:
            on_delta(completion.text)
        source = self._strip_code_fence(completion.text)
        if not source.strip():
            raise RuntimeError("opencode returned an empty agent program")
        return PlannedTurn(source, request, completion.text, self.model, completion.usage)
