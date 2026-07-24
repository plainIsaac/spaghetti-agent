# Spaghetti Agent

**Give agents a runtime, not an ever-growing chat transcript.**

Spaghetti Agent is a durable, programmable runtime where people send ordinary
messages and agents pull context, coordinate work, and expose inspectable state.

**[Install Spaghetti Agent](#installation)** with Python 3.11+ and `pip`.

## Why Spaghetti Agent?

Chat is a useful interface, but a poor substitute for program state. Long-running
agents need durable inboxes, explicit tasks, error history, context they can
query, and a way to coordinate without flooding the user.

Spaghetti Agent gives the model a persistent Python environment for doing that work.
The user can keep sending messages while agents run, inspect state when desired,
and review concise results instead of babysitting every model turn.

## Note: disclosures

> **MVP status:** Spaghetti Agent is experimental. Expect rough edges, especially
> with free or small models.
> We expect you to give full machine access to the agent, insure your usage is in such environment.

- Spaghetti Agent runs locally with the permissions of its process. It is not an
  operating-system sandbox.
- Model providers receive the context sent to them. Raw model programs, HTTP
  traces, and runtime events may be retained locally for debugging.
- Free inference can be slow, rate-limited, or unavailable.
- The managed-project UI uses isolated project workspaces. It does not modify
  the repository from which Spaghetti Agent was launched.
- Standalone terminal mode intentionally uses the current directory as its
  workspace.

## Project essence

The core runtime provides:

- durable user and agent inboxes;
- persistent Python REPL kernels for agents;
- pull-based context with session, task, message, line, and error lifetimes;
- durable tasks, ownership, failures, and observable state;
- multi-agent coordination and dynamic subagents;
- provider fallback, token budgets, streaming, and diagnostic logs.

Managed files, task branches, verification, and review are the first packaged
application of this runtime. They are capabilities built on Spaghetti Agent,
not the definition of Spaghetti Agent itself.

## Installation

Requirements: Python 3.11 or newer, `pip`, and Git.

```text
git clone https://github.com/plainIsaac/spaghetti-agent.git
cd spaghetti-agent
python -m venv .venv
```

Activate the environment:

```text
# macOS / Linux
source .venv/bin/activate

# Windows PowerShell
.\.venv\Scripts\Activate.ps1

# Windows Command Prompt
.venv\Scripts\activate.bat
```

Install Spaghetti Agent using either standard package metadata or
`requirements.txt`:

```text
python -m pip install .

# Equivalent dependency-file installation for contributors and tooling:
python -m pip install -r requirements.txt
python -m pip install --no-deps .
```

The first form is recommended. Both install the `spaghetti-agent` command into
the active virtual environment. The legacy `agent-repl` command remains
available as an alias.

### Install as a global command

Use [`pipx`](https://pipx.pypa.io/) to make `spaghetti-agent` available in
every shell without installing its dependencies into your system Python:

```text
python -m pip install --user pipx
python -m pipx ensurepath
python -m pipx install .
spaghetti-agent --help
```

Open a new terminal after `ensurepath` if the command is not found immediately.
Run `python -m pipx upgrade spaghetti-agent` after pulling a newer version of
the project. Use the virtual-environment installation above when developing or
running the test suite.

## Usage example

Create `.env` beside `pyproject.toml` and configure at least one provider:

```dotenv
OPENROUTER_API_KEY=your_key_here
```

Start the managed-project application:

```text
spaghetti-agent --project-manager --openrouter --fallback-free --token-budget 20000
```

Open the URL printed in the terminal, create a project, and send an ordinary
message:

> Build a small landing page with `index.html` and `styles.css`. Verify that the
> page contains a `main` element, then submit the result for review.

You can continue sending messages while agents work. The UI shows active agents,
tasks, verification results, submitted diffs, provider status, and token usage.
Merging a submitted branch remains an explicit user action.

## Project details

### Runtime model

Messages are durable inbox events, not interrupts. Agents decide when to process
them and retrieve additional context through Python APIs in the same model turn.
They publish a small presentable state for the default UI; deeper state and logs
remain available for intentional inspection.

The managed-project lifecycle is:

```text
user message → coordinator task → delegated branch work → verification
             → submitted diff → user review → explicit merge
```

### Managed project storage

The project manager creates one runtime database and one workspace per project:

```text
.spaghetti-agent-projects/
├── projects.sqlite
└── project-1/
    ├── project.json
    ├── runtime.sqlite
    └── workspace/
```

It neither injects files into the launch directory nor implicitly attaches to
an existing Git repository.

### Providers

Configure the matching environment variable and choose a provider:

| Provider | Option | Environment variable |
| --- | --- | --- |
| OpenRouter | `--openrouter` | `OPENROUTER_API_KEY` |
| Groq | `--groq` | `GROQ_API_KEY` |
| Gemini | `--gemini` | `GEMINI_API_KEY` |
| OpenAI | `--openai` | `OPENAI_API_KEY` |

Useful runtime options:

```text
--fallback-free                 Try configured free-test providers on failure
--model MODEL                   Override the provider's default model
--token-budget TOKENS           Set a hard project or session token limit
--turn-token-reserve TOKENS     Reserve completion capacity before a turn
--request-timeout SECONDS       Set first-token and stream-idle timeouts
--no-subagents                  Disable dynamic agent creation
```

Provider-reported usage is used when available; otherwise Spaghetti Agent records a
conservative token estimate.

## Project usage details

### Managed-project browser UI

```text
spaghetti-agent --project-manager --openrouter
```

Use this mode for independent managed workspaces, durable multi-agent tasks,
branch verification, diff review, and explicit merge control.

### Standalone terminal UI

```text
spaghetti-agent --openrouter
```

This mode operates in the current directory. Normal input becomes a user
message. Debug commands include:

| Command | Purpose |
| --- | --- |
| `:agents` | Show agents and active tasks |
| `:state` | Show presentable runtime state |
| `:python` | Enter Python inspection mode |
| `:model-log` | Show the latest raw model program |
| `:repl-log` | Show model-to-REPL evaluations and errors |
| `:http-log` | Show provider request and stream timing |

### Failure and resume behavior

Provider failures, exhausted budgets, agent errors, and incomplete tasks remain
durable. After correcting the provider policy or budget, use **Resume pending
work** in the browser UI. Spaghetti Agent resumes existing work instead of
silently creating replacement tasks.

## What Spaghetti Agent is not

- It is not a hosted agent service.
- It is not an OS sandbox, container manager, or permissions system.
- It is not limited to local-file or coding agents.
- It is not an autonomous merge bot; submitted work requires review.
- It does not yet provide implicit attachment to arbitrary existing
  repositories.
- It does not guarantee that a model will complete a task correctly in one
  turn.

## Contributing

Issues and focused pull requests are welcome while the runtime is taking shape.
Please include a reproduction for behavioral changes and tests for new runtime
semantics. Keep provider credentials, `.env`, runtime databases, logs, and
generated project workspaces out of commits.

## Development instructions

Create and activate a Python 3.11+ virtual environment, then install Spaghetti
Agent in editable mode with its development dependencies:

```text
python -m pip install --editable ".[dev]"
```

The `--editable` installation keeps the `spaghetti-agent` command linked to
this checkout, so Python source changes take effect without reinstalling the
package. Run the test suite with:

```text
python -m pytest -q
```

The test suite covers inbox durability, persistent kernels, context retrieval,
task ownership, error reporting, multi-agent delegation, managed branches,
verification, provider fallback, token budgeting, and the end-to-end MVP
workflow.
