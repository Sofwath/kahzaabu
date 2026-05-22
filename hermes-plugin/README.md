# kahzaabu hermes plugin

Native plugin that wires the kahzaabu fact-checking archive into Hermes Agent. Replaces the legacy stdio MCP server (moved to `kahzaabu/legacy/mcp_server.py`) with an in-process plugin: 9 agent tools, a `hermes kahzaabu` CLI subcommand, and a `/kahzaabu` slash command that works inside any hermes chat — including chats routed through the messaging gateway (Telegram / WhatsApp / Slack / Discord).

This README is for **someone reading the plugin source**. For an overview of the kahzaabu project itself, see `<dev tree>/README.md`.

---

## Files

| File | Purpose |
|---|---|
| `plugin.yaml` | Manifest — name, version, provides_tools, supported platforms |
| `__init__.py` | `register(ctx)` entry. Three jobs: env hydration, import bootstrap, tool/CLI registration. |
| `tools.py` | 9 agent-facing tools wrapping `kahzaabu.qna_agentic`, `kahzaabu.claims_db`, and `kahzaabu.constitution` |
| `cli.py` | `hermes kahzaabu {setup,status,update,ask,doctor,web}` subcommand |
| `SKILL.md` | Agent-facing usage guide (when to use which tool) |

The plugin **imports** the canonical `kahzaabu` package from the user's dev tree — it does **not** vendor code. Editing the dev tree updates the plugin immediately.

---

## How `register()` bootstraps itself

Hermes' venv intentionally strips `PYTHONPATH` (see `~/.local/bin/hermes`) and has no `pip` (so `pip install -e` is not an option). That means `import kahzaabu` will fail out of the box unless something puts the dev tree on `sys.path`.

`__init__.py` solves this in three layers:

1. **Try the import.** If `import kahzaabu` works, do nothing.
2. **Discover and inject `sys.path`.** Check `$KAHZAABU_HOME`, then `~/.hermes/.env`, then common dev layouts (`~/Developer/myLabs/kahzaabu`, `~/Developer/kahzaabu`, `~/code/kahzaabu`, `~/src/kahzaabu`, `~/kahzaabu`). The first one that has a `kahzaabu/__init__.py` wins. `sys.path.insert(0, …)` makes the current process succeed.
3. **Self-heal `.pth`.** Write `kahzaabu.pth` into hermes' venv site-packages. Future processes (including hermes upgrades that recreate the venv) get the import automatically.

Path discovery is in `_discover_kahzaabu_home()` in `__init__.py`. To override, set in `~/.hermes/.env`:

```
KAHZAABU_HOME=/path/to/your/dev/tree
```

---

## Why some operations shell out to a *separate* venv

| `hermes kahzaabu` subcommand | Where it runs |
|---|---|
| `setup` | Inside hermes |
| `status` | Inside hermes |
| `ask` | Inside hermes (Anthropic SDK is already in hermes venv) |
| `doctor` | Inside hermes |
| `update` | Shells out to `<dev>/.venv/bin/kahzaabu pipeline` |
| `web` | Shells out to `<dev>/.venv/bin/kahzaabu web` |

The pipeline + web UI need scikit-learn, FastAPI, slowapi, bs4, lxml, etc. — none of which live in hermes' lean venv. Rather than bloat hermes' venv, the plugin shells out. The dev tree's full-deps venv is created normally with `python3 -m venv .venv && .venv/bin/pip install -e .`.

`doctor` explicitly checks the dev venv exists AND `kahzaabu --help` returns 0. If either fails, it prints remediation steps.

---

## Tool schemas

All 9 tools are in `tools.py` as `*_SCHEMA` dicts following the OpenAI shape (`{name, description, parameters}`) — the same shape hermes' tool registry expects. Handlers take `(args: dict, **_kw) -> str` and return JSON-encoded strings (hermes' tool-result contract).

Tool names are prefixed `kahzaabu_` to avoid collisions with built-in tools. All belong to the `"kahzaabu"` toolset.

---

## How channels reach the plugin

```
user on Telegram/WhatsApp/Slack/Discord
        │
        ▼
hermes gateway (one-time: hermes gateway setup + install + start)
        │
        ▼
hermes agent loop (sees `kahzaabu_*` tools as in-process functions)
        │
        ▼
plugin handler in tools.py
        │
        ▼
kahzaabu package → SQLite (read) and/or Anthropic API
```

No code in this plugin handles Telegram/WhatsApp/etc. directly — that's hermes' gateway. The plugin only registers tools; the agent decides when to call them. Configure channels with:

```bash
hermes gateway setup     # interactive — pick platforms, paste tokens
hermes gateway install   # systemd / launchd
hermes gateway start
```

Viber is **not** natively supported by hermes. Adding it would require `ctx.register_platform(...)` (see `~/.hermes/hermes-agent/hermes_cli/plugins.py:645`) plus a webhook-driven platform adapter.

---

## Provider/model inheritance

Today the plugin's main agentic loop (`kahzaabu_ask`) calls `anthropic.Anthropic()` directly. The narrative-tricks **guarantee-pass** could be migrated to `ctx.llm.complete()` (hermes' host-owned LLM facade — see `agent/plugin_llm.py`) to fully inherit the user's provider config. The main loop can't migrate yet because `ctx.llm.complete()` doesn't yet support multi-turn tool-use.

For now, the plugin reads `ANTHROPIC_API_KEY` from `~/.hermes/.env` via `_load_hermes_env()`. The user still picks LLM through hermes' main setup (`hermes setup model`); kahzaabu sees the same key but uses Claude regardless of hermes' default.

---

## Removing / disabling

```bash
hermes plugins disable kahzaabu       # keep files, stop loading
hermes plugins remove kahzaabu        # remove the plugin
rm ~/.hermes/hermes-agent/venv/lib/python3.11/site-packages/kahzaabu.pth   # belt-and-suspenders
```

If you also want to restore the legacy MCP server (rollback), add this to `~/.hermes/config.yaml mcp_servers:`:

```yaml
kahzaabu:
  command: /<dev>/.venv-mcp/bin/python
  args: ["-m", "kahzaabu.mcp_server"]
  env:
    PYTHONPATH: /<dev>
    ANTHROPIC_API_KEY: $(cat ~/.config/kahzaabu/api_key)
    KAHZAABU_MCP_ALLOW_PIPELINE: "1"
```

---

## Ambient context injection (the `pre_llm_call` hook)

The plugin installs a `pre_llm_call` hook that turns kahzaabu from "a tool you call" into "an ambient knowledge layer that pays attention". When a user mentions a Maldivian-politics topic in **any** hermes chat — terminal, Telegram, Slack, Discord, gateway — the hook:

1. Prefilters cheaply (regex against a stem-keyword list — sub-millisecond per turn).
2. On match, runs a fast BM25 lookup against the local `fact_checks_fts` + `constitution_articles_fts` indexes.
3. Returns up to 3 relevant fact-checks + 2 constitution articles as **user-message context** for that turn (preserves the prompt-cache prefix).

Result: someone discussing the President in a hermes chat gets kahzaabu's grounding automatically; they don't have to remember `/kahzaabu`.

**Opt out** — set `KAHZAABU_AMBIENT_DISABLE=1` in `~/.hermes/.env`. The hook short-circuits at registration time; the plugin keeps the 9 tools + slash command + CLI.

The hook is **defensive**: missing DB → returns `None`, search throws → returns `None`. A misbehaving hook can never break a hermes turn.

**Performance budget**:
- Non-match path: < 1 ms (regex only)
- Match path: typically < 50 ms (1 SQLite open + 2 BM25 queries)

Regression-guarded by `tests/test_ambient_hook.py` (13 tests).

---

## Langfuse observability (optional)

Kahzaabu uses `ctx.llm` for its agentic Q&A loop, which means every LLM call goes through hermes' provider abstraction. If you enable hermes' bundled Langfuse plugin, **kahzaabu's LLM calls are auto-traced with zero plugin code changes**.

```bash
pip install langfuse
hermes plugins enable observability/langfuse
```

Set in `~/.hermes/.env`:
```bash
HERMES_LANGFUSE_PUBLIC_KEY=pk-lf-...
HERMES_LANGFUSE_SECRET_KEY=sk-lf-...
HERMES_LANGFUSE_BASE_URL=https://cloud.langfuse.com   # or self-hosted
```

What you'll see in Langfuse for each `kahzaabu_ask` invocation:

- The **tool-call tree** — every internal call the agentic loop makes (web search, claims_db queries, constitution lookups, narrative-tricks guarantee-pass).
- **Cost per question** — Sonnet + Haiku token breakdowns, USD per call, USD per session.
- **Latency** — end-to-end + per-stage.
- **Prompt cache hit rate** — useful for tuning the system prompt prefix.

The plugin fails open: no Langfuse SDK / no credentials → silent no-op. See `~/.hermes/hermes-agent/plugins/observability/langfuse/README.md` for tuning knobs (sample rate, env tags, char caps).

---

## Live-testing the ambient hook

Unit tests cover the hook's logic but won't catch issues that show up only in a real hermes process (different `session_id` handling, agent behaviour around injected context, file-system side-effects). Three rules from past live-test runs to avoid stepping on the same rakes:

### Rule 1 — Use read-only questions

The agent will sometimes act on imperative prompts. `Write me a Python function that reverses a list` caused Claude Haiku to literally write `reverse_list.py` to the working tree. The file then got accidentally staged in the next git commit (cleaned up in `120c6a3`).

Use questions, not commands. Phrasing examples:

| ✅ Safe | ❌ Risky |
|---|---|
| "What did Muizzu announce this week?" | "Save Muizzu's announcements to a file." |
| "What's the syntax to reverse a list in Python?" | "Write me a function that reverses a list." |
| "How does kahzaabu cite fact-checks?" | "Generate a fact-check for this claim." |

Run `git status` before every commit during a live-test session. If a `.py` / `.md` / `.json` you didn't intend appears, delete or move it out of the repo before staging.

### Rule 2 — Tests must never touch `data/kahzaabu.db`

The pre-existing `_mark_session_hot` writes to whatever DB `_resolve_db_path()` finds — production by default. Earlier runs leaked 8 fixture session IDs (`cv-muizzu`, `cv-maldiv`, …) into `ambient_hot_sessions`. Fixed structurally: `tests/test_ambient_hook.py` has `setUpModule` / `tearDownModule` that redirect `KAHZAABU_DB` to a per-run tempfile.

When adding a NEW test file that calls hook internals, follow the same pattern (copy the `setUpModule` block from `test_ambient_hook.py`).

After a live-test session, sanity-check the production DB:

```bash
sqlite3 data/kahzaabu.db \
  "SELECT session_id, hot_until FROM ambient_hot_sessions WHERE session_id LIKE 'cv-%' OR session_id LIKE 'session-%' OR session_id LIKE 'crossproc-%'"
```

Any test-fixture IDs that show up are leaks — delete them.

### Rule 3 — Sticky-session needs interactive hermes, not `-z --resume`

Hermes' `-z` (one-shot) mode generates a fresh `session_id` for each invocation, even when paired with `--resume SID`. The `--resume` flag loads the prior conversation's *content* but the new turn lives under a new ID.

Concrete: turn 1 `hermes -z "What did Muizzu announce?"` (session `…68081e`) marks `…68081e` hot in the persistent DB. Turn 2 `hermes -z --resume …68081e "what about housing?"` runs under a NEW session `…1d6b38` whose hot-row doesn't exist → sticky-session path doesn't fire.

To live-test the sticky path you need session_id stability — use interactive mode:

```bash
hermes chat
> What did Muizzu announce about the JSC?     # turn 1: strong → marks hot
> what about housing then?                    # turn 2: same session_id → sticky fires
```

Or verify directly without hermes (what unit tests do):

```bash
# After the first session marks itself hot, the hook will inject
# even when called from a sibling process with the same session_id.
.venv/bin/python -c "
from plugins.kahzaabu.hooks import on_pre_llm_call
r = on_pre_llm_call(session_id='<hot-session-id>', user_message='what about housing?')
print(r['context'][:200] if r else 'no match')
"
```

### Verifying the hook fired

Three live signals, in decreasing strength:

1. **Agent's text response** — if it mentions specifics from the archive (`presidency.gov.mv`, document numbers like `2026-287`, real article dates, "based on the kahzaabu archive"), the hook fired and the agent used the injected context.
2. **`hermes kahzaabu doctor`** — the line `ambient hook : enabled (platforms: all, hot sessions: N in-proc, M persistent)` shows the cross-process count after the run.
3. **Direct DB inspection** — `SELECT session_id, hot_until FROM ambient_hot_sessions ORDER BY hot_until DESC LIMIT 5`.

---

## Status

- ✅ All 9 tools wired and tested via `hermes chat`
- ✅ CLI subcommand `hermes kahzaabu {setup,status,update,ask,doctor,web}` works
- ✅ Self-healing `.pth` (proven: delete it, next invocation rewrites it)
- ✅ Hardcoded paths removed (derived from `Path(kahzaabu.__file__).parents[1]`)
- ✅ Doctor surfaces the dev `.venv` dependency clearly
- 🟡 `ctx.llm` migration for guarantee-pass — not done
- 🟡 launchd → `hermes cron` migration — documented, not executed
- 🔴 Viber adapter — not started (out of scope unless user demand)

See the project `README.md` for the full TODO list.
