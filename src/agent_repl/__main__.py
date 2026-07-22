"""Minimal interactive entry point for the single-agent runtime spike."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .openai_driver import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    OpenAIAgentDriver,
    OpenAIConfigurationError,
    OpenRouterAgentDriver,
)
from .session import ModelTurnWorker, SingleAgentSession, _NOT_READY
from .multi_agent import MultiAgentSession


def _format_state(session: SingleAgentSession) -> str:
    if isinstance(session, MultiAgentSession):
        snapshot = session.state_snapshot()
        lines = ["agents:"]
        lines.extend(
            f"  {item['name']} ({item['role']}): {item['phase']} {item['elapsed_seconds']:.1f}s, {item['pending_messages']} pending"
            for item in snapshot["agents"]
        )
        lines.append("active tasks:")
        if snapshot["active_tasks"]:
            lines.extend(f"  #{task['id']} {task['owner']} {task['state']}: {task['title']}" for task in snapshot["active_tasks"])
        else:
            lines.append("  none")
        if snapshot["recent_errors"]:
            lines.append("recent errors:")
            lines.extend(f"  #{error['task_id']} {error['owner']}: {error['error']} (x{error['count']})" for error in snapshot["recent_errors"])
        if snapshot["branches"]:
            lines.append("branches:")
            lines.extend(f"  task #{branch['task_id']} {branch['agent']}: {branch['state']} ({branch['files']} file(s))" for branch in snapshot["branches"])
        return "\n".join(lines)
    values = [value for value in session.observe() if value.show_by_default]
    if not values:
        return "No observable state has been published."
    return "\n".join(f"{value.label or value.name}: {value.value}" for value in values)


def _print_model_log(session: SingleAgentSession) -> None:
    entries = [entry for entry in session.model_program_log() if entry.get("event") == "model_program"]
    if not entries:
        print("No model programs have been recorded.")
        return
    print(entries[-1]["raw_output"])


def _print_help() -> None:
    print("Type a normal message for the agent. :agents shows workers and tasks; :state, :python, :log, :model-log, :repl-log, and :http-log are debug controls.")


def _print_agents(session) -> None:
    if isinstance(session, MultiAgentSession):
        statuses = session.agent_status()
        for agent in session.agents:
            phase, elapsed = statuses.get(agent, ("idle", 0.0))
            pending = len(session.supervisor.journal.pending(agent))
            tasks = [task for task in session.supervisor.tasks.list(agent) if task.state != "completed"]
            task_text = ", ".join(f"#{task.id} {task.state}: {task.title}" for task in tasks) or "no active tasks"
            print(f"{agent}: {phase} ({elapsed:.1f}s), {pending} pending — {task_text}")
    else:
        pending = len(session.supervisor.journal.pending(session.agent))
        print(f"{session.agent}: {pending} pending message(s)")


def _print_repl_log(session: SingleAgentSession) -> None:
    entries = session.repl_log()
    if not entries:
        print("No model-to-REPL evaluations have been recorded.")
        return
    for entry in entries:
        if entry["event"] == "model_program":
            print(f"{entry['timestamp']} {entry.get('resolved_model') or 'model'} -> repl:")
            print(entry["source"])
        else:
            print(f"{entry['timestamp']} repl -> supervisor: {entry['status']}" + (f" ({entry['error']})" if entry["error"] else ""))


def _render_default_presentation(
    session: SingleAgentSession,
    seen_message_ids: set[int],
    seen_state_revisions: dict[str, int],
) -> None:
    for value in session.observe():
        if not value.show_by_default or seen_state_revisions.get(value.name) == value.revision:
            continue
        print(f"state> {value.label or value.name}: {value.value}")
        seen_state_revisions[value.name] = value.revision
    for message in session.user_messages():
        if message.id not in seen_message_ids:
            print(f"reply[{session.user_message_label(message)}]> {message.text}")
            seen_message_ids.add(message.id)


def _run_user_repl(session: SingleAgentSession) -> None:
    print("Python inspection mode. Enter one Python statement/block per line; :back returns to messages.")
    while True:
        source = input("py> ")
        if source.strip() == ":back":
            return
        result = session.user_evaluate(source)
        if result.status == "ok":
            if result.value is not None:
                print(result.value)
        else:
            print(result.error)


def _load_project_environment() -> None:
    """Load a local .env when the optional provider dependencies are installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    _load_project_environment()
    parser = argparse.ArgumentParser(description="Agent REPL single-agent session")
    parser.add_argument("--data-dir", type=Path, default=Path(".agent-repl"), help="Directory for durable session state")
    turn_mode = parser.add_mutually_exclusive_group()
    turn_mode.add_argument("--demo", action="store_true", help="Run a deterministic demo agent turn after each normal message")
    turn_mode.add_argument("--openai", action="store_true", help="Run an OpenAI-planned agent turn after each normal message")
    turn_mode.add_argument("--openrouter", action="store_true", help="Run an OpenRouter-planned agent turn after each normal message")
    agent_mode = parser.add_mutually_exclusive_group()
    agent_mode.add_argument("--multi-agent", dest="multi_agent", action="store_true", default=True, help="Run the coordinator runtime (default)")
    agent_mode.add_argument("--single-agent", dest="multi_agent", action="store_false", help="Use the legacy one-agent runtime")
    parser.add_argument("--no-subagents", action="store_true", help="Disable dynamic subagent creation")
    parser.add_argument("--model", help="Provider model override")
    parser.add_argument(
        "--default-context-window",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include bounded recent conversation, pending work, and active tasks in model activation",
    )
    parser.add_argument("--request-timeout", type=float, default=30.0, help="Provider first-token and stream-idle timeout in seconds")
    arguments = parser.parse_args()
    arguments.data_dir.mkdir(parents=True, exist_ok=True)
    multi_agent = arguments.multi_agent and not arguments.demo
    session = MultiAgentSession.open(str(arguments.data_dir), specialists=[]) if multi_agent else SingleAgentSession.open(
        str(arguments.data_dir / "inbox.sqlite"), str(arguments.data_dir / "observable-state.sqlite"),
    )
    model_driver = None
    if arguments.openai:
        model_driver = OpenAIAgentDriver(arguments.model or DEFAULT_OPENAI_MODEL, request_timeout=arguments.request_timeout)
    if arguments.openrouter:
        model_driver = OpenRouterAgentDriver(arguments.model or DEFAULT_OPENROUTER_MODEL, request_timeout=arguments.request_timeout)
    if model_driver is not None:
        model_driver.set_http_log_path(str(arguments.data_dir / "provider-http.jsonl"))
    print("Agent REPL. Send a message; the agent may continue independently. Use :help for controls.")
    seen_message_ids: set[int] = set()
    seen_state_revisions: dict[str, int] = {}
    def render_completion(result) -> None:
        print()
        _render_default_presentation(session, seen_message_ids, seen_state_revisions)
        print(f"model> evaluation {result.status if result else 'skipped'}")
        print("you> ", end="", flush=True)

    if multi_agent and model_driver is None:
        parser.error("The default coordinator runtime requires --openai or --openrouter; use --demo or --single-agent for local experiments")
    if multi_agent and model_driver is not None:
        driver_type = type(model_driver)
        drivers = {agent: driver_type(model_driver.model, request_timeout=arguments.request_timeout) for agent in session.agents}
        for agent, driver in drivers.items():
            driver.set_http_log_path(str(arguments.data_dir / f"provider-http-{agent}.jsonl"))
        session.start_workers(drivers, arguments.default_context_window)
        session.supervisor.allow_subagents = not arguments.no_subagents
        worker = session.worker(session.coordinator)
    else:
        worker = ModelTurnWorker(session, model_driver, render_completion, default_context_window=arguments.default_context_window) if model_driver is not None else None
    agents_were_active = False
    try:
        while True:
            _render_default_presentation(session, seen_message_ids, seen_state_revisions)
            prompt = "you> "
            if worker is not None:
                if multi_agent:
                    for agent in session.agents:
                        session.worker(agent).collect()
                    active = [
                        (agent, phase, elapsed)
                        for agent, (phase, elapsed) in session.agent_status().items()
                        if phase not in {"idle", "completed"}
                    ]
                    if active:
                        prompt = "you [" + ", ".join(
                            f"{agent}:{phase} {elapsed:.1f}s" for agent, phase, elapsed in active
                        ) + "]> "
                        agents_were_active = True
                    elif agents_were_active:
                        queued = session.pending_agent_messages()
                        suffix = f"; {queued} message(s) remain queued" if queued else ""
                        print(f"agents> idle — no model turns are running{suffix}.")
                        agents_were_active = False
                else:
                    result = worker.collect()
                    if result is not _NOT_READY:
                        if result is not None and result.status == "ok" and session.supervisor.journal.pending("agent"):
                            worker.request_turn()
                    phase, elapsed = worker.status()
                    if phase not in {"idle", "completed"}:
                        prompt = f"you [{phase} {elapsed:.1f}s]> "
                        agents_were_active = True
                    elif agents_were_active:
                        queued = len(session.supervisor.journal.pending("agent"))
                        suffix = f"; {queued} message(s) remain queued" if queued else ""
                        print(f"agents> idle — no model turns are running{suffix}.")
                        agents_were_active = False
            line = input(prompt).strip()
            if not line:
                continue
            if line == ":quit":
                return
            if line == ":help":
                _print_help()
                continue
            if line == ":python":
                _run_user_repl(session)
                continue
            if line == ":state":
                print(_format_state(session))
                continue
            if line == ":agents":
                _print_agents(session)
                continue
            if line == ":log":
                for message in session.conversation_log():
                    print(f"{message.created_at.isoformat()} {message.sender} -> {message.recipient}: {message.text}")
                continue
            if line == ":model-log":
                _print_model_log(session)
                continue
            if line == ":repl-log":
                _print_repl_log(session)
                continue
            if line == ":http-log":
                path = arguments.data_dir / "provider-http.jsonl"
                print(path.read_text(encoding="utf-8") if path.exists() else "No provider HTTP requests have been recorded.")
                continue
            if line == ":restart":
                print(session.restart())
                continue

            session.send(line)
            if arguments.demo:
                print(f"Demo agent processed {session.run_demo_turn()} message(s).")
            if model_driver is not None:
                try:
                    model_driver.validate_configuration()
                    if worker is not None and not worker.request_turn():
                        pass
                except OpenAIConfigurationError as error:
                    print(f"Model setup required: {error}")
    finally:
        if worker is not None:
            worker.close()
        session.close()


if __name__ == "__main__":
    main()
