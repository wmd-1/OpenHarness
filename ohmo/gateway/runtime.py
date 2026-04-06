"""Session-aware runtime pool for ohmo gateway."""

from __future__ import annotations

from pathlib import Path

from openharness.channels.bus.events import InboundMessage
from openharness.engine.stream_events import AssistantTextDelta, AssistantTurnComplete
from openharness.ui.runtime import RuntimeBundle, build_runtime, start_runtime

from ohmo.prompts import build_ohmo_system_prompt
from ohmo.session_storage import OhmoSessionBackend


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

        bundle = await build_runtime(
            model=self._model,
            max_turns=self._max_turns,
            system_prompt=build_ohmo_system_prompt(self._cwd, workspace=self._workspace, extra_prompt=None),
            active_profile=self._provider_profile,
            session_backend=self._session_backend,
            enforce_max_turns=self._max_turns is not None,
        )
        await start_runtime(bundle)
        self._bundles[session_key] = bundle
        return bundle

    async def handle_message(self, message: InboundMessage, session_key: str) -> str:
        """Submit an inbound channel message and return the assistant reply."""
        bundle = await self.get_bundle(session_key, latest_user_prompt=message.content)
        bundle.engine.set_system_prompt(
            build_ohmo_system_prompt(self._cwd, workspace=self._workspace, extra_prompt=None)
        )
        reply_parts: list[str] = []
        async for event in bundle.engine.submit_message(message.content):
            if isinstance(event, AssistantTextDelta):
                reply_parts.append(event.text)
            elif isinstance(event, AssistantTurnComplete) and not reply_parts:
                reply_parts.append(event.message.text.strip())
        reply = "".join(reply_parts).strip()
        self._session_backend.save_snapshot(
            cwd=self._cwd,
            model=bundle.current_settings().model,
            system_prompt=build_ohmo_system_prompt(self._cwd, workspace=self._workspace, extra_prompt=None),
            messages=bundle.engine.messages,
            usage=bundle.engine.total_usage,
            session_id=bundle.session_id,
        )
        return reply
