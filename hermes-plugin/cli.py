"""hermes kahzaabu CLI subcommand.

Subcommands:
  setup    Interactive setup wizard (inherits hermes provider config)
  status   Archive counts, freshness, plugin health
  update   Run pipeline (scrape → extract → curate → verify)
  ask      Natural-language Q&A
  doctor   Diagnose plugin install + dependencies
  web      Start the FastAPI web UI (localhost or public)
"""
from __future__ import annotations

import argparse
import functools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


HERMES_HOME = Path.home() / ".hermes"
HERMES_ENV = HERMES_HOME / ".env"
HERMES_CONFIG = HERMES_HOME / "config.yaml"


@functools.lru_cache(maxsize=1)
def kahzaabu_home() -> Optional[Path]:
    """Resolve the dev tree from the imported package."""
    try:
        import kahzaabu
        return Path(kahzaabu.__file__).resolve().parents[1]
    except ImportError:
        return None


def kahzaabu_cli_path() -> Optional[Path]:
    """Path to the `kahzaabu` console script in the dev tree's full-deps venv.

    Returns None if the venv hasn't been created. The full kahzaabu pipeline
    (scraping, scikit-learn clustering, etc.) needs deps that aren't in
    hermes' lean venv — so `update` and `web` shell out to this script.
    """
    home = kahzaabu_home()
    if home is None:
        return None
    p = home / ".venv" / "bin" / "kahzaabu"
    return p if p.exists() else None


def register_cli(subp: argparse.ArgumentParser) -> None:
    """Hermes calls this with the argparse subparser for `hermes kahzaabu`."""
    sub = subp.add_subparsers(dest="kahzaabu_cmd", metavar="<command>")

    sub.add_parser("setup", help="Interactive setup (LLM provider, budgets, channels)")
    sub.add_parser("status", help="Archive counts + freshness + plugin health")
    sub.add_parser("doctor", help="Diagnose install + dependencies")

    upd = sub.add_parser("update", help="Run scrape → extract → curate pipeline")
    upd.add_argument("--budget", type=float, default=1.0, help="dollar cap (default 1.0)")

    ask = sub.add_parser("ask", help="Ask a natural-language question")
    ask.add_argument("question", nargs="+")
    ask.add_argument("--no-web", action="store_true", help="disable web_search")
    ask.add_argument("--session", help="continue a session by id")

    web = sub.add_parser("web", help="Start the FastAPI web UI")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--host", default="127.0.0.1")


def kahzaabu_command(args: argparse.Namespace) -> int:
    """Dispatcher — hermes sets this as the command's default handler."""
    cmd = getattr(args, "kahzaabu_cmd", None)
    if not cmd:
        print("usage: hermes kahzaabu {setup,status,update,ask,doctor,web}")
        return 1

    dispatch = {
        "setup":   _cmd_setup,
        "status":  _cmd_status,
        "update":  _cmd_update,
        "ask":     _cmd_ask,
        "doctor":  _cmd_doctor,
        "web":     _cmd_web,
    }
    fn = dispatch.get(cmd)
    if fn is None:
        print(f"unknown subcommand: {cmd}")
        return 1
    return fn(args)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def _cmd_setup(_args) -> int:
    print("=== kahzaabu setup ===\n")
    print("This will:")
    print("  1. Verify hermes provider config (LLM selection)")
    print("  2. Read ANTHROPIC_API_KEY from ~/.hermes/.env (or prompt)")
    print("  3. Configure daily LLM budget cap")
    print("  4. Configure data freshness threshold")
    print("  5. Show how to wire WhatsApp / Telegram channels\n")

    # 1. Provider check
    provider, model = _hermes_provider()
    print(f"hermes provider : {provider}")
    print(f"hermes model    : {model}\n")
    if provider != "anthropic":
        print(f"⚠️  Note: kahzaabu's research loop requires Anthropic Claude.")
        print(f"   Your hermes default is '{provider}', which is fine for the agent")
        print(f"   shell — but kahzaabu_ask still calls Anthropic directly. Run")
        print(f"   `hermes setup model` to change hermes' default, independent of")
        print(f"   this.\n")

    # 2. API key check
    has_key = _has_anthropic_key()
    if has_key:
        print("✅ ANTHROPIC_API_KEY found in environment / ~/.hermes/.env\n")
    else:
        print("❌ ANTHROPIC_API_KEY missing.")
        key = input("   Paste your key (or press Enter to skip): ").strip()
        if key:
            _append_env("ANTHROPIC_API_KEY", key)
            print("   Saved to ~/.hermes/.env\n")
        else:
            print("   Skipped. Add it later via: `hermes auth` or edit ~/.hermes/.env\n")

    # 3. Budget
    budget = input("Daily LLM budget cap in USD [5.00]: ").strip() or "5.00"
    _append_env("KAHZAABU_DAILY_BUDGET_USD", budget)

    # 4. Freshness threshold
    stale = input("Stale-data warning threshold in hours [24]: ").strip() or "24"
    _append_env("KAHZAABU_STALE_HOURS", stale)

    # 5. Pipeline gate
    allow = input("Allow agent-triggered pipeline runs? [y/N]: ").strip().lower()
    if allow == "y":
        _append_env("KAHZAABU_MCP_ALLOW_PIPELINE", "1")
        print("   Agent can now call kahzaabu_pipeline_run\n")

    print("\n=== Channels (Telegram / WhatsApp / Slack / Discord) ===")
    print("These run through hermes' messaging gateway, NOT kahzaabu.")
    print("Configure each platform once:")
    print("  hermes gateway setup        # interactive: pick platforms, paste tokens")
    print("  hermes gateway install      # install as systemd/launchd service")
    print("  hermes gateway start        # start it\n")
    print("Once running, messages to your bot route to hermes → kahzaabu_ask tool.")
    print("\n=== Cron (12h pipeline cycle) ===")
    print("  hermes cron add 'hermes kahzaabu update --budget 0.50' --every 12h")
    print("\nSetup complete.")
    return 0


def _cmd_status(_args) -> int:
    from plugins.kahzaabu.tools import handle_stats
    out = json.loads(handle_stats({}))
    print(f"Articles (Muizzu era): {out['articles_muizzu_era']:>6}")
    print(f"Claims extracted    : {out['claims_extracted']:>6}")
    print(f"Fact-checks         : {out['fact_checks']:>6}")
    print(f"Web evidence rows   : {out['web_evidence_rows']:>6}")
    print(f"Manifesto promises  : {out['manifesto_promises']:>6}")
    if out.get("manifesto_by_delivery_status"):
        print("  by delivery status:")
        for status, n in sorted(out["manifesto_by_delivery_status"].items(),
                                  key=lambda x: -x[1]):
            print(f"    {status:<20} {n:>4}")
    f = out["freshness"]
    fresh_icon = "✅" if not f["is_stale"] else "⚠️ "
    print(f"\n{fresh_icon} Last scrape: {f['last_scrape_at']}  "
          f"({f['hours_since']:.1f}h ago, threshold {f['threshold_hours']}h)")
    print(f"DB: {out['db_path']}")
    return 0


def _cmd_update(args) -> int:
    cli = kahzaabu_cli_path()
    home = kahzaabu_home()
    if cli is None or home is None:
        print("❌ kahzaabu .venv not found.")
        print("   The pipeline needs the full-deps venv (scikit-learn, httpx, "
              "bs4, ...).\n   Create it with:\n")
        if home:
            print(f"     cd {home}")
            print( "     python3 -m venv .venv")
            print( "     .venv/bin/pip install -e .\n")
        else:
            print("     (also: KAHZAABU_HOME not resolvable — run `hermes "
                  "kahzaabu doctor`)\n")
        return 2
    cmd = [str(cli), "pipeline", "--budget", str(args.budget)]
    print(f"→ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(home))


def _cmd_ask(args) -> int:
    from plugins.kahzaabu.tools import handle_ask, _has_anthropic_key
    if not _has_anthropic_key():
        print("ANTHROPIC_API_KEY not set; add to ~/.hermes/.env")
        return 2
    payload: dict[str, Any] = {
        "question": " ".join(args.question),
        "enable_web": not args.no_web,
    }
    if args.session:
        payload["session_id"] = args.session
    print("...thinking...\n")
    out = json.loads(handle_ask(payload))
    if "error" in out:
        print(f"error: {out['error']}")
        return 2
    print(out["answer"])
    print(f"\n--- session: {out['session_id']}  cost: ${out['cost_usd']:.4f}  "
          f"iterations: {out['n_iterations']}  web: {out['web_searches']} ---")
    return 0


def _cmd_doctor(_args) -> int:
    ok = True

    def check(label: str, condition: bool, hint: str = "") -> None:
        nonlocal ok
        if condition:
            print(f"  ✅ {label}")
        else:
            print(f"  ❌ {label}" + (f"  ({hint})" if hint else ""))
            ok = False

    print("Plugin diagnostics:\n")

    home = kahzaabu_home()
    if home is None:
        check("kahzaabu package importable", False,
              "set KAHZAABU_HOME in ~/.hermes/.env, then restart hermes")
        # Without home, downstream checks can't run meaningfully.
        return 1
    check(f"kahzaabu package importable (from {home})", True)

    db = home / "data" / "kahzaabu.db"
    check(f"DB exists at {db}", db.exists(),
          "run `hermes kahzaabu update` to populate")

    # Check the .pth self-heal — the file should exist in hermes' venv so the
    # import survives across hermes upgrades.
    try:
        import site
        venv_sp = next(
            (Path(p) for p in site.getsitepackages()
             if "hermes-agent" in p and "site-packages" in p),
            None,
        )
        if venv_sp:
            pth = venv_sp / "kahzaabu.pth"
            pth_ok = pth.exists() and pth.read_text().strip() == str(home)
            check(f".pth self-heal file present ({pth})", pth_ok,
                  "re-run any `hermes kahzaabu` command — register() rewrites it")
    except Exception:
        pass

    # The pipeline + web subcommands shell out to the dev tree's full-deps venv.
    cli = kahzaabu_cli_path()
    if cli is None:
        check(f"kahzaabu full-deps .venv at {home}/.venv", False,
              "create: python3 -m venv .venv && .venv/bin/pip install -e .  "
              "(needed for `update` and `web`)")
    else:
        # Verify the script actually runs (e.g. not broken after Python upgrade)
        try:
            r = subprocess.run([str(cli), "--help"],
                                 capture_output=True, text=True, timeout=10)
            check(f"kahzaabu CLI runs ({cli})", r.returncode == 0,
                  f"exit {r.returncode}: {(r.stderr or '').splitlines()[-1:][0] if r.stderr else 'no stderr'}")
        except Exception as e:
            check(f"kahzaabu CLI runs ({cli})", False, str(e))

    check("ANTHROPIC_API_KEY set", _has_anthropic_key(),
          "add to ~/.hermes/.env or run `hermes kahzaabu setup`")

    check("hermes config readable", HERMES_CONFIG.exists())
    check("hermes env file present", HERMES_ENV.exists())

    provider, model = _hermes_provider()
    print(f"\n  hermes default model : {provider}/{model}")

    pipeline_allowed = os.environ.get("KAHZAABU_MCP_ALLOW_PIPELINE") == "1"
    print(f"  pipeline gated      : {'OPEN (agent can trigger)' if pipeline_allowed else 'closed'}")

    # Try hermes mcp list to verify the legacy MCP server is or is not registered
    try:
        result = subprocess.run(
            ["hermes", "mcp", "list"], capture_output=True, text=True, timeout=10
        )
        has_legacy_mcp = "kahzaabu" in (result.stdout or "")
        if has_legacy_mcp:
            print("\n  ⚠️  Legacy MCP server 'kahzaabu' is still registered in "
                   "~/.hermes/config.yaml.\n     The native plugin supersedes it. "
                   "Remove with:  hermes mcp remove kahzaabu")
    except Exception:
        pass

    print()
    return 0 if ok else 1


def _cmd_web(args) -> int:
    cli = kahzaabu_cli_path()
    home = kahzaabu_home()
    if cli is None or home is None:
        print("❌ kahzaabu .venv not found — `web` needs the full-deps venv "
              "(fastapi, uvicorn, ...).")
        print("   Run `hermes kahzaabu doctor` for remediation.")
        return 2
    cmd = [str(cli), "web", "--port", str(args.port), "--host", args.host]
    print(f"→ {' '.join(cmd)}")
    return subprocess.call(cmd, cwd=str(home))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hermes_provider() -> tuple[str, str]:
    """Return (provider, model) from ~/.hermes/config.yaml — best-effort."""
    try:
        import yaml
        cfg = yaml.safe_load(HERMES_CONFIG.read_text())
        m = cfg.get("model", {})
        return (m.get("provider", "unknown"), m.get("default", "unknown"))
    except Exception:
        return ("unknown", "unknown")


def _has_anthropic_key() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    if HERMES_ENV.exists():
        for line in HERMES_ENV.read_text().splitlines():
            if line.startswith("ANTHROPIC_API_KEY="):
                return True
    return False


def _append_env(key: str, value: str) -> None:
    """Idempotent: replace existing line or append."""
    lines: list[str] = []
    if HERMES_ENV.exists():
        lines = HERMES_ENV.read_text().splitlines()
    out = [ln for ln in lines if not ln.startswith(f"{key}=")]
    out.append(f"{key}={value}")
    HERMES_ENV.write_text("\n".join(out) + "\n")
