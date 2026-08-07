# Spaghetti Agent Guide

This file extends the workspace-level `AGENTS.md` for this repository.

## Scope and references

- Read `README.md`, `product.md`, `runtime-semantics.md`, and `pyproject.toml` before changing runtime behavior.
- Keep production code under `src/agent_repl/` and tests under `tests/`.
- Treat live tests, builds, logs, caches, virtual environments, and `.env` as local or generated state; do not commit them.

## Design constraints

- Keep it idiomatic, the code, format , APIs, standards are there for a reason.
- APIs, names, structure, documentation, and usage patterns should be intuitive.
- Most common behavior should be the default; With granular control where needed.
- We try to avoid API, rigidity, if an API is understood to be used a different way, within bounds of course, we should allow it. 

- Preserve durable inbox, task, error, and runtime-state semantics. Update `runtime-semantics.md` when an experiment changes a documented runtime decision.
- Keep managed-project workspaces isolated and retain explicit user review before merge.
- Never expose provider credentials or sensitive event payloads. Maintain redaction and bounded logging at all output surfaces.

## UI
- For UI, keep to modern standards, we aren't building time machines.
- In general, UI should reflect the state of the application, and inform the user about it without overloading them.
- Should be visibly pleasing, always feel interactive, do not dump on the user, keep it to the logs.

## Verification

- Run the narrowest relevant test first, then `python -m pytest -q`.
- `python -m unittest discover -s tests -q` is the supported fallback when pytest is unavailable.
- For packaging or CLI changes, install with `python -m pip install --editable ".[dev]"` and smoke-test the affected command.

## Live tests
For some features we need to use the actual product

- Insure live inspectibility is available for the feature you are attempting to test, it saves a lot of time of having to wait the full cycle.
- Make sure to test it how how it would be used, a clever trick wouldn't help.
- I case you are lost without a clue, live debugging might give you better insides.

## Source control
In order to not lose our hard work.
- Use semantic/standard commit messages.
- Fetch, Pull, Commit, and Push as required for a respectable project.
- Keep the repo free of free floating logs, caches and smoke tests, everything tracked and untracked should be properly organised. You got to clean after yourself.

## Issues and features
- Issues not only need to be fixed, they also need to be tacked, to prevent work redundancy and rabbit-holes, and to aid with task delegation.
- Features that are not bug fixes and can not be feature flagged, should get their own branch and pull request.
- Logs are very important, make sure what's necessary to be gets logged.


