"""Interactive TUI for kahzaabu with slash commands.

Usage: kahzaabu tui
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import claims_db, db
from .qna_agentic import ask_agentic

logger = logging.getLogger("kahzaabu")
logger.setLevel(logging.WARNING)  # quiet kahzaabu logs in the TUI
# Silence httpx info-level logs from the anthropic SDK
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)

console = Console()
HISTORY_FILE = Path.home() / ".cache" / "kahzaabu_history"
HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)


SLASH_COMMANDS = [
    "/help", "/stats", "/lies", "/promises", "/credit", "/recent",
    "/date", "/location", "/topic", "/filters", "/clear",
    "/cost", "/pipeline", "/new", "/exit", "/quit", "/q",
]


@dataclass
class Session:
    db_path: Path
    conn: sqlite3.Connection
    date_filter: Optional[str] = None       # natural-language phrase, prepended to questions
    location_filter: Optional[str] = None
    topic_filter: Optional[str] = None
    session_cost: float = 0.0
    qna_session_id: Optional[str] = None    # agentic ask conversation thread


def render_intent(intent: dict) -> str:
    parts = [f"intent={intent.get('intent', '?')}"]
    df, dt = intent.get("date_from"), intent.get("date_to")
    if df or dt:
        parts.append(f"dates={df or '(open)'}..{dt or '(open)'}")
    locs = intent.get("location_keywords") or []
    if locs:
        parts.append(f"loc={locs}")
    topics = intent.get("topic_keywords") or []
    if topics:
        parts.append(f"topics={topics}")
    cats = intent.get("fact_check_categories") or []
    if cats:
        parts.append(f"cats={cats}")
    return "  ".join(parts)


def apply_session_filters(question: str, sess: Session) -> str:
    """Prepend session filters as natural-language hints if not already in the question."""
    qlow = question.lower()
    extras = []
    if sess.date_filter and not any(
        kw in qlow for kw in ("this week", "this month", "this year", "last week",
                              "last month", "since taking office", "in 20")
    ):
        extras.append(f"({sess.date_filter})")
    if sess.location_filter and sess.location_filter.lower() not in qlow:
        extras.append(f"(in {sess.location_filter})")
    if sess.topic_filter and sess.topic_filter.lower() not in qlow:
        extras.append(f"(about {sess.topic_filter})")
    if not extras:
        return question
    return f"{question} {' '.join(extras)}"


def cmd_help(sess: Session, args: list[str]):
    table = Table(title="Slash commands", show_lines=False, box=None)
    table.add_column("Command", style="bold cyan")
    table.add_column("Description")
    rows = [
        ("/help", "Show this help"),
        ("/stats", "Claims + fact-check coverage"),
        ("/lies [topic]", "Recent fact-checked lies/contradictions"),
        ("/promises [topic]", "Recent promises (numeric or with deadlines)"),
        ("/credit", "Credit-theft items"),
        ("/recent [n]", "Last N articles (default 10)"),
        ("/date <phrase>", "Set session date filter (e.g. 'this week', '2024')"),
        ("/location <name>", "Set session location filter (e.g. 'Hulhumale')"),
        ("/topic <kw>", "Set session topic filter (e.g. 'housing')"),
        ("/filters", "Show current session filters"),
        ("/clear", "Clear all session filters"),
        ("/cost", "Today's API spend + session total"),
        ("/pipeline", "Run pipeline manually (one cycle)"),
        ("/exit  /quit  /q", "Quit"),
        ("(anything else)", "Natural-language question — goes to LLM Q&A"),
    ]
    for cmd, desc in rows:
        table.add_row(cmd, desc)
    console.print(table)
    console.print("\n[dim]Examples:[/dim]")
    console.print('  [dim]→[/dim] what is kahzaabu up to this week?')
    console.print('  [dim]→[/dim] /lies housing')
    console.print('  [dim]→[/dim] /date this-month  then  what lies did he tell?')


def cmd_stats(sess: Session, args: list[str]):
    s = claims_db.stats(sess.conn)
    t = Table(box=None)
    t.add_column("Metric", style="bold")
    t.add_column("Value", justify="right")
    t.add_row("Muizzu-era articles (EN, with body)", str(s["n_articles_muizzu_total"]))
    t.add_row("Articles with claims extracted", f"{s['n_articles_with_claims']} ({s['coverage_pct']}%)")
    t.add_row("Total claims", str(s["n_claims"]))
    t.add_row("Total fact-checks", str(s["n_fact_checks"]))
    console.print(t)
    if s.get("last_extraction"):
        e = s["last_extraction"]
        console.print(f"\n[dim]Last extraction: {e['status']} on {e['started_at'][:19]} — "
                      f"{e['articles_processed']} articles, ${e['cost_usd']:.2f}[/dim]")
    if s.get("last_curation"):
        c = s["last_curation"]
        console.print(f"[dim]Last curation:   {c['status']} on {c['started_at'][:19]} — "
                      f"{c['new_items']} new items, ${c['cost_usd']:.2f}[/dim]")


def cmd_lies(sess: Session, args: list[str]):
    topic = " ".join(args) if args else sess.topic_filter
    q = "what lies did kahzaabu tell"
    if topic:
        q += f" about {topic}"
    _run_question(q, sess)


def cmd_promises(sess: Session, args: list[str]):
    topic = " ".join(args) if args else sess.topic_filter
    q = "what did kahzaabu promise"
    if topic:
        q += f" about {topic}"
    _run_question(q, sess)


def cmd_credit(sess: Session, args: list[str]):
    _run_question("what credit-theft has been documented?", sess)


def cmd_recent(sess: Session, args: list[str]):
    n = 10
    if args:
        try:
            n = int(args[0])
        except ValueError:
            pass
    rows = sess.conn.execute(
        """SELECT id, title, published_date, category
           FROM articles
           WHERE language='EN' AND body_text IS NOT NULL AND body_text != ''
             AND published_date >= '2023-11-17'
             AND category IN ('press_release','speech','vp_speech')
           ORDER BY published_date DESC, id DESC LIMIT ?""",
        (n,)
    ).fetchall()
    t = Table(title=f"Last {len(rows)} articles", box=None)
    t.add_column("ID", style="dim", justify="right")
    t.add_column("Date", style="cyan")
    t.add_column("Cat", style="yellow")
    t.add_column("Title")
    for r in rows:
        t.add_row(str(r["id"]), r["published_date"][:10], r["category"][:6], r["title"][:90])
    console.print(t)


def cmd_date(sess: Session, args: list[str]):
    if not args:
        console.print(f"[dim]current date filter: {sess.date_filter or '(none)'}[/dim]")
        return
    sess.date_filter = " ".join(args).replace("-", " ")
    console.print(f"[green]date filter set:[/green] {sess.date_filter}")


def cmd_location(sess: Session, args: list[str]):
    if not args:
        console.print(f"[dim]current location filter: {sess.location_filter or '(none)'}[/dim]")
        return
    sess.location_filter = " ".join(args)
    console.print(f"[green]location filter set:[/green] {sess.location_filter}")


def cmd_topic(sess: Session, args: list[str]):
    if not args:
        console.print(f"[dim]current topic filter: {sess.topic_filter or '(none)'}[/dim]")
        return
    sess.topic_filter = " ".join(args)
    console.print(f"[green]topic filter set:[/green] {sess.topic_filter}")


def cmd_new(sess: Session, args: list[str]):
    """Start a fresh conversation thread (clears qna_session_id)."""
    sess.qna_session_id = None
    console.print("[green]conversation reset[/green] — next question starts a new thread")


def cmd_filters(sess: Session, args: list[str]):
    t = Table(box=None)
    t.add_column("Filter")
    t.add_column("Value")
    t.add_row("date", sess.date_filter or "[dim](none)[/dim]")
    t.add_row("location", sess.location_filter or "[dim](none)[/dim]")
    t.add_row("topic", sess.topic_filter or "[dim](none)[/dim]")
    console.print(t)


def cmd_clear(sess: Session, args: list[str]):
    sess.date_filter = None
    sess.location_filter = None
    sess.topic_filter = None
    console.print("[green]filters cleared[/green]")


def cmd_cost(sess: Session, args: list[str]):
    daily = claims_db.daily_spend(sess.conn)
    console.print(f"Today (UTC):    [bold]${daily:.2f}[/bold]")
    console.print(f"This session:   [bold]${sess.session_cost:.4f}[/bold]")


def cmd_pipeline(sess: Session, args: list[str]):
    if "ANTHROPIC_API_KEY" not in os.environ:
        console.print("[red]ANTHROPIC_API_KEY not set; pipeline LLM stages will skip[/red]")
    from .pipeline import run_pipeline
    with console.status("[bold green]running pipeline..."):
        res = run_pipeline(sess.db_path, daily_budget_usd=1.0)
    console.print(Panel(json.dumps(res, indent=2, default=str), title="pipeline result"))


def _run_question(question: str, sess: Session):
    if "ANTHROPIC_API_KEY" not in os.environ:
        console.print("[red]ANTHROPIC_API_KEY not set. Run:[/red]")
        console.print("  [dim]export ANTHROPIC_API_KEY=$(cat ~/.config/kahzaabu/api_key)[/dim]")
        return
    q = apply_session_filters(question, sess)
    if q != question:
        console.print(f"[dim](with filters: {q})[/dim]")
    try:
        with console.status("[bold green]researching..."):
            res = ask_agentic(sess.conn, q, session_id=sess.qna_session_id,
                              daily_budget_usd=20.0)
    except Exception as e:
        console.print(f"[red]error:[/red] {e}")
        return
    sess.session_cost += res["cost_usd"]
    if not sess.qna_session_id:
        sess.qna_session_id = res["session_id"]
    # Trace + cost line
    tool_calls = res.get("tool_trace") or []
    tool_summary = ", ".join(f"{t['tool']}" for t in tool_calls[:6]) or "(no tools)"
    console.print(Text(
        f"🧵 session {res['session_id'][:8]}  •  {res['n_iterations']} iter  "
        f"•  tools: {tool_summary}  •  cost ${res['cost_usd']:.4f}",
        style="dim",
    ))
    console.print()
    if res.get("answer"):
        console.print(Markdown(res["answer"]))
    else:
        console.print("[dim](no LLM answer)[/dim]")


SLASH_HANDLERS = {
    "/help": cmd_help,
    "/stats": cmd_stats,
    "/lies": cmd_lies,
    "/promises": cmd_promises,
    "/credit": cmd_credit,
    "/recent": cmd_recent,
    "/date": cmd_date,
    "/location": cmd_location,
    "/topic": cmd_topic,
    "/filters": cmd_filters,
    "/clear": cmd_clear,
    "/cost": cmd_cost,
    "/pipeline": cmd_pipeline,
    "/new": cmd_new,
}


def run_tui(db_path: Path):
    conn = db.get_connection(db_path)
    db.init_db(conn)
    claims_db.init_claims_schema(conn)
    conn.row_factory = sqlite3.Row
    sess = Session(db_path=db_path, conn=conn)

    # Banner
    s = claims_db.stats(conn)
    console.print(Panel(
        Text.from_markup(
            "[bold]kahzaabu[/bold]  —  Maldives Presidency archive\n"
            f"{s['n_articles_muizzu_total']} articles  •  "
            f"{s['n_claims']} claims  •  "
            f"{s['n_fact_checks']} fact-checks\n\n"
            "[dim]Type a question, or [/dim][cyan]/help[/cyan][dim] for commands.[/dim]"
        ),
        border_style="cyan",
    ))

    completer = WordCompleter(SLASH_COMMANDS, ignore_case=True, match_middle=False)
    style = Style.from_dict({"prompt": "ansicyan bold"})
    session = PromptSession(
        history=FileHistory(str(HISTORY_FILE)),
        completer=completer,
        style=style,
    )

    while True:
        try:
            text = session.prompt("kahzaabu> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye.[/dim]")
            break
        if not text:
            continue
        if text.startswith("/"):
            parts = text.split()
            cmd = parts[0].lower()
            args = parts[1:]
            if cmd in ("/exit", "/quit", "/q"):
                console.print("[dim]bye.[/dim]")
                break
            handler = SLASH_HANDLERS.get(cmd)
            if handler is None:
                console.print(f"[red]unknown command:[/red] {cmd}  ([dim]/help[/dim])")
                continue
            try:
                handler(sess, args)
            except Exception as e:
                console.print(f"[red]error in {cmd}:[/red] {e}")
        else:
            _run_question(text, sess)
        console.print()
