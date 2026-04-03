"""Swarm backend type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable


BackendType = Literal["subprocess", "in_process", "tmux"]


@dataclass
class TeammateIdentity:
    """Identity fields for a teammate agent."""

    agent_id: str
    """Unique agent identifier (format: agentName@teamName)."""

    name: str
    """Agent name (e.g. 'researcher', 'tester')."""

    team: str
    """Team name this teammate belongs to."""

    color: str | None = None
    """Assigned color for UI differentiation."""

    parent_session_id: str | None = None
    """Parent session ID for context linking."""


@dataclass
class TeammateSpawnConfig:
    """Configuration for spawning a teammate."""

    name: str
    team: str
    prompt: str
    cwd: str
    parent_session_id: str
    model: str | None = None
    system_prompt: str | None = None
    color: str | None = None
    permissions: list[str] = field(default_factory=list)
    plan_mode_required: bool = False
    allow_permission_prompts: bool = False


@dataclass
class SpawnResult:
    """Result from spawning a teammate."""

    task_id: str
    """Task ID in the task manager."""

    agent_id: str
    """Unique agent identifier (format: agentName@teamName)."""

    backend_type: BackendType
    """The backend used to spawn this agent."""

    success: bool = True
    error: str | None = None


@dataclass
class TeammateMessage:
    """Message to send to a teammate."""

    text: str
    from_agent: str
    color: str | None = None
    timestamp: str | None = None
    summary: str | None = None


@runtime_checkable
class TeammateExecutor(Protocol):
    """Protocol for teammate execution backends.

    Abstracts spawn/messaging/shutdown across subprocess, in-process, and tmux backends.
    """

    type: BackendType

    def is_available(self) -> bool:
        """Check if this backend is available on the system."""
        ...

    async def spawn(self, config: TeammateSpawnConfig) -> SpawnResult:
        """Spawn a new teammate with the given configuration."""
        ...

    async def send_message(self, agent_id: str, message: TeammateMessage) -> None:
        """Send a message to a running teammate via stdin."""
        ...

    async def shutdown(self, agent_id: str, *, force: bool = False) -> bool:
        """Terminate a teammate.

        Args:
            agent_id: The agent to terminate.
            force: If True, kill immediately. If False, attempt graceful shutdown.

        Returns:
            True if the agent was terminated successfully.
        """
        ...
