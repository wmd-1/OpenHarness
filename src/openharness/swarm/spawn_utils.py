"""Shared utilities for spawning teammate processes."""

from __future__ import annotations

import os
import shutil
import sys


# Environment variable to override the teammate command
TEAMMATE_COMMAND_ENV_VAR = "OPENHARNESS_TEAMMATE_COMMAND"

# Environment variables forwarded to spawned teammates.
# Tmux may start a login shell that does not inherit the parent env.
_TEAMMATE_ENV_VARS = [
    # API provider selection
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    # Config directory override
    "CLAUDE_CONFIG_DIR",
    # Remote / CCR markers
    "CLAUDE_CODE_REMOTE",
    "CLAUDE_CODE_REMOTE_MEMORY_DIR",
    # Proxy settings
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
]


def get_teammate_command() -> str:
    """Return the executable used to spawn teammate processes.

    Checks the OPENHARNESS_TEAMMATE_COMMAND env var first, then falls back to
    the current Python executable running the ``openharness`` module.
    """
    override = os.environ.get(TEAMMATE_COMMAND_ENV_VAR)
    if override:
        return override
    # Use the same Python interpreter that is running now
    return sys.executable


def build_inherited_cli_flags(
    *,
    model: str | None = None,
    permission_mode: str | None = None,
    plan_mode_required: bool = False,
    extra_flags: list[str] | None = None,
) -> list[str]:
    """Build CLI flags to propagate from the current session to spawned teammates.

    Ensures teammates inherit important settings like permission mode and model
    selection from their parent.

    Args:
        model: Model override to forward (e.g. ``"claude-opus-4-6"``).
        permission_mode: One of ``"bypassPermissions"``, ``"acceptEdits"``, or None.
        plan_mode_required: When True, bypass-permissions flag is suppressed.
        extra_flags: Additional flags to append verbatim.

    Returns:
        List of CLI flag strings ready to be passed to ``subprocess``.
    """
    flags: list[str] = ["--headless"]

    # Propagate permission mode (plan mode takes precedence for safety)
    if not plan_mode_required:
        if permission_mode == "bypassPermissions":
            flags.append("--dangerously-skip-permissions")
        elif permission_mode == "acceptEdits":
            flags.extend(["--permission-mode", "acceptEdits"])

    if model:
        flags.extend(["--model", model])

    if extra_flags:
        flags.extend(extra_flags)

    return flags


def build_inherited_env_vars() -> dict[str, str]:
    """Build environment variables to forward to spawned teammates.

    Always includes ``OPENHARNESS_AGENT_TEAMS=1`` plus any provider/proxy vars
    set in the current process.

    Returns:
        Dict of env var name → value to merge into the subprocess environment.
    """
    env: dict[str, str] = {
        "OPENHARNESS_AGENT_TEAMS": "1",
    }

    for key in _TEAMMATE_ENV_VARS:
        value = os.environ.get(key)
        if value:
            env[key] = value

    return env


def is_tmux_available() -> bool:
    """Return True if the ``tmux`` binary is on PATH."""
    return shutil.which("tmux") is not None


def is_inside_tmux() -> bool:
    """Return True if the current process is running inside a tmux session."""
    return bool(os.environ.get("TMUX"))
