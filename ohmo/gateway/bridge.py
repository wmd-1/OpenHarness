"""Gateway bridge connecting channel bus traffic to ohmo runtimes."""

from __future__ import annotations

import asyncio
import logging

from openharness.channels.bus.events import OutboundMessage
from openharness.channels.bus.queue import MessageBus

from ohmo.gateway.router import session_key_for_message
from ohmo.gateway.runtime import OhmoSessionRuntimePool

logger = logging.getLogger(__name__)


class OhmoGatewayBridge:
    """Consume inbound messages and publish assistant replies."""

    def __init__(self, *, bus: MessageBus, runtime_pool: OhmoSessionRuntimePool) -> None:
        self._bus = bus
        self._runtime_pool = runtime_pool
        self._running = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                message = await asyncio.wait_for(self._bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            session_key = session_key_for_message(message)
            try:
                reply = await self._runtime_pool.handle_message(message, session_key)
            except Exception:  # pragma: no cover - gateway failure path
                logger.exception("ohmo gateway failed to process inbound message")
                reply = "[ohmo gateway error]"
            if not reply:
                continue
            await self._bus.publish_outbound(
                OutboundMessage(
                    channel=message.channel,
                    chat_id=message.chat_id,
                    content=reply,
                    metadata={"_session_key": session_key},
                )
            )

    def stop(self) -> None:
        self._running = False

