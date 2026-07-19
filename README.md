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

Run the tests from this directory:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

The process experiment deliberately has no durable Python heap. That is not a
missing implementation detail: it makes the remaining design question explicit.
The next spike must define which values receive a durable representation and how
an agent REPL reconstructs them after restart.
