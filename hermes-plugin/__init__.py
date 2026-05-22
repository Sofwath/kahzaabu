# SPDX-License-Identifier: Apache-2.0
"""kahzaabu plugin — Maldives Presidency press-release fact-checking archive.

Replaces the legacy stdio MCP server in ~/.hermes/config.yaml.mcp_servers.kahzaabu
with a native in-process plugin: same 8 tools, plus a `hermes kahzaabu` CLI.

The plugin imports the canonical `kahzaabu` package from the user's dev tree —
it does NOT vendor code. Discovery is resilient: candidate paths are probed in
order until one is found, and a .pth file is self-healed into hermes' venv so
the import survives across hermes upgrades.

LLM credentials are sourced from ~/.hermes/.env (ANTHROPIC_API_KEY etc.) — no
separate kahzaabu config needed.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

HERMES_HOME = Path.home() / ".hermes"
HERMES_ENV = HERMES_HOME / ".env"


def _kahzaabu_home_candidates() -> list[Path]:
    """Ordered list of plausible kahzaabu dev-tree locations."""
    candidates: list[Path] = []
    # 1. Explicit override (env or ~/.hermes/.env)
    explicit = os.environ.get("KAHZAABU_HOME")
    if not explicit and HERMES_ENV.exists():
        for line in HERMES_ENV.read_text().splitlines():
            if line.startswith("KAHZAABU_HOME="):
                explicit = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if explicit:
        candidates.append(Path(explicit).expanduser())
    # 2. Common dev tree layouts (cheap probes)
    home = Path.home()
    for rel in ("Developer/myLabs/kahzaabu", "Developer/kahzaabu",
                "code/kahzaabu", "src/kahzaabu", "kahzaabu"):
        candidates.append(home / rel)
    return candidates


def _discover_kahzaabu_home() -> Path | None:
    """Find the kahzaabu dev tree. Returns None if nothing looks plausible."""
    for candidate in _kahzaabu_home_candidates():
        if (candidate / "kahzaabu" / "__init__.py").exists():
            return candidate.resolve()
    return None


def _ensure_kahzaabu_importable() -> Path | None:
    """Make `import kahzaabu` work under hermes' (PYTHONPATH-stripped) venv.

    Three layers, in priority order:
      1. Already importable — nothing to do.
      2. sys.path defensive insert — gets us through this process even if
         the .pth was wiped by a venv recreation.
      3. Self-heal the .pth file in hermes' venv site-packages so future
         processes don't need step 2.

    Returns the resolved KAHZAABU_HOME, or None if discovery failed.
    """
    # Layer 1
    try:
        import kahzaabu  # noqa: F401
        return Path(kahzaabu.__file__).resolve().parents[1]
    except ImportError:
        pass

    # Layer 2 + 3
    home = _discover_kahzaabu_home()
    if home is None:
        logger.warning(
            "kahzaabu plugin: cannot locate dev tree. Set KAHZAABU_HOME in "
            "~/.hermes/.env to fix."
        )
        return None

    if str(home) not in sys.path:
        sys.path.insert(0, str(home))

    # Self-heal .pth — best-effort, never blocks plugin load
    try:
        import site
        venv_sp = next(
            (Path(p) for p in site.getsitepackages()
             if "hermes-agent" in p and "site-packages" in p),
            None,
        )
        if venv_sp:
            pth_file = venv_sp / "kahzaabu.pth"
            if not pth_file.exists() or pth_file.read_text().strip() != str(home):
                pth_file.write_text(str(home) + "\n")
                logger.info("kahzaabu plugin: wrote %s -> %s", pth_file, home)
    except Exception as e:
        logger.debug("kahzaabu plugin: .pth self-heal skipped: %s", e)

    try:
        import kahzaabu  # noqa: F401
        return home
    except ImportError:
        return None


def _load_hermes_env() -> None:
    """Best-effort: hydrate os.environ from ~/.hermes/.env so the plugin's
    handlers see ANTHROPIC_API_KEY, KAHZAABU_DAILY_BUDGET_USD, etc., without
    requiring the user to also export them in their shell.

    Hermes itself loads this file at startup, but plugin code runs in the same
    process so the variables are already present in os.environ. This loader is
    defensive — if some pathway misses them (e.g. direct hermes_cli invocation
    in tests), we still pick them up.
    """
    if not HERMES_ENV.exists():
        return
    try:
        for line in HERMES_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as e:
        logger.debug("kahzaabu: env hydration skipped: %s", e)


def register(ctx) -> None:
    """Plugin entry point. Hermes calls this once at enable time."""
    _load_hermes_env()
    _ensure_kahzaabu_importable()

    # Stash ctx.llm so tool handlers (loaded after register) can route the
    # narrative-tricks guarantee-pass through hermes' configured provider.
    # Tool handlers are called with just (args, **kw) — they have no
    # direct route to ctx — so we use module-level state.
    from plugins.kahzaabu import tools as _tools_mod
    _tools_mod.HOST_LLM = ctx.llm

    # Register the 8 agent-facing tools.
    from plugins.kahzaabu.tools import TOOLS, check_kahzaabu_requirements
    for name, schema, handler, emoji in TOOLS:
        ctx.register_tool(
            name=name,
            toolset="kahzaabu",
            schema=schema,
            handler=handler,
            check_fn=check_kahzaabu_requirements,
            emoji=emoji,
        )

    # Register the `hermes kahzaabu` CLI subcommand.
    from plugins.kahzaabu.cli import register_cli, kahzaabu_command
    ctx.register_cli_command(
        name="kahzaabu",
        help="Maldives press-release fact-check archive (status, ask, update, web)",
        setup_fn=register_cli,
        handler_fn=kahzaabu_command,
        description=(
            "Manage the kahzaabu fact-checking archive: run the pipeline, ask "
            "natural-language questions, start the web UI, run setup. See "
            "`hermes kahzaabu setup` first."
        ),
    )

    # Slash commands — available in any hermes chat including
    # messaging gateways (Telegram, WhatsApp, Slack, Discord). The
    # split (Slice D) gives mobile users autocomplete for specific
    # actions instead of typing free-form questions every time.
    # `/kahzaabu <args>` is kept as a backwards-compat alias that
    # routes to /kahzaabu-ask.
    ctx.register_command(
        name="kahzaabu",
        handler=_slash_kahzaabu,
        description="Ask kahzaabu a question (alias of /kahzaabu-ask)",
        args_hint="<question>",
    )
    ctx.register_command(
        name="kahzaabu-ask",
        handler=_slash_kahzaabu,
        description="Ask kahzaabu a natural-language question; agentic loop with optional web search",
        args_hint="<question>",
    )
    ctx.register_command(
        name="kahzaabu-recent",
        handler=_slash_recent,
        description="List articles from the last N days (default 7)",
        args_hint="[days]",
    )
    ctx.register_command(
        name="kahzaabu-stats",
        handler=_slash_stats,
        description="Archive freshness + counts (articles, claims, fact-checks)",
        args_hint="",
    )
    ctx.register_command(
        name="kahzaabu-promise",
        handler=_slash_promise,
        description="Search 2023 manifesto promises by topic keyword",
        args_hint="<topic>",
    )
    ctx.register_command(
        name="kahzaabu-factcheck",
        handler=_slash_factcheck,
        description="Fetch a single fact-check by ID with web evidence",
        args_hint="<id>",
    )
    ctx.register_command(
        name="kahzaabu-translate",
        handler=_slash_translate,
        description="Translate EN↔DV in the press office's distinctive style (Slice 16, ADR 0016)",
        args_hint="<text>",
    )

    # Register the pre_llm_call ambient-context hook unless opted out.
    # The hook itself short-circuits on KAHZAABU_AMBIENT_DISABLE, but
    # we honour the same env var at registration time too so a user
    # who's opted out doesn't even pay the hook-dispatch overhead.
    if not os.environ.get("KAHZAABU_AMBIENT_DISABLE"):
        from plugins.kahzaabu.hooks import on_pre_llm_call
        ctx.register_hook("pre_llm_call", on_pre_llm_call)
        logger.info("kahzaabu plugin registered: 14 tools + `hermes kahzaabu` CLI "
                    "+ /kahzaabu slash command + pre_llm_call ambient hook")
    else:
        logger.info("kahzaabu plugin registered: 14 tools + `hermes kahzaabu` CLI "
                    "+ /kahzaabu slash command (ambient hook DISABLED via env)")


def _slash_kahzaabu(raw_args: str) -> str:
    """Handler for `/kahzaabu <question>` slash commands.

    Reuses handle_ask so behaviour matches the agent-callable tool exactly
    (same session memory, same narrative-tricks layer, same cost cap).
    The slash command auto-continues the most-recent session — typing
    `/kahzaabu follow-up` after a prior `/kahzaabu` retains context.
    """
    import json
    raw_args = (raw_args or "").strip()
    if not raw_args:
        return ("Usage: /kahzaabu <question>\n"
                "Example: /kahzaabu what did Muizzu do this week?")

    # Auto-continue: pick up the most-recent session if any exists.
    session_id = None
    try:
        from kahzaabu import claims_db
        import sqlite3
        # Derive the DB path from the installed kahzaabu package — works
        # regardless of where the user cloned the repo. The hardcoded
        # absolute path that used to live here leaked the developer's
        # machine layout and broke installs on any other host.
        try:
            import kahzaabu as _kpkg
            db_path = Path(_kpkg.__file__).resolve().parents[1] / "data" / "kahzaabu.db"
        except ImportError:
            # If kahzaabu isn't on sys.path, fall back to a $KAHZAABU_DB
            # override or the OS-conventional location.
            db_path = Path(os.environ.get(
                "KAHZAABU_DB",
                str(Path.home() / ".local" / "share" / "kahzaabu" / "kahzaabu.db"),
            ))
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            session_id = claims_db.most_recent_session_id(conn)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("slash kahzaabu: session-continue lookup failed: %s", e)

    from plugins.kahzaabu.tools import handle_ask
    payload = {"question": raw_args, "enable_web": False}
    if session_id:
        payload["session_id"] = session_id
    result_json = handle_ask(payload)
    try:
        result = json.loads(result_json)
    except json.JSONDecodeError:
        return result_json

    if "error" in result:
        return f"❌ {result['error']}"

    footer = (
        f"\n\n— *session {result.get('session_id', '?')[:8]}… · "
        f"${result.get('cost_usd', 0):.3f}*"
    )
    return result.get("answer", "(no answer)") + footer


def _slash_recent(raw_args: str) -> str:
    """/kahzaabu-recent [days] — list articles from the last N days.

    Wraps handle_recent_activity (the same tool kahzaabu_ask uses
    internally), so the slash command and the agent-callable tool
    share semantics: defaults to 7 days, capped at 30, lists
    articles + their checkable claims."""
    import json
    raw_args = (raw_args or "").strip()
    days = 7
    if raw_args:
        try:
            days = max(1, min(int(raw_args), 30))
        except ValueError:
            return f"Usage: /kahzaabu-recent [days]  (got '{raw_args}')"
    from plugins.kahzaabu.tools import handle_recent_activity
    out = json.loads(handle_recent_activity({"days": days, "limit": 12}))
    if "error" in out:
        return f"❌ {out['error']}"
    items = out.get("articles") or out.get("items") or []
    if not items:
        return f"No articles in the last {days} day(s)."
    lines = [f"**Articles in the last {days} day(s):**\n"]
    for a in items[:12]:
        title = (a.get('title') or '')[:80]
        date = a.get('published_date') or ''
        aid = a.get('id', '?')
        lines.append(f"• {date}  [{aid}] {title}")
    return "\n".join(lines)


def _slash_stats(raw_args: str) -> str:
    """/kahzaabu-stats — archive snapshot."""
    import json
    from plugins.kahzaabu.tools import handle_stats
    out = json.loads(handle_stats({}))
    if "error" in out:
        return f"❌ {out['error']}"
    fresh = out.get("freshness") or {}
    return (
        f"**kahzaabu archive snapshot**\n\n"
        f"• Articles (Muizzu era, EN): {out.get('articles_muizzu_era', '?'):,}\n"
        f"• Claims extracted: {out.get('claims_extracted', '?'):,}\n"
        f"• Published fact-checks: {out.get('fact_checks', '?'):,}\n"
        f"• Manifesto promises tracked: {out.get('manifesto_promises', '?'):,}\n\n"
        f"_Last scrape: {fresh.get('last_scrape_at') or 'never'} "
        f"({'⚠️ stale' if fresh.get('is_stale') else 'fresh'})_"
    )


def _slash_promise(raw_args: str) -> str:
    """/kahzaabu-promise <topic> — search 2023 manifesto promises."""
    import json
    raw_args = (raw_args or "").strip()
    if not raw_args:
        return ("Usage: /kahzaabu-promise <topic>\n"
                "Example: /kahzaabu-promise housing")
    from plugins.kahzaabu.tools import handle_manifesto
    out = json.loads(handle_manifesto({"q": raw_args, "limit": 8}))
    if "error" in out:
        return f"❌ {out['error']}"
    items = out.get("promises") or out.get("items") or []
    if not items:
        return f"No manifesto promises match '{raw_args}'."
    lines = [f"**Manifesto promises matching '{raw_args}':**\n"]
    for p in items[:8]:
        status = p.get("delivery_status") or "?"
        section = p.get("section") or "?"
        text = (p.get("promise_text_en") or "")[:120]
        lines.append(f"• [{status}] ({section}) {text}")
    return "\n".join(lines)


def _slash_factcheck(raw_args: str) -> str:
    """/kahzaabu-factcheck <id> — fetch one fact-check."""
    import json
    raw_args = (raw_args or "").strip()
    if not raw_args:
        return ("Usage: /kahzaabu-factcheck <id>\n"
                "Tip: /kahzaabu-recent first, then look up the ID.")
    try:
        fc_id = int(raw_args)
    except ValueError:
        return f"Expected a numeric fact-check id; got '{raw_args}'"
    from plugins.kahzaabu.tools import handle_get_factcheck
    out = json.loads(handle_get_factcheck({"id": fc_id}))
    if "error" in out:
        return f"❌ {out['error']}"
    fc = out.get("fact_check") or out
    verdict = fc.get("verdict_label") or fc.get("category") or "?"
    truth = fc.get("truth_score_label") or ""
    claim = (fc.get("claim") or "")[:200]
    explanation = (fc.get("what_actually_happened") or "")[:400]
    evidence = out.get("web_evidence") or []
    lines = [
        f"**Fact-check #{fc_id}**  ·  verdict: **{verdict}**  ·  {truth}",
        f"",
        f"_Claim:_ {claim}",
    ]
    if explanation:
        lines.append(f"")
        lines.append(f"_What actually happened:_ {explanation}")
    if evidence:
        lines.append(f"\n_Web evidence ({len(evidence)} source(s)):_")
        for ev in evidence[:3]:
            lines.append(f"  • {ev.get('relevance', '?')}: {(ev.get('url') or ev.get('title', ''))[:80]}")
    return "\n".join(lines)


def _slash_translate(raw_args: str) -> str:
    """/kahzaabu-translate <text> — press-office-style EN↔DV
    translation. Source language is auto-detected from the input."""
    import json
    raw_args = (raw_args or "").strip()
    if not raw_args:
        return ("Usage: /kahzaabu-translate <text>\n"
                "Example: /kahzaabu-translate The President met with the Cabinet today.")
    from plugins.kahzaabu.tools import handle_translate
    out = json.loads(handle_translate({"text": raw_args, "target_language": "auto"}))
    if "error" in out:
        return f"❌ {out['error']}"
    translation = out.get("translation", "(no translation)")
    src = out.get("source_lang", "?")
    tgt = out.get("target_lang", "?")
    n_ex = len(out.get("exemplar_ids") or [])
    n_gl = out.get("glossary_terms_used", 0)
    cost = out.get("cost_usd", 0.0)
    cached = " (cached)" if out.get("cache_hit") else ""
    return (
        f"{translation}\n\n"
        f"— _{src} → {tgt}  ·  {n_ex} exemplar(s), {n_gl} glossary term(s)  "
        f"·  ${cost:.4f}{cached}_\n"
        f"— _Reference-implementation output — review before publishing._"
    )
