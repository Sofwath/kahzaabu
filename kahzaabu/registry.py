# SPDX-License-Identifier: Apache-2.0
"""Public-sector entity registry — the authoritative external-reference
trust anchor for kahzaabu.

Source of truth lives at:
  data/registry/maldives_public_sector.yaml  (human-editable)
  data/registry/maldives_public_sector.json  (machine-loaded twin)

The YAML is canonical for contributors. The JSON twin is what code
reads (stdlib-only, no pyyaml dependency). A test
(`tests/test_registry.py::TestRegistryParity`) asserts the two stay
in sync — drift either way fails CI.

See ADR 0011 for the architecture decision.

Use:
    from kahzaabu.registry import (
        load_registry, is_authoritative, entity_for_url, ENTITY_TYPES,
    )

    if is_authoritative("https://presidency.gov.mv/news/12345"):
        ...  # treat evidence as primary-source

    ent = entity_for_url("https://foreign.gov.mv/contact")
    # → {"entity_id": "foreign", "official_name": "Ministry of Foreign Affairs", ...}

The registry is **additive trust signal**, not a filter: non-registry
URLs are still ingested as evidence; they're just tagged as secondary.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

REGISTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "data" / "registry" / "maldives_public_sector.json"
)

# Entity-type families surface in the verifier prompt + web UI legend.
# Keep aligned with the `entity_type` enums used by the YAML.
ENTITY_TYPES: frozenset[str] = frozenset({
    "constitutional_executive",
    "digital_service_portal",
    "identity_authentication",
    "ministry",
    "agency",
    "law_enforcement",
    "customs",
    "tax_authority",
    "legislature",
    "judiciary",
    "independent_commission",
    "regulator",
    "state_owned_enterprise",
    "utility",
    "airport_operator",
    "infrastructure_corporation",
    "development_corporation",
})


@lru_cache(maxsize=1)
def load_registry(path: Optional[Path] = None) -> dict:
    """Load the registry JSON. Cached: parsed once per process.

    Pass an explicit `path` to override the canonical location (used
    by tests). Cache invalidation: callers that mutate the file in
    the same process must call `load_registry.cache_clear()`.
    """
    p = path or REGISTRY_PATH
    with p.open() as f:
        data = json.load(f)
    # Validate shape.
    if "entities" not in data:
        raise ValueError(f"registry at {p} missing 'entities' key")
    seen_ids: set[str] = set()
    seen_domains: set[str] = set()
    for ent in data["entities"]:
        eid = ent.get("entity_id")
        if not eid:
            raise ValueError(f"registry entity missing entity_id: {ent}")
        if eid in seen_ids:
            raise ValueError(f"duplicate entity_id in registry: {eid}")
        seen_ids.add(eid)
        d = ent.get("domain")
        if d:
            if d in seen_domains:
                raise ValueError(f"duplicate domain in registry: {d}")
            seen_domains.add(d)
        et = ent.get("entity_type")
        if et not in ENTITY_TYPES:
            raise ValueError(f"unknown entity_type for {eid}: {et!r}")
    return data


def _domain_of(url: str) -> Optional[str]:
    """Extract a normalised hostname from a URL. Handles bare hostnames
    ('presidency.gov.mv'), schemed URLs ('https://...'), and strips
    leading 'www.' so cosmetic differences don't escape registry match."""
    if not url:
        return None
    u = url.strip()
    if "://" not in u:
        u = "https://" + u
    try:
        host = urlparse(u).hostname or ""
    except ValueError:
        return None
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def entity_for_url(url: str) -> Optional[dict]:
    """Return the registry entity dict whose domain matches `url`, or
    None. Match rule: hostname equals a registered domain, OR is a
    subdomain of one (e.g. news.presidency.gov.mv → presidency)."""
    host = _domain_of(url)
    if not host:
        return None
    data = load_registry()
    for ent in data["entities"]:
        d = ent.get("domain")
        if not d:
            continue
        d_norm = d.lower()
        if host == d_norm or host.endswith("." + d_norm):
            return dict(ent)
    return None


def is_authoritative(url: str) -> bool:
    """True if the URL is on a registered authoritative domain."""
    return entity_for_url(url) is not None


def entity_by_id(entity_id: str) -> Optional[dict]:
    """Lookup an entity by its `entity_id` (e.g. 'presidency')."""
    data = load_registry()
    for ent in data["entities"]:
        if ent.get("entity_id") == entity_id:
            return dict(ent)
    return None


def all_entities() -> list[dict]:
    """Return all entities (defensive copy)."""
    return [dict(e) for e in load_registry()["entities"]]
