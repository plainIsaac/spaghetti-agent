# Agent REPL

Agent REPL is a local project agent runtime. You send a project ordinary
messages; a coordinator can delegate work, edit files through a managed
workspace, run bounded verification commands, and submit changes for review
and merge. The browser UI is intentionally quiet: it shows concise work,
provider, and budget state by default, with logs and raw state available on
inspection.

This is an MVP for local, code-oriented projects. It is not a hosted service,
and it does not sandbox the machine for you. Run it only in a workspace and
environment you are willing to let an agent use.

## What you can do

- Create several independent local projects from one project-manager page.
- Send normal-language requests without blocking on model responses.
- Use a coordinator and builder/researcher specialists; agents can delegate
  durable work and use managed task branches.
- Review submitted diffs and merge branches from the browser UI.
- Enforce a shared project token budget and see provider/model status.
- Try OpenRouter, Groq, Gemini, or OpenAI-compatible inference.
- Let agents run bounded, argv-only verification commands in the workspace.

## Quick start (Windows PowerShell)

Prerequisites: Python 3.11 or later and an API key for at least one provider.
OpenRouter is a convenient low-cost testing option.

```powershell
git clone <your-agent-repl-repository>
Set-Location agent-repl

python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[openai]"

Copy-Item .env.example .env
# Edit .env and set OPENROUTER_API_KEY=...

.\.venv\Scripts\agent-repl.exe --project-manager --openrouter --fallback-free --token-budget 20000
```

Open the local URL printed by the command. Create a project, click **Open**,
then send a request such as:

> Build a small static landing page with an `index.html` and `styles.css`.
> Verify the page contains a `main` element, then submit the change for review.

The process remains running until you stop it with `Ctrl+C`.

## Your first project workflow

1. Create and open a project from the manager page.
2. Send a normal message. It is immediately stored in the coordinator inbox.
3. Watch **Work status** for active agents and tasks.
4. Inspect **Verification** for test/command output.
5. When a branch is submitted, inspect its diff under **Submitted branches**.
6. Click **Merge reviewed branch** only after reviewing it.

If a provider is temporarily unavailable, the work remains pending. The
**Inference** panel explains the provider/budget state and offers **Resume
pending work**. Resuming reuses durable inbox work; it does not create a
replacement task.

## Providers and budgets

Choose exactly one primary provider:

```powershell
agent-repl --project-manager --openrouter
agent-repl --project-manager --groq
agent-repl --project-manager --gemini
agent-repl --project-manager --openai
```

Required environment variables are respectively `OPENROUTER_API_KEY`,
`GROQ_API_KEY`, `GEMINI_API_KEY`, and `OPENAI_API_KEY`. `--fallback-free` adds
the available free-test providers after the selected one. A project’s ordered
provider/model policy is durable and changing it restarts only that project’s
runtime.

Useful controls:

```text
--model <provider-model>        Override the primary model
--token-budget <tokens>         Hard project/session estimated-token cap
--turn-token-reserve <tokens>   Capacity reserved before each model turn
--request-timeout <seconds>     First-token and stream-idle timeout
--no-subagents                  Disable dynamic agent creation
```

Token usage comes from provider-reported usage when available and otherwise
uses a conservative estimate. A budget exhaustion leaves work durable and
visible; raise the budget or update the project policy before resuming.

## Files, commands, and safety

Each project has:

```text
.agent-repl-projects/
  projects.sqlite              # manager registry shared by all projects
  project-1/
    project.json               # name and inference policy
    runtime.sqlite             # inbox, tasks, state, workspace metadata, budget
    workspace/                 # the actual project files
```

Agents write shared project files through `workspace.write_text` or
`workspace.write_file`. These writes are task-scoped, revision-aware, and use
branches for delegated implementation work.

Agents may verify work with `workspace.run(["program", "arg"])`. Commands run
from the project workspace, never through a shell string, and have a maximum
60-second timeout. Output is stored and displayed in the UI. This is a bounded
interface, not a security boundary: it runs with the permissions of the local
Agent REPL process.

## Terminal use and diagnostics

The browser project manager is the recommended MVP experience. For terminal
experiments:

```powershell
.\.venv\Scripts\agent-repl.exe --openrouter --single-agent
```

Terminal controls include `:help`, `:state`, `:agents`, `:model-log`,
`:repl-log`, `:http-log`, and `:python`. They are diagnostics; ordinary input
is always treated as a message for the agent.

Provider HTTP traces and raw model-to-REPL records are kept locally in the
chosen data directory for debugging. Treat them as sensitive: they can contain
project prompts and generated code.

## Known MVP limits

- Free providers can be slow, rate-limited, or return imperfect code. The
  runtime preserves work and supplies repair feedback, but no provider can
  guarantee a successful project in one turn.
- Review/merge is explicit: a submitted branch is not silently merged.
- Agent command execution is local-process execution; use a VM, container, or
  disposable workspace if that is your security requirement.
- Existing project folders made by pre-MVP versions may contain older separate
  SQLite files. New projects use one `runtime.sqlite` file.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The test suite includes a deterministic MVP acceptance flow:
coordinator delegation → managed branch write → branch-aware verification →
completion → review/merge.
