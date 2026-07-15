"""multi-tenancy: Row-Level Security on video_tasks + audit_log (Phase 3, WS-A)

Enables PostgreSQL Row-Level Security on the two tenant-scoped tables so a
session can only read/write rows belonging to the tenant resolved for that
request (``app.current_tenant``, set per-session by ``app.db.get_async_session``
for PostgreSQL). The trusted internal ``system`` tenant is exempt so admin /
internal automation can see every row.

RLS is enforced only for non-superuser roles; the API's DB connection role
MUST therefore be a non-superuser for the policy to take effect. The policy
uses ``current_setting('app.current_tenant', true)`` with missing-ok so that if
the tenant is never set the default is "see nothing" (defense in depth).

PG-only: guarded by dialect so ``alembic upgrade`` on sqlite (local/dev) is a
no-op rather than an error. The sandbox test suite builds tables via
``Base.metadata.create_all`` and never runs these migrations.

Revision ID: 005_rls
Revises: 004_tenant
Create Date: 2026-07-14
"""

from typing import Sequence, Union

from alembic import op

revision: str = "005_rls"
down_revision: Union[str, None] = "004_tenant"
branch_labels: Union[str, Sequence[str] | None] = None
depends_on: Union[str, Sequence[str] | None] = None

# Permissive policy: a row is visible/modifiable when it belongs to the current
# tenant, OR the current tenant is the trusted `system` scope (admin bypass).
_TENANT_POLICY = (
    "tenant_id = current_setting('app.current_tenant', true) "
    "OR current_setting('app.current_tenant', true) = 'system'"
)


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return

    for table in ("video_tasks", "audit_log"):
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(
            f"CREATE POLICY {table}_tenant_isolation ON {table} "
            f"FOR ALL TO PUBLIC USING ({_TENANT_POLICY})"
        )


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return

    for table in ("video_tasks", "audit_log"):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
