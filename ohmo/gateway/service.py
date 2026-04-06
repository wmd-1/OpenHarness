"""Gateway service lifecycle for ohmo."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from openharness.channels.bus.queue import MessageBus
from openharness.channels.impl.manager import ChannelManager

from ohmo.gateway.bridge import OhmoGatewayBridge
from ohmo.gateway.config import build_channel_manager_config, load_gateway_config
from ohmo.gateway.models import GatewayState
from ohmo.gateway.runtime import OhmoSessionRuntimePool
from ohmo.workspace import get_logs_dir, get_state_path, get_workspace_root, initialize_workspace

logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parents[2]


class OhmoGatewayService:
    """Foreground/background service wrapper for the personal gateway."""

    def __init__(self, cwd: str | Path | None = None, workspace: str | Path | None = None) -> None:
        self._cwd = str(Path(cwd or Path.cwd()).resolve())
        self._workspace = workspace
        os.chdir(self._cwd)
        initialize_workspace(self._workspace)
        self._config = load_gateway_config(self._workspace)
        self._bus = MessageBus()
        self._runtime_pool = OhmoSessionRuntimePool(
            cwd=self._cwd,
            workspace=self._workspace,
            provider_profile=self._config.provider_profile,
        )
        self._bridge = OhmoGatewayBridge(bus=self._bus, runtime_pool=self._runtime_pool)
        self._manager = ChannelManager(build_channel_manager_config(self._config), self._bus)

    @property
    def pid_file(self) -> Path:
        return get_workspace_root(self._workspace) / "gateway.pid"

    @property
    def log_file(self) -> Path:
        return get_logs_dir(self._workspace) / "gateway.log"

    @property
    def state_file(self) -> Path:
        return get_state_path(self._workspace)

    def write_state(self, *, running: bool, last_error: str | None = None) -> None:
        state = GatewayState(
            running=running,
            pid=os.getpid() if running else None,
            active_sessions=self._runtime_pool.active_sessions,
            provider_profile=self._config.provider_profile,
            enabled_channels=self._config.enabled_channels,
            last_error=last_error,
        )
        self.state_file.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")

    async def run_foreground(self) -> int:
        self.pid_file.write_text(str(os.getpid()), encoding="utf-8")
        self.write_state(running=True)
        bridge_task = asyncio.create_task(self._bridge.run(), name="ohmo-gateway-bridge")
        manager_task = asyncio.create_task(self._manager.start_all(), name="ohmo-gateway-channels")
        stop_event = asyncio.Event()

        def _stop(*_: object) -> None:
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _stop)

        try:
            await stop_event.wait()
        finally:
            self._bridge.stop()
            bridge_task.cancel()
            manager_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bridge_task
            with contextlib.suppress(asyncio.CancelledError):
                await manager_task
            await self._manager.stop_all()
            self.write_state(running=False)
            self.pid_file.unlink(missing_ok=True)
        return 0


def start_gateway_process(cwd: str | Path | None = None, workspace: str | Path | None = None) -> int:
    """Start the gateway as a detached subprocess."""
    service = OhmoGatewayService(cwd, workspace)
    service.log_file.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    pythonpath_entries = [str(_REPO_ROOT)]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    with service.log_file.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ohmo",
                "gateway",
                "run",
                "--cwd",
                service._cwd,
                "--workspace",
                str(get_workspace_root(workspace)),
            ],
            cwd=service._cwd,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            env=env,
        )
    return process.pid


def stop_gateway_process(cwd: str | Path | None = None, workspace: str | Path | None = None) -> bool:
    """Stop the background gateway process if present."""
    service = OhmoGatewayService(cwd, workspace)
    if not service.pid_file.exists():
        return False
    try:
        pid = int(service.pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        service.pid_file.unlink(missing_ok=True)
        return False
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    service.pid_file.unlink(missing_ok=True)
    service.write_state(running=False)
    return True


def gateway_status(cwd: str | Path | None = None, workspace: str | Path | None = None) -> GatewayState:
    """Load the last known gateway state."""
    service = OhmoGatewayService(cwd, workspace)
    if service.state_file.exists():
        return GatewayState.model_validate_json(service.state_file.read_text(encoding="utf-8"))
    return GatewayState(
        running=False,
        provider_profile=service._config.provider_profile,
        enabled_channels=service._config.enabled_channels,
    )
