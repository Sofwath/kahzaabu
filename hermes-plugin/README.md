# kahzaabu hermes plugin

Native plugin that wires the [kahzaabu fact-checking archive](https://example.invalid) into Hermes Agent. Replaces the legacy stdio MCP server in `~/.hermes/config.yaml mcp_servers.kahzaabu` with an in-process plugin: same 8 tools, plus a `hermes kahzaabu` CLI subcommand.

This README is for **someone reading the plugin source**. For an overview of the kahzaabu project itself, see `<dev tree>/README.md`.

---

## Files

| File | Purpose |
|---|---|
| `plugin.yaml` | Manifest — name, version, provides_tools, supported platforms |
| `__init__.py` | `register(ctx)` entry. Three jobs: env hydration, import bootstrap, tool/CLI registration. |
| `tools.py` | 8 agent-facing tools wrapping `kahzaabu.qna_agentic` and `kahzaabu.claims_db` |
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

The pipeline + web UI need scikit-learn, FastAPI, slowapi, bs4, lxml, passlib, bcrypt, etc. — none of which live in hermes' lean venv. Rather than bloat hermes' venv, the plugin shells out. The dev tree's full-deps venv is created normally with `python3 -m venv .venv && .venv/bin/pip install -e .`.

`doctor` explicitly checks the dev venv exists AND `kahzaabu --help` returns 0. If either fails, it prints remediation steps.

---

## Tool schemas

All 8 tools are in `tools.py` as `*_SCHEMA` dicts following the OpenAI shape (`{name, description, parameters}`) — the same shape hermes' tool registry expects. Handlers take `(args: dict, **_kw) -> str` and return JSON-encoded strings (hermes' tool-result contract).

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

## Status

- ✅ All 8 tools wired and tested via `hermes chat`
- ✅ CLI subcommand `hermes kahzaabu {setup,status,update,ask,doctor,web}` works
- ✅ Self-healing `.pth` (proven: delete it, next invocation rewrites it)
- ✅ Hardcoded paths removed (derived from `Path(kahzaabu.__file__).parents[1]`)
- ✅ Doctor surfaces the dev `.venv` dependency clearly
- 🟡 `ctx.llm` migration for guarantee-pass — not done
- 🟡 launchd → `hermes cron` migration — documented, not executed
- 🔴 Viber adapter — not started (out of scope unless user demand)

See the project `README.md` for the full TODO list.
