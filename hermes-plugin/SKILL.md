---
name: kahzaabu
description: Search and reason over the Maldives Presidency fact-checking archive (3,000+ press releases, 200+ curated fact-checks, 700+ manifesto promises with delivery status). Subject is President Mohamed Muizzu (street nickname "kahzaabu" — same person).
---

# kahzaabu — fact-check archive

You have 8 in-process tools for querying a curated SQLite archive of Maldives Presidency communications. **"Kahzaabu" and "Muizzu" refer to the same person** — translate freely between the two names.

## When to use which tool

**Default: call `kahzaabu_ask` for any natural-language question.** It's an internal agent loop with 8 DB tools + web_search and produces better answers than chaining the low-level tools yourself. Only call the low-level tools when you need a *specific* row by id, or to compose data the user explicitly asked you to compose (e.g. "make a CSV of all BROKEN DEADLINE entries").

| User asks… | Tool |
|---|---|
| Anything natural-language: "what's he up to", "what lies", "what about housing" | **`kahzaabu_ask`** ← almost always this |
| "show me fact-check #87" (specific id) | `kahzaabu_get_factcheck` |
| "show article 32675" (specific id) | `kahzaabu_get_article` |
| Pure stats: archive size, fact-check breakdown by category | `kahzaabu_stats` |
| Pure listing with filters (no synthesis): "list every BROKEN DEADLINE in 2025" | `kahzaabu_list_lies` |
| "what did the manifesto say about housing" (filterable) | `kahzaabu_manifesto` |
| "anything in the past N days" (raw list, no synthesis) | `kahzaabu_recent_activity` |
| User explicitly asks to refresh the archive | `kahzaabu_pipeline_run` (gated; check `kahzaabu_stats.freshness` first) |

## Session memory

`kahzaabu_ask` returns a `session_id`. **Pass it back unchanged on every follow-up turn** so the internal agent retains prior tool results and quotes — otherwise it re-fetches everything and the user pays twice. The `/kahzaabu` slash command auto-continues the most recent session; you should do the same when invoking the tool directly.

## Always-on: narrative-tricks analysis

Every answer from `kahzaabu_ask` that quotes press-release text ends with a `🎭 Narrative tricks observed` section flagging communication techniques (hero framing, manufactured momentum, goalpost shifting, empty markers of action, vague timeframes, etc.). Treat this as **part of the answer**, not metadata.

## Data freshness

If the user asks about *recent* events, call `kahzaabu_stats` first. The result's `freshness.is_stale` field tells you whether the data is older than the configured threshold (default 24h). If stale, say so in your answer and suggest `hermes kahzaabu update`. Do not call `kahzaabu_pipeline_run` yourself without explicit user approval — it costs money.

## Channel-routed messages

When this skill is reached via the messaging gateway (Telegram, WhatsApp, Slack, Discord), follow chat etiquette:

- **Keep answers under ~1,500 chars on first response.** Long answers get truncated by some platforms (Telegram caps at 4,096 chars; WhatsApp at 4,096; Slack splits at 40,000 but that's still rude). If the answer is necessarily long, lead with a 3-line TL;DR and offer to expand any section on request.
- **Skip the tool trace.** Web users have a `[Show tool trace ▾]` toggle; chat users don't. Don't paste tool-call lists or iteration counts unless the user explicitly asks "how did you get that".
- **Don't surface cost in chat.** The slash command's footer already shows it. Repeating it in the body is noise.
- **Markdown works on Telegram/Discord, partially on WhatsApp, fully in Slack.** Headings, bullets, **bold**, *italic*, `code` and tables all degrade gracefully. Don't rely on the agent renderer.
- **Keep the 🎭 Narrative tricks observed section** — it's part of the answer, not an appendix. Chat users get value from spotting framing on the small screen too.

## Citation discipline

Cite article ids inline as `[NNNNN]`, fact_check ids as `[FC #NN]`, promise ids as `[promise NN]`. The user expects citations on factual claims. Don't fabricate ids — if the underlying tool didn't return one, say so plainly rather than guess.

## Web search budget

`kahzaabu_ask` accepts `enable_web` (default true). If the user's question is clearly archive-only ("how many fact-checks", "show me the broken deadlines for 2025") pass `enable_web=false` to skip the web_search tool and save ~$0.10 per call. Use web only when the question genuinely needs external corroboration the archive can't provide.
