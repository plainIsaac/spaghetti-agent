"""Summarize long-session runs: recall, convention keeping, and cache-priced cost.

    python -m benchmarks.long_session.report [results/<run> ...]

Each prompt is built by the harness, so the cached prefix is known exactly: the
system prompt plus the characters shared with the previous prompt. Cached
tokens are read; the rest is written to the cache (it will be read next call).
Calls after a long pause are cold (`with_pauses`); `without_pauses` prices the
same calls as if the session had been continuous. Costs are in units of one
uncached input token, using the price sheets from the context-economics report.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import json
import statistics
import sys

from benchmarks.context_economics.report import PRICES

RESULTS = Path(__file__).resolve().parent / "results"


def load(run_dir: Path) -> dict:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    entries = [json.loads(line) for line in (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines() if line]
    run["calls"] = [entry for entry in entries if entry.get("usage")]
    run["compaction_events"] = [entry for entry in entries if entry.get("event") == "compaction"]
    run["name"] = run_dir.name
    return run


def call_cost(call: dict, sheet: str, honor_pauses: bool, first: bool) -> float:
    prices, usage = PRICES[sheet], call["usage"]
    prompt = usage["input_tokens"]
    total_chars = call["system_chars"] + call["prompt_chars"]
    cold = first or (honor_pauses and call["cold"])
    read = 0 if cold else round(prompt * (call["system_chars"] + call["shared_prefix_chars"]) / max(total_chars, 1))
    return prices["read"] * read + prices["write"] * (prompt - read) + prices["output"] * usage["output_tokens"]


def run_cost(run: dict, sheet: str, honor_pauses: bool) -> float:
    return sum(call_cost(call, sheet, honor_pauses, index == 0) for index, call in enumerate(run["calls"]))


def summarize(runs: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        groups[run["strategy"]].append(run)
    summary = {}
    for strategy, group in sorted(groups.items()):
        def mean(values):
            values = list(values)
            return statistics.mean(values) if values else 0.0

        def score(run, predicate=lambda outcome: True):
            graded = [o for o in run["outcomes"] if o["passed"] is not None and predicate(o)]
            return sum(o["passed"] for o in graded), len(graded)

        categories = sorted({o["category"] for run in group for o in run["outcomes"] if o["passed"] is not None})
        summary[strategy] = {
            "runs": len(group),
            "messages": mean(len(run["outcomes"]) for run in group),
            "score": [score(run) for run in group],
            "by_category": {category: [score(run, lambda o, c=category: o["category"] == c) for run in group] for category in categories},
            "late_messages": [score(run, lambda o: o["step"] >= 100) for run in group],
            "failed_recall": [[o["step"] for o in run["outcomes"] if o["category"] == "recall" and o["passed"] is False] for run in group],
            "compactions": mean(len(run["compaction_events"]) for run in group),
            "calls": mean(len(run["calls"]) for run in group),
            "summary_calls": mean(sum(1 for c in run["calls"] if c["kind"] == "summary") for run in group),
            "prompt_tokens": mean(sum(c["usage"]["input_tokens"] for c in run["calls"]) for run in group),
            "output_tokens": mean(sum(c["usage"]["output_tokens"] for c in run["calls"]) for run in group),
            "reasoning_per_call": mean(sum(c["usage"]["reasoning_tokens"] for c in run["calls"]) / max(len(run["calls"]), 1) for run in group),
            "peak_prompt": mean(max(c["usage"]["input_tokens"] for c in run["calls"]) for run in group),
            "notes": mean(len(run.get("notes", [])) for run in group),
            "minutes": mean(sum(c["seconds"] for c in run["calls"]) / 60 for run in group),
            "cost": {f"{sheet}/{'with' if pauses else 'without'}_pauses": mean(run_cost(run, sheet, pauses) for run in group)
                     for sheet in PRICES for pauses in (True, False)},
            "resume_cost": {sheet: mean(sum(call_cost(c, sheet, True, False) for c in run["calls"] if c["cold"])
                                        for run in group) for sheet in PRICES},
        }
    return summary


def main() -> None:
    directories = [Path(arg) for arg in sys.argv[1:]] or sorted(
        path for path in RESULTS.iterdir() if (path / "run.json").exists() and not path.name.endswith("smoke"))
    runs = [load(path) for path in directories]
    print(json.dumps({"runs": [run["name"] for run in runs], "summary": summarize(runs)}, indent=2))


if __name__ == "__main__":
    main()
