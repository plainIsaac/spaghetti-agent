"""Summarize benchmark runs under explicit prompt-caching cost models.

    python -m benchmarks.context_economics.report [results/<run> ...]

Costs are in units of one uncached input token, so they do not depend on a
provider's current price list. Measured provider cache hits are reported but not
used for cost: opencode keys prompt caches per session, which would penalize the
runtime's fresh-session-per-activation design for a reason unrelated to it.
Instead both arms get the same idealized prefix cache:

* transcript: each call can read the previous call's prompt and visible output
  as a cached prefix and writes the new tail (it will be read next call);
* runtime: each call can read the static system prompt; the activation tail is
  unique per call, so it is sent as plain input rather than written to cache.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import json
import statistics
import sys

from .scenario import PAUSE_BEFORE

RESULTS = Path(__file__).resolve().parent / "results"
PRICES = {
    # relative to one uncached input token
    "anthropic": {"write": 1.25, "read": 0.1, "output": 5.0},
    "openai": {"write": 1.0, "read": 0.1, "output": 8.0},
}


def load(run_dir: Path) -> dict:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    calls = [json.loads(line) for line in (run_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines() if line]
    run["calls"] = [call for call in calls if call.get("usage")]
    run["runtime_errors"] = [call for call in calls if call.get("runtime_error")]
    run["name"] = run_dir.name
    return run


def static_prefix(run: dict) -> int:
    """Smallest prompt seen: the system prompt plus a minimal message."""
    return min(call["usage"]["input_tokens"] for call in run["calls"])


def cost(run: dict, sheet: str, mode: str) -> float:
    """mode: warm (cache never expires), resume (cache expires before PAUSE_BEFORE), none."""
    prices = PRICES[sheet]
    prefix = static_prefix(run)
    transcript = run["arm"] == "transcript"
    total, previous = 0.0, None
    for call in run["calls"]:
        usage = call["usage"]
        prompt, output = usage["input_tokens"], usage["output_tokens"]
        visible = output - usage.get("reasoning_tokens", 0)
        expired = mode == "resume" and call["step"] == PAUSE_BEFORE and call["call"] == 0
        if mode == "none":
            total += prompt + prices["output"] * output
            continue
        if previous is None or expired:
            read = 0
        elif transcript:
            read = min(prompt, previous)
        else:
            read = min(prompt, prefix)
        tail = prompt - read
        if transcript:
            total += prices["read"] * read + prices["write"] * tail
        else:
            written = prefix if read == 0 else 0  # (re)write the static prefix once
            total += prices["read"] * read + prices["write"] * written + (tail - written)
        total += prices["output"] * output
        previous = prompt + visible
    return total


def summarize(runs: list[dict]) -> dict:
    by_arm: dict[str, list[dict]] = defaultdict(list)
    for run in runs:
        arm = run["arm"]
        by_arm[arm].append(run)
    summary = {}
    for arm, group in sorted(by_arm.items()):
        def mean(values):
            values = list(values)
            return statistics.mean(values) if values else 0.0
        summary[arm] = {
            "runs": len(group),
            "score": mean(run["score"] for run in group),
            "graded": group[0]["graded"],
            "passed_steps": [sorted(o["step"] for o in run["outcomes"] if o["passed"]) for run in group],
            "calls": mean(len(run["calls"]) for run in group),
            "prompt_tokens": mean(sum(c["usage"]["input_tokens"] for c in run["calls"]) for run in group),
            "output_tokens": mean(sum(c["usage"]["output_tokens"] for c in run["calls"]) for run in group),
            "peak_prompt": mean(max(c["usage"]["input_tokens"] for c in run["calls"]) for run in group),
            "static_prefix": mean(static_prefix(run) for run in group),
            "measured_cache_read": mean(sum(c["usage"]["cache_read_tokens"] for c in run["calls"]) for run in group),
            "runtime_errors": mean(len(run["runtime_errors"]) for run in group),
            "seconds": mean(sum(c.get("seconds", 0) for c in run["calls"]) for run in group),
            "cost": {f"{sheet}/{mode}": mean(cost(run, sheet, mode) for run in group)
                     for sheet in PRICES for mode in ("warm", "resume", "none")},
            "reasoning_tokens": mean(sum(c["usage"].get("reasoning_tokens", 0) for c in run["calls"]) for run in group),
            "output_cost_share": {sheet: mean(PRICES[sheet]["output"] * sum(c["usage"]["output_tokens"] for c in run["calls"]) / cost(run, sheet, "warm")
                                              for run in group) for sheet in PRICES},
            "resume_penalty": {sheet: mean(cost(run, sheet, "resume") - cost(run, sheet, "warm") for run in group) for sheet in PRICES},
            "prompt_by_step": {step: mean(max((c["usage"]["input_tokens"] for c in run["calls"] if c["step"] == step), default=0) for run in group)
                               for step in range(1, 13)},
        }
    return summary


def projection(summary: dict, messages: list[int], sheet: str = "anthropic") -> dict:
    """Extrapolate per-message cost for long sessions from the measured shape.

    Transcript prompt grows linearly by the measured per-message growth; the
    runtime prompt stays at its measured average. Compaction is not modelled,
    so transcript figures beyond a context window are a lower bound on work
    that would otherwise be summarized away.
    """
    prices = PRICES[sheet]
    result = {}
    t, r = summary.get("transcript"), summary.get("runtime-observe") or summary.get("runtime")
    if not t or not r:
        return result
    steps = sorted(t["prompt_by_step"])
    growth = (t["prompt_by_step"][steps[-1]] - t["prompt_by_step"][steps[0]]) / (steps[-1] - steps[0])
    t_calls = t["calls"] / 12
    t_out = t["output_tokens"] / t["calls"]
    r_calls = r["calls"] / 12
    r_prompt = r["prompt_tokens"] / r["calls"]
    r_out = r["output_tokens"] / r["calls"]
    for n in messages:
        prompt = t["static_prefix"] + growth * n
        # warm: each call reads the history and writes ~growth/calls new tokens
        t_warm = t_calls * (prices["read"] * prompt + prices["output"] * t_out) + prices["write"] * growth
        t_cold = prices["write"] * prompt + (t_calls - 1) * prices["read"] * prompt + t_calls * prices["output"] * t_out + prices["write"] * growth
        r_warm = r_calls * (prices["read"] * r["static_prefix"] + (r_prompt - r["static_prefix"]) + prices["output"] * r_out)
        r_cold = r_warm + (prices["write"] - prices["read"]) * r["static_prefix"]
        result[n] = {"transcript_prompt": prompt, "transcript_warm": t_warm, "transcript_cold": t_cold,
                     "runtime_warm": r_warm, "runtime_cold": r_cold}
    return {"growth_per_message": growth, "per_message": result}


def main() -> None:
    directories = [Path(arg) for arg in sys.argv[1:]] or sorted(path for path in RESULTS.iterdir() if (path / "run.json").exists())
    runs = [load(path) for path in directories]
    summary = summarize(runs)
    report = {"runs": [run["name"] for run in runs], "summary": summary,
              "projection": projection(summary, [12, 50, 100, 200, 400])}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
