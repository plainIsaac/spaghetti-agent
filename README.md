# Agent REPL

**A local project agent that keeps work durable, reviewable, and quiet.**

Agent REPL turns a project request into a durable workflow instead of a long
chat transcript. A coordinator can delegate implementation, agents work in
managed branches, verification output is retained, and you decide when to
merge a submitted change.

> Status: MVP. Best for experiments, prototypes, and small local projects.

## Why Agent REPL?

Most coding agents make progress hard to inspect: the user receives long
messages while the actual work, errors, and decisions are scattered across a
conversation. Agent REPL separates the two:

- You send ordinary messages and can keep sending them while work runs.
- Agents pull durable context through Python APIs instead of relying on a
  growing chat history.
- Tasks, provider state, token budget, branches, command output, and errors
  are inspectable state.
- Delegated work is submitted for review; it is never silently merged.

## Install

Agent REPL is currently installed from source.

```powershell
git clone <repository-url>
Set-Location agent-repl

py -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
```

Python 3.11+ is required. The `agent-repl` command is installed into the
virtual environment.

## Quick start

1. Copy the environment template and add one provider key.

   ```powershell
   Copy-Item .env.example .env
   # Edit .env and set OPENROUTER_API_KEY=...
   ```

2. Start the local project manager.

   ```powershell
   .\.venv\Scripts\agent-repl.exe --project-manager --openrouter --fallback-free --token-budget 20000
   ```

3. Open the printed local URL, create a project, and send a request:

   > Build a small landing page with `index.html` and `styles.css`. Verify that
   > the page contains a `main` element, then submit the result for review.

4. Use the project page to watch work, inspect verification output and diffs,
   then choose **Merge reviewed branch** when you are satisfied.

Press `Ctrl+C` in the terminal to stop the manager.

## How projects work

The project manager creates an isolated workspace for each project:

```text
.agent-repl-projects/
├── projects.sqlite                 # project registry
└── project-1/
    ├── project.json                # project name and inference policy
    ├── runtime.sqlite              # inbox, tasks, state, branches, budget
    └── workspace/                  # files the agents are allowed to change
```

**It does not inject files into the directory you launched it from or attach
to an existing repository.** The MVP intentionally creates a fresh workspace
per project. Existing-repository attachment is planned as an explicit future
workflow, not an implicit side effect.

The standalone terminal mode is different: it uses the current directory as
its workspace. Use it only when you intentionally want direct local access.

## What the UI shows

| Area | Purpose |
| --- | --- |
| Inference | Provider/model, fallback state, token usage, and resume control |
| Work status | Active agents and durable tasks |
| Verification | Bounded command results, including output and exit status |
| Submitted branches | Reviewable diffs and explicit merge action |
| Inspect work | Raw state, logs, and conversation for debugging |

If a provider fails or a budget is exhausted, work remains pending. Use
**Resume pending work** after updating the provider policy or budget; it
reuses durable work rather than creating duplicate tasks.

## Providers

Choose one primary provider when launching the manager:

```powershell
agent-repl --project-manager --openrouter
agent-repl --project-manager --groq
agent-repl --project-manager --gemini
agent-repl --project-manager --openai
```

| Provider | Environment variable |
| --- | --- |
| OpenRouter | `OPENROUTER_API_KEY` |
| Groq | `GROQ_API_KEY` |
| Gemini | `GEMINI_API_KEY` |
| OpenAI | `OPENAI_API_KEY` |

Useful options:

```text
--fallback-free                 Try configured free-test providers on failure
--model MODEL                   Override the primary model
--token-budget TOKENS           Set a hard project/session token limit
--turn-token-reserve TOKENS     Reserve completion capacity before a turn
--request-timeout SECONDS       First-token and stream-idle timeout
--no-subagents                  Disable dynamic agent creation
```

Token usage uses provider-reported values when available and a conservative
estimate otherwise.

## Safety model

Agent REPL is a local-process tool, not a sandbox.

- Project-manager agents can read/write only their managed project workspace
  through the workspace API.
- Verification commands use an argv list, not shell strings, run from that
  workspace, and have a 60-second maximum timeout.
- Those commands still execute with the permissions of the Agent REPL process.
  Use a VM, container, or disposable machine when that is your requirement.
- Provider HTTP traces and raw model programs are stored locally for debugging;
  treat them as sensitive project data.

## Agent workflow

The default flow is deliberately simple:

```text
user message → coordinator task → delegated branch work → verification
            → submitted diff → user review → explicit merge
```

The runtime has deterministic coverage for this lifecycle, including
branch-aware verification before merge.

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

The suite includes the core MVP acceptance flow: delegation, managed branch
write, verification, completion, review, and merge.

## Limitations

- Free models may be slow, rate-limited, or generate imperfect programs.
- The agent runtime preserves and reports failures, but does not guarantee a
  successful implementation in one model turn.
- Repository attachment, container management, and a hosted installation path
  are not MVP features yet.
