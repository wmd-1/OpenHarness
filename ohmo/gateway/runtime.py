"""Session-aware runtime pool for ohmo gateway."""

from __future__ import annotations

from dataclasses import dataclass
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
            bundle.engine.set_system_prompt(
                build_ohmo_system_prompt(self._cwd, workspace=self._workspace, extra_prompt=None)
            )
            return bundle

        snapshot = self._session_backend.load_latest_for_session_key(session_key)
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
        self._bundles[session_key] = bundle
        return bundle

    async def stream_message(self, message: InboundMessage, session_key: str):
        """Submit an inbound channel message and yield progress + final reply updates."""
        bundle = await self.get_bundle(session_key, latest_user_prompt=message.content)
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
                yield GatewayStreamUpdate(
                    kind="progress",
                    text=event.message,
                    metadata={"_progress": True, "_session_key": session_key},
                )
                continue
            if isinstance(event, ToolExecutionStarted):
                summary = _summarize_tool_input(event.tool_name, event.tool_input)
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
                continue
            if isinstance(event, ErrorEvent):
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
        if reply:
            yield GatewayStreamUpdate(
                kind="final",
                text=reply,
                metadata={"_session_key": session_key},
            )


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
