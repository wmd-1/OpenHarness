"""Swarm backend abstraction for teammate execution."""

from openharness.swarm.registry import BackendRegistry, get_backend_registry
from openharness.swarm.subprocess_backend import SubprocessBackend
from openharness.swarm.types import (
    BackendType,
    SpawnResult,
    TeammateExecutor,
    TeammateIdentity,
    TeammateMessage,
    TeammateSpawnConfig,
)

__all__ = [
    "BackendRegistry",
    "BackendType",
    "SpawnResult",
    "SubprocessBackend",
    "TeammateExecutor",
    "TeammateIdentity",
    "TeammateMessage",
    "TeammateSpawnConfig",
    "get_backend_registry",
]
