"""Tests for compaction and token estimation helpers."""

from __future__ import annotations

import asyncio

import pytest

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, ImageBlock, TextBlock, ToolUseBlock
from openharness.hooks import HookEvent
from openharness.services import (
    compact_conversation,
    compact_messages,
    estimate_conversation_tokens,
    estimate_message_tokens,
    estimate_tokens,
    summarize_messages,
)
from openharness.services.compact import (
    AutoCompactState,
    auto_compact_if_needed,
    try_context_collapse,
    try_session_memory_compaction,
)


def test_token_estimation_helpers():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_message_tokens(["abcd", "abcdefgh"]) == 3


def test_compact_and_summarize_messages():
    messages = [
        ConversationMessage(role="user", content=[TextBlock(text="first question")]),
        ConversationMessage(role="assistant", content=[TextBlock(text="first answer")]),
        ConversationMessage(role="user", content=[TextBlock(text="second question")]),
        ConversationMessage(role="assistant", content=[TextBlock(text="second answer")]),
    ]

    summary = summarize_messages(messages, max_messages=2)
    assert "user: second question" in summary
    assert "assistant: second answer" in summary

    compacted = compact_messages(messages, preserve_recent=2)
    assert len(compacted) == 3
    assert "[conversation summary]" in compacted[0].text
    assert estimate_conversation_tokens(compacted) >= 1


class _CompactApiClient:
    def __init__(self, responses):
        self._responses = list(responses)

    async def stream_message(self, request):
        del request
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if asyncio.iscoroutinefunction(response):
            await response()
            return
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=[TextBlock(text=response)]),
            usage=UsageSnapshot(input_tokens=1, output_tokens=1),
            stop_reason=None,
        )


class _HookExecutorStub:
    def __init__(self) -> None:
        self.events: list[tuple[HookEvent, dict[str, object]]] = []

    async def execute(self, event: HookEvent, payload: dict[str, object]):
        self.events.append((event, payload))
        from openharness.hooks.types import AggregatedHookResult

        return AggregatedHookResult()


def test_try_session_memory_compaction_reduces_long_history():
    messages = [
        ConversationMessage(role="user", content=[TextBlock(text=(f"user {index} " * 200).strip())])
        if index % 2 == 0
        else ConversationMessage(role="assistant", content=[TextBlock(text=(f"assistant {index} " * 200).strip())])
        for index in range(20)
    ]

    result = try_session_memory_compaction(messages)

    assert result is not None
    assert len(result) < len(messages)
    assert "Session memory summary" in result[0].text


def test_try_context_collapse_trims_oversized_messages():
    giant = ("alpha " * 1200).strip()
    messages = [
        ConversationMessage(role="user", content=[TextBlock(text=giant)]),
        ConversationMessage(role="assistant", content=[TextBlock(text=giant)]),
        ConversationMessage(role="user", content=[TextBlock(text=giant)]),
        ConversationMessage(role="assistant", content=[TextBlock(text=giant)]),
        ConversationMessage(role="user", content=[TextBlock(text=giant)]),
        ConversationMessage(role="assistant", content=[TextBlock(text="keep recent")]),
        ConversationMessage(role="user", content=[TextBlock(text="latest")]),
    ]

    result = try_context_collapse(messages, preserve_recent=2)

    assert result is not None
    assert "[collapsed" in result[0].text


@pytest.mark.asyncio
async def test_compact_conversation_retries_after_incomplete_response():
    messages = [
        ConversationMessage(role="user", content=[TextBlock(text="alpha")]),
        ConversationMessage(role="assistant", content=[TextBlock(text="beta")]),
        ConversationMessage(role="user", content=[TextBlock(text="gamma")]),
        ConversationMessage(role="assistant", content=[TextBlock(text="delta")]),
        ConversationMessage(role="user", content=[TextBlock(text="epsilon")]),
        ConversationMessage(role="assistant", content=[TextBlock(text="zeta")]),
        ConversationMessage(role="user", content=[TextBlock(text="eta")]),
    ]

    compacted = await compact_conversation(
        messages,
        api_client=_CompactApiClient(["", "<summary>condensed</summary>"]),
        model="claude-test",
    )

    assert compacted[0].text.startswith("This session is being continued")


@pytest.mark.asyncio
async def test_compact_conversation_runs_hooks_and_preserves_carryover_state(tmp_path):
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDAT\x08\x99c``\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    hook_executor = _HookExecutorStub()
    messages = [
        ConversationMessage(role="user", content=[ImageBlock.from_path(image_path)]),
        ConversationMessage(role="assistant", content=[TextBlock(text="Looking at the attachment")]),
        ConversationMessage(
            role="assistant",
            content=[ToolUseBlock(name="read_file", input={"path": str(image_path)})],
        ),
        ConversationMessage(role="user", content=[TextBlock(text="Please keep going")]),
        ConversationMessage(role="assistant", content=[TextBlock(text="Working through it")]),
        ConversationMessage(role="user", content=[TextBlock(text="And preserve context")]),
        ConversationMessage(role="assistant", content=[TextBlock(text="Sure")]),
    ]

    compacted = await compact_conversation(
        messages,
        api_client=_CompactApiClient(["<summary>condensed</summary>"]),
        model="claude-test",
        preserve_recent=2,
        hook_executor=hook_executor,
        carryover_metadata={
            "permission_mode": "plan",
            "session_id": "sess123",
            "read_file_state": [
                {
                    "path": str(image_path),
                    "span": "lines 1-20",
                    "preview": "1\tPNG header",
                }
            ],
            "invoked_skills": ["pikastream-video-meeting"],
            "async_agent_state": ["Spawned async agent [task_id=task_123]"],
            "compact_last": {"checkpoint": "query_auto_triggered", "token_count": 12345},
        },
    )

    assert [event for event, _payload in hook_executor.events] == [HookEvent.PRE_COMPACT, HookEvent.POST_COMPACT]
    assert compacted[0].text.startswith("This session is being continued")
    assert "Carry-over context preserved after compaction" in compacted[1].text
    assert "Plan mode is still active" in compacted[1].text
    assert str(image_path) in compacted[1].text
    assert "read_file" in compacted[1].text
    assert "Recently read files" in compacted[1].text
    assert "Skills invoked earlier" in compacted[1].text
    assert "Async agent / background task state" in compacted[1].text
    assert "Last compact checkpoint" in compacted[1].text


@pytest.mark.asyncio
async def test_auto_compact_records_richer_checkpoint_metadata(monkeypatch):
    monkeypatch.setattr("openharness.services.compact.try_session_memory_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr("openharness.services.compact.should_autocompact", lambda *args, **kwargs: True)
    long_text = "alpha " * 50000
    messages = [
        ConversationMessage(role="user", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="assistant", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="user", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="assistant", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="user", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="assistant", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="user", content=[TextBlock(text=long_text)]),
    ]
    metadata: dict[str, object] = {}

    result, was_compacted = await auto_compact_if_needed(
        messages,
        api_client=_CompactApiClient(["<summary>condensed</summary>"]),
        model="claude-sonnet-4-6",
        state=AutoCompactState(),
        carryover_metadata=metadata,
    )

    assert was_compacted is True
    assert result[0].text.startswith("This session is being continued")
    checkpoints = metadata.get("compact_checkpoints")
    assert isinstance(checkpoints, list)
    checkpoint_names = [entry["checkpoint"] for entry in checkpoints]
    assert "query_auto_triggered" in checkpoint_names
    assert "query_microcompact_end" in checkpoint_names
    assert "compact_end" in checkpoint_names
    assert isinstance(metadata.get("compact_last"), dict)
    assert metadata["compact_last"]["checkpoint"] == "compact_end"


@pytest.mark.asyncio
async def test_auto_compact_if_needed_returns_original_messages_after_timeout(monkeypatch):
    async def _stall():
        await asyncio.sleep(0.05)

    monkeypatch.setattr("openharness.services.compact.COMPACT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("openharness.services.compact.try_session_memory_compaction", lambda *args, **kwargs: None)
    monkeypatch.setattr("openharness.services.compact.should_autocompact", lambda *args, **kwargs: True)
    long_text = "alpha " * 50000
    messages = [
        ConversationMessage(role="user", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="assistant", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="user", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="assistant", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="user", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="assistant", content=[TextBlock(text=long_text)]),
        ConversationMessage(role="user", content=[TextBlock(text=long_text)]),
    ]

    result, was_compacted = await auto_compact_if_needed(
        messages,
        api_client=_CompactApiClient([_stall]),
        model="claude-sonnet-4-6",
        state=AutoCompactState(),
    )

    assert was_compacted is False
    assert result == messages
