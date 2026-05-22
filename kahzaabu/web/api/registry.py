# SPDX-License-Identifier: Apache-2.0
"""GET /api/registry/entities — the public-sector entity registry
(ADR 0011) used by the /laws page tile grid (Slice E) and as the
trust anchor for fact-check evidence URLs.

Reads from kahzaabu.registry — that module is the source of truth;
this endpoint just exposes it over HTTP for the web UI.
"""
from __future__ import annotations

from fastapi import APIRouter

from kahzaabu import registry

router = APIRouter()


@router.get("/registry/entities")
def list_entities() -> dict:
    """List all 25 entities in the public-sector registry.

    Each entity has:
      - entity_id    short slug ("presidency", "elections", ...)
      - official_name human-readable institution name
      - domain        official website domain (no scheme)
      - entity_type   categorical bucket
                      (constitutional_executive, ministry, regulator,
                       independent_commission, soe, ...)"""
    return {"entities": registry.all_entities()}
