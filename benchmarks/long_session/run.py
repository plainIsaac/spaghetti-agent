"""Run one arm of the long-session benchmark.

    python -m benchmarks.long_session.run --strategy rebuild --model opencode-go/minimax-m3.1-flash-free

Writes `results/<run>/calls.jsonl` (per call and per compaction) and
`results/<run>/run.json` (replies and grades), keeping the workspace beside them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import argparse
import json
import os
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))

from agent_repl.opencode_driver import DEFAULT_OPENCODE_MODEL, OpencodeClient  # noqa: E402

from .harness import PROMPTS, Agent  # noqa: E402
from .scenario import MESSAGES, PAUSES_BEFORE, build  # noqa: E402

RESULTS = HERE / "results"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--strategy", choices=sorted(PROMPTS), required=True)
    parser.add_argument("--model", default=DEFAULT_OPENCODE_MODEL)
    parser.add_argument("--budget", type=int, default=24_000, help="Context budget in tokens that triggers compaction")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--steps", type=int, default=MESSAGES)
    parser.add_argument("--label", default="")
    arguments = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS / f"{stamp}-{arguments.strategy}{'-' + arguments.label if arguments.label else ''}"
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    steps = build(workspace, arguments.seed)[: arguments.steps]
    calls_path = run_dir / "calls.jsonl"

    def record(entry: dict) -> None:
        with calls_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), **entry}, default=str) + "\n")

    os.chdir(workspace)
    agent = Agent(arguments.strategy, arguments.model, OpencodeClient(), workspace, record, arguments.budget)
    outcomes = []
    for step in steps:
        try:
            replies, error = agent.handle(step.number, step.text, step.number in PAUSES_BEFORE), None
        except Exception as failure:
            replies, error = [], f"{type(failure).__name__}: {failure}"
        joined = "\n".join(replies)
        try:
            passed = None if step.grade is None else bool(step.grade(joined, workspace))
        except Exception as failure:
            passed, error = False, error or f"grader: {type(failure).__name__}: {failure}"
        outcomes.append({"step": step.number, "category": step.category, "text": step.text, "replies": replies, "passed": passed, "error": error})
        print(f"[{arguments.strategy}] #{step.number} {step.category:<10} passed={passed} compactions={agent.compactions} "
              f"reply={joined[:100]!r}{' error=' + error if error else ''}", flush=True)
        (run_dir / "run.json").write_text(json.dumps({
            "strategy": arguments.strategy, "model": agent.model, "budget": arguments.budget, "seed": arguments.seed,
            "compactions": agent.compactions, "notes": agent.history.notes, "outcomes": outcomes,
        }, indent=2, default=str), encoding="utf-8")
    graded = [outcome for outcome in outcomes if outcome["passed"] is not None]
    print(f"{arguments.strategy}: {sum(o['passed'] for o in graded)}/{len(graded)} → {run_dir}")


if __name__ == "__main__":
    main()
