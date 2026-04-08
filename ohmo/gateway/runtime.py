"""Session-aware runtime pool for ohmo gateway."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import json

from openharness.channels.bus.events import InboundMessage
from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    ErrorEvent,
    StatusEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.ui.runtime import RuntimeBundle, build_runtime, start_runtime

from ohmo.prompts import build_ohmo_system_prompt
from ohmo.session_storage import OhmoSessionBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GatewayStreamUpdate:
    """One outbound update produced while processing a channel message."""

    kind: str
    text: str
    metadata: dict[str, object]


class OhmoSessionRuntimePool:
    """Maintain one runtime bundle per chat/thread session."""

    def __init__(
        self,
        *,
        cwd: str | Path,
        workspace: str | Path | None = None,
        provider_profile: str,
        model: str | None = None,
        max_turns: int | None = None,
    ) -> None:
        self._cwd = str(Path(cwd).resolve())
        self._workspace = workspace
        self._provider_profile = provider_profile
        self._model = model
        self._max_turns = max_turns
        self._session_backend = OhmoSessionBackend(workspace)
        self._bundles: dict[str, RuntimeBundle] = {}

    @property
    def active_sessions(self) -> int:
        return len(self._bundles)

    async def get_bundle(self, session_key: str, latest_user_prompt: str | None = None) -> RuntimeBundle:
        """Return an existing bundle or create a new one."""
        bundle = self._bundles.get(session_key)
        if bundle is not None:
            logger.info(
                "ohmo runtime reusing session session_key=%s session_id=%s prompt=%r",
                session_key,
                bundle.session_id,
                _content_snippet(latest_user_prompt or ""),
            )
            bundle.engine.set_system_prompt(
                build_ohmo_system_prompt(self._cwd, workspace=self._workspace, extra_prompt=None)
            )
            return bundle

        snapshot = self._session_backend.load_latest_for_session_key(session_key)
        logger.info(
            "ohmo runtime creating session session_key=%s restored=%s prompt=%r",
            session_key,
            bool(snapshot),
            _content_snippet(latest_user_prompt or ""),
        )
        bundle = await build_runtime(
            model=self._model,
            max_turns=self._max_turns,
            system_prompt=build_ohmo_system_prompt(self._cwd, workspace=self._workspace, extra_prompt=None),
            active_profile=self._provider_profile,
            session_backend=self._session_backend,
            enforce_max_turns=self._max_turns is not None,
            restore_messages=snapshot.get("messages") if snapshot else None,
        )
        if snapshot and snapshot.get("session_id"):
            bundle.session_id = str(snapshot["session_id"])
        await start_runtime(bundle)
        logger.info(
            "ohmo runtime started session_key=%s session_id=%s restored_messages=%s",
            session_key,
            bundle.session_id,
            len(snapshot.get("messages") or []) if snapshot else 0,
        )
        self._bundles[session_key] = bundle
        return bundle

    async def stream_message(self, message: InboundMessage, session_key: str):
        """Submit an inbound channel message and yield progress + final reply updates."""
        bundle = await self.get_bundle(session_key, latest_user_prompt=message.content)
        logger.info(
            "ohmo runtime processing start channel=%s chat_id=%s session_key=%s session_id=%s content=%r",
            message.channel,
            message.chat_id,
            session_key,
            bundle.session_id,
            _content_snippet(message.content),
        )
        bundle.engine.set_system_prompt(
            build_ohmo_system_prompt(self._cwd, workspace=self._workspace, extra_prompt=None)
        )
        reply_parts: list[str] = []
        yield GatewayStreamUpdate(
            kind="progress",
            text="Thinking...",
            metadata={"_progress": True, "_session_key": session_key},
        )
        async for event in bundle.engine.submit_message(message.content):
            if isinstance(event, AssistantTextDelta):
                reply_parts.append(event.text)
                continue
            if isinstance(event, StatusEvent):
                logger.info(
                    "ohmo runtime status session_key=%s session_id=%s message=%r",
                    session_key,
                    bundle.session_id,
                    _content_snippet(event.message),
                )
                yield GatewayStreamUpdate(
                    kind="progress",
                    text=event.message,
                    metadata={"_progress": True, "_session_key": session_key},
                )
                continue
            if isinstance(event, ToolExecutionStarted):
                summary = _summarize_tool_input(event.tool_name, event.tool_input)
                logger.info(
                    "ohmo runtime tool start session_key=%s session_id=%s tool=%s summary=%r",
                    session_key,
                    bundle.session_id,
                    event.tool_name,
                    summary,
                )
                hint = f"Using {event.tool_name}"
                if summary:
                    hint = f"{hint}: {summary}"
                yield GatewayStreamUpdate(
                    kind="tool_hint",
                    text=hint,
                    metadata={
                        "_progress": True,
                        "_tool_hint": True,
                        "_session_key": session_key,
                    },
                )
                continue
            if isinstance(event, ToolExecutionCompleted):
                logger.info(
                    "ohmo runtime tool complete session_key=%s session_id=%s tool=%s",
                    session_key,
                    bundle.session_id,
                    event.tool_name,
                )
                continue
            if isinstance(event, ErrorEvent):
                logger.error(
                    "ohmo runtime error session_key=%s session_id=%s message=%r",
                    session_key,
                    bundle.session_id,
                    _content_snippet(event.message),
                )
                yield GatewayStreamUpdate(
                    kind="error",
                    text=event.message,
                    metadata={"_session_key": session_key},
                )
                return
            if isinstance(event, AssistantTurnComplete) and not reply_parts:
                reply_parts.append(event.message.text.strip())
        reply = "".join(reply_parts).strip()
        self._session_backend.save_snapshot(
            cwd=self._cwd,
            model=bundle.current_settings().model,
            system_prompt=build_ohmo_system_prompt(self._cwd, workspace=self._workspace, extra_prompt=None),
            messages=bundle.engine.messages,
            usage=bundle.engine.total_usage,
            session_id=bundle.session_id,
            session_key=session_key,
        )
        logger.info(
            "ohmo runtime saved snapshot session_key=%s session_id=%s message_count=%s reply_chars=%s",
            session_key,
            bundle.session_id,
            len(bundle.engine.messages),
            len(reply),
        )
        if reply:
            logger.info(
                "ohmo runtime processing complete session_key=%s session_id=%s reply=%r",
                session_key,
                bundle.session_id,
                _content_snippet(reply),
            )
            yield GatewayStreamUpdate(
                kind="final",
                text=reply,
                metadata={"_session_key": session_key},
            )


def _content_snippet(text: str, *, limit: int = 160) -> str:
    """Return a compact single-line preview for logs."""
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3] + "..."


def _summarize_tool_input(tool_name: str, tool_input: dict[str, object]) -> str:
    if not tool_input:
        return ""
    for key in ("url", "query", "pattern", "path", "file_path", "command"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            return text if len(text) <= 120 else text[:120] + "..."
    try:
        raw = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
    except TypeError:
        raw = str(tool_input)
    return raw if len(raw) <= 120 else raw[:120] + "..."
