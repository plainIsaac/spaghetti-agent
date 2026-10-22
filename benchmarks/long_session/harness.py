"""A code-act agent with a token budget and pluggable context compaction.

Every arm keeps an ordinary transcript (cache-friendly: each prompt extends the
previous one) and a persistent Python namespace. They differ only in what
happens when the transcript exceeds the budget, or after a long pause:

* `summary`  — the model writes a summary of the conversation (today's harnesses);
* `rebuild`  — the runtime rebuilds a small context from durable state with no
  model call, and exposes the full history through Python (`history`, `note`);
* `rebuild-notes` — `rebuild` with low reasoning effort and explicit note-taking.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
import difflib
import hashlib
import io
import os
import signal
import traceback
from typing import Any, Callable

from agent_repl.opencode_driver import OpencodeAgentDriver, OpencodeClient

OUTPUT_LIMIT = 4_000
MAX_CALLS_PER_MESSAGE = 6
RECENT_MESSAGES = 2  # completed user messages kept verbatim across a compaction
USER_MESSAGE_CHARS = 16_000
PAUSE_REBUILD_TOKENS = 8_000  # rebuild on resume when the cold prompt would be larger than this

_BASE_PROMPT = """You are a coding agent working with a user inside a persistent Python REPL whose
current directory is the user's project workspace.
The conversation so far is in the user message. Reply with only the next Python program:
no Markdown fences, no prose. After it runs you will see its stdout, stderr, and any
exception (truncated beyond 4000 characters). Variables persist between programs.
Use ordinary Python (open, csv, subprocess, ...) to inspect and change files.
When you have answered or finished the user's latest request, call reply(text) with a
concise message for the user; that ends your turn."""

PROMPTS = {
    "summary": _BASE_PROMPT + """
When the conversation grows long, earlier parts are replaced by a summary you wrote.""",
    "rebuild": _BASE_PROMPT + """
When the conversation grows long, earlier turns are dropped and replaced by session state
rebuilt by the runtime. The full history stays available in Python:
history.messages(query=None) -> user messages [(number, text)], history.turns(number) ->
your programs, outputs and replies for that message, history.replies(query=None),
history.file_changes(path=None) -> files you changed, with diffs.
note(text) saves a note that is shown in every rebuilt context.""",
}
PROMPTS["rebuild-notes"] = PROMPTS["rebuild"] + """
Keep your reasoning brief. Whenever you learn something worth keeping (a fact, preference,
convention, constraint, decision, or result), save it immediately with note(text)."""

SUMMARY_REQUEST = """[runtime] The context limit has been reached. Instead of a program, write plain text: a
summary of the conversation so far for your future self, who will see only this summary
and the latest messages. Include every fact, preference, convention, constraint and
instruction the user gave; files you changed and how; open work; and anything else
needed to continue."""

Recorder = Callable[[dict[str, Any]], None]


class _Timeout(Exception):
    pass


def _alarm(_signum: int, _frame: Any) -> None:
    raise _Timeout("program exceeded 60 seconds")


@dataclass
class Turn:
    message: int
    entries: list[str] = field(default_factory=list)  # rendered transcript lines for this message


class History:
    """Durable session record, exposed to the agent in the rebuild arms."""

    def __init__(self) -> None:
        self.user: list[tuple[int, str]] = []
        self.turns: dict[int, list[dict[str, str]]] = {}
        self.reply_log: list[tuple[int, str]] = []
        self.changes: list[dict[str, Any]] = []
        self.notes: list[str] = []

    # -- agent API --------------------------------------------------------
    def messages(self, query: str | None = None) -> list[tuple[int, str]]:
        return [item for item in self.user if query is None or query.lower() in item[1].lower()]

    def turns_for(self, number: int) -> list[dict[str, str]]:
        return [{key: value[:2_000] for key, value in entry.items()} for entry in self.turns.get(number, [])]

    def replies(self, query: str | None = None) -> list[tuple[int, str]]:
        return [item for item in self.reply_log if query is None or query.lower() in item[1].lower()]

    def file_changes(self, path: str | None = None) -> list[dict[str, Any]]:
        return [change for change in self.changes if path is None or change["path"] == path]


class _HistoryAPI:
    def __init__(self, history: History) -> None:
        self._history = history
        self.messages = history.messages
        self.turns = history.turns_for
        self.replies = history.replies
        self.file_changes = history.file_changes

    def __repr__(self) -> str:
        return "history: messages(query), turns(number), replies(query), file_changes(path)"


class Agent:
    def __init__(self, strategy: str, model: str, client: OpencodeClient, workspace: Path, record: Recorder, budget_tokens: int) -> None:
        self.strategy, self.client, self.workspace, self.record = strategy, client, workspace, record
        self.model = model + "#low" if strategy == "rebuild-notes" else model
        self.budget_tokens = budget_tokens
        self.agent_name = f"long-session-{strategy}"
        client.define_agent(self.agent_name, PROMPTS[strategy])
        self.history = History()
        self.replies: list[str] = []
        self.namespace: dict[str, Any] = {"__name__": "__agent__", "reply": self.replies.append}
        if strategy != "summary":
            self.namespace["history"] = _HistoryAPI(self.history)
            self.namespace["note"] = self._note
        self.preamble = ""       # summary or rebuilt state; empty until the first compaction
        self.turns: list[Turn] = []
        self.tokens_per_char = 0.3  # refined from provider usage
        self.previous_prompt = ""
        self.cold = True
        self.compactions = 0
        self.files = self._snapshot()

    # -- conversation ---------------------------------------------------
    def handle(self, number: int, text: str, after_pause: bool) -> list[str]:
        if after_pause:
            self.cold = True
            if self.strategy != "summary" and self._estimate(self._prompt()) > PAUSE_REBUILD_TOKENS:
                self._compact(number, reason="resume")
        self.replies.clear()
        self.history.user.append((number, text))
        self.history.turns[number] = []
        self.turns.append(Turn(number, [f"[user #{number}] {text}"]))
        for call in range(MAX_CALLS_PER_MESSAGE):
            if self._estimate(self._prompt()) > self.budget_tokens:
                self._compact(number, reason="budget")
            program = self._call(number, call, self._prompt(), kind="turn")
            output = self._execute(number, program)
            self.turns[-1].entries.append(f"[program]\n{program}")
            self.turns[-1].entries.append(f"[output]\n{output}" if output.strip() else "[output] (none)")
            self.history.turns[number].append({"program": program, "output": output})
            if self.replies:
                break
        for text_reply in self.replies:
            self.history.reply_log.append((number, text_reply))
            self.turns[-1].entries.append(f"[reply] {text_reply}")
        return list(self.replies)

    def _prompt(self) -> str:
        parts = [self.preamble] if self.preamble else []
        parts += ["\n".join(turn.entries) for turn in self.turns]
        return "\n\n".join(parts)

    def _estimate(self, prompt: str) -> int:
        return int((len(prompt) + len(PROMPTS[self.strategy])) * self.tokens_per_char)

    def _call(self, number: int, call: int, prompt: str, kind: str) -> str:
        shared = len(_common_prefix(self.previous_prompt, prompt))
        completion = self.client.complete(self.model, self.agent_name, prompt)
        self.client.delete_session(completion.session_id)
        usage = completion.usage
        if prompt:
            self.tokens_per_char = 0.8 * self.tokens_per_char + 0.2 * (usage["input_tokens"] / (len(prompt) + len(PROMPTS[self.strategy])))
        self.record({"step": number, "call": call, "kind": kind, "usage": usage, "seconds": completion.duration_seconds,
                     "prompt_chars": len(prompt), "system_chars": len(PROMPTS[self.strategy]), "shared_prefix_chars": shared,
                     "cold": self.cold, "program": completion.text[:4_000]})
        self.previous_prompt, self.cold = prompt, False
        return OpencodeAgentDriver._strip_code_fence(completion.text) if kind == "turn" else completion.text.strip()

    # -- compaction ------------------------------------------------------
    def _compact(self, number: int, reason: str) -> None:
        current = self.turns[-1] if self.turns and self.turns[-1].message == number else None
        completed = [turn for turn in self.turns if turn is not current]
        recent = completed[-RECENT_MESSAGES:]
        if self.strategy == "summary":
            summary = self._call(number, -1, self._prompt() + "\n\n" + SUMMARY_REQUEST, kind="summary")
            self.preamble = f"[summary of the earlier conversation, written by you]\n{summary}"
        else:
            self.preamble = self._rebuild(number)
        self.turns = recent + ([current] if current else [])
        self.compactions += 1
        self.record({"step": number, "event": "compaction", "reason": reason, "preamble_chars": len(self.preamble)})

    def _rebuild(self, number: int) -> str:
        lines = [f"[session state rebuilt by the runtime before message #{number}; earlier turns were dropped]"]
        if self.history.notes:
            lines.append("Your notes:\n" + "\n".join(f"- {item}" for item in self.history.notes))
        messages, used = [], 0
        for message_number, text in reversed(self.history.user):
            used += len(text)
            if used > USER_MESSAGE_CHARS:
                messages.append("(older messages: history.messages())")
                break
            messages.append(f"#{message_number}: {text}")
        lines.append("User messages so far, oldest first:\n" + "\n".join(reversed(messages)))
        changed: dict[str, list[int]] = {}
        for change in self.history.changes:
            changed.setdefault(change["path"], []).append(change["message"])
        if changed:
            lines.append("Files you changed (details: history.file_changes(path)):\n" + "\n".join(
                f"- {path} at messages {', '.join(f'#{n}' for n in sorted(set(numbers)))}" for path, numbers in changed.items()))
        variables = [f"{name}: {type(value).__name__}" + (f"[{len(value)}]" if isinstance(value, (list, dict, set, tuple)) else "")
                     for name, value in self.namespace.items()
                     if not name.startswith("_") and name not in {"reply", "history", "note"} and not callable(value) and not isinstance(value, type(io))]
        if variables:
            lines.append("REPL variables still defined: " + ", ".join(variables[:40]))
        return "\n\n".join(lines)

    def _note(self, text: str) -> str:
        self.history.notes.append(str(text)[:500])
        return "noted"

    # -- execution and file tracking ------------------------------------
    def _execute(self, number: int, source: str) -> str:
        buffer = io.StringIO()
        previous = signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(60)
        try:
            with redirect_stdout(buffer), redirect_stderr(buffer):
                exec(compile(source, "<agent>", "exec"), self.namespace)
        except BaseException:  # model programs may raise anything, including SystemExit
            buffer.write(traceback.format_exc(limit=3))
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        self._track_changes(number)
        output = buffer.getvalue()
        return output if len(output) <= OUTPUT_LIMIT else output[:OUTPUT_LIMIT] + "\n[output truncated]"

    def _snapshot(self) -> dict[str, tuple[str, str]]:
        files = {}
        for path in self.workspace.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.stat().st_size < 1_000_000:
                text = path.read_text(encoding="utf-8", errors="replace")
                files[str(path.relative_to(self.workspace))] = (hashlib.sha1(text.encode()).hexdigest(), text)
        return files

    def _track_changes(self, number: int) -> None:
        current = self._snapshot()
        for path in sorted(set(current) | set(self.files)):
            before, after = self.files.get(path), current.get(path)
            if before and after and before[0] == after[0]:
                continue
            diff = "".join(difflib.unified_diff((before or ("", ""))[1].splitlines(True), (after or ("", ""))[1].splitlines(True), path, path))
            self.history.changes.append({"message": number, "path": path,
                                         "kind": "created" if not before else "deleted" if not after else "modified", "diff": diff[:3_000]})
        self.files = current


def _common_prefix(left: str, right: str) -> str:
    return os.path.commonprefix([left, right])
