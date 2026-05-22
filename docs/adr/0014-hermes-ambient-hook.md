# ADR 0014 — Hermes ambient pre_llm_call hook

**Status**: Accepted (2026-05-22)

## Context

The kahzaabu hermes plugin (ADR 0001, ADR 0013) initially exposed 9
agent tools that an LLM could call when it decided kahzaabu was
relevant. This made kahzaabu **a tool that gets called**, not a
service that **pays attention**.

In practice, that meant:
- A user discussing the President in any hermes chat (terminal,
  Telegram, Slack, gateway) had to remember `/kahzaabu` to surface
  context.
- The agent's grounding for Maldivian-politics topics depended on
  the agent's own judgment to call `kahzaabu_ask` — variable, often
  missed.
- Cross-references the project went to substantial effort to wire
  on the web side (constitution / authoritative-entity registry —
  ADR 0011, ADR 0012) had no analogue in the agent surface.

The user's framing: *"make kahzaabu more agentic and smart. it
should pay attention."*

## Decision

**Install a `pre_llm_call` hook in the hermes plugin that
auto-injects context into ANY chat turn that mentions a
Maldivian-politics topic.**

The hook is a two-stage pipeline:

### Stage 1 — Prefilter (the hot path)

Runs on EVERY user turn across hermes. Must be near-instant —
target sub-100µs per call.

Three match paths:

1. **STRONG keyword match** — high-precision compiled regex over
   Maldivian-politics stems: `muizzu`, `kahzaabu`, `maldiv`,
   `majlis`, `JSC`, `atoll`, `raajje`, `gulhifalhu`, `hulhumale`,
   `presidency.gov`. Always fires + marks the session "hot".

2. **STICKY follow-up match** — a previously-hot session within
   the 30-minute TTL window matches against a broader pattern
   (baseline keywords ∪ corpus-derived `fact_checks.topic` tokens).
   Loose follow-ups like *"what about housing?"* still match
   because we already know the conversation is on-topic.

3. **No match** — the hook returns immediately.

A previous "co-occurrence" path (generic-term + Maldivian-anchor
pair) was removed: every token in the anchor regex was also a
strong keyword, so the strong path always preempted it — dead code.

### Stage 2 — BM25 retrieval (the warm path)

Only runs on a prefilter match.

- Opens the local SQLite DB.
- BM25 lookup against `fact_checks_fts` (limit 3) and
  `constitution_articles_fts` (limit 2) — both indexes built by
  the corresponding init schemas.
- Formats hits as ≤1.5 KB of context.
- Returns `{"context": str}` — hermes injects into the user
  message (NOT the system prompt) so the prompt-cache prefix is
  preserved across turns.

The context block ALWAYS ends with a reminder that kahzaabu is a
reference implementation, not authoritative. Pinned by tests so
an agent can't quote the verdict as fact.

## Alternatives considered

- **Convert `kahzaabu_ask` into a hermes "routine"**. Routines are
  multi-step persistent goals; `kahzaabu_ask` is synchronous-per-
  query by design. Scheduled fact-checking is a separate use case
  best handled by `hermes cron` + the existing skill, not by
  changing the plugin.
- **Use a hermes memory plugin** (honcho / hindsight / retaindb).
  Their value is cross-session user modelling. kahzaabu's session
  memory is minimal and cheap; integrating would add latency and
  cost for marginal benefit. Defer until users ask for cross-
  session recall.
- **LLM-based intent detection** instead of regex prefilter.
  Sub-millisecond regex vs ~300 ms LLM call on every hermes turn.
  The regex prefilter is correct for ~95% of cases at near-zero
  latency; the cost-benefit doesn't justify the LLM call.
- **Inject into the system prompt** instead of user message.
  Would invalidate the prompt cache on every turn (hermes' big
  perf optimisation). Per the hermes `invoke_hook` contract,
  context goes into the user message specifically to preserve
  the cache prefix.

## Consequences

### Positive

- **Ambient grounding**. ANY user mentioning Muizzu, JSC,
  Constitution, etc. in any hermes chat gets kahzaabu's fact-check
  + constitution cross-reference for that turn — no `/kahzaabu`
  required.
- **Survives across platforms**. The hook is gateway-transparent;
  works identically in terminal, Telegram DMs, Slack threads,
  Discord channels.
- **Cross-process sticky state**. Multi-process hermes deployments
  share the `ambient_hot_sessions` table; a strong match on CLI
  marks the session hot for follow-ups arriving on Telegram.

### Negative — Operational complexity

- **Performance budget**. Non-match path < 1 ms (regex only).
  Match path typically < 50 ms (1 SQLite open + 2 BM25 queries).
  Regression-guarded.
- **Defensive everywhere**. Missing DB / search throws / corrupt
  DB → hook returns None. A misbehaving hook can never break a
  hermes turn.
- **One-time setup hint**. Plugin enabled + no DB → STRONG match
  injects a one-time "run `hermes kahzaabu setup`" hint into the
  user's chat (not just the operator log) so misconfiguration is
  noticed without flooding.

### Negative — Configuration surface

The decision added five env vars (all documented in
`hermes-plugin/plugin.yaml` `optional_env`):

| Env var | Effect |
|---|---|
| `KAHZAABU_AMBIENT_DISABLE=1` | Kill switch; plugin keeps tools + slash + CLI |
| `KAHZAABU_AMBIENT_PLATFORMS=cli,telegram` | Per-platform allowlist |
| `KAHZAABU_FOLLOWUP_TTL_SECONDS=N` | Vocab-cache TTL (default 21600 = 6 h) |
| `KAHZAABU_DB=path` | DB-path override |
| `KAHZAABU_ALLOW_PIPELINE=1` | Pre-existing — pipeline tool gate |

## Persistence

Slice 14 added `ambient_hot_sessions(session_id PRIMARY KEY,
hot_until REAL)` in `kahzaabu/claims_db.py`. Single tiny table;
write on every strong/sticky match, lazy GC on read. Scoped
per-user-install (see schema doc) — multi-tenant deployments are
explicit non-goals per ADR 0013.

## Regression guards

Single test file pins the whole stack:

- `tests/test_ambient_hook.py` (~40 tests):
  - prefilter relevance (10+ should-fire / 10+ should-not-fire)
  - 1000 prefilter calls in <100 ms (hot-path budget)
  - strong-keyword invariants — refactor that strips `muizzu` /
    `JSC` / `maldiv` etc. fails with named-token errors
  - sticky-session TTL eviction
  - LRU cap on the in-memory hot-session dict
  - per-platform allowlist (set / unset / case-insensitive)
  - one-time DB-missing hint injection (strong only — sticky stays
    silent)
  - cross-process hot-session persistence via SQLite
  - corpus-derived follow-up vocab from `fact_checks.topic`
  - TTL refresh rebuild
  - hook_status() shape for `hermes kahzaabu doctor` integration
  - manifest pins (`pre_llm_call` declared; all env vars listed)

## Disable

Set `KAHZAABU_AMBIENT_DISABLE=1` in `~/.hermes/.env`. The plugin
keeps the 9 tools + slash command + CLI; only the hook short-
circuits at registration time.
