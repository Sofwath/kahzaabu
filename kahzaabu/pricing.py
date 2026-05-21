# SPDX-License-Identifier: Apache-2.0
"""Centralised pricing + model-id registry.

Single source of truth for:
    - Anthropic model IDs used by the pipeline
    - Per-million-token pricing for cost calculation
    - The `cost()` helper that every stage uses to compute LLM spend

Before this module, PRICE_IN_PER_M / PRICE_OUT_PER_M were duplicated
across nine stage files and model IDs were hardcoded in seventeen
places. A price change or model upgrade required editing every one in
lockstep — easy to miss.

Now: add or change a row in MODELS and every consumer picks it up.
The frozen dataclass makes accidental mutation a type error.

Use:

    from kahzaabu.pricing import MODELS, cost

    res = client.messages.create(model=MODELS["sonnet"].id, ...)
    spend = cost("sonnet", tokens_in=t_in, tokens_out=t_out)

Aliases match the spelling each upstream organisation uses internally
(Anthropic refers to "Sonnet 4.6", "Haiku 4.5") — short, stable across
minor revisions, decoupled from full model IDs that change with each
point release.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Mapping


@dataclass(frozen=True)
class Model:
    """Pricing + identity for a single LLM. Frozen so accidental
    re-assignment is a TypeError, not a silent state bug.

    (No `slots=True` — that's Python 3.10+ and we support 3.8+. The
    memory cost of 3 instances without __slots__ is negligible.)
    """
    id: str
    in_per_m: float           # USD per 1M input tokens
    out_per_m: float          # USD per 1M output tokens
    web_search_per_call: float = 0.0  # Anthropic web_search server-tool surcharge


# ───────────────────────────────────────────────────────────────────
# Model registry
#
# Keys are short stable aliases used throughout kahzaabu. Add a new
# entry when a new model becomes pipeline-default; never remove an
# entry that fact_check_evidence / reproducibility manifests
# reference, because old rows carry the alias forever.
# ───────────────────────────────────────────────────────────────────

MODELS: Final[Mapping[str, Model]] = {
    "sonnet":   Model(id="claude-sonnet-4-6", in_per_m=3.0, out_per_m=15.0),
    "haiku":    Model(id="claude-haiku-4-5",  in_per_m=1.0, out_per_m=5.0),
    # web_search surcharge applies whenever the verifier or inspector
    # uses Anthropic's `web_search_20250305` server tool.
    "haiku-ws": Model(id="claude-haiku-4-5",  in_per_m=1.0, out_per_m=5.0,
                       web_search_per_call=0.01),
}


# ───────────────────────────────────────────────────────────────────
# Cost helper
# ───────────────────────────────────────────────────────────────────

def cost(alias: str, *,
          tokens_in: int = 0,
          tokens_out: int = 0,
          web_searches: int = 0) -> float:
    """Compute USD cost for a single call (or aggregate run).

    Args:
        alias:        key into MODELS (e.g. "sonnet", "haiku")
        tokens_in:    total input tokens (across an entire run, if aggregating)
        tokens_out:   total output tokens
        web_searches: Anthropic web_search calls invoked

    Raises:
        KeyError: if `alias` isn't registered.
    """
    m = MODELS[alias]
    return (tokens_in / 1_000_000.0 * m.in_per_m
            + tokens_out / 1_000_000.0 * m.out_per_m
            + web_searches * m.web_search_per_call)


def model_id(alias: str) -> str:
    """Resolve an alias to the canonical Anthropic model ID. Centralises
    the one-place-to-change-when-Anthropic-renames decision."""
    return MODELS[alias].id
