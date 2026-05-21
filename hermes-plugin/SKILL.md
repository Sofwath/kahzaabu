---
name: kahzaabu
description: Search and reason over the Maldives Presidency fact-checking archive (3,000+ press releases, 200+ curated fact-checks, 700+ manifesto promises with delivery status). Subject is President Mohamed Muizzu (street nickname "kahzaabu" — same person).
---

# kahzaabu — fact-check archive

You have 8 in-process tools for querying a curated SQLite archive of Maldives Presidency communications. **"Kahzaabu" and "Muizzu" refer to the same person** — translate freely between the two names.

## When to use which tool

| User asks… | Start with |
|---|---|
| "what's he up to" / "this week" / "recent" | `kahzaabu_recent_activity` then `kahzaabu_get_article` |
| "what lies / contradictions" | `kahzaabu_list_lies` then `kahzaabu_get_factcheck` |
| "what did he promise about X" | `kahzaabu_manifesto` |
| any specific subject / open-ended | **`kahzaabu_ask`** — it runs a multi-turn internal agent loop |
| archive size / freshness | `kahzaabu_stats` |
| trigger fresh data scrape | `kahzaabu_pipeline_run` (gated; check first if allowed) |

## Critical: prefer `kahzaabu_ask` for open questions

`kahzaabu_ask` is itself an agentic loop with 8 internal DB tools + web_search. For anything beyond a single-table lookup, it produces a better answer than chaining the low-level tools yourself. It also returns a **session_id** — pass it back on follow-up questions to retain context.

## Always-on: narrative-tricks analysis

Every answer from `kahzaabu_ask` that quotes press-release text ends with a `🎭 Narrative tricks observed` section flagging communication techniques (hero framing, manufactured momentum, goalpost shifting, empty markers of action, vague timeframes, etc.). Treat this as **part of the answer**, not metadata.

## Data freshness

If the user asks about *recent* events, call `kahzaabu_stats` first. The result's `freshness.is_stale` field tells you whether the data is older than the configured threshold (default 24h). If stale, say so in your answer and suggest `hermes kahzaabu update`. Do not call `kahzaabu_pipeline_run` yourself without explicit user approval — it costs money.

## Channel-routed messages

When this skill is reached via the messaging gateway (Telegram, WhatsApp, Slack, Discord), the user is in a chat window — keep answers brief and Markdown-friendly. Long article quotes are fine; deep tool traces are not. The web UI at `hermes kahzaabu web` is better for exploration.

## Citation discipline

Cite article ids inline as `[NNNNN]`, fact_check ids as `[FC #NN]`, promise ids as `[promise NN]`. The user expects citations on factual claims.
