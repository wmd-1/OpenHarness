"""File-based async message queue for leader-worker communication in OpenHarness swarms.

Each message is stored as an individual JSON file:
    ~/.openharness/teams/<team>/agents/<agent_id>/inbox/<timestamp>_<message_id>.json

Atomic writes use a .tmp file followed by os.rename to prevent partial reads.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

MessageType = Literal[
    "user_message",
    "permission_request",
    "permission_response",
    "shutdown",
    "idle_notification",
]


@dataclass
class MailboxMessage:
    """A single message exchanged between swarm agents."""

    id: str
    type: MessageType
    sender: str
    recipient: str
    payload: dict[str, Any]
    timestamp: float
    read: bool = False

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "sender": self.sender,
            "recipient": self.recipient,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "read": self.read,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MailboxMessage":
        return cls(
            id=data["id"],
            type=data["type"],
            sender=data["sender"],
            recipient=data["recipient"],
            payload=data.get("payload", {}),
            timestamp=data["timestamp"],
            read=data.get("read", False),
        )


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def get_team_dir(team_name: str) -> Path:
    """Return ~/.openharness/teams/<team_name>/"""
    base = Path.home() / ".openharness" / "teams" / team_name
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_agent_mailbox_dir(team_name: str, agent_id: str) -> Path:
    """Return ~/.openharness/teams/<team_name>/agents/<agent_id>/inbox/"""
    inbox = get_team_dir(team_name) / "agents" / agent_id / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


# ---------------------------------------------------------------------------
# TeammateMailbox
# ---------------------------------------------------------------------------


class TeammateMailbox:
    """File-based mailbox for a single agent within a swarm team.

    Each message lives in its own JSON file named ``<timestamp>_<id>.json``
    inside the agent's inbox directory.  Writes are atomic: the payload is
    first written to a ``.tmp`` file, then renamed into place so that readers
    never see a partial message.
    """

    def __init__(self, team_name: str, agent_id: str) -> None:
        self.team_name = team_name
        self.agent_id = agent_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_mailbox_dir(self) -> Path:
        """Return the inbox directory path, creating it if necessary."""
        return get_agent_mailbox_dir(self.team_name, self.agent_id)

    async def write(self, msg: MailboxMessage) -> None:
        """Atomically write *msg* to the inbox as a JSON file.

        The file is first written to ``<name>.tmp`` then renamed into the
        inbox directory so that concurrent readers never observe a partial
        write.
        """
        inbox = self.get_mailbox_dir()
        filename = f"{msg.timestamp:.6f}_{msg.id}.json"
        final_path = inbox / filename
        tmp_path = inbox / f"{filename}.tmp"

        payload = json.dumps(msg.to_dict(), indent=2)
        tmp_path.write_text(payload, encoding="utf-8")
        os.rename(tmp_path, final_path)

    async def read_all(self, unread_only: bool = True) -> list[MailboxMessage]:
        """Return messages from the inbox, sorted by timestamp (oldest first).

        Args:
            unread_only: When *True* (default) only unread messages are
                returned.  Pass *False* to retrieve all messages including
                already-read ones.
        """
        inbox = self.get_mailbox_dir()
        messages: list[MailboxMessage] = []

        for path in sorted(inbox.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                msg = MailboxMessage.from_dict(data)
                if not unread_only or not msg.read:
                    messages.append(msg)
            except (json.JSONDecodeError, KeyError):
                # Skip corrupted message files rather than crashing.
                continue

        return messages

    async def mark_read(self, message_id: str) -> None:
        """Mark the message with *message_id* as read (in-place update)."""
        inbox = self.get_mailbox_dir()

        for path in inbox.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue

            if data.get("id") == message_id:
                data["read"] = True
                tmp_path = path.with_suffix(".json.tmp")
                tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.rename(tmp_path, path)
                return

    async def clear(self) -> None:
        """Remove all message files from the inbox."""
        inbox = self.get_mailbox_dir()
        for path in inbox.glob("*.json"):
            try:
                path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Factory helpers
# ---------------------------------------------------------------------------


def _make_message(
    msg_type: MessageType,
    sender: str,
    recipient: str,
    payload: dict[str, Any],
) -> MailboxMessage:
    return MailboxMessage(
        id=str(uuid.uuid4()),
        type=msg_type,
        sender=sender,
        recipient=recipient,
        payload=payload,
        timestamp=time.time(),
    )


def create_user_message(sender: str, recipient: str, content: str) -> MailboxMessage:
    """Create a plain text user message."""
    return _make_message("user_message", sender, recipient, {"content": content})


def create_shutdown_request(sender: str, recipient: str) -> MailboxMessage:
    """Create a shutdown request message."""
    return _make_message("shutdown", sender, recipient, {})


def create_idle_notification(
    sender: str, recipient: str, summary: str
) -> MailboxMessage:
    """Create an idle-notification message with a brief summary."""
    return _make_message(
        "idle_notification", sender, recipient, {"summary": summary}
    )
