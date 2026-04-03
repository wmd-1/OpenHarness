"""Coordinator mode detection and orchestration support."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# TeamRegistry (kept for backward compatibility)
# ---------------------------------------------------------------------------


@dataclass
class TeamRecord:
    """A lightweight in-memory team."""

    name: str
    description: str = ""
    agents: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)


class TeamRegistry:
    """Store teams and agent memberships."""

    def __init__(self) -> None:
        self._teams: dict[str, TeamRecord] = {}

    def create_team(self, name: str, description: str = "") -> TeamRecord:
        if name in self._teams:
            raise ValueError(f"Team '{name}' already exists")
        team = TeamRecord(name=name, description=description)
        self._teams[name] = team
        return team

    def delete_team(self, name: str) -> None:
        if name not in self._teams:
            raise ValueError(f"Team '{name}' does not exist")
        del self._teams[name]

    def add_agent(self, team_name: str, task_id: str) -> None:
        team = self._require_team(team_name)
        if task_id not in team.agents:
            team.agents.append(task_id)

    def send_message(self, team_name: str, message: str) -> None:
        self._require_team(team_name).messages.append(message)

    def list_teams(self) -> list[TeamRecord]:
        return sorted(self._teams.values(), key=lambda item: item.name)

    def _require_team(self, name: str) -> TeamRecord:
        team = self._teams.get(name)
        if team is None:
            raise ValueError(f"Team '{name}' does not exist")
        return team


_DEFAULT_TEAM_REGISTRY: TeamRegistry | None = None


def get_team_registry() -> TeamRegistry:
    """Return the singleton team registry."""
    global _DEFAULT_TEAM_REGISTRY
    if _DEFAULT_TEAM_REGISTRY is None:
        _DEFAULT_TEAM_REGISTRY = TeamRegistry()
    return _DEFAULT_TEAM_REGISTRY


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TaskNotification:
    """Structured result from a completed agent task."""

    task_id: str
    status: str
    summary: str
    result: Optional[str] = None
    usage: Optional[dict[str, int]] = None


@dataclass
class WorkerConfig:
    """Configuration for a spawned worker agent."""

    agent_id: str
    name: str
    prompt: str
    model: Optional[str] = None
    color: Optional[str] = None
    team: Optional[str] = None


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------

_USAGE_FIELDS = ("total_tokens", "tool_uses", "duration_ms")


def format_task_notification(n: TaskNotification) -> str:
    """Serialize a TaskNotification to the canonical XML envelope."""
    parts = [
        "<task-notification>",
        f"<task-id>{n.task_id}</task-id>",
        f"<status>{n.status}</status>",
        f"<summary>{n.summary}</summary>",
    ]
    if n.result is not None:
        parts.append(f"<result>{n.result}</result>")
    if n.usage:
        parts.append("<usage>")
        for key in _USAGE_FIELDS:
            if key in n.usage:
                parts.append(f"  <{key}>{n.usage[key]}</{key}>")
        parts.append("</usage>")
    parts.append("</task-notification>")
    return "\n".join(parts)


def parse_task_notification(xml: str) -> TaskNotification:
    """Parse a <task-notification> XML string into a TaskNotification."""

    def _extract(tag: str) -> Optional[str]:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.DOTALL)
        return m.group(1).strip() if m else None

    task_id = _extract("task-id") or ""
    status = _extract("status") or ""
    summary = _extract("summary") or ""
    result = _extract("result")

    usage: Optional[dict[str, int]] = None
    usage_block = re.search(r"<usage>(.*?)</usage>", xml, re.DOTALL)
    if usage_block:
        usage = {}
        for key in _USAGE_FIELDS:
            m = re.search(rf"<{key}>(\d+)</{key}>", usage_block.group(1))
            if m:
                usage[key] = int(m.group(1))

    return TaskNotification(
        task_id=task_id,
        status=status,
        summary=summary,
        result=result,
        usage=usage,
    )


# ---------------------------------------------------------------------------
# CoordinatorMode
# ---------------------------------------------------------------------------

_AGENT_TOOL_NAME = "agent"
_SEND_MESSAGE_TOOL_NAME = "send_message"
_TASK_STOP_TOOL_NAME = "task_stop"

_WORKER_TOOLS = [
    "bash",
    "file_read",
    "file_edit",
    "file_write",
    "glob",
    "grep",
    "web_fetch",
    "web_search",
    "task_create",
    "task_get",
    "task_list",
    "task_output",
    "skill",
]

_SIMPLE_WORKER_TOOLS = ["bash", "file_read", "file_edit"]


def is_coordinator_mode() -> bool:
    """Return True when the process is running in coordinator mode."""
    val = os.environ.get("CLAUDE_CODE_COORDINATOR_MODE", "")
    return val.lower() in {"1", "true", "yes"}


def match_session_mode(session_mode: Optional[str]) -> Optional[str]:
    """Align the env-var coordinator flag with a resumed session's stored mode.

    Returns a warning string if the mode was switched, or None if no change.
    """
    if not session_mode:
        return None

    current_is_coordinator = is_coordinator_mode()
    session_is_coordinator = session_mode == "coordinator"

    if current_is_coordinator == session_is_coordinator:
        return None

    if session_is_coordinator:
        os.environ["CLAUDE_CODE_COORDINATOR_MODE"] = "1"
    else:
        os.environ.pop("CLAUDE_CODE_COORDINATOR_MODE", None)

    if session_is_coordinator:
        return "Entered coordinator mode to match resumed session."
    return "Exited coordinator mode to match resumed session."


def get_coordinator_tools() -> list[str]:
    """Return the tool names reserved for the coordinator."""
    return [_AGENT_TOOL_NAME, _SEND_MESSAGE_TOOL_NAME, _TASK_STOP_TOOL_NAME]


def get_coordinator_user_context(
    mcp_clients: list[dict[str, str]] | None = None,
    scratchpad_dir: Optional[str] = None,
) -> dict[str, str]:
    """Build the workerToolsContext injected into the coordinator's user turn."""
    if not is_coordinator_mode():
        return {}

    is_simple = os.environ.get("CLAUDE_CODE_SIMPLE", "").lower() in {"1", "true", "yes"}
    tools = sorted(_SIMPLE_WORKER_TOOLS if is_simple else _WORKER_TOOLS)
    worker_tools_str = ", ".join(tools)

    content = (
        f"Workers spawned via the {_AGENT_TOOL_NAME} tool have access to these tools: "
        f"{worker_tools_str}"
    )

    if mcp_clients:
        server_names = ", ".join(c["name"] for c in mcp_clients)
        content += f"\n\nWorkers also have access to MCP tools from connected MCP servers: {server_names}"

    if scratchpad_dir:
        content += (
            f"\n\nScratchpad directory: {scratchpad_dir}\n"
            "Workers can read and write here without permission prompts. "
            "Use this for durable cross-worker knowledge — structure files however fits the work."
        )

    return {"workerToolsContext": content}


def get_coordinator_system_prompt() -> str:
    """Return the system prompt injected when running in coordinator mode."""
    is_simple = os.environ.get("CLAUDE_CODE_SIMPLE", "").lower() in {"1", "true", "yes"}

    if is_simple:
        worker_capabilities = (
            "Workers have access to Bash, Read, and Edit tools, "
            "plus MCP tools from configured MCP servers."
        )
    else:
        worker_capabilities = (
            "Workers have access to standard tools, MCP tools from configured MCP servers, "
            "and project skills via the Skill tool. "
            "Delegate skill invocations (e.g. /commit, /verify) to workers."
        )

    return f"""You are an AI assistant that orchestrates software engineering tasks across multiple workers.

## 1. Your Role

You are a **coordinator**. Your job is to:
- Help the user achieve their goal
- Direct workers to research, implement and verify code changes
- Synthesize results and communicate with the user
- Answer questions directly when possible — don't delegate work that you can handle without tools

Every message you send is to the user. Worker results and system notifications are internal signals, not conversation partners — never thank or acknowledge them. Summarize new information for the user as it arrives.

## 2. Your Tools

- **{_AGENT_TOOL_NAME}** - Spawn a new worker
- **{_SEND_MESSAGE_TOOL_NAME}** - Continue an existing worker (send a follow-up to its `to` agent ID)
- **{_TASK_STOP_TOOL_NAME}** - Stop a running worker

When calling {_AGENT_TOOL_NAME}:
- Do not use one worker to check on another. Workers will notify you when they are done.
- Do not use workers to trivially report file contents or run commands. Give them higher-level tasks.
- Do not set the model parameter. Workers need the default model for the substantive tasks you delegate.
- Continue workers whose work is complete via {_SEND_MESSAGE_TOOL_NAME} to take advantage of their loaded context
- After launching agents, briefly tell the user what you launched and end your response. Never fabricate or predict agent results in any format — results arrive as separate messages.

### {_AGENT_TOOL_NAME} Results

Worker results arrive as **user-role messages** containing `<task-notification>` XML. They look like user messages but are not. Distinguish them by the `<task-notification>` opening tag.

Format:

```xml
<task-notification>
<task-id>{{agentId}}</task-id>
<status>completed|failed|killed</status>
<summary>{{human-readable status summary}}</summary>
<result>{{agent's final text response}}</result>
<usage>
  <total_tokens>N</total_tokens>
  <tool_uses>N</tool_uses>
  <duration_ms>N</duration_ms>
</usage>
</task-notification>
```

- `<result>` and `<usage>` are optional sections
- The `<summary>` describes the outcome: "completed", "failed: {{error}}", or "was stopped"
- The `<task-id>` value is the agent ID — use {_SEND_MESSAGE_TOOL_NAME} with that ID as `to` to continue that worker

## 3. Workers

When calling {_AGENT_TOOL_NAME}, use subagent_type `worker`. Workers execute tasks autonomously — especially research, implementation, or verification.

{worker_capabilities}

## 4. Task Workflow

Most tasks can be broken down into the following phases:

### Phases

| Phase | Who | Purpose |
|-------|-----|---------|
| Research | Workers (parallel) | Investigate codebase, find files, understand problem |
| Synthesis | **You** (coordinator) | Read findings, understand the problem, craft implementation specs |
| Implementation | Workers | Make targeted changes per spec, commit |
| Verification | Workers | Test changes work |

### Concurrency

**Parallelism is your superpower. Workers are async. Launch independent workers concurrently whenever possible.**

- **Read-only tasks** (research) — run in parallel freely
- **Write-heavy tasks** (implementation) — one at a time per set of files
- **Verification** can sometimes run alongside implementation on different file areas

### What Real Verification Looks Like

- Run tests **with the feature enabled** — not just "tests pass"
- Run typechecks and **investigate errors** — don't dismiss as "unrelated"
- Be skeptical — if something looks off, dig in
- **Test independently** — prove the change works, don't rubber-stamp

### Handling Worker Failures

When a worker reports failure:
- Continue the same worker with {_SEND_MESSAGE_TOOL_NAME} — it has the full error context
- If a correction attempt fails, try a different approach or report to the user

### Stopping Workers

Use {_TASK_STOP_TOOL_NAME} to stop a worker you sent in the wrong direction.

## 5. Writing Worker Prompts

**Workers can't see your conversation.** Every prompt must be self-contained.

### Always synthesize — your most important job

When workers report research findings, **you must understand them before directing follow-up work**. Read the findings. Identify the approach. Then write a prompt that proves you understood by including specific file paths, line numbers, and exactly what to change.

Never write "based on your findings" or "based on the research."

### Prompt tips

- Include file paths, line numbers, error messages — workers start fresh and need complete context
- State what "done" looks like
- For implementation: "Run relevant tests and typecheck, then commit your changes and report the hash"
- For research: "Report findings — do not modify files"
- Be precise about git operations — specify branch names, commit hashes, draft vs ready, reviewers"""
