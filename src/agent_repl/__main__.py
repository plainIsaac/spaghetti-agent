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
from .session import SingleAgentSession


def _format_state(session: SingleAgentSession) -> str:
    values = [value for value in session.observe() if value.show_by_default]
    if not values:
        return "No observable state has been published."
    return "\n".join(f"{value.label or value.name}: {value.value}" for value in values)


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
    print("Agent REPL. Enter a message. Use :python for user Python, :restart, or :quit.")
    seen_message_ids: set[int] = set()
    try:
        while True:
            _render_default_presentation(session, seen_message_ids)
            line = input("you> ").strip()
            if not line:
                continue
            if line == ":quit":
                return
            if line == ":python":
                _run_user_repl(session)
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
                    result = session.run_openai_turn(model_driver)
                    print(f"Model agent evaluation: {result.status if result else 'no pending message'}")
                except OpenAIConfigurationError as error:
                    print(f"Model setup required: {error}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
