"""Backend registry for teammate execution."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from openharness.swarm.spawn_utils import is_inside_tmux, is_tmux_available
from openharness.swarm.types import BackendType, TeammateExecutor

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class BackendRegistry:
    """Registry that maps BackendType names to TeammateExecutor instances.

    Usage::

        registry = BackendRegistry()
        executor = registry.get_executor()   # auto-detect best backend
        executor = registry.get_executor("subprocess")  # explicit selection
    """

    def __init__(self) -> None:
        self._backends: dict[BackendType, TeammateExecutor] = {}
        self._detected: BackendType | None = None
        self._register_defaults()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_backend(self, executor: TeammateExecutor) -> None:
        """Register a custom executor under its declared ``type`` key."""
        self._backends[executor.type] = executor
        logger.debug("Registered backend: %s", executor.type)

    def detect_backend(self) -> BackendType:
        """Detect and cache the most capable available backend.

        Detection priority:
        1. ``tmux`` — if inside an active tmux session and tmux binary is present.
        2. ``subprocess`` — always available as the safe fallback.

        Returns:
            The detected :data:`BackendType` string.
        """
        if self._detected is not None:
            return self._detected

        if is_inside_tmux() and is_tmux_available():
            if "tmux" in self._backends:
                logger.debug("Detected backend: tmux (inside tmux session)")
                self._detected = "tmux"
                return self._detected

        logger.debug("Detected backend: subprocess (default fallback)")
        self._detected = "subprocess"
        return self._detected

    def get_executor(self, backend: BackendType | None = None) -> TeammateExecutor:
        """Return a TeammateExecutor for the given backend type.

        Args:
            backend: Explicit backend type to use. When *None* the registry
                     auto-detects the best available backend.

        Returns:
            The registered :class:`~openharness.swarm.types.TeammateExecutor`.

        Raises:
            KeyError: If the requested backend has not been registered.
        """
        resolved = backend or self.detect_backend()
        executor = self._backends.get(resolved)
        if executor is None:
            available = list(self._backends.keys())
            raise KeyError(
                f"Backend {resolved!r} is not registered. Available: {available}"
            )
        return executor

    def available_backends(self) -> list[BackendType]:
        """Return sorted list of registered backend types."""
        return sorted(self._backends.keys())  # type: ignore[return-value]

    def reset(self) -> None:
        """Clear detection cache and re-register defaults.

        Intended for testing — allows re-detection after env changes.
        """
        self._detected = None
        self._backends.clear()
        self._register_defaults()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _register_defaults(self) -> None:
        """Register built-in backends that are unconditionally available."""
        from openharness.swarm.subprocess_backend import SubprocessBackend

        self._backends["subprocess"] = SubprocessBackend()

        # Tmux backend registration is deferred until implementation exists.
        # If a TmuxBackend is available it can be registered via register_backend().


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: BackendRegistry | None = None


def get_backend_registry() -> BackendRegistry:
    """Return the process-wide singleton BackendRegistry."""
    global _registry
    if _registry is None:
        _registry = BackendRegistry()
    return _registry
