"""In-process teammate execution backend.

Runs teammate agents as asyncio Tasks inside the current Python process,
using :mod:`contextvars` for per-teammate context isolation (the Python
equivalent of Node's AsyncLocalStorage).

Architecture summary
--------------------
* :class:`TeammateContext` – dataclass holding identity + ``asyncio.Event``
  for graceful cancellation.
* :func:`get_teammate_context` / :func:`set_teammate_context` – ContextVar
  accessors so any code running inside a teammate task can discover its own
  identity without explicit argument threading.
* :func:`start_in_process_teammate` – the actual coroutine that sets up
  context, drives the query engine, and cleans up on exit.
* :class:`InProcessBackend` – implements
  :class:`~openharness.swarm.types.TeammateExecutor` and manages the dict of
  live asyncio Tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from openharness.swarm.mailbox import (
    TeammateMailbox,
    create_idle_notification,
)
from openharness.swarm.types import (
    BackendType,
    SpawnResult,
    TeammateMessage,
    TeammateSpawnConfig,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-teammate context isolation via ContextVar
# ---------------------------------------------------------------------------


@dataclass
class TeammateContext:
    """All per-teammate state that must be isolated across concurrent agents.

    Stored in a :data:`ContextVar` so that each asyncio Task sees its own
    copy without any locking.
    """

    agent_id: str
    """Unique agent identifier (``agentName@teamName``)."""

    agent_name: str
    """Human-readable name, e.g. ``"researcher"``."""

    team_name: str
    """Team this teammate belongs to."""

    parent_session_id: str | None = None
    """Session ID of the spawning leader for transcript correlation."""

    color: str | None = None
    """Optional UI color string."""

    plan_mode_required: bool = False
    """Whether this agent must enter plan mode before making changes."""

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    """Set this event to request graceful cancellation of the agent loop."""


_teammate_context_var: ContextVar[TeammateContext | None] = ContextVar(
    "_teammate_context_var", default=None
)


def get_teammate_context() -> TeammateContext | None:
    """Return the :class:`TeammateContext` for the currently-running teammate task.

    Returns ``None`` when called outside of an in-process teammate.
    """
    return _teammate_context_var.get()


def set_teammate_context(ctx: TeammateContext) -> None:
    """Bind *ctx* to the current async context (task-local)."""
    _teammate_context_var.set(ctx)


# ---------------------------------------------------------------------------
# Agent execution loop
# ---------------------------------------------------------------------------


async def start_in_process_teammate(
    *,
    config: TeammateSpawnConfig,
    agent_id: str,
    cancel_event: asyncio.Event,
    query_context: Any | None = None,
) -> None:
    """Run the agent query loop for an in-process teammate.

    This coroutine is launched as an :class:`asyncio.Task` by
    :class:`InProcessBackend`.  It:

    1. Binds a fresh :class:`TeammateContext` to the current async context.
    2. Drives the query engine loop (reusing
       :func:`~openharness.engine.query.run_query`).
    3. Polls the teammate's mailbox between turns for incoming messages /
       shutdown requests.
    4. Writes an idle-notification to the leader when done.
    5. Cleans up on normal exit *or* cancellation.

    Parameters
    ----------
    config:
        Spawn configuration from the leader.
    agent_id:
        Fully-qualified agent identifier (``name@team``).
    cancel_event:
        Shared event – set by :meth:`InProcessBackend.shutdown` to request
        graceful exit.
    query_context:
        Optional pre-built
        :class:`~openharness.engine.query.QueryContext`.  When *None* this
        function attempts to import and build a minimal default context so
        that tests / direct invocations still work.
    """
    ctx = TeammateContext(
        agent_id=agent_id,
        agent_name=config.name,
        team_name=config.team,
        parent_session_id=config.parent_session_id,
        color=config.color,
        plan_mode_required=config.plan_mode_required,
        cancel_event=cancel_event,
    )
    set_teammate_context(ctx)

    mailbox = TeammateMailbox(team_name=config.team, agent_id=agent_id)

    logger.debug("[in_process] %s: starting", agent_id)

    try:
        if query_context is not None:
            await _run_query_loop(query_context, config, ctx, mailbox)
        else:
            # Minimal stub: just log that we received the prompt.
            # Replace this branch with a real QueryContext builder once the
            # harness wires up the full engine for in-process teammates.
            logger.info(
                "[in_process] %s: no query_context supplied — stub run for prompt: %.80s",
                agent_id,
                config.prompt,
            )
            # Simulate work while honouring cancel requests
            for _ in range(10):
                if cancel_event.is_set():
                    logger.debug("[in_process] %s: cancelled during stub run", agent_id)
                    return
                await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        logger.debug("[in_process] %s: task cancelled", agent_id)
        raise
    except Exception:
        logger.exception("[in_process] %s: unhandled exception in agent loop", agent_id)
    finally:
        # Notify the leader that this teammate has gone idle / finished.
        with contextlib.suppress(Exception):
            idle_msg = create_idle_notification(
                sender=agent_id,
                recipient="leader",
                summary=f"{config.name} finished",
            )
            leader_mailbox = TeammateMailbox(team_name=config.team, agent_id="leader")
            await leader_mailbox.write(idle_msg)

        logger.debug("[in_process] %s: exiting", agent_id)


async def _run_query_loop(
    query_context: Any,
    config: TeammateSpawnConfig,
    ctx: TeammateContext,
    mailbox: TeammateMailbox,
) -> None:
    """Drive :func:`~openharness.engine.query.run_query` until done or cancelled.

    Between turns we check the mailbox for messages from the leader and handle
    shutdown requests.
    """
    # Deferred import to avoid circular dependencies at module load time.
    from openharness.engine.query import run_query
    from openharness.engine.messages import ConversationMessage

    messages: list[ConversationMessage] = [
        ConversationMessage(role="user", content=config.prompt)
    ]

    async for _event, _usage in run_query(query_context, messages):
        # Check for cancellation or shutdown between events
        if ctx.cancel_event.is_set():
            logger.debug("[in_process] %s: cancel_event set, stopping query loop", ctx.agent_id)
            return

        # Drain mailbox – handle shutdown requests immediately
        try:
            pending = await mailbox.read_all(unread_only=True)
        except Exception:
            pending = []

        for msg in pending:
            await mailbox.mark_read(msg.id)
            if msg.type == "shutdown":
                logger.debug("[in_process] %s: received shutdown message", ctx.agent_id)
                ctx.cancel_event.set()
                return


# ---------------------------------------------------------------------------
# InProcessBackend
# ---------------------------------------------------------------------------


class InProcessBackend:
    """TeammateExecutor that runs agents as asyncio Tasks in the current process.

    Context isolation is provided by :mod:`contextvars`: each spawned
    :class:`asyncio.Task` runs with its own copy of the context, so
    :func:`get_teammate_context` returns the correct identity for every
    concurrent agent.
    """

    type: BackendType = "in_process"

    def __init__(self) -> None:
        # Maps agent_id -> (asyncio.Task, cancel_event)
        self._active: dict[str, tuple[asyncio.Task[None], asyncio.Event]] = {}

    # ------------------------------------------------------------------
    # TeammateExecutor protocol
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """In-process backend is always available — no external dependencies."""
        return True

    async def spawn(self, config: TeammateSpawnConfig) -> SpawnResult:
        """Spawn an in-process teammate as an asyncio Task.

        Creates a :class:`TeammateContext`, binds it to a new Task via
        :mod:`contextvars` copy-on-create semantics, and registers the task in
        :attr:`_active`.
        """
        agent_id = f"{config.name}@{config.team}"
        task_id = f"in_process_{uuid.uuid4().hex[:12]}"

        if agent_id in self._active:
            task, _ = self._active[agent_id]
            if not task.done():
                logger.warning(
                    "[InProcessBackend] spawn(): %s is already running", agent_id
                )
                return SpawnResult(
                    task_id=task_id,
                    agent_id=agent_id,
                    backend_type=self.type,
                    success=False,
                    error=f"Agent {agent_id!r} is already running",
                )

        cancel_event = asyncio.Event()

        # asyncio.create_task() copies the current Context automatically,
        # so each Task starts with an independent ContextVar state.
        task = asyncio.create_task(
            start_in_process_teammate(
                config=config,
                agent_id=agent_id,
                cancel_event=cancel_event,
            ),
            name=f"teammate-{agent_id}",
        )

        self._active[agent_id] = (task, cancel_event)

        def _on_done(t: asyncio.Task[None]) -> None:
            self._active.pop(agent_id, None)
            if not t.cancelled() and t.exception() is not None:
                logger.error(
                    "[InProcessBackend] %s raised: %s", agent_id, t.exception()
                )

        task.add_done_callback(_on_done)

        logger.debug("[InProcessBackend] spawned %s (task_id=%s)", agent_id, task_id)
        return SpawnResult(
            task_id=task_id,
            agent_id=agent_id,
            backend_type=self.type,
        )

    async def send_message(self, agent_id: str, message: TeammateMessage) -> None:
        """Write *message* to the teammate's file-based mailbox.

        The agent name and team are inferred from *agent_id* (``name@team``
        format).  This mirrors how pane-based backends work so the rest of
        the swarm stack stays backend-agnostic.
        """
        if "@" not in agent_id:
            raise ValueError(
                f"Invalid agent_id {agent_id!r}: expected 'agentName@teamName'"
            )
        agent_name, team_name = agent_id.split("@", 1)

        from openharness.swarm.mailbox import MailboxMessage

        msg = MailboxMessage(
            id=str(uuid.uuid4()),
            type="user_message",
            sender=message.from_agent,
            recipient=agent_id,
            payload={"content": message.text, **({"color": message.color} if message.color else {})},
            timestamp=message.timestamp and float(message.timestamp) or time.time(),
        )
        mailbox = TeammateMailbox(team_name=team_name, agent_id=agent_name)
        await mailbox.write(msg)
        logger.debug("[InProcessBackend] sent message to %s", agent_id)

    async def shutdown(self, agent_id: str, *, force: bool = False, timeout: float = 10.0) -> bool:
        """Terminate a running in-process teammate.

        Parameters
        ----------
        agent_id:
            The agent to terminate.
        force:
            If *True*, cancel the asyncio Task immediately without waiting for
            graceful shutdown.
        timeout:
            How long (seconds) to wait for the task to complete after setting
            the cancel event before falling back to :meth:`asyncio.Task.cancel`.

        Returns
        -------
        bool
            *True* if the agent was found and termination was initiated.
        """
        entry = self._active.get(agent_id)
        if entry is None:
            logger.debug(
                "[InProcessBackend] shutdown(): %s not found in active tasks", agent_id
            )
            return False

        task, cancel_event = entry

        if task.done():
            self._active.pop(agent_id, None)
            return True

        if force:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        else:
            # Graceful: set the cancel_event and wait for self-exit
            cancel_event.set()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "[InProcessBackend] %s did not exit within %.1fs — forcing cancel",
                    agent_id,
                    timeout,
                )
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        self._active.pop(agent_id, None)
        logger.debug("[InProcessBackend] shut down %s", agent_id)
        return True

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def is_active(self, agent_id: str) -> bool:
        """Return *True* if the teammate has a running (not-done) Task."""
        entry = self._active.get(agent_id)
        if entry is None:
            return False
        task, _ = entry
        return not task.done()

    def active_agents(self) -> list[str]:
        """Return a list of agent_ids with currently running Tasks."""
        return [aid for aid, (task, _) in self._active.items() if not task.done()]

    async def shutdown_all(self, *, force: bool = False, timeout: float = 10.0) -> None:
        """Gracefully (or forcefully) terminate all active teammates."""
        agent_ids = list(self._active.keys())
        await asyncio.gather(
            *(self.shutdown(aid, force=force, timeout=timeout) for aid in agent_ids),
            return_exceptions=True,
        )
