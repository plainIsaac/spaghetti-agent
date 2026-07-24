# Agent REPL

**A durable, programmable runtime for agents and the people working with them.**

Agent REPL turns an agent conversation into durable, inspectable program state
instead of a long chat transcript. You send ordinary messages; agents use a
persistent Python REPL to pull context, manage tasks, publish concise state,
and communicate intentionally. The managed project workspace is the first MVP
application of that runtime—not its definition.

> Status: MVP. Best for experiments, prototypes, and small agent workflows.

## Why Agent REPL?

Most coding agents make progress hard to inspect: the user receives long
messages while the actual work, errors, and decisions are scattered across a
conversation. Agent REPL separates the two:

- You send ordinary messages and can keep sending them while work runs.
- Agents pull durable context through Python APIs instead of relying on a
  growing chat history.
- Tasks, provider state, token budget, context, and errors are inspectable
  state.
- Runtime capabilities can be composed. Managed files, branches, and review
  are one capability set, rather than an assumption every agent needs.

## Core runtime and the MVP application

The runtime provides durable inboxes, persistent agent kernels, task and error
history, pull-based context APIs, presentable state, provider policy, and
multi-agent coordination. It can support many kinds of agent workflows.

The packaged MVP is a **managed-project application** built on those pieces:
agents can write files in an isolated workspace, split work into branches, run
bounded verification, and submit changes for review. It is a useful starting
point, not a claim that every Agent REPL session is about local files.

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

## MVP application: managed projects

The project manager is one way to use the runtime. It creates an isolated
workspace for each project:

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

## What the MVP UI shows

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
  through the workspace API. This is a capability of that application, not a
  general requirement of the runtime.
- Verification commands use an argv list, not shell strings, run from that
  workspace, and have a 60-second maximum timeout.
- Those commands still execute with the permissions of the Agent REPL process.
  Use a VM, container, or disposable machine when that is your requirement.
- Provider HTTP traces and raw model programs are stored locally for debugging;
  treat them as sensitive project data.

## Runtime model

The managed-project flow is deliberately simple:

```text
user message → coordinator task → delegated branch work → verification
            → submitted diff → user review → explicit merge
```

The runtime has deterministic coverage for this lifecycle, including
branch-aware verification before merge.

Outside that application, the core model remains the same: messages become
durable inbox events, agents retrieve the context they need through Python,
and state is published for people or other agents to inspect.

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
- Existing-repository attachment, container management, and a hosted
  installation path are not MVP features yet.
