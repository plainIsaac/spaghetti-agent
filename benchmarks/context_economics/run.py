"""Run one arm of the context-economics benchmark.

    python -m benchmarks.context_economics.run --arm runtime --model openai/gpt-6-luna

Each run writes `results/<run>/calls.jsonl` (one line per model call) and
`results/<run>/run.json` (replies and grades) and leaves its workspace beside them.
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

from .arms import RuntimeArm, TranscriptArm  # noqa: E402
from .scenario import build_workspace, steps  # noqa: E402

RESULTS = HERE / "results"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arm", choices=["runtime", "runtime-continue", "runtime-observe", "transcript"], required=True)
    parser.add_argument("--model", default=DEFAULT_OPENCODE_MODEL)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--steps", type=int, default=12, help="Run only the first N messages (for smoke tests)")
    parser.add_argument("--label", default="", help="Suffix for the run directory")
    arguments = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RESULTS / f"{stamp}-{arguments.arm}{'-' + arguments.label if arguments.label else ''}"
    workspace = run_dir / "workspace"
    workspace.mkdir(parents=True)
    expected = build_workspace(workspace, arguments.seed)
    client = OpencodeClient()
    calls_path = run_dir / "calls.jsonl"

    def record(entry: dict) -> None:
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), **entry}
        with calls_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")

    os.chdir(workspace)  # both arms treat the current directory as the project
    if arguments.arm == "transcript":
        arm = TranscriptArm(arguments.model, client, record)
    else:
        arm = RuntimeArm(arguments.model, client, record, run_dir / "runtime",
                         continue_until_answered=arguments.arm != "runtime", observe_program_output=arguments.arm == "runtime-observe")
    outcomes = []
    try:
        for step in steps(expected)[: arguments.steps]:
            print(f"[{arm.name}] step {step.number}: {step.text[:70]}", flush=True)
            try:
                replies = arm.handle(step.number, step.text)
                error = None
            except Exception as failure:
                replies, error = [], f"{type(failure).__name__}: {failure}"
            joined = "\n".join(replies)
            passed = None if step.grade is None else bool(step.grade(joined, workspace))
            outcomes.append({"step": step.number, "replies": replies, "passed": passed, "error": error})
            print(f"    passed={passed} reply={joined[:160]!r}{' error=' + error if error else ''}", flush=True)
    finally:
        arm.close()
    summary = {
        "arm": arguments.arm, "model": arguments.model, "seed": arguments.seed, "expected": expected,
        "outcomes": outcomes,
        "score": sum(1 for outcome in outcomes if outcome["passed"]),
        "graded": sum(1 for outcome in outcomes if outcome["passed"] is not None),
    }
    (run_dir / "run.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"{arguments.arm}: {summary['score']}/{summary['graded']} → {run_dir}")


if __name__ == "__main__":
    main()
