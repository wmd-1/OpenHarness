"""Seed two demo tenants + API keys for the Phase 3 e2e suite.

Run inside the e2e image as ``OH_ROLE=seed`` (``python e2e_seed.py``).
Idempotent: safe to run on every stack bring-up. The raw keys below are the
well-known values the e2e runner sends as ``X-API-Key`` (see
``e2e/run_e2e_phase3.sh``).
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db import async_session
from app.middleware.auth import hash_api_key
from app.models import ApiKey, Quota, Tenant

# (tenant_id, raw_api_key) — MUST match the constants in e2e/run_e2e_phase3.sh
TENANTS: dict[str, str] = {
    "alpha": "alpha-secret-key",
    "beta": "beta-secret-key",
}


async def main() -> None:
    async with async_session() as s:
        for tenant_id, raw_key in TENANTS.items():
            tenant = await s.get(Tenant, tenant_id)
            if tenant is None:
                s.add(Tenant(id=tenant_id, name=tenant_id))
                print(f"seeded tenant {tenant_id}")

            key_hash = hash_api_key(raw_key)
            existing = (
                await s.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))
            ).scalar_one_or_none()
            if existing is None:
                s.add(ApiKey(tenant_id=tenant_id, key_hash=key_hash, label=f"{tenant_id}-e2e"))
                print(f"seeded api key for {tenant_id}")

            quota = await s.get(Quota, tenant_id)
            if quota is None:
                s.add(
                    Quota(
                        tenant_id=tenant_id,
                        max_concurrent=10,
                        daily_submit_limit=1000,
                        rate_per_min=100,
                    )
                )
                print(f"seeded quota for {tenant_id}")

        await s.commit()
    print("seed complete")


if __name__ == "__main__":
    asyncio.run(main())
