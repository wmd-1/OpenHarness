"""Persistent team lifecycle management for OpenHarness swarms.

Teams are stored as JSON files on disk:
    ~/.openharness/teams/<name>/team.json

This module provides TeamMember, TeamFile, and TeamLifecycleManager.
The TeamLifecycleManager can work alongside the in-memory TeamRegistry
in coordinator_mode.py without modifying that module.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from openharness.swarm.mailbox import get_team_dir
from openharness.swarm.types import BackendType


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TeamMember:
    """A member of a swarm team."""

    agent_id: str
    name: str
    backend_type: BackendType
    joined_at: float
    permissions: list[str] = field(default_factory=list)
    worktree_path: str | None = None
    status: Literal["active", "idle", "stopped"] = "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "backend_type": self.backend_type,
            "permissions": self.permissions,
            "worktree_path": self.worktree_path,
            "joined_at": self.joined_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TeamMember":
        return cls(
            agent_id=data["agent_id"],
            name=data["name"],
            backend_type=data["backend_type"],
            permissions=data.get("permissions", []),
            worktree_path=data.get("worktree_path"),
            joined_at=data["joined_at"],
            status=data.get("status", "active"),
        )


@dataclass
class TeamFile:
    """Persistent team metadata stored as team.json inside the team directory."""

    name: str
    created_at: float
    description: str = ""
    members: dict[str, TeamMember] = field(default_factory=dict)
    allowed_paths: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "members": {k: v.to_dict() for k, v in self.members.items()},
            "allowed_paths": self.allowed_paths,
            "created_at": self.created_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TeamFile":
        members = {
            k: TeamMember.from_dict(v)
            for k, v in data.get("members", {}).items()
        }
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            members=members,
            allowed_paths=data.get("allowed_paths", []),
            created_at=data["created_at"],
            metadata=data.get("metadata", {}),
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """Atomically write this team file to *path*."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.rename(path)

    @classmethod
    def load(cls, path: Path) -> "TeamFile":
        """Load a TeamFile from *path*.

        Raises:
            FileNotFoundError: if *path* does not exist.
            json.JSONDecodeError: if the file is not valid JSON.
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# TeamLifecycleManager
# ---------------------------------------------------------------------------

_TEAM_FILE_NAME = "team.json"


def _team_file_path(name: str) -> Path:
    """Return the path to the team.json for *name*."""
    return get_team_dir(name) / _TEAM_FILE_NAME


class TeamLifecycleManager:
    """Manage the on-disk lifecycle of swarm teams.

    Persists team metadata to ``~/.openharness/teams/<name>/team.json``.
    Integrates with the mailbox system's directory layout — the team
    directory created here is the same one that :class:`TeammateMailbox`
    uses, so agents can be added and messaged without separate setup.

    This class is stateless: every method reads from and writes to disk
    directly, making it safe to instantiate multiple times.
    """

    # ------------------------------------------------------------------
    # Team CRUD
    # ------------------------------------------------------------------

    def create_team(self, name: str, description: str = "") -> TeamFile:
        """Create a new team and persist it to disk.

        Raises:
            ValueError: if a team with *name* already exists.
        """
        path = _team_file_path(name)
        if path.exists():
            raise ValueError(f"Team '{name}' already exists at {path}")

        team = TeamFile(
            name=name,
            description=description,
            created_at=time.time(),
        )
        team.save(path)
        return team

    def delete_team(self, name: str) -> None:
        """Remove a team directory and all its contents (mailboxes included).

        Raises:
            ValueError: if the team does not exist.
        """
        team_dir = get_team_dir(name)
        team_file = team_dir / _TEAM_FILE_NAME
        if not team_file.exists():
            raise ValueError(f"Team '{name}' does not exist")
        shutil.rmtree(team_dir)

    def get_team(self, name: str) -> TeamFile | None:
        """Return the TeamFile for *name*, or ``None`` if it does not exist."""
        path = _team_file_path(name)
        if not path.exists():
            return None
        try:
            return TeamFile.load(path)
        except (json.JSONDecodeError, KeyError):
            return None

    def list_teams(self) -> list[TeamFile]:
        """Return all teams found in ``~/.openharness/teams/``, sorted by name."""
        base = Path.home() / ".openharness" / "teams"
        if not base.exists():
            return []

        teams: list[TeamFile] = []
        for team_dir in sorted(base.iterdir()):
            team_file = team_dir / _TEAM_FILE_NAME
            if not team_file.exists():
                continue
            try:
                teams.append(TeamFile.load(team_file))
            except (json.JSONDecodeError, KeyError):
                continue
        return teams

    # ------------------------------------------------------------------
    # Member management
    # ------------------------------------------------------------------

    def add_member(self, team_name: str, member: TeamMember) -> TeamFile:
        """Add *member* to *team_name* and persist.

        If a member with the same ``agent_id`` already exists it is replaced.

        Raises:
            ValueError: if the team does not exist.
        """
        path = _team_file_path(team_name)
        team = self._require_team(team_name, path)
        team.members[member.agent_id] = member
        team.save(path)
        return team

    def remove_member(self, team_name: str, agent_id: str) -> TeamFile:
        """Remove the member with *agent_id* from *team_name* and persist.

        Raises:
            ValueError: if the team or member does not exist.
        """
        path = _team_file_path(team_name)
        team = self._require_team(team_name, path)
        if agent_id not in team.members:
            raise ValueError(
                f"Agent '{agent_id}' is not a member of team '{team_name}'"
            )
        del team.members[agent_id]
        team.save(path)
        return team

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_team(self, name: str, path: Path) -> TeamFile:
        if not path.exists():
            raise ValueError(f"Team '{name}' does not exist")
        return TeamFile.load(path)
