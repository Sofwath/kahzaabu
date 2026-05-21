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

    # Register the `/kahzaabu <question>` slash command — available in any
    # hermes chat session, including messaging gateway (Telegram, WhatsApp).
    ctx.register_command(
        name="kahzaabu",
        handler=_slash_kahzaabu,
        description="Ask kahzaabu a question over the Maldives Presidency archive",
        args_hint="<question>",
    )

    logger.info("kahzaabu plugin registered: 8 tools + `hermes kahzaabu` CLI "
                "+ /kahzaabu slash command")


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
        db_path = Path("/Users/sofwath/Developer/myLabs/kahzaabu/data/kahzaabu.db")
        try:
            import kahzaabu as _kpkg
            db_path = Path(_kpkg.__file__).resolve().parents[1] / "data" / "kahzaabu.db"
        except ImportError:
            pass
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
