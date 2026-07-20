# Agent REPL runtime spike

This is the first executable experiment for the semantics in
[`runtime-semantics.md`](runtime-semantics.md). It is intentionally not a
general agent framework or a complete REPL.

It currently proves four narrow claims:

- a user message is durably appended before any agent work is scheduled;
- inbox delivery is opt-in and runs later in the agent's serialized execution
  lane;
- execution units are serialized per logical REPL;
- arbitrary non-yielding code can be terminated at a process boundary, and
  dynamically imported code works in that isolated boundary.

The second spike adds a persistent process-backed agent kernel and a supervisor
observable-state registry. Kernel evaluations retain ordinary Python variables
and imports between evaluations; user messages are hydrated from the durable
inbox and delivered into the kernel's `inbox` value. The registry stores only
explicitly published JSON-presentable values, so it is an inspection contract
rather than a dump of every local variable.

The kernel is granted three intuitive supervisor capabilities:

```python
observable.publish("progress", {"percent": 30})
inbox.ack(message_id)
user.inbox.add("I need a decision about the database.")
```

They are RPC requests, not shared object references. The supervisor checks the
agent's ownership when acknowledging a message and remains the durable source
of truth.

Published state can also describe its default presentation:

```python
observable.publish(
    "progress", {"percent": 30}, label="Current work", priority=10
)
observable.publish("debug", {"trace": True}, show_by_default=False)
```

Only values marked `show_by_default` belong in the quiet default UI; the rest
remain available to explicit inspection.

An agent may also opt into inbox delivery with normal Python:

```python
def handle(message):
    observable.publish("latest_input", {"text": message["text"]})
    inbox.ack(message["id"])

inbox.on_message(handle)
```

The handler runs later in the agent kernel, after the user message has already
been durably appended; it is not an interruption of the user's input action.

Restart is explicit: `supervisor.restart_agent_kernel("agent")` returns both a
fresh kernel and a report of rehydrated inbox and observable-state entries.
Ordinary Python variables and imports are intentionally not restored. An inbox
message remains eligible for delivery until `inbox.ack(id)` succeeds, giving the
current spike at-least-once delivery semantics.

The supervisor also publishes a compact `runtime` presentable value with the
kernel's evaluation health. If an evaluation stops yielding, the supervisor can
use `recover_agent_kernel("agent")` to terminate and replace the kernel; durable
inbox and observable state are then rehydrated as usual.

## Single-agent session

The current vertical slice is deliberately one agent. It accepts ordinary user
messages, exposes only explicitly published observable state, retains concise
agent messages for the user, and can restart with the documented recovery
contract. Run it with:

```powershell
$env:PYTHONPATH = "src"
python -m agent_repl --demo
```

Type normal text to queue it for the agent. `--demo` runs a deterministic agent
turn after each message so the full flow is visible without connecting an LLM.
The compact default presentation renders before each prompt. Use `:python` to
enter the user Python REPL, `:restart` to exercise recovery, or `:quit` to exit.
A real model adapter belongs at the `evaluate` boundary; it should not alter the
runtime's message or persistence semantics.

These commands are developer-harness controls only. The intended UI will render
presentable state continuously, reserve ordinary input for natural-language
messages, and provide a separate Python user REPL for specific inspection and
intervention.

The runtime now exposes that user-REPL distinction through
`session.user_evaluate(...)`. Its first capabilities are
`presentable.list()`, `presentable["name"]`, `agent.inbox.pending()`, and
`agent.send(text)`.

Run the tests from this directory:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

Install a local development entry point with:

```powershell
python -m pip install -e .
agent-repl --demo
```

`python -m agent_repl --demo` remains equivalent when working directly from the
source tree.

## OpenAI agent driver

Install the optional SDK and set an API key before using the live driver:

```powershell
python -m pip install -e ".[openai]"
# Add OPENAI_API_KEY=... to .env
agent-repl --openai
```

The OpenAI integration uses the Responses API. Its default is the
cost-sensitive `gpt-5.6-luna`, which can be replaced with `--model`. Each turn sends the
current durable agent inbox and explicitly published state; it does not replay a
chat transcript. The model returns Python source for the persistent agent REPL,
where it can inspect state, acknowledge messages, publish presentation state,
and intentionally message the user.

## OpenRouter smoke tests

OpenRouter is available through the same OpenAI-compatible adapter:

```powershell
# Add OPENROUTER_API_KEY=... to .env
agent-repl --openrouter
```

It defaults to `openrouter/free`, which selects from OpenRouter's available free
models. Use it for cheap, non-deterministic smoke tests only; provider selection,
availability, and rate limits can vary. Use `--model provider/model:free` to
pin a specific free variant when reproducibility matters more than breadth.
Provider requests default to a 30-second timeout with no automatic retry; adjust
the limit deliberately with `--request-timeout` when a model warrants it.

The process experiment deliberately has no durable Python heap. That is not a
missing implementation detail: it makes the remaining design question explicit.
The next spike must define which values receive a durable representation and how
an agent REPL reconstructs them after restart.
