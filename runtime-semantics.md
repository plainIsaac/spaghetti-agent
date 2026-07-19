# Runtime Semantics — Working Reference

This document records the design decisions reached during the initial product
exploration. It is a reference point for experiments and implementation; a
conclusion from an experiment may amend it, but should not silently replace it.

## Product posture

Agent REPL is a persistent, shared programmable environment. Chat is an easy
human input surface, not the canonical record of work. The canonical record is
live runtime state plus durable events and selected snapshots.

The product should be quiet by default. A user ordinarily sends a normal
message and leaves the agent to work. The agent should communicate intentionally
and concisely: when it needs a decision, reaches a meaningful result, or cannot
resolve an error. The user may inspect the program at any time, but should not
need to supervise it through notifications, lengthy status prose, or a required
task dashboard.

## REPLs and authority

- A user and an agent have distinct REPLs/scopes.
- Each REPL owns its ordinary local variables, imports, and execution queue.
- State crossing a REPL boundary is shared explicitly through a granted
  capability; there is no implicit global transcript or universal namespace.
- The agent normally acts with the user's authority. Capability discovery must
  match enforcement: unavailable things should not be presented as usable agent
  affordances.
- Any confirmation or destructive-action policy is user-owned configuration,
  rather than an artificial agent-versus-user security boundary.

## Messages

A normal user message is data, not source code and not an interrupt. Conceptually
it causes a small, atomic evaluation in the user REPL that appends a structured
message to a shared, agent-accessible inbox:

```python
orchestrator_agent.inbox.add(sender="user", text=user_message)
```

The actual UI input must be passed as a value, never interpolated into evaluated
source text.

An inbox is durable state. It records messages that have arrived even if the
agent is busy, stopped, or later restarted. An inbox-change event is separate
from the inbox itself:

- The inbox is the source of truth.
- An event is an optional notification that new inbox state exists.
- Agents opt into a delivery policy: polling, waiting at a yield point, or a
  registered handler.
- A handler is scheduled as a later agent execution unit. It never runs
  synchronously inside the user's inbox-append operation.

The initial handler API is `inbox.on_message(handler)`. The handler runs when
the supervisor later delivers the already-durable message into the agent kernel;
it therefore shares the kernel's normal serialized execution lane. Handler
failures do not kill the kernel and are published as high-priority presentable
`inbox_error` state.

Thus user input is free-form and reliable without forcefully interrupting the
agent or creating re-entrant execution.

## State and inspection

The runtime must not impose a fixed presentable-state ontology such as only
goals, tasks, and agents. The agent and user can work with general language
state: variables, functions, collections, modules, processes, caches, files,
and custom objects.

The UI is an inspection surface over that state. It may provide first-class
inspectors for common runtime concepts, but must also support bounded, lazy,
adapter-based inspection of arbitrary values. Inspection must not accidentally
invoke expensive or side-effectful properties, representations, or iterators.

The supervisor also owns an explicit observable-state registry. Agents and users
can publish selected, presentable values to it; this is a declaration that state
is intended for inspection, not a claim that all REPL locals are UI state. The
first registry representation is durable JSON plus presenter metadata. Richer
inspectors and adapters can be added later without making arbitrary object
serialization or `repr` part of the runtime contract.

Each published value also declares whether it is shown by default, an optional
human label, and a priority. The default presentation renders only that selected
subset, ordered by priority. Other published values remain available to an
appropriate inspector without turning the default UI into a state dump.

### Intended user interface

The UI should render a compact, live subset of presentable state by default.
The user should not have to request snapshots or manage the agent through CLI
commands in order to know the meaningful current state.

Normal user input remains free-form natural-language messaging to the agent.
For deeper inspection or direct intervention, the user enters actual Python in
their own REPL, using the capabilities granted there. This is distinct from
normal messaging: it is not a collection of special colon commands. The current
single-agent CLI is a temporary developer harness and must not define the final
interaction model.

The first user-REPL capability surface is deliberately small:

```python
presentable.list()
presentable["progress"]
agent.inbox.pending()
agent.send("Use Postgres, not SQLite.")
```

The user REPL has its own persistent namespace. These objects are supervisor-
mediated capabilities, rather than direct references to the agent's Python
objects.

The agent receives the registry as an explicit capability, not as a direct
reference to supervisor internals. Its initial API is intentionally small:
`observable.publish(name, value, presenter="json")`. Likewise, the agent can
acknowledge durable inbox entries with `inbox.ack(message_id)` and communicate
intentionally through `user.inbox.add(text)`. These are supervisor-mediated
requests with ownership checked by the supervisor.

High-level collaboration helpers and a granular runtime API are complementary.
The latter must expose the machinery needed for agents to build their own
caches, scheduling, memory, messaging, synchronization, and workflows.

## Execution units

An execution unit is a unit that evaluates: a submitted REPL expression or
block, a scheduled message handler, an async task, or another callback. It is
not the value being evaluated.

The initial working model is:

- Evaluations are serialized within one REPL.
- Separate REPLs may execute concurrently.
- A supervisor owns REPL lifecycle, persistence, capability grants, inspection,
  and scheduling coordination.
- Shared capabilities must define their own concurrency semantics; durable
  inbox append is atomic.
- A user message append and a later agent handler are distinct execution units.

## Durability, cancellation, and imports

- Durable state is explicit: an event journal and snapshots for supported
  values. The system must report clearly what cannot survive a restart.
- Processes, sockets, generators, native extension state, and similar resources
  are normally ephemeral; they must be recreated or represented by a durable
  handle after restart.
- A kernel restart rehydrates unacknowledged inbox messages and explicitly
  published observable values. Its ordinary Python namespace, imports, local
  variables, and in-memory resources are ephemeral. The supervisor must report
  this distinction rather than imply that an interpreter heap was restored.
- Inbox handling is at-least-once until acknowledgement: an unacknowledged
  message is eligible for re-delivery after a restart. Handlers should therefore
  make acknowledgement part of their deliberate completion logic.
- Work that may not yield must have a cancellable isolation boundary. Python
  threads alone are insufficient for reliable termination of arbitrary code;
  process-level containment is the baseline hypothesis to test.
- The supervisor publishes a compact `runtime` presentable value for each agent
  kernel. It reports evaluation health (`idle`, `running`, `completed`,
  `failed`, `unresponsive`, or `terminated`) without requiring the user to
  inspect raw logs. A timed-out kernel can be force-terminated and restarted;
  the normal durable recovery contract then applies.
- The supervisor owns child-process lifecycle so cancellation does not leave
  orphaned commands or services.
- Imports are executable, stateful work. REPLs need isolated/tracked module
  namespaces and explicit reload or recreation behavior.

## Questions to validate experimentally

The first prototype should test, rather than assume, these semantics:

1. An agent performs long-running work while the user appends messages.
2. Messages persist and are handled at controlled yield points.
3. Inspection remains safe and useful for arbitrary state.
4. A runaway evaluation and its children can be stopped cleanly.
5. Restart restores selected state and accurately labels ephemeral state.
6. Dynamic imports and reloads behave predictably in isolated REPLs.

## Non-goals for the first runtime spike

- A mandatory task/goal graph or notification-heavy management UI.
- Treating a chat transcript as the system of record.
- Forcing agents into only a high-level orchestration API.
- Solving every persistence case before proving the basic lifecycle.
