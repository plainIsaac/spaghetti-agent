"""Minimal interactive entry point for the single-agent runtime spike."""

from __future__ import annotations

import argparse
from pathlib import Path

from .session import SingleAgentSession


def _format_state(session: SingleAgentSession) -> str:
    values = [value for value in session.observe() if value.show_by_default]
    if not values:
        return "No observable state has been published."
    return "\n".join(f"{value.label or value.name}: {value.value}" for value in values)


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent REPL single-agent session")
    parser.add_argument("--data-dir", type=Path, default=Path(".agent-repl"), help="Directory for durable session state")
    parser.add_argument("--demo", action="store_true", help="Run a deterministic demo agent turn after each normal message")
    arguments = parser.parse_args()
    arguments.data_dir.mkdir(parents=True, exist_ok=True)
    session = SingleAgentSession.open(
        str(arguments.data_dir / "inbox.sqlite"),
        str(arguments.data_dir / "observable-state.sqlite"),
    )
    print("Agent REPL. Enter a message, or use :state, :messages, :restart, :eval <source>, :quit.")
    try:
        while True:
            line = input("you> ").strip()
            if not line:
                continue
            if line == ":quit":
                return
            if line == ":state":
                print(_format_state(session))
                continue
            if line == ":messages":
                messages = session.user_messages()
                print("\n".join(message.text for message in messages) or "No agent messages.")
                continue
            if line == ":restart":
                print(session.restart())
                continue
            if line.startswith(":eval "):
                print(session.evaluate(line.removeprefix(":eval ")))
                continue

            session.send(line)
            print("Queued for the agent.")
            if arguments.demo:
                print(f"Demo agent processed {session.run_demo_turn()} message(s).")
    finally:
        session.close()


if __name__ == "__main__":
    main()
