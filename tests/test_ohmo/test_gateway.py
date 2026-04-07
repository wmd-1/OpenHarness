from datetime import datetime

from openharness.channels.bus.events import InboundMessage

from ohmo.gateway.bridge import _format_gateway_error
from ohmo.gateway.router import session_key_for_message


def test_gateway_router_uses_thread_when_present():
    message = InboundMessage(
        channel="slack",
        sender_id="u1",
        chat_id="c1",
        content="hello",
        timestamp=datetime.utcnow(),
        metadata={"thread_ts": "t1"},
    )
    assert session_key_for_message(message) == "slack:c1:t1"


def test_gateway_router_falls_back_to_chat_scope():
    message = InboundMessage(
        channel="telegram",
        sender_id="u1",
        chat_id="chat-1",
        content="hello",
        timestamp=datetime.utcnow(),
    )
    assert session_key_for_message(message) == "telegram:chat-1"


def test_gateway_error_formats_claude_refresh_failure():
    exc = ValueError("Claude OAuth refresh failed: HTTP Error 400: Bad Request")
    assert "claude-login" in _format_gateway_error(exc)
    assert "Claude subscription auth refresh failed" in _format_gateway_error(exc)


def test_gateway_error_formats_generic_auth_failure():
    exc = ValueError("API key missing for current profile")
    assert "Authentication failed" in _format_gateway_error(exc)
