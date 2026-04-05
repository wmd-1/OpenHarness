"""Tests for sandbox runtime adapter behavior."""

from __future__ import annotations

import json

import pytest

from openharness.config.settings import SandboxSettings, Settings
from openharness.sandbox.adapter import (
    SandboxUnavailableError,
    build_sandbox_runtime_config,
    get_sandbox_availability,
    wrap_command_for_sandbox,
)


def test_build_sandbox_runtime_config_maps_settings():
    settings = Settings(
        sandbox=SandboxSettings(
            enabled=True,
            network={"allowed_domains": ["github.com"], "denied_domains": ["example.com"]},
            filesystem={"allow_write": [".", "/tmp"], "deny_read": ["~/.ssh"]},
        )
    )

    config = build_sandbox_runtime_config(settings)

    assert config["network"]["allowedDomains"] == ["github.com"]
    assert config["network"]["deniedDomains"] == ["example.com"]
    assert config["filesystem"]["allowWrite"] == [".", "/tmp"]
    assert config["filesystem"]["denyRead"] == ["~/.ssh"]


def test_sandbox_availability_reports_native_windows_unsupported(monkeypatch):
    settings = Settings(sandbox=SandboxSettings(enabled=True))
    monkeypatch.setattr("openharness.sandbox.adapter.get_platform", lambda: "windows")

    availability = get_sandbox_availability(settings)

    assert availability.available is False
    assert "native Windows" in (availability.reason or "")


def test_wrap_command_for_sandbox_returns_original_when_disabled():
    command, settings_path = wrap_command_for_sandbox(["bash", "-lc", "echo hi"], settings=Settings())
    assert command == ["bash", "-lc", "echo hi"]
    assert settings_path is None


def test_wrap_command_for_sandbox_writes_settings_file(monkeypatch):
    settings = Settings(sandbox=SandboxSettings(enabled=True))

    def fake_which(name: str) -> str | None:
        mapping = {
            "srt": "/usr/local/bin/srt",
            "bwrap": "/usr/bin/bwrap",
        }
        return mapping.get(name)

    monkeypatch.setattr("openharness.sandbox.adapter.get_platform", lambda: "linux")
    monkeypatch.setattr("openharness.sandbox.adapter.shutil.which", fake_which)

    command, settings_path = wrap_command_for_sandbox(["bash", "-lc", "echo hi"], settings=settings)

    assert command[:3] == ["/usr/local/bin/srt", "--settings", str(settings_path)]
    assert settings_path is not None and settings_path.exists()
    payload = json.loads(settings_path.read_text(encoding="utf-8"))
    assert payload["filesystem"]["allowWrite"] == ["."]
    settings_path.unlink(missing_ok=True)


def test_wrap_command_for_sandbox_raises_when_required(monkeypatch):
    settings = Settings(sandbox=SandboxSettings(enabled=True, fail_if_unavailable=True))
    monkeypatch.setattr("openharness.sandbox.adapter.get_platform", lambda: "linux")
    monkeypatch.setattr("openharness.sandbox.adapter.shutil.which", lambda name: None)

    with pytest.raises(SandboxUnavailableError):
        wrap_command_for_sandbox(["bash", "-lc", "echo hi"], settings=settings)
