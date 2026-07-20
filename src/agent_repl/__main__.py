"""Minimal interactive entry point for the single-agent runtime spike."""

from __future__ import annotations

import argparse
from pathlib import Path

from .openai_driver import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    OpenAIAgentDriver,
    OpenAIConfigurationError,
    OpenRouterAgentDriver,
)
from .session import ModelTurnWorker, SingleAgentSession, _NOT_READY


def _format_state(session: SingleAgentSession) -> str:
    values = [value for value in session.observe() if value.show_by_default]
    if not values:
        return "No observable state has been published."
    return "\n".join(f"{value.label or value.name}: {value.value}" for value in values)


def _print_model_log(session: SingleAgentSession) -> None:
    entries = session.model_program_log()
    if not entries:
        print("No model programs have been recorded.")
        return
    print(entries[-1]["raw_output"])


def _print_help() -> None:
    print("Type a normal message for the agent. :state shows presentable state; :python opens inspection; :log and :model-log show debug logs.")


def _render_default_presentation(session: SingleAgentSession, seen_message_ids: set[int]) -> None:
    print(_format_state(session))
    for message in session.user_messages():
        if message.id not in seen_message_ids:
            print(f"agent> {message.text}")
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
    _load_project_environment()
    parser = argparse.ArgumentParser(description="Agent REPL single-agent session")
    parser.add_argument("--data-dir", type=Path, default=Path(".agent-repl"), help="Directory for durable session state")
    turn_mode = parser.add_mutually_exclusive_group()
    turn_mode.add_argument("--demo", action="store_true", help="Run a deterministic demo agent turn after each normal message")
    turn_mode.add_argument("--openai", action="store_true", help="Run an OpenAI-planned agent turn after each normal message")
    turn_mode.add_argument("--openrouter", action="store_true", help="Run an OpenRouter-planned agent turn after each normal message")
    parser.add_argument("--model", help="Provider model override")
    parser.add_argument("--request-timeout", type=float, default=30.0, help="Provider request timeout in seconds")
    arguments = parser.parse_args()
    arguments.data_dir.mkdir(parents=True, exist_ok=True)
    session = SingleAgentSession.open(
        str(arguments.data_dir / "inbox.sqlite"),
        str(arguments.data_dir / "observable-state.sqlite"),
    )
    model_driver = None
    if arguments.openai:
        model_driver = OpenAIAgentDriver(arguments.model or DEFAULT_OPENAI_MODEL, request_timeout=arguments.request_timeout)
    if arguments.openrouter:
        model_driver = OpenRouterAgentDriver(arguments.model or DEFAULT_OPENROUTER_MODEL, request_timeout=arguments.request_timeout)
    print("Agent REPL. Enter a message. Use :help for controls.")
    seen_message_ids: set[int] = set()
    worker = ModelTurnWorker(session, model_driver) if model_driver is not None else None
    try:
        while True:
            _render_default_presentation(session, seen_message_ids)
            if worker is not None:
                result = worker.collect()
                if result is not _NOT_READY:
                    print(f"Model agent evaluation: {result.status if result else 'no pending message'}")
                    if result is not None and result.status == "ok" and session.supervisor.journal.pending("agent"):
                        worker.request_turn()
                phase, elapsed = worker.status()
                if phase not in {"idle", "completed"}:
                    print(f"Model: {phase} ({elapsed:.1f}s); you can keep sending messages.")
            line = input("you> ").strip()
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
            if line == ":log":
                for message in session.conversation_log():
                    print(f"{message.created_at.isoformat()} {message.sender} -> {message.recipient}: {message.text}")
                continue
            if line == ":model-log":
                _print_model_log(session)
                continue
            if line == ":restart":
                print(session.restart())
                continue

            session.send(line)
            print("Queued for the agent.")
            if arguments.demo:
                print(f"Demo agent processed {session.run_demo_turn()} message(s).")
            if model_driver is not None:
                try:
                    model_driver.validate_configuration()
                    if worker is not None and not worker.request_turn():
                        print("Model is already working; this message remains queued.")
                    else:
                        print("Model turn started; you can keep sending messages.")
                except OpenAIConfigurationError as error:
                    print(f"Model setup required: {error}")
    finally:
        if worker is not None:
            worker.close()
        session.close()


if __name__ == "__main__":
    main()
