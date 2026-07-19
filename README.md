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

Run the tests from this directory:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

The process experiment deliberately has no durable Python heap. That is not a
missing implementation detail: it makes the remaining design question explicit.
The next spike must define which values receive a durable representation and how
an agent REPL reconstructs them after restart.
