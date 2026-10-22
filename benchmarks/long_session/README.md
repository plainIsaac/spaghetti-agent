# Long-session benchmark

Tracks [#5](https://github.com/plainIsaac/spaghetti-agent/issues/5). Can durable
runtime state replace model-written summaries as the way a transcript agent
survives an unbounded session?

## Scenario

`scenario.py` builds a 150-message session over a generated workspace:

- **Planted early:** facts (codename, on-call lead, staging host, budget code),
  a constraint (never edit `notes/`), and two conventions (money in integer
  cents from #8, names as "Lastname, Firstname" from #40).
- **Throughout:** code edits (#12, #30, #55, #80, #105), three long document
  reads (#20, #70, #95), and graded data questions as filler.
- **Pauses:** three long, cache-expiring breaks (before #50, #100, #140).
- **Late:** recall checks (#112–#144), a request that would violate the
  constraint (#136), and a hidden test suite over all code changes (#150).

## Arms

All arms are the same code-act loop with an ordinary, cache-friendly
transcript and a persistent Python namespace. They differ only in what happens
when the transcript exceeds the token budget (default 24k), or on resume:

| Strategy | Compaction |
| --- | --- |
| `summary` | The model writes a summary of the conversation (what current harnesses do) |
| `rebuild` | The runtime rebuilds a small context from state with no model call: notes, user messages, changed files, REPL variables. `history.*` exposes full turns and diffs |
| `rebuild-notes` | `rebuild` at low reasoning effort, told to save durable facts with `note()` |

Rebuild arms also compact on resume after a pause when the cold prompt would
exceed 8k tokens.

## Run

```text
python -m benchmarks.long_session.run --strategy rebuild --model opencode-go/minimax-m3.1-flash-free --label r1
python -m benchmarks.long_session.report
```

`report.py` prices each call from its exact cached prefix (system prompt plus
characters shared with the previous prompt), with and without the pauses.
Results go to `results/` (git-ignored).
