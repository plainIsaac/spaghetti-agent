# Context-economics benchmark

Does Spaghetti Agent's pull-based context (bounded activations, state in the
runtime) beat a conventional transcript loop once prompt caching is priced in?
Results and the verdict: [`docs/experiments/context-economics.md`](../../docs/experiments/context-economics.md).

## What it runs

One scripted 12-message session (`scenario.py`) against a generated workspace:
CSV questions, reading a long incident report, two code changes graded by
pytest, a fact to remember, a convention change to keep, and a long pause
before recall (message 9). Ten messages are graded automatically.

Arms (`arms.py`), all on the same model through `opencode run`:

| Arm | Context strategy |
| --- | --- |
| `transcript` | One growing conversation; the model writes Python, sees output, continues, calls `reply()` |
| `runtime` | Spaghetti Agent as shipped: one blind program per activation, repairs on error |
| `runtime-continue` | As shipped, but re-triggered while the user message is still unanswered |
| `runtime-observe` | Re-triggered, and the next activation carries the previous program's output (`observe_program_output`) |

## Run

```text
python -m pip install --editable ".[dev]"
python -m benchmarks.context_economics.run --arm transcript --model opencode-go/minimax-m3.1-flash-free --label r1
python -m benchmarks.context_economics.run --arm runtime-observe --model opencode-go/minimax-m3.1-flash-free --label r1
python -m benchmarks.context_economics.report > summary.json
```

Requires the `opencode` CLI logged in to the chosen provider. Runs write to
`results/` (git-ignored): `calls.jsonl` has per-call token usage, `run.json`
has replies and grades, and the generated workspace is kept for inspection.

## Cost model

`report.py` prices tokens in units of one uncached input token and applies the
same idealized prefix cache to every arm (see its docstring). Measured provider
cache hits are recorded but not used, because opencode keys caches per session.
