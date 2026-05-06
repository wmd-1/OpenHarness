"""Security regressions for Feishu/Lark channel media handling."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openharness.channels.bus.queue import MessageBus
from openharness.channels.bus.events import OutboundMessage
from openharness.channels.impl.feishu import FeishuChannel
from openharness.config.schema import FeishuConfig


@pytest.mark.asyncio
async def test_feishu_inbound_file_attachment_cannot_escape_media_dir(tmp_path: Path, monkeypatch):
    """Remote Feishu filenames are metadata and must not be trusted as paths."""
    workspace = tmp_path / "ohmo"
    workspace.mkdir()
    protected_file = workspace / "soul.md"
    protected_file.write_text("ORIGINAL", encoding="utf-8")
    monkeypatch.setenv("OHMO_WORKSPACE", str(workspace))

    channel = FeishuChannel(
        FeishuConfig(allow_from=["user-open-id"], react_emoji="eyes"), MessageBus()
    )

    def fake_download(message_id: str, file_key: str, resource_type: str = "file"):
        assert message_id == "message-id"
        assert file_key == "file-key"
        return b"ATTACKER_OVERWRITE", "../../soul.md"

    async def fake_add_reaction(*args, **kwargs):
        return None

    monkeypatch.setattr(channel, "_download_file_sync", fake_download)
    monkeypatch.setattr(channel, "_add_reaction", fake_add_reaction)
    monkeypatch.setattr(
        channel, "_resolve_sender_display_name_sync", lambda sender_id: "Allowed User"
    )

    forwarded = {}

    async def fake_handle_message(**kwargs):
        forwarded.update(kwargs)

    monkeypatch.setattr(channel, "_handle_message", fake_handle_message)

    event = SimpleNamespace(
        sender=SimpleNamespace(
            sender_type="user",
            sender_id=SimpleNamespace(open_id="user-open-id"),
        ),
        message=SimpleNamespace(
            message_id="message-id",
            chat_id="chat-id",
            chat_type="p2p",
            message_type="file",
            content=json.dumps({"file_key": "file-key"}),
        ),
    )

    await channel._on_message(SimpleNamespace(event=event))

    media_dir = workspace / "attachments" / "feishu"
    saved_paths = [Path(path).resolve() for path in forwarded["media"]]
    assert protected_file.read_text(encoding="utf-8") == "ORIGINAL"
    assert saved_paths == [(media_dir / "soul.md").resolve()]
    assert saved_paths[0].read_bytes() == b"ATTACKER_OVERWRITE"
    assert saved_paths[0].is_relative_to(media_dir.resolve())
    assert "../../" not in forwarded["content"]


@pytest.mark.asyncio
async def test_feishu_send_does_not_reply_in_thread_for_private_messages(monkeypatch):
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._client = object()
    sent: list[str | None] = []

    def fake_send(*args):
        sent.append(args[-1])
        return True

    monkeypatch.setattr(channel, "_send_message_sync", fake_send)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="ou_private",
            content="hello",
            metadata={"chat_type": "p2p", "message_id": "om_private"},
        )
    )

    assert sent == [None]


@pytest.mark.asyncio
async def test_feishu_send_replies_in_thread_for_group_messages(monkeypatch):
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    channel._client = object()
    sent: list[str | None] = []

    def fake_send(*args):
        sent.append(args[-1])
        return True

    monkeypatch.setattr(channel, "_send_message_sync", fake_send)

    await channel.send(
        OutboundMessage(
            channel="feishu",
            chat_id="oc_group",
            content="hello",
            metadata={"chat_type": "group", "message_id": "om_group"},
        )
    )

    assert sent == ["om_group"]


@pytest.mark.asyncio
async def test_feishu_create_managed_group_builds_expected_request():
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    captured = {}

    class FakeChat:
        def create(self, request):
            captured["request"] = request
            return SimpleNamespace(
                success=lambda: True,
                data=SimpleNamespace(chat_id="oc_new_group"),
            )

    channel._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(chat=FakeChat())))

    chat_id = await channel.create_managed_group(user_open_id="ou_user", name="OpenHarness 讨论群")

    request = captured["request"]
    assert chat_id == "oc_new_group"
    assert request.user_id_type == "open_id"
    assert request.set_bot_manager is True
    assert request.body.name == "OpenHarness 讨论群"
    assert request.body.user_id_list == ["ou_user"]
    assert request.body.chat_mode == "group"
    assert request.body.chat_type == "private"


@pytest.mark.asyncio
async def test_feishu_create_managed_group_reports_api_failure():
    channel = FeishuChannel(FeishuConfig(), MessageBus())

    class FakeChat:
        def create(self, request):
            return SimpleNamespace(
                success=lambda: False,
                code=99991663,
                msg="missing scope",
                get_log_id=lambda: "log-1",
            )

    channel._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(chat=FakeChat())))

    with pytest.raises(RuntimeError, match="99991663.*missing scope.*log-1"):
        await channel.create_managed_group(user_open_id="ou_user", name="OpenHarness 讨论群")


@pytest.mark.asyncio
async def test_feishu_rename_group_builds_expected_request():
    channel = FeishuChannel(FeishuConfig(), MessageBus())
    captured = {}

    class FakeChat:
        def update(self, request):
            captured["request"] = request
            return SimpleNamespace(success=lambda: True)

    channel._client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(chat=FakeChat())))

    await channel.rename_group(chat_id="oc_group", name="New Name")

    request = captured["request"]
    assert request.user_id_type == "open_id"
    assert request.chat_id == "oc_group"
    assert request.body.name == "New Name"
