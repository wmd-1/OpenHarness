"""Permission sync protocol for leader-worker coordination in OpenHarness swarms.

Workers forward tool-permission requests to the team leader's mailbox; the
leader evaluates (or prompts the user) and sends a response back to the
worker's mailbox.

Flow:
    1. Worker calls ``create_permission_request`` then ``send_permission_request``.
    2. Leader polls for mailbox messages, finds a ``permission_request`` payload,
       calls ``handle_permission_request`` with the existing PermissionChecker.
    3. Leader (or user) resolves the request and the response is written to the
       worker's mailbox via the internal helper.
    4. Worker calls ``poll_permission_response`` and waits up to *timeout* seconds.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from openharness.swarm.mailbox import MailboxMessage, TeammateMailbox

if TYPE_CHECKING:
    from openharness.permissions.checker import PermissionChecker


# ---------------------------------------------------------------------------
# Read-only tool heuristic
# ---------------------------------------------------------------------------

_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "Read",
        "Glob",
        "Grep",
        "WebFetch",
        "WebSearch",
        "TaskGet",
        "TaskList",
        "TaskOutput",
        "CronList",
    }
)


def _is_read_only(tool_name: str) -> bool:
    """Return True for tools that are considered safe/read-only."""
    return tool_name in _READ_ONLY_TOOLS


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SwarmPermissionRequest:
    """Permission request forwarded from a worker to the team leader."""

    id: str
    """Unique identifier for this request (uuid4)."""

    tool_name: str
    """Name of the tool requiring permission (e.g. 'Bash', 'Edit')."""

    tool_use_id: str
    """Original tool-use ID from the worker's execution context."""

    input: dict[str, Any]
    """Serialized tool input parameters."""

    description: str | None = None
    """Human-readable description of the requested operation."""

    permission_suggestions: list[dict[str, Any]] = field(default_factory=list)
    """Suggested rule updates produced by the worker's local permission system."""


@dataclass
class SwarmPermissionResponse:
    """Response sent from the leader back to the requesting worker."""

    request_id: str
    """ID of the ``SwarmPermissionRequest`` this responds to."""

    allowed: bool
    """True if the tool use is approved."""

    feedback: str | None = None
    """Optional rejection reason or leader comment."""

    updated_rules: list[dict[str, Any]] = field(default_factory=list)
    """Permission-rule updates the leader decided to apply."""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_permission_request(
    tool_name: str,
    tool_use_id: str,
    tool_input: dict[str, Any],
    description: str | None = None,
    permission_suggestions: list[dict[str, Any]] | None = None,
) -> SwarmPermissionRequest:
    """Build a new :class:`SwarmPermissionRequest` with a fresh UUID.

    Args:
        tool_name: Name of the tool requesting permission.
        tool_use_id: Original tool-use ID from the execution context.
        tool_input: The tool's input parameters.
        description: Optional human-readable description of the operation.
        permission_suggestions: Optional list of suggested permission-rule dicts.

    Returns:
        A new :class:`SwarmPermissionRequest` in *pending* state.
    """
    return SwarmPermissionRequest(
        id=str(uuid.uuid4()),
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        input=tool_input,
        description=description,
        permission_suggestions=permission_suggestions or [],
    )


# ---------------------------------------------------------------------------
# Worker helpers: send request / poll response
# ---------------------------------------------------------------------------


async def send_permission_request(
    request: SwarmPermissionRequest,
    team_name: str,
    worker_id: str,
    leader_id: str = "leader",
) -> None:
    """Serialize *request* and write it to the leader's mailbox.

    Args:
        request: The permission request to forward.
        team_name: The swarm team name used for mailbox routing.
        worker_id: The sending worker's agent ID.
        leader_id: The leader's agent ID (default ``"leader"``).
    """
    payload: dict[str, Any] = {
        "request_id": request.id,
        "tool_name": request.tool_name,
        "tool_use_id": request.tool_use_id,
        "input": request.input,
        "description": request.description,
        "permission_suggestions": request.permission_suggestions,
        "worker_id": worker_id,
    }
    msg = MailboxMessage(
        id=str(uuid.uuid4()),
        type="permission_request",
        sender=worker_id,
        recipient=leader_id,
        payload=payload,
        timestamp=time.time(),
    )
    leader_mailbox = TeammateMailbox(team_name, leader_id)
    await leader_mailbox.write(msg)


async def poll_permission_response(
    team_name: str,
    worker_id: str,
    request_id: str,
    timeout: float = 60.0,
) -> SwarmPermissionResponse | None:
    """Poll the worker's own mailbox until a matching ``permission_response`` arrives.

    Checks every 0.5 s up to *timeout* seconds.  When a response matching
    *request_id* is found, the message is marked read and the decoded
    :class:`SwarmPermissionResponse` is returned.

    Args:
        team_name: The swarm team name.
        worker_id: The worker's agent ID (owns this mailbox).
        request_id: The ``SwarmPermissionRequest.id`` to match against.
        timeout: Maximum seconds to wait before returning ``None``.

    Returns:
        A :class:`SwarmPermissionResponse`, or ``None`` on timeout.
    """
    worker_mailbox = TeammateMailbox(team_name, worker_id)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        messages = await worker_mailbox.read_all(unread_only=True)
        for msg in messages:
            if msg.type == "permission_response":
                payload = msg.payload
                if payload.get("request_id") == request_id:
                    await worker_mailbox.mark_read(msg.id)
                    return SwarmPermissionResponse(
                        request_id=payload["request_id"],
                        allowed=bool(payload.get("allowed", False)),
                        feedback=payload.get("feedback"),
                        updated_rules=payload.get("updated_rules", []),
                    )
        await asyncio.sleep(0.5)

    return None


# ---------------------------------------------------------------------------
# Leader helper: evaluate and send response
# ---------------------------------------------------------------------------


async def handle_permission_request(
    request: SwarmPermissionRequest,
    checker: "PermissionChecker",
) -> SwarmPermissionResponse:
    """Evaluate *request* using the existing :class:`PermissionChecker`.

    Read-only tools are auto-approved without consulting the checker.  For
    all other tools the checker's ``evaluate`` method is called; if the tool
    is allowed or only requires confirmation (and nothing blocks it), it is
    approved; otherwise it is denied.

    Args:
        request: The incoming permission request from a worker.
        checker: An already-configured :class:`~openharness.permissions.checker.PermissionChecker`.

    Returns:
        A :class:`SwarmPermissionResponse` with the decision.
    """
    # Fast path: unconditionally approve read-only tools.
    if _is_read_only(request.tool_name):
        return SwarmPermissionResponse(
            request_id=request.id,
            allowed=True,
            feedback=None,
        )

    # Extract optional path/command hints from the input dict.
    file_path: str | None = (
        request.input.get("file_path")  # type: ignore[assignment]
        or request.input.get("path")
        or None
    )
    command: str | None = request.input.get("command")  # type: ignore[assignment]

    decision = checker.evaluate(
        request.tool_name,
        is_read_only=False,
        file_path=file_path,
        command=command,
    )

    allowed = decision.allowed
    feedback: str | None = None if allowed else decision.reason

    return SwarmPermissionResponse(
        request_id=request.id,
        allowed=allowed,
        feedback=feedback,
    )


# ---------------------------------------------------------------------------
# Leader helper: write response back to a worker's mailbox
# ---------------------------------------------------------------------------


async def send_permission_response(
    response: SwarmPermissionResponse,
    team_name: str,
    worker_id: str,
    leader_id: str = "leader",
) -> None:
    """Write *response* to the worker's mailbox.

    This is a convenience helper for leader code that already holds a
    :class:`SwarmPermissionResponse` and needs to route it back.

    Args:
        response: The resolution to send.
        team_name: The swarm team name.
        worker_id: The target worker's agent ID.
        leader_id: The sending leader's agent ID (default ``"leader"``).
    """
    payload: dict[str, Any] = {
        "request_id": response.request_id,
        "allowed": response.allowed,
        "feedback": response.feedback,
        "updated_rules": response.updated_rules,
    }
    msg = MailboxMessage(
        id=str(uuid.uuid4()),
        type="permission_response",
        sender=leader_id,
        recipient=worker_id,
        payload=payload,
        timestamp=time.time(),
    )
    worker_mailbox = TeammateMailbox(team_name, worker_id)
    await worker_mailbox.write(msg)
