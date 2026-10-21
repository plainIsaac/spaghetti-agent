"""The two context strategies under comparison.

`TranscriptArm` is the conventional agent loop: one long conversation, the model
writes Python, sees its output, and continues; every call replays the history.
`RuntimeArm` is Spaghetti Agent as shipped: each activation is a fresh, bounded
request and the agent pulls anything else from durable runtime state.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import io
import json
import signal
import time
import traceback
from typing import Any, Callable

from agent_repl.opencode_driver import OpencodeAgentDriver, OpencodeClient

OUTPUT_LIMIT = 6_000
MAX_STEPS_PER_MESSAGE = 8

TRANSCRIPT_SYSTEM_PROMPT = """You are a coding agent working with a user inside a persistent Python REPL.
The REPL's current directory is the user's project workspace.
Every message you send must be only Python source code, with no Markdown fences or prose.
After each program runs you receive its stdout, stderr, and any exception; output beyond
6000 characters is truncated. Variables persist between programs.
Use ordinary Python (open, csv, subprocess, and so on) to inspect and change files.
When you have answered or finished the user's request, call reply(text) with a concise
message for the user; that ends your turn. Ask the user through reply(text) if something
important is ambiguous."""

Recorder = Callable[[dict[str, Any]], None]


class _Timeout(Exception):
    pass


def _alarm(_signum: int, _frame: Any) -> None:
    raise _Timeout("program exceeded 60 seconds")


class TranscriptArm:
    name = "transcript"

    def __init__(self, model: str, client: OpencodeClient, record: Recorder) -> None:
        self.model, self.client, self.record = model, client, record
        self.client.define_agent("transcript-baseline", TRANSCRIPT_SYSTEM_PROMPT)
        self.session_id: str | None = None
        self.replies: list[str] = []
        self.namespace: dict[str, Any] = {"reply": self.replies.append, "__name__": "__agent__"}

    def handle(self, step: int, text: str) -> list[str]:
        self.replies.clear()
        message = f"User message: {text}"
        for call in range(MAX_STEPS_PER_MESSAGE):
            completion = self.client.complete(self.model, "transcript-baseline", message, self.session_id)
            self.session_id = completion.session_id
            output = self._execute(OpencodeAgentDriver._strip_code_fence(completion.text))
            self.record({"step": step, "call": call, "usage": completion.usage, "seconds": completion.duration_seconds,
                         "program": completion.text, "output": output[:2_000]})
            if self.replies:
                break
            message = f"Program output:\n{output}" if output.strip() else "Program output: (none)"
        return list(self.replies)

    def _execute(self, source: str) -> str:
        buffer = io.StringIO()
        previous = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(60)
        try:
            with redirect_stdout(buffer), redirect_stderr(buffer):
                exec(compile(source, "<agent>", "exec"), self.namespace)
        except BaseException:  # the model's program may raise anything, including SystemExit
            buffer.write(traceback.format_exc(limit=3))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        output = buffer.getvalue()
        return output if len(output) <= OUTPUT_LIMIT else output[:OUTPUT_LIMIT] + "\n[output truncated]"

    def close(self) -> None:
        if self.session_id:
            self.client.delete_session(self.session_id)


class _RecordingDriver(OpencodeAgentDriver):
    def __init__(self, model: str, client: OpencodeClient, record: Recorder) -> None:
        super().__init__(model, client)
        self.record, self.step, self.calls = record, 0, 0

    def plan(self, activation, model_feedback, on_delta=None, role="agent"):
        started = time.monotonic()
        planned = super().plan(activation, model_feedback, on_delta, role)
        self.record({"step": self.step, "call": self.calls, "usage": planned.usage, "seconds": time.monotonic() - started,
                     "program": planned.raw_output, "activation_chars": len(json.dumps(planned.request))})
        self.calls += 1
        return planned


class RuntimeArm:
    """Drives one Spaghetti Agent session the way ModelTurnWorker does.

    With `continue_until_answered`, the arm also re-triggers the agent while the
    user's message is still pending; the shipped worker does not.
    """

    MAX_REPAIRS = 2
    MAX_WAKEUPS = 4

    def __init__(self, model: str, client: OpencodeClient, record: Recorder, data_dir: Path, continue_until_answered: bool = False,
                 observe_program_output: bool = False) -> None:
        from agent_repl.session import SingleAgentSession
        self.session = SingleAgentSession.open(str(data_dir / "inbox.sqlite"), str(data_dir / "observable-state.sqlite"))
        self.driver = _RecordingDriver(model, client, record)
        self.record = record
        self.continue_until_answered = continue_until_answered
        self.observe_program_output = observe_program_output
        self.name = "runtime-observe" if observe_program_output else "runtime-continue" if continue_until_answered else "runtime"

    def handle(self, step: int, text: str) -> list[str]:
        self.driver.step, self.driver.calls = step, 0
        journal = self.session.supervisor.journal
        seen = {message.id for message in journal.conversation("user", self.session.agent)}
        sent = self.session.send(text)
        self._turn_with_repairs()
        for _ in range(self.MAX_WAKEUPS):
            pending = journal.pending(self.session.agent)
            wakeups = [message for message in pending if message.sender == "supervisor"]
            unanswered = self.continue_until_answered and any(message.id == sent.id for message in pending)
            if not wakeups and not unanswered:
                break
            self._turn_with_repairs()
        return [message.text for message in journal.conversation("user", self.session.agent)
                if message.id not in seen and message.sender != "user"]

    def _turn_with_repairs(self) -> None:
        for attempt in range(1 + self.MAX_REPAIRS):
            try:
                result = self.session.run_openai_turn(self.driver, observe_program_output=self.observe_program_output)
            except Exception as error:  # surfaced runtime defects are part of the measurement
                self.record({"step": self.driver.step, "runtime_error": f"{type(error).__name__}: {error}"})
                return
            if result is None or result.status != "error":
                return

    def close(self) -> None:
        self.session.close()
