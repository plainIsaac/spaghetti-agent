# Spaghetti Agent
Spaghetti Agent turns the agent–user conversation into a shared programmable runtime, replacing chat history with live state.

## Inspiration
Models have gotten good at coding, making them do more then they used to. Developers entrust models to execute commands freely, without sandboxing. Coding harnesses are tasked with more and more complex work, requiring them to come up with makeshift solutions on how to solve task management, orchestration and parallelism. Developers are now writing goal/task loops, something the harnesses don't do. As for memory and context windows, these are now in higher demand and are often mismanaged. Then you have the other issue, most of the time the agent output are way to long and uninformative to be useful, it's very hard to read through it. Another issue is with User messaging and interruptions, the user can't see the outputs or interact with the sub agents, have a hard time knowing the actual status of the process, and interruptions might not lead to better outcomes. Add to that tool call issues, like the fact that tool calls challenge the conversational nature of the models, and MCP inefficiencies and you get a functioning but very confusing system.

The idea here is to provide a programmable environment where everything if accessible through programmable interfaces, there is still a user interface for showing the user stats and summaries, but the models and the user messages run in a REPL, where every unescaped new line is an expression to be evaluated. Where instead of having to cache the whole conversation for one relevant part, the model can create its own caches of things it thinks of as important, as of now when the model writes `note: xyz` the model latter has to relay on the hope that its context wasn't distilled and that that part of the conversation is still in cache, and that it hasn't had a crash out on a different topic that takes all the attention away. With this type of management the model can manage its own memory and decide where it want to place its priorities, and even when it crashes out on something it knows where to find the relevant parts to the current task latter. It could utilize better state locking and management practices instead of a write to disk and lock, it could use mutexs. It can send messages to subagent directly knowing how they are listening. The list goes on and on.


## goals
Frictionless experience providing advanced features.

- Reduce the need for caching. (Atomic caching)
- Improve inspectibilty.
- Improve interaction with subagent.
- Provide intuitive API's for agents and users.
- Allow for never ending sessions.
- Provide better user interfaces then what already exists
- Reproducibility
- Good error reporting and handling
- Observability
- Token budgeting

## Implementation
This is rather an abstract over implementation, and quite uninformed.
- The current runtime decisions and hypotheses are recorded in
  [runtime-semantics.md](runtime-semantics.md). Implementation experiments
  should treat it as the working reference and amend it explicitly when a
  result invalidates an assumption.
- The REPL obviously, A read evaluated print loop, of a commonly used general programming language.
- The runtime, administrating the models, managing processes REPLs, and providing the APIs.
- The TUI, providing a user interface for observation and interaction.
The rest are to be ironed out.

In other words:
Spaghetti Agent is a CLI for developer agents where the agent and user share a live execution environment instead of a chat transcript.
Rather than exchanging long messages, both interact through a programmable state space: variables, functions, and explicit commands. The agent can store, reuse, and inspect state directly, while the user can inspect or modify the same runtime at any point.

This makes the agent feel less like a chatbot and more like a collaborator inside the workspace. It reduces token overhead, improves context persistence, and makes long-running tasks easier to manage because important information lives in state, not in repeated text.

The system is designed for code-heavy workflows where users already trust agents to execute commands. By moving interaction into a REPL, the agent can manage memory, caching, and intermediate results more naturally, while the session remains inspectable and controllable.
