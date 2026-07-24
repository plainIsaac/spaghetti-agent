"""Framework-neutral view model for the quiet user-facing project UI."""

from __future__ import annotations

from typing import Any


def project_view(session: Any) -> dict[str, Any]:
    """Return presentation data without exposing kernel internals or raw queues.

    The default surface is intentionally small; activity, tasks, branches, and
    logs are inspectable sections for a UI to reveal on demand.
    """
    snapshot = session.state_snapshot() if hasattr(session, "state_snapshot") else {"agents": [], "active_tasks": [], "recent_errors": [], "branches": session.supervisor.workspace.branches()}
    replies = [
        {"id": message.id, "sender": session.user_message_label(message), "text": message.text, "created_at": message.created_at.isoformat()}
        for message in session.user_messages()
    ]
    presentable = {
        value.name: {"value": value.value, "label": value.label, "presenter": value.presenter}
        for value in session.observe()
        if value.show_by_default
    }
    branches = []
    for branch in snapshot["branches"]:
        item = dict(branch)
        if branch["state"] == "submitted":
            item["diff"] = "\n".join(change["diff"] for change in session.supervisor.workspace.diff(branch["task_id"]))
        branches.append(item)
    inference = snapshot.get("token_budget", session.supervisor.token_budget.snapshot())
    inference["policy"] = getattr(session, "inference_policy", {})
    return {
        "default": {"replies": replies, "state": presentable, "inference": inference},
        "inspection": {
            "agents": snapshot["agents"],
            "tasks": snapshot["active_tasks"],
            "branches": branches,
            "errors": snapshot["recent_errors"],
            "conversation": [
                {"id": message.id, "sender": message.sender, "recipient": message.recipient, "text": message.text, "created_at": message.created_at.isoformat()}
                for message in session.conversation_log()
            ],
            "repl_log": session.repl_log(),
        },
    }


def project_index(manager: Any) -> dict[str, Any]:
    """Quiet project-switcher data; opening a runtime remains an explicit action."""
    projects = []
    for project in manager.registry.list(include_archived=True):
        root = manager.root / f"project-{project.id}"
        projects.append(
            {
                "id": project.id,
                "name": project.name,
                "state": project.state,
                "created_at": project.created_at,
                "archived_at": project.archived_at,
                "format_version": project.format_version,
                "runtime_initialized": root.exists(),
                "runtime_open": manager.is_open(project.id),
                "workspace_path": str(root / "workspace") if (root / "workspace").exists() else None,
                "inference_policy": manager.inference_policy(project.id),
            }
        )
    return {"projects": projects}
