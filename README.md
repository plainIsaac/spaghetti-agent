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

Restart is explicit: `supervisor.restart_agent_kernel("agent")` returns both a
fresh kernel and a report of rehydrated inbox and observable-state entries.
Ordinary Python variables and imports are intentionally not restored. An inbox
message remains eligible for delivery until `inbox.ack(id)` succeeds, giving the
current spike at-least-once delivery semantics.

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
Use `:state`, `:messages`, `:restart`, `:eval <source>`, and `:quit` to inspect
the session. A real model adapter belongs at the `evaluate` boundary; it should
not alter the runtime's message or persistence semantics.

These commands are developer-harness controls only. The intended UI will render
presentable state continuously, reserve ordinary input for natural-language
messages, and provide a separate Python user REPL for specific inspection and
intervention.

Run the tests from this directory:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

The process experiment deliberately has no durable Python heap. That is not a
missing implementation detail: it makes the remaining design question explicit.
The next spike must define which values receive a durable representation and how
an agent REPL reconstructs them after restart.
