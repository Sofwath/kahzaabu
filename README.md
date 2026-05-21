# Kahzaabu

> Automated fact-checking archive for the Maldives Presidency.
> *"Kahzaabu"* (ކަޒާބު) is Dhivehi for *falsehood* — and the street nickname for Mohamed Muizzu.
> The two names refer to the same person; the project treats them as synonyms.

This is a **research / educational project**: it scrapes public press releases from `presidency.gov.mv`, extracts factual claims with an LLM, curates contradictions across time, verifies them against the open web, and stores the result in a queryable SQLite archive. A native [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin exposes the archive to a chat agent so you can ask questions in plain English (or through Telegram / WhatsApp / Slack via the hermes gateway).

**This is not journalism.** It is an automated pipeline that surfaces patterns. Every claim links back to the original press release on `presidency.gov.mv`. Read sources before drawing conclusions.

---

## Table of contents

1. [What it does](#what-it-does)
2. [Quick start (two paths)](#quick-start)
3. [Architecture in one diagram](#architecture)
4. [The pipeline, stage by stage](#the-pipeline)
5. [Data model](#data-model)
6. [The agentic Q&A loop](#the-agentic-qa-loop)
7. [The narrative-tricks layer](#the-narrative-tricks-layer)
8. [Hermes plugin: how it's wired](#hermes-plugin)
9. [Web UI tour](#web-ui-tour)
10. [TUI tour](#tui-tour)
11. [Costs](#costs)
12. [Known issues & TODOs](#known-issues--todos)
13. [Security & ethics](#security--ethics)

---

## What it does

Today (May 2026), the archive holds:

| Item | Count |
|---|---|
| Muizzu-era press releases (EN, 2023-11-17 onwards) | ~3,099 |
| Extracted factual claims | ~8,954 |
| Curated fact-checks (published) | 218 |
| Web-evidence rows backing fact-checks | 304 |
| 2023 campaign manifesto promises (tracked) | 717 |
| EN ↔ DV translation diff rows | varies |

Fact-checks are classified into one of six categories:

| Category | Meaning |
|---|---|
| **LIE** | Statement is provably false against a primary source |
| **MISLEADING** | Technically true but framed to deceive |
| **BROKEN DEADLINE** | A specific date was given and missed |
| **CREDIT THEFT** | Claimed credit for an inherited project |
| **SHIFTING NUMBERS** | The same metric reported with different values |
| **CONTRADICTION** | Two statements that cannot both be true |

A separate layer — the [narrative-tricks analysis](#the-narrative-tricks-layer) — sits on top of every article-derived answer and surfaces *framing* techniques (hero framing, manufactured momentum, vague timeframes, etc.) even when no factual error is present.

---

## Quick start

### Path A — standalone (CLI + web)

```bash
git clone <this repo> kahzaabu && cd kahzaabu
python3 -m venv .venv
.venv/bin/pip install -e ".[all]"       # core + web + TUI + MCP server
# or pick extras: .[web]  .[tui]  .[mcp]  — bare `-e .` gets pipeline only
export ANTHROPIC_API_KEY=sk-ant-...

.venv/bin/kahzaabu pipeline --budget 1.00   # one full cycle
.venv/bin/kahzaabu web --port 8765           # open http://127.0.0.1:8765
.venv/bin/kahzaabu tui                       # interactive TUI
.venv/bin/kahzaabu ask "What's Muizzu been doing this month?"
```

### Path B — as a Hermes plugin (recommended)

If you have [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed, kahzaabu integrates natively. **The plugin source lives in `hermes-plugin/` inside this repo** — install symlinks it into hermes' plugins dir so edits are live, no copy step.

```bash
# One-time install (symlinks hermes-plugin/ -> ~/.hermes/hermes-agent/plugins/kahzaabu)
./scripts/install-hermes-plugin.sh

hermes kahzaabu setup        # interactive: API key, daily budget, freshness threshold
hermes kahzaabu doctor       # health check (all should be ✅)

# Use it — three surfaces
hermes kahzaabu status                          # archive counts + freshness
hermes kahzaabu ask "what did he promise about housing?"
hermes kahzaabu ask --continue "and the deadlines on those?"   # ↑ same session
hermes kahzaabu update --budget 0.50            # run pipeline
hermes kahzaabu web                             # start the web UI

# Inside any hermes chat session (terminal OR gateway-routed):
#   /kahzaabu what is he up to this week?
#   /kahzaabu and what about housing?           # ↑ auto-continues the session

# Wire messaging channels (Telegram, WhatsApp, Slack, Discord)
hermes gateway setup       # one-time
hermes gateway install     # install as systemd / launchd service
hermes gateway start       # now messages to your bot route to kahzaabu tools
```

**Three things to know about the integration:**

1. **`/kahzaabu` slash command** is available in every hermes chat — terminal, Telegram, WhatsApp, Slack, Discord. Auto-continues the most-recent session (within 24h), so follow-ups don't lose context.
2. **`hermes kahzaabu ask --continue`** mirrors hermes' own `--continue` UX for the CLI — picks up the previous session_id from the qna_sessions table.
3. **LLM-provider inheritance**: the narrative-tricks pass routes through hermes' configured provider (whatever you picked in `hermes setup model`). Switch hermes from Anthropic to OpenAI to OpenRouter — the secondary pass follows. (Main agentic loop still uses Anthropic — it needs multi-turn tool-use that `ctx.llm.complete()` doesn't yet support.)

The hermes plugin source lives at `hermes-plugin/` in this repo and is symlinked into `~/.hermes/hermes-agent/plugins/kahzaabu/` by the install script. It **does not vendor code** — it imports the package from this dev tree. See [hermes plugin section](#hermes-plugin) for details.

---

## Architecture

```
                       ┌─────────────────────────────────┐
                       │  presidency.gov.mv (EN + DV)    │
                       └──────────────┬──────────────────┘
                                      │ scrape (incremental, 12h cycle)
                                      ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │                     SQLite (data/kahzaabu.db, WAL)                   │
   │                                                                       │
   │   articles ── claims    fact_checks ── fact_check_evidence           │
   │       │         │           │  (source_article_ids JSON array → articles.id)
   │       │                                                              │
   │       ├── article_fact_cards (per-article inspector output)          │
   │       └── dv_en_inconsistencies (translation diffs)                  │
   │                                                                       │
   │   manifesto_promises ── manifesto_evidence (cross-ref)               │
   │                                                                       │
   │   qna_sessions (multi-turn agent memory)  ── scrape_runs ── auth     │
   └──────────────────────────────────────────────────────────────────────┘
                                      │
                ┌─────────────────────┼─────────────────────┐
                ▼                     ▼                     ▼
        ┌────────────┐         ┌────────────┐        ┌────────────┐
        │  CLI       │         │  FastAPI   │        │  Hermes    │
        │  + TUI     │         │  web UI    │        │  plugin    │
        │            │         │  :8765     │        │            │
        └────────────┘         └────────────┘        └─────┬──────┘
                                                          │
                                                  ┌───────┴──────────────┐
                                                  ▼                       ▼
                                          ┌──────────────┐       ┌───────────────┐
                                          │  agent loop  │       │ hermes gateway│
                                          │ (kahzaabu_   │       │ Telegram /    │
                                          │  ask + 8     │       │ WhatsApp /    │
                                          │  tools +     │       │ Slack /       │
                                          │  web_search) │       │ Discord       │
                                          └──────────────┘       └───────────────┘
```

The DB is the source of truth. Every consumer is read-only over it except the pipeline, which appends.

---

## The pipeline

`kahzaabu pipeline` runs **six stages** in sequence. Each stage is idempotent — re-runnable, with budgets, with cost tracking.

| # | Stage | What it does | LLM cost per item |
|---|---|---|---|
| 1 | **scrape** | `scraper.py` — incremental crawl of `presidency.gov.mv/news/{press_release,speech,vp_speech}` (EN + DV). HTTP only. | $0 |
| 2 | **extract** | `extractor.py` — Sonnet reads each article, returns a list of `{type, text, value, unit, target_date, location, persons}` claim records. | ~$0.005-0.010 |
| 3 | **inspect** | `inspector.py` — generates a per-article *fact card* (summary, history-check, severity, viz spec). Stored in `article_fact_cards`. | ~$0.015 |
| 4 | **curate** | `curator.py` — Sonnet sees *all claims on the same topic across time* and flags contradictions / broken deadlines / credit theft. Inserts `fact_checks` rows. | ~$0.05/topic |
| 5 | **verify** | `verifier.py` — Haiku does Anthropic web_search for each fact-check; agrees/disagrees evidence saved to `fact_check_evidence`. Bounded — only the high-severity ones. | ~$0.03 + $0.01/search |
| 6 | **dv-compare** | `dv_compare.py` — Sonnet reads paired EN+DV bodies, flags numeric / omission / softening differences. Inserts `dv_en_inconsistencies`. | ~$0.08/pair |

Defaults: cycle runs every **12h** via launchd (`scripts/com.kahzaabu.pipeline.plist`). Budget cap defaults to **$1.00 per cycle**. Total project spend to-date: ~$58.

A separate `manifesto-extract` + `manifesto-crossref` flow extracts ~717 promises from the 2023 campaign PDF (Dhivehi, 51 MB) and cross-references each against the archive to assign a delivery status.

---

## Data model

> **Editor protocol** — when changing this block, derive column lists from
> `sqlite3 data/kahzaabu.db ".schema"` rather than memory. Each entry below
> uses the format `tablename -- description` followed by indented `-- cols: a, b, c`
> lines. The `cols:` convention is load-bearing: `tests/test_readme_schema_drift.py`
> parses it and fails if any documented column is absent from the live
> schema. Run `./scripts/test.sh` before committing.

The interesting tables:

```sql
articles            -- PK (id, language). EN ↔ DV pairs via shared id + paired_id.
                    -- cols: title, category, body_text, body_html, published_date,
                    --       reference, scraped_at, raw_page_html
claims              -- extracted from article body_text by the LLM.
                    -- cols: article_id+language (FK), type, subject, value,
                    --       deadline, actor_credited, quote, extraction_run_id
fact_checks         -- curated contradictions / broken deadlines / etc.
                    -- cols: category, claim_date, claim, what_actually_happened,
                    --       topic, confidence, source_article_ids (JSON array
                    --       of articles.id), evidence_quotes (JSON), published,
                    --       public_summary, fingerprint (dedupe key)
fact_check_evidence -- web-search hits backing each fact-check.
                    -- cols: fact_check_id (FK), url, title, snippet, relevance
                    --       ('confirms'|'contradicts'|'context'|'unclear'|
                    --       'not_found'), summary, retrieved_at
article_fact_cards  -- per-article inspector output.
                    -- cols: article_id, language, summary, key_claims_json,
                    --       history_check, severity, visualization_spec_json,
                    --       web_evidence_json, cost_usd, inspection_run_id,
                    --       published
dv_en_inconsistencies -- EN/DV translation diffs.
                    -- cols: en_article_id, dv_article_id (FKs), severity,
                    --       category, en_quote, dv_quote, dv_translation_to_en
manifesto_promises  -- 2023 campaign promises with delivery tracking.
                    -- cols: section, promise_text_dv, promise_text_en,
                    --       category, subject, target_value, deadline_stated,
                    --       delivery_status, delivery_evidence_json (JSON:
                    --       linked article_ids + fact_check_ids + notes),
                    --       chunk_index, published
qna_sessions        -- agentic-ask multi-turn memory.
                    -- cols: id (uuid), messages_json (full message history),
                    --       total_cost_usd, n_turns, created_at, last_used_at
constitution_articles -- parsed Constitution of the Republic of Maldives.
                    -- cols: article_no, chapter, title, body, source_version,
                    --       imported_at
scrape_runs         -- audit log of pipeline cycles.
                    -- cols: category_id, language, started_at, finished_at,
                    --       pages_scraped, articles_scraped, articles_new,
                    --       status, resume_page, error_message
web_users           -- admin/editor accounts for the web UI's publish workflow.
                    -- cols: username, password_hash, role, created_at
```

**Article ↔ fact-check linkage** is via the JSON column `fact_checks.source_article_ids` — a list of `articles.id` values. Use SQLite's `json_each()` to traverse it (or `LIKE` on the serialized form as a fallback).

Migrations are idempotent ALTER-COLUMN style in `claims_db.py:init_claims_schema()`. WAL mode is on; `check_same_thread=False` for the FastAPI threadpool.

---

## The agentic Q&A loop

`kahzaabu/qna_agentic.py:ask_agentic()` is the heart of the Q&A experience. It is **itself** an agent loop — Sonnet calls *internal* tools to satisfy a question.

```
user question
    │
    ▼
Sonnet 4.6 + tools = [
    archive_stats, search_articles, get_article,
    search_factchecks, get_factcheck,
    search_manifesto, get_promise,
    list_recent,
    web_search (Anthropic server tool)
]
    │
    ▼
loop up to max_iterations (default 7):
    if Sonnet returns tool_use:
        execute, append result, continue
    else:
        capture final_text, break
    │
    ▼
guarantee-pass (Haiku 4.5, ~$0.01):
    if final_text quotes article text BUT lacks "🎭 Narrative tricks observed":
        ask Haiku to append the section using the catalog
    │
    ▼
return {answer, session_id, n_iterations, cost_usd, tool_trace, web_searches}
```

Session memory lives in the `qna_sessions` table. Pass the returned `session_id` back to continue a conversation — the loop will re-load all prior tool results and turns.

Cost per question:
- Simple "how many fact-checks?" (data-only) — **~$0.025**
- Article-heavy ("what did he say last week?") — **~$0.05-0.10**
- Open-ended with web_search — **~$0.10-0.30**

Daily budget cap (default $5) is enforced at the top of `ask_agentic`.

---

## The narrative-tricks layer

A 16-technique catalog (hero framing, manufactured momentum, goalpost shifting, empty markers of action, vague timeframes, etc.) is appended in the system prompt with anti-over-claiming rules:

- Cap of 5 items per answer
- Every flag must include the verbatim quote
- Ceremonial language ("expressed gratitude") is explicitly NOT a trick
- Hedging language ("could be seen as", "this might imply") is forbidden

The section is enforced via a **guarantee-pass**: if the agent quotes article text but skips the section, a follow-up Haiku call appends it. Cost: ~$0.01 per article-touching question.

Pure-data questions (e.g. "how many fact-checks?") correctly **omit** the section — the guarantee-pass is gated on `tool_trace` containing article-content tools.

See `qna_agentic.py:SYSTEM_PROMPT` for the catalog and `_ARTICLE_TOOLS` for the gating set.

---

## Hermes plugin

The plugin source lives at `hermes-plugin/` in this repo. The install script (`scripts/install-hermes-plugin.sh`) symlinks it into `~/.hermes/hermes-agent/plugins/kahzaabu/` so hermes can find it. Edits in `hermes-plugin/` are live — no copy/sync step.

Layout:

```
hermes-plugin/
├── plugin.yaml    Manifest: name, version, provides_tools, platforms
├── __init__.py    register(ctx) — entry point. Three jobs:
│                    1. Hydrate ~/.hermes/.env into os.environ
│                    2. Ensure kahzaabu is importable (self-heal .pth)
│                    3. Register 8 tools + `hermes kahzaabu` CLI
├── tools.py       8 handler functions wrapping qna_agentic / claims_db
├── cli.py         argparse setup for `hermes kahzaabu {setup,status,…}`
├── SKILL.md       Agent-facing guidance: when to use which tool
└── README.md      Plugin-source README (design choices, bootstrap layers)
```

**Design choices to know**:

- **Imports, doesn't vendor.** Plugin imports the canonical `kahzaabu` package from this dev tree. Editing code here updates the plugin immediately.
- **Path discovery is robust.** `kahzaabu_home()` derives the dev tree from `Path(kahzaabu.__file__).resolve().parents[1]`. No hardcoded paths anywhere.
- **`.pth` self-heal.** Hermes' venv has no `pip`, so the plugin writes `~/.hermes/hermes-agent/venv/lib/python3.11/site-packages/kahzaabu.pth` on first run. If hermes ever recreates its venv, the next `hermes kahzaabu *` invocation rewrites it.
- **Tools are in-process.** Unlike the previous MCP-over-stdio design, hermes calls plugin tools directly — no subprocess, ~5-10× faster per call.
- **`update` and `web` shell out.** Both need scikit-learn / FastAPI / etc. that don't live in hermes' lean venv, so they exec `<dev>/.venv/bin/kahzaabu pipeline|web`. `doctor` checks this.

The 8 tools exposed to the agent:

| Tool | What it does |
|---|---|
| `kahzaabu_stats` | Counts + freshness — call first for "recent" questions |
| **`kahzaabu_ask`** | **Run the full agentic loop — preferred for any natural-language question** |
| `kahzaabu_list_lies` | List fact-checks with filters |
| `kahzaabu_get_factcheck` | One fact-check + web evidence + linked source articles |
| `kahzaabu_manifesto` | 2023 promises with delivery status |
| `kahzaabu_get_article` | One article with claims + linked fact-checks |
| `kahzaabu_recent_activity` | Last N days of articles |
| `kahzaabu_pipeline_run` | Trigger pipeline (gated by `KAHZAABU_MCP_ALLOW_PIPELINE=1`) |

**Three integration surfaces share one Q&A engine:**

- **Agent tool call**: `hermes chat -q "..."` → agent invokes `kahzaabu_ask` and gets back `{answer, session_id, cost_usd, tool_trace, web_searches}`.
- **CLI subcommand**: `hermes kahzaabu ask [--continue] [--no-web] [--session ID] "..."` — direct human use.
- **Slash command**: `/kahzaabu <question>` works inside any hermes session, including chats routed through the messaging gateway. Auto-continues the most-recent session.

All three call the same `kahzaabu/qna_agentic.py:ask_agentic()` function, so session memory, the narrative-tricks layer, daily-budget caps, and cost accounting behave identically across surfaces. Sessions persist in the `qna_sessions` table and survive process restarts; the `--continue` and slash auto-continue affordances both use `claims_db.most_recent_session_id()` to find the latest one within a 24h window.

**LLM-provider inheritance**: the secondary narrative-tricks pass calls `ctx.llm.complete()` when invoked from the plugin (so it follows `hermes setup model`), and falls back to Anthropic Haiku 4.5 when called from the standalone CLI / TUI / web. The main agentic loop always uses Anthropic Sonnet — `ctx.llm.complete()` doesn't yet support multi-turn tool-use.

---

## Web UI tour

`kahzaabu web --port 8765` (or `hermes kahzaabu web`) serves:

| Page | What |
|---|---|
| `/` | Dashboard: 5 stat cards + 6 charts (categories, topics, claims/month, articles/month, manifesto-status, stacked-by-month) + freshness banner |
| `/browse` | Article browser with filters |
| `/lies` | Fact-check browser with category/severity filters |
| `/article/{id}` | One article + claims + linked fact-checks + fact-card chart |
| `/compare` | EN ↔ DV translation inconsistencies |
| `/compare/{id}` | Side-by-side EN/DV with the flagged region highlighted |
| `/manifesto` | 2023 promises with delivery status |
| `/manifesto/{id}` | Per-promise detail + supporting articles |
| `/ask` | The agentic Q&A interface (sessions, web toggle, tool-trace) |
| `/methodology` | How the pipeline works (public-facing) |
| `/corrections` | Public report-a-correction form |
| `/admin/*` | Login-gated: publish queue, run pipeline, manage users |

Auth: session-cookie via `itsdangerous.URLSafeTimedSerializer`; passwords bcrypt-hashed. Rate-limited via `slowapi`. Public mode (`KAHZAABU_PUBLIC_MODE=1`) gates fact-check visibility to `published=1` for anonymous viewers.

---

## TUI tour

`kahzaabu tui` (or `python -m kahzaabu.tui`) is a Textual-based interactive terminal. Slash commands:

| Command | What |
|---|---|
| `/ask <question>` | Multi-turn agentic ask (session preserved) |
| `/stats` | Archive counts + freshness |
| `/lies [category]` | List fact-checks |
| `/article <id>` | Show an article |
| `/refresh` | Re-query freshness |
| `/help` | Show all commands |
| `/quit` | Exit |

A startup banner shows freshness; if stale, it prompts to run `kahzaabu update`.

---

## Costs

Total spend to date: ~$58. Typical ongoing costs:

| Activity | Per item | Per 12h cycle (typical) |
|---|---|---|
| Scrape (HTTP) | $0 | $0 |
| Extract claims | $0.005-0.010 | ~$0.05 |
| Inspect (fact-card) | $0.015 | ~$0.15 |
| Curate (cross-time) | $0.05/topic | ~$0.10 |
| Verify (web-search) | $0.03 + $0.01/hit | ~$0.20 |
| DV/EN compare | $0.08/pair | ~$0.40 |
| **Total per 12h cycle** | | **~$0.90** |
| `/api/ask` question | $0.025 (data) → $0.30 (web) | n/a |

Daily caps:
- Pipeline: `--budget 1.00` (CLI flag)
- Q&A (per process): `KAHZAABU_DAILY_BUDGET_USD=5.00`
- Public web Q&A (anon): hard cap returns 503 once daily spend exceeds env var

---

## Known issues & TODOs

### Known issues

1. **Pipeline via MCP silently skips scrape stage.** When the agent calls `kahzaabu_pipeline_run`, the scrape sub-stage runs but produces no `scrape_runs` entries. Direct CLI (`kahzaabu pipeline`) works correctly. The MCP-path bug existed in the legacy MCP server too — the native plugin version may or may not still have it; not retested.
2. **`hermes default model` shows `anthropic/anthropic/...` in doctor.** Pre-existing cosmetic bug in `_hermes_provider()` formatting — concatenates provider with a default that already includes the provider prefix.
3. **launchd plist still in use.** Migration to `hermes cron` is documented in `hermes kahzaabu setup` but not executed. Both can run side-by-side; once you're confident, `launchctl unload ~/Library/LaunchAgents/com.kahzaabu.pipeline.plist`.

### Recently fixed

- ~~**Four plugin handlers used a hallucinated schema.**~~ `kahzaabu_list_lies`, `kahzaabu_get_factcheck`, `kahzaabu_get_article` were querying columns that don't exist (`title`/`severity`/`summary`) and joining a table that doesn't exist (`fact_check_claims`). Now rewritten against the real schema — fact-check ↔ article linkage uses the JSON `source_article_ids` column.

### TODOs

| Priority | Item |
|---|---|
| 🔴 High | **Public VPS deploy.** Caddy + systemd templates in `scripts/`. Methodology page, robots.txt, rate-limits done. Needs: domain, server, DB sync strategy (push from laptop vs. run pipeline on server). |
| 🟡 Medium | **Viber channel.** Hermes doesn't support Viber. Would require a custom `ctx.register_platform(...)` adapter — 3-5 days. Out of scope unless Maldives-market demand justifies. |
| ~~🟡 Medium~~ ✅ done | ~~**Migrate guarantee-pass to `ctx.llm`.**~~ Shipped: narrative-tricks pass now uses `ctx.llm.complete()` inside the plugin (anthropic fallback for non-plugin paths). Main loop still uses anthropic — needs tool-use. |
| 🟡 Medium | **Self-improver loop.** A hermes skill at `~/.hermes/skills/kahzaabu/kahzaabu-self-improver/` already exists. Has produced `test_claims_db.py` with 17 unit tests. Pending: branch merge, additional iterations. |
| 🟢 Low | **Replace launchd with `hermes cron`** (see Known issues #3). |
| ~~🟢 Low~~ ✅ partial | ~~**Per-tenant LLM selection.**~~ Secondary tricks pass now follows hermes' provider config. Main loop still hard-coded to Anthropic — would need a tool-use-capable host-LLM facade. |
| 🟢 Low | **Fix doctor's `anthropic/anthropic/...` cosmetic bug.** Strip the provider prefix from `model.default` before formatting. |
| 🟢 Low | **Compare-presidents page.** Would need historical pre-Muizzu data. Out of scope but the schema supports it. |
| 🟢 Low | **RSS/Atom feed of new fact-checks** for public consumers. |
| 🟢 Low | **One pre-existing scrape-stage MCP bug investigation** (see Known issues #1). Good first target for the self-improver. |

---

## Security & ethics

- The corpus is **already public** at `presidency.gov.mv`. No leaks, no inside sources.
- Every fact-check links back to the original press release URL.
- "Report a correction" form on `/corrections` creates an admin queue item.
- Public-mode (`KAHZAABU_PUBLIC_MODE=1`) shows only `published=1` fact-checks. Unpublished items stay admin-only until reviewed.
- Pipeline LLM calls are budget-capped; daily Q&A spend is capped; anonymous web traffic is rate-limited (`slowapi`).
- Subject is a sitting head of state. Treat output as automated analysis, not finished journalism — review the source article before quoting.
- No mass scraping of social-media or non-official sources. Web-search-verify uses Anthropic's `web_search_20250305` server tool, which respects publisher robots.txt.

---

## Testing

```bash
./scripts/test.sh                              # full local suite (unit, ~0.01s)
.venv/bin/python -m unittest discover tests/   # just the unit tests
.venv/bin/python tests/system_check.py         # live web-stack integration check
```

The unit suite is offline, no external deps, and runs in milliseconds. It catches:
- `host_llm` branch invariants in the agentic Q&A
- JSON1 vs LIKE-fallback parity in `handle_get_article`
- Drift between the README's `## Data model` block and the real DB schema (the bug I shipped twice before this test existed)

CI: `.github/workflows/test.yml` runs the unit suite on every push and PR to `main`. See `tests/README.md` for the file-by-file map.

---

## Repository layout

```
kahzaabu/                   The Python package
├── __init__.py
├── cli.py                  Click-based CLI (kahzaabu <subcommand>)
├── pipeline.py             Orchestrates the 6 stages
├── scraper.py              presidency.gov.mv crawler (EN + DV)
├── extractor.py            Per-article claim extraction (Sonnet)
├── inspector.py            Per-article fact card (Sonnet)
├── curator.py              Cross-time contradiction detector (Sonnet)
├── verifier.py             Web-search-verifier (Haiku)
├── dv_compare.py           EN/DV diff (Sonnet)
├── manifesto.py            2023 promise extractor + cross-referencer
├── qna.py                  Legacy single-shot Q&A (kept for CLI parity)
├── qna_agentic.py          The current agentic Q&A loop + narrative-tricks
├── claims_db.py            Schema, migrations, all DB helpers
├── db.py                   Connection plumbing
├── models.py               Type aliases
├── report.py               JSON/CSV export of fact_checks
├── infographics.py         Static-HTML viz generators (legacy tracker)
├── auth.py                 Web-user password hashing + session helpers
├── scheduler.py            launchd helper
├── tui.py                  Textual TUI
├── mcp_server.py           [legacy] stdio MCP server — superseded by plugin
└── web/                    FastAPI app
    ├── app.py
    ├── api/                JSON endpoints
    │   ├── articles.py / factchecks.py / manifesto.py
    │   ├── ask.py / freshness.py / stats.py / viz.py
    │   ├── auth.py / admin.py
    │   ├── corrections.py / inspect.py
    ├── static/             HTML / CSS / JS (no SPA)
    ├── db_dep.py           FastAPI Depends() for DB
    └── limits.py           Rate-limiter + LRU cache for /api/ask

hermes-plugin/              Hermes plugin source (symlinked from ~/.hermes/...)
                            — see hermes-plugin/README.md
tests/                      End-to-end + unit tests (see tests/README.md)
research/                   Historical one-shot scripts (see research/README.md)
                            — NOT imported by the package
scripts/                    test.sh, install-hermes-plugin.sh, launchd plist,
                            run_pipeline.sh, Caddyfile, systemd unit
.github/workflows/          CI: test.yml runs unit suite on push + PR
data/                       SQLite DB + manifesto/ (other contents gitignored)
```

---

## Further reading

- The hermes plugin's own README: `~/.hermes/hermes-agent/plugins/kahzaabu/README.md` (created by this commit)
- The agent's usage guide for kahzaabu: `~/.hermes/hermes-agent/plugins/kahzaabu/SKILL.md`
- Hermes docs: <https://github.com/NousResearch/hermes-agent>

If you want to *change* something, start with `hermes kahzaabu doctor` to confirm your environment is healthy, then read the relevant module — they're small (mostly 200-400 LOC) and prose-heavy. The pipeline is the most opinionated part; everything else is glue.
