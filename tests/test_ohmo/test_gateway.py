from datetime import datetime

from openharness.channels.bus.events import InboundMessage

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

