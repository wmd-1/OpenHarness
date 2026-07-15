"""Append-only audit trail (Phase 3, WS-A).

``record_audit`` queues an :class:`AuditLog` row on the caller's session. The
row is flushed by the caller's own ``commit`` so the business mutation and its
audit entry are written atomically (or rolled back together). Audit writes are
explicitly *not* allowed to raise into the user path: any failure is swallowed
and logged so an audit outage can never block a legitimate request.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


async def record_audit(
    db: AsyncSession,
    action: str,
    *,
    tenant_id: str | None = None,
    actor_key_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    meta: dict | None = None,
) -> None:
    """Queue an audit row on ``db``. The caller is responsible for committing.

    Failures are swallowed (logged) so audit never breaks the request path.
    """
    try:
        row = AuditLog(
            tenant_id=tenant_id,
            actor_key_id=actor_key_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            ts=datetime.now(timezone.utc),
            meta_json=json.dumps(meta) if meta else None,
        )
        db.add(row)
    except Exception:  # pragma: no cover - audit must never break the request
        import logging

        logging.getLogger(__name__).exception("audit write failed (non-fatal)")
