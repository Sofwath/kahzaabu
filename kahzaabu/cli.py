import csv
import io
import json
import logging
import sys
from pathlib import Path

import click

from . import db, scraper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("kahzaabu")

CATEGORY_NAMES = list(scraper.CATEGORIES.keys())


@click.group()
@click.option("--db-path", default=None, help="Path to SQLite database")
@click.pass_context
def main(ctx, db_path):
    """Kahzaabu - Maldives Presidency content archiver."""
    ctx.ensure_object(dict)
    path = Path(db_path) if db_path else db.DEFAULT_DB_PATH
    conn = db.get_connection(path)
    db.init_db(conn)
    ctx.obj["conn"] = conn
    ctx.obj["db_path"] = path


@main.command()
@click.option("--category", type=click.Choice(CATEGORY_NAMES), default=None, help="Scrape specific category")
@click.option("--resume", is_flag=True, help="Resume last interrupted backfill")
@click.option("--no-dhivehi", is_flag=True, help="Skip Dhivehi versions")
@click.pass_context
def backfill(ctx, category, resume, no_dhivehi):
    """Full backfill of all content."""
    conn = ctx.obj["conn"]
    session = scraper.create_session()
    categories = [category] if category else CATEGORY_NAMES

    for cat_name in categories:
        start_page = 1
        if resume:
            cat_id = scraper.CATEGORIES[cat_name]["id"]
            last_run = db.get_last_run(conn, cat_id, "EN+DV")
            if last_run and last_run["status"] == "interrupted":
                start_page = last_run["resume_page"]
                click.echo(f"Resuming '{cat_name}' from page {start_page}")

        click.echo(f"Backfilling '{cat_name}'...")
        try:
            new = scraper.scrape_category(
                session,
                conn,
                cat_name,
                mode="backfill",
                start_page=start_page,
                fetch_dhivehi=not no_dhivehi,
            )
            click.echo(f"  Done: {new} new articles")
        except KeyboardInterrupt:
            click.echo("\nInterrupted. Use --resume to continue.")
            sys.exit(1)


@main.command()
@click.option("--category", type=click.Choice(CATEGORY_NAMES), default=None)
@click.option("--no-dhivehi", is_flag=True)
@click.pass_context
def update(ctx, category, no_dhivehi):
    """Incremental scrape for new content."""
    conn = ctx.obj["conn"]
    session = scraper.create_session()
    categories = [category] if category else CATEGORY_NAMES

    total_new = 0
    for cat_name in categories:
        click.echo(f"Updating '{cat_name}'...")
        new = scraper.scrape_category(
            session, conn, cat_name, mode="incremental", fetch_dhivehi=not no_dhivehi
        )
        total_new += new
        click.echo(f"  {new} new articles")

    click.echo(f"Total: {total_new} new articles")


@main.command()
@click.option("--interval", default=12.0, type=float, help="Hours between pipeline runs")
@click.option("--scrape-only", is_flag=True, help="Skip extraction + curation (legacy behaviour)")
@click.option("--budget", default=1.0, type=float, help="Daily LLM budget cap in USD")
@click.option("--curate-min-age", default=168.0, type=float,
              help="Minimum hours between curation runs (default 168 = weekly)")
@click.option("--verify-limit", default=5, type=int,
              help="Max fact-checks to web-verify per cycle (~$0.17/item)")
@click.pass_context
def schedule(ctx, interval, scrape_only, budget, curate_min_age, verify_limit):
    """Run scrape + LLM extract + LLM curate + web verify on a loop."""
    if scrape_only:
        from .scheduler import run_scheduled
        run_scheduled(ctx.obj["db_path"], interval_hours=interval)
    else:
        from .pipeline import run_scheduled as run_pipeline_scheduled
        run_pipeline_scheduled(
            ctx.obj["db_path"],
            interval_hours=interval,
            daily_budget_usd=budget,
            curate_min_age_hours=curate_min_age,
            verify_limit_per_cycle=verify_limit,
        )


@main.command()
@click.option("--budget", default=1.0, type=float, help="Daily LLM budget cap in USD")
@click.option("--concurrency", default=6, type=int)
@click.option("--limit", default=0, type=int, help="Cap to N articles (testing)")
@click.pass_context
def extract(ctx, budget, concurrency, limit):
    """Extract claims from articles that don't have them yet (LLM)."""
    from .extractor import run_extraction
    from . import claims_db

    conn = ctx.obj["conn"]
    claims_db.init_claims_schema(conn)

    def _progress(done, total, t_in, t_out, cost):
        if done % 25 == 0 or done == total:
            click.echo(f"  {done}/{total}  tokens_in={t_in} out={t_out}  cost=${cost:.2f}")

    res = run_extraction(
        conn, concurrency=concurrency, daily_budget_usd=budget,
        limit=(limit or None), progress_cb=_progress,
    )
    if res.get("skipped"):
        click.echo(f"Skipped: {res.get('reason')} (today_spent=${res.get('today_spent', 0):.2f})")
    else:
        click.echo(f"\nExtracted: {res.get('articles_processed', 0)} articles, "
                   f"{res.get('claims_extracted', 0)} claims, ${res.get('cost_usd', 0):.2f}")


@main.command()
@click.option("--budget", default=1.0, type=float, help="Daily LLM budget cap in USD")
@click.option("--days-back", default=7, type=int)
@click.option("--full", is_flag=True, help="Curate over ALL claims, not just recent")
@click.option("--concurrency", default=4, type=int)
@click.pass_context
def curate(ctx, budget, days_back, full, concurrency):
    """Run LLM curation pass over recent claims; insert new fact-checks."""
    from .curator import run_curation
    from . import claims_db

    conn = ctx.obj["conn"]
    claims_db.init_claims_schema(conn)

    def _progress(topic, chunk, n_new, cost):
        click.echo(f"  [{topic} ch{chunk}] new={n_new} cost=${cost:.2f}")

    res = run_curation(
        conn, days_back=days_back, force_full=full,
        concurrency=concurrency, daily_budget_usd=budget,
        progress_cb=_progress,
    )
    if res.get("skipped"):
        click.echo(f"Skipped: {res.get('reason')}")
    else:
        click.echo(f"\nCuration: {res.get('proposed', 0)} proposed, "
                   f"{res.get('inserted', 0)} inserted, "
                   f"{res.get('duplicates', 0)} dupes, ${res.get('cost_usd', 0):.2f}")


@main.command()
@click.option("--budget", default=1.0, type=float, help="Daily LLM budget cap in USD")
@click.option("--no-scrape", is_flag=True)
@click.option("--no-extract", is_flag=True)
@click.option("--no-inspect", is_flag=True)
@click.option("--no-curate", is_flag=True)
@click.option("--no-verify", is_flag=True)
@click.option("--no-dv-compare", is_flag=True)
@click.option("--curate-min-age", default=168.0, type=float)
@click.option("--verify-limit", default=5, type=int)
@click.option("--inspect-limit", default=10, type=int)
@click.option("--dv-compare-limit", default=5, type=int)
@click.pass_context
def pipeline(ctx, budget, no_scrape, no_extract, no_inspect, no_curate, no_verify,
             no_dv_compare, curate_min_age, verify_limit, inspect_limit, dv_compare_limit):
    """Run scrape + extract + inspect + curate + verify + dv-compare end-to-end."""
    from .pipeline import run_pipeline
    res = run_pipeline(
        ctx.obj["db_path"], scrape=not no_scrape,
        extract=not no_extract, inspect_stage=not no_inspect,
        curate=not no_curate, verify=not no_verify, dv_compare_stage=not no_dv_compare,
        daily_budget_usd=budget, curate_min_age_hours=curate_min_age,
        verify_limit_per_cycle=verify_limit,
        inspect_limit_per_cycle=inspect_limit,
        dv_compare_limit_per_cycle=dv_compare_limit,
    )
    click.echo("\n=== Pipeline summary ===")
    if res.get("scrape"):
        click.echo(f"  scrape:    {res['scrape']}")
    if res.get("extract"):
        click.echo(f"  extract:   articles={res['extract'].get('articles_processed', 0)} "
                   f"claims={res['extract'].get('claims_extracted', 0)} "
                   f"cost=${res['extract'].get('cost_usd', 0):.2f}")
    if res.get("inspect"):
        ri = res["inspect"]
        if ri.get("skipped"):
            click.echo(f"  inspect:   SKIPPED ({ri.get('reason')})")
        else:
            click.echo(f"  inspect:   cards={ri.get('cards_generated', 0)} "
                       f"flag={ri.get('flagged', 0)} red_flag={ri.get('red_flagged', 0)} "
                       f"cost=${ri.get('cost_usd', 0):.2f}")
    if res.get("curate"):
        rc = res["curate"]
        if rc.get("skipped"):
            click.echo(f"  curate:    SKIPPED ({rc.get('reason')})")
        else:
            click.echo(f"  curate:    proposed={rc.get('proposed', 0)} "
                       f"inserted={rc.get('inserted', 0)} "
                       f"cost=${rc.get('cost_usd', 0):.2f}")
    if res.get("verify"):
        rv = res["verify"]
        if rv.get("skipped"):
            click.echo(f"  verify:    SKIPPED ({rv.get('reason')})")
        else:
            click.echo(f"  verify:    items={rv.get('items_processed', 0)} "
                       f"evidence={rv.get('evidence_collected', 0)} "
                       f"searches={rv.get('web_searches', 0)} "
                       f"cost=${rv.get('cost_usd', 0):.2f}")
    if res.get("dv_compare"):
        rd = res["dv_compare"]
        if rd.get("skipped"):
            click.echo(f"  dv-compare: SKIPPED ({rd.get('reason')})")
        else:
            click.echo(f"  dv-compare: pairs={rd.get('pairs_processed', 0)} "
                       f"flagged={rd.get('pairs_with_issues', 0)} "
                       f"inconsistencies={rd.get('inconsistencies_logged', 0)} "
                       f"cost=${rd.get('cost_usd', 0):.2f}")
    click.echo(f"  today_spend: ${res.get('today_spend_usd', 0):.2f}")
    click.echo(f"  total_claims: {res['stats']['n_claims']}  "
               f"coverage: {res['stats']['coverage_pct']}%  "
               f"fact_checks: {res['stats']['n_fact_checks']}")


@main.command()
@click.option("--budget", default=1.0, type=float, help="Daily LLM+search budget in USD")
@click.option("--limit", default=20, type=int, help="Cap articles per run (0=no cap)")
@click.option("--concurrency", default=4, type=int)
@click.option("--no-web", is_flag=True, help="Skip web verification for flagged items")
@click.pass_context
def inspect(ctx, budget, limit, concurrency, no_web):
    """Generate per-article fact cards for articles missing one (LLM)."""
    from .inspector import run_inspection
    from . import claims_db

    conn = ctx.obj["conn"]
    claims_db.init_claims_schema(conn)

    def _progress(done, total, flagged, red_flagged, cost):
        if done % 5 == 0 or done == total:
            click.echo(f"  {done}/{total} flagged={flagged} red_flagged={red_flagged} cost=${cost:.2f}")

    res = run_inspection(
        conn, limit=(limit or None), concurrency=concurrency,
        daily_budget_usd=budget, web_verify_flagged=not no_web,
        progress_cb=_progress,
    )
    if res.get("skipped"):
        click.echo(f"Skipped: {res.get('reason')}")
    else:
        click.echo(f"\nInspect: {res.get('cards_generated', 0)} cards "
                   f"({res.get('flagged', 0)} flag, {res.get('red_flagged', 0)} red_flag), "
                   f"{res.get('web_searches', 0)} searches, "
                   f"${res.get('cost_usd', 0):.2f}")


@main.command(name="dv-compare")
@click.option("--budget", default=1.0, type=float, help="Daily LLM budget in USD")
@click.option("--limit", default=20, type=int)
@click.option("--since-date", default="2024-01-01")
@click.option("--all-paired", is_flag=True, help="Compare ALL paired articles (not just claim-bearing)")
@click.option("--concurrency", default=3, type=int)
@click.pass_context
def dv_compare(ctx, budget, limit, since_date, all_paired, concurrency):
    """Compare paired EN/DV press releases for factual differences (LLM)."""
    from .dv_compare import run_dv_compare
    from . import claims_db

    conn = ctx.obj["conn"]
    claims_db.init_claims_schema(conn)

    def _progress(done, total, inconsistencies, cost):
        if done % 3 == 0 or done == total:
            click.echo(f"  {done}/{total} inconsistencies={inconsistencies} cost=${cost:.2f}")

    res = run_dv_compare(
        conn, limit=limit, since_date=since_date,
        require_claims=not all_paired,
        concurrency=concurrency, daily_budget_usd=budget,
        progress_cb=_progress,
    )
    if res.get("skipped"):
        click.echo(f"Skipped: {res.get('reason')}")
    else:
        click.echo(f"\nDV-compare: {res.get('pairs_processed', 0)} pairs, "
                   f"{res.get('pairs_with_issues', 0)} flagged, "
                   f"{res.get('inconsistencies_logged', 0)} inconsistencies, "
                   f"${res.get('cost_usd', 0):.2f}")


@main.command()
@click.option("--budget", default=1.0, type=float, help="Daily LLM+search budget in USD")
@click.option("--limit", default=20, type=int, help="Cap fact-checks per run")
@click.option("--concurrency", default=3, type=int)
@click.option("--categories", default="LIE,CONTRADICTION,SHIFTING NUMBERS,CREDIT THEFT",
              help="Comma-separated categories to verify")
@click.pass_context
def verify(ctx, budget, limit, concurrency, categories):
    """Web-search-verify fact-checks (only items that haven't been verified yet)."""
    from .verifier import run_verification
    from . import claims_db

    conn = ctx.obj["conn"]
    claims_db.init_claims_schema(conn)
    cats = tuple(c.strip() for c in categories.split(",") if c.strip())

    def _progress(done, total, searches, cost):
        click.echo(f"  {done}/{total} searches={searches} cost=${cost:.2f}")

    res = run_verification(
        conn, limit=(limit or None), categories=cats,
        concurrency=concurrency, daily_budget_usd=budget,
        progress_cb=_progress,
    )
    if res.get("skipped"):
        click.echo(f"Skipped: {res.get('reason')}")
    else:
        click.echo(f"\nVerify: {res.get('items_processed', 0)} items, "
                   f"{res.get('evidence_collected', 0)} evidence rows, "
                   f"{res.get('web_searches', 0)} searches, "
                   f"${res.get('cost_usd', 0):.2f}")


@main.command()
@click.pass_context
def tui(ctx):
    """Interactive terminal UI with slash commands and natural-language Q&A."""
    from .tui import run_tui
    run_tui(ctx.obj["db_path"])


@main.command(name="manifesto-extract")
@click.option("--text-file", default="data/manifesto/drmuizzu2023.txt", type=click.Path())
@click.option("--budget", default=10.0, type=float)
@click.option("--limit-chunks", default=0, type=int, help="Cap chunks (0=all)")
@click.option("--concurrency", default=4, type=int)
@click.pass_context
def manifesto_extract(ctx, text_file, budget, limit_chunks, concurrency):
    """Extract promises from the manifesto text file (LLM)."""
    from .manifesto import run_extraction
    from . import claims_db

    text = Path(text_file).read_text()
    conn = ctx.obj["conn"]
    claims_db.init_claims_schema(conn)

    def _p(done, total, promises, cost):
        if done % 5 == 0 or done == total:
            click.echo(f"  chunk {done}/{total}  promises={promises}  cost=${cost:.2f}")

    res = run_extraction(conn, text, concurrency=concurrency,
                         daily_budget_usd=budget,
                         limit_chunks=(limit_chunks or None),
                         progress_cb=_p)
    if res.get("skipped"):
        click.echo(f"Skipped: {res.get('reason')}")
    else:
        click.echo(f"\n{res['promises']} promises extracted from {res['chunks']} chunks, "
                   f"${res['cost_usd']:.2f}")


@main.command(name="manifesto-crossref")
@click.option("--budget", default=10.0, type=float)
@click.option("--limit", default=0, type=int)
@click.option("--all", "redo_all", is_flag=True,
              help="Re-cross-ref even promises that already have a status")
@click.option("--concurrency", default=4, type=int)
@click.pass_context
def manifesto_crossref(ctx, budget, limit, redo_all, concurrency):
    """For each manifesto promise, cross-reference against claims+fact-checks to set delivery_status."""
    from .manifesto import run_cross_ref
    from . import claims_db

    conn = ctx.obj["conn"]
    claims_db.init_claims_schema(conn)

    def _p(done, total, status_counts, cost):
        if done % 5 == 0 or done == total:
            click.echo(f"  {done}/{total}  statuses={status_counts}  cost=${cost:.2f}")

    res = run_cross_ref(conn, limit=(limit or None), concurrency=concurrency,
                        daily_budget_usd=budget, only_unmentioned=not redo_all,
                        progress_cb=_p)
    if res.get("skipped"):
        click.echo(f"Skipped: {res.get('reason')}")
    else:
        click.echo(f"\nCross-ref: {res['processed']} promises, "
                   f"statuses={res['status_counts']}, ${res['cost_usd']:.2f}")


@main.command(name="create-user")
@click.argument("username")
@click.option("--role", default="admin", type=click.Choice(["admin", "editor"]))
@click.option("--password", default=None, help="If omitted, you'll be prompted")
@click.pass_context
def create_user(ctx, username, role, password):
    """Create an admin/editor user for the web admin."""
    from . import auth as kauth, claims_db
    conn = ctx.obj["conn"]
    claims_db.init_claims_schema(conn)
    if claims_db.get_user(conn, username):
        click.echo(f"user '{username}' already exists. Use set-password to change.", err=True)
        ctx.exit(1)
    if not password:
        password = click.prompt("password", hide_input=True, confirmation_prompt=True)
    claims_db.create_user(conn, username, kauth.hash_password(password), role=role)
    click.echo(f"created user '{username}' with role={role}")


@main.command(name="set-password")
@click.argument("username")
@click.option("--password", default=None)
@click.pass_context
def set_password(ctx, username, password):
    """Set/reset a web user's password."""
    from . import auth as kauth, claims_db
    conn = ctx.obj["conn"]
    claims_db.init_claims_schema(conn)
    if not claims_db.get_user(conn, username):
        click.echo(f"user '{username}' not found", err=True)
        ctx.exit(1)
    if not password:
        password = click.prompt("new password", hide_input=True, confirmation_prompt=True)
    n = claims_db.update_user_password(conn, username, kauth.hash_password(password))
    click.echo(f"updated password for '{username}' ({n} row)")


@main.command()
@click.argument("fact_check_id", type=int)
@click.option("--unpublish", is_flag=True, help="Set published=0 instead of 1")
@click.option("--reviewer", default="cli")
@click.pass_context
def publish(ctx, fact_check_id, unpublish, reviewer):
    """Publish (or unpublish) a fact-check by ID."""
    from . import claims_db
    conn = ctx.obj["conn"]
    claims_db.init_claims_schema(conn)
    n = claims_db.set_fact_check_published(
        conn, fact_check_id,
        published=not unpublish, reviewed_by=reviewer,
    )
    if n == 0:
        click.echo(f"fact_check {fact_check_id} not found", err=True)
        ctx.exit(1)
    click.echo(f"fact_check {fact_check_id} {'unpublished' if unpublish else 'published'} by {reviewer}")


@main.command()
@click.option("--host", default="127.0.0.1", help="Bind host (use 0.0.0.0 for LAN/public)")
@click.option("--port", default=8765, type=int)
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (dev)")
@click.pass_context
def web(ctx, host, port, reload):
    """Start the FastAPI web UI."""
    import uvicorn
    # The app reads the DB path via kahzaabu.web.db_dep.DEFAULT_DB; we keep that
    # global rather than passing through env to avoid worker-spawn complications.
    click.echo(f"kahzaabu web → http://{host}:{port}")
    click.echo("  endpoints:  /  /browse  /lies  /ask  /api/docs")
    uvicorn.run("kahzaabu.web.app:app", host=host, port=port, reload=reload)


@main.command()
@click.argument("question", nargs=-1, required=True)
@click.option("--limit", default=20, type=int, help="Max rows to consider")
@click.option("--no-llm", is_flag=True, help="Skip LLM summarization; dump rows only")
@click.option("--json", "as_json", is_flag=True, help="Output structured JSON")
@click.pass_context
def ask(ctx, question, limit, no_llm, as_json):
    """Ask a natural-language question about the archive.

    Examples:
      kahzaabu ask what is kahzaabu up to this week?
      kahzaabu ask what lies did muizzu tell about housing?
      kahzaabu ask what did he say in Vaadhoo?
    """
    from .qna import ask as ask_fn
    conn = ctx.obj["conn"]
    q = " ".join(question)
    res = ask_fn(conn, q, default_limit=limit, format_with_llm=not no_llm)

    if as_json:
        import json as _json
        click.echo(_json.dumps(res, indent=2, ensure_ascii=False))
        return

    click.echo(f"Question: {q}")
    click.echo(f"Intent:   {res['intent']}")
    click.echo(f"Matches:  {res['n_matches']}")
    click.echo(f"Cost:     ${res['cost_usd']:.4f}")
    click.echo("")
    if no_llm:
        import json as _json
        for r in res["rows"][:limit]:
            click.echo(_json.dumps(r, ensure_ascii=False)[:400])
    else:
        click.echo("=" * 60)
        click.echo(res["answer"])


@main.command()
@click.option("--out-dir", default="data/exports", type=click.Path())
@click.pass_context
def report(ctx, out_dir):
    """Export fact_checks + claims to JSON in data/exports/."""
    from .report import export_all
    out = Path(out_dir)
    res = export_all(ctx.obj["db_path"], out)
    click.echo(f"Wrote to {out}/")
    for k, v in res.items():
        click.echo(f"  {k}: {v}")


@main.command()
@click.pass_context
def claims_stats(ctx):
    """Show claims/fact-check coverage stats."""
    from . import claims_db

    conn = ctx.obj["conn"]
    claims_db.init_claims_schema(conn)
    s = claims_db.stats(conn)
    click.echo(f"Articles (Muizzu era, EN, with body): {s['n_articles_muizzu_total']}")
    click.echo(f"  with claims extracted: {s['n_articles_with_claims']} ({s['coverage_pct']}%)")
    click.echo(f"Total claims: {s['n_claims']}")
    click.echo(f"Fact-checks: {s['n_fact_checks']}")
    if s["last_extraction"]:
        e = s["last_extraction"]
        click.echo(f"\nLast extraction run #{e['id']}: status={e['status']}")
        click.echo(f"  started={e['started_at']}  finished={e['finished_at']}")
        click.echo(f"  articles={e['articles_processed']} claims={e['claims_extracted']} "
                   f"cost=${e['cost_usd']:.2f}")
    if s["last_curation"]:
        c = s["last_curation"]
        click.echo(f"\nLast curation run #{c['id']}: status={c['status']}")
        click.echo(f"  started={c['started_at']}  finished={c['finished_at']}")
        click.echo(f"  new_items={c['new_items']} cost=${c['cost_usd']:.2f}")


@main.command()
@click.pass_context
def stats(ctx):
    """Show archive statistics."""
    conn = ctx.obj["conn"]
    rows = db.get_stats(conn)
    if not rows:
        click.echo("No articles in database yet.")
        return

    click.echo(f"\n{'Category':<20} {'Lang':<6} {'Count':>8} {'Earliest':<12} {'Latest':<12}")
    click.echo("-" * 62)
    total = 0
    for row in rows:
        click.echo(
            f"{row['category']:<20} {row['language']:<6} {row['count']:>8} "
            f"{row['earliest'] or 'N/A':<12} {row['latest'] or 'N/A':<12}"
        )
        total += row["count"]
    click.echo("-" * 62)
    click.echo(f"{'Total':<28} {total:>8}")


@main.command()
@click.argument("query")
@click.option("--limit", default=50)
@click.pass_context
def search(ctx, query, limit):
    """Search articles by text."""
    conn = ctx.obj["conn"]
    rows = db.search_articles(conn, query, limit)
    if not rows:
        click.echo("No results.")
        return

    for row in rows:
        click.echo(
            f"\n[{row['id']}] ({row['language']}) {row['category']}\n"
            f"  {row['title']}\n"
            f"  {row['published_date']}\n"
            f"  {row['snippet']}..."
        )
    click.echo(f"\n{len(rows)} results")


@main.command()
@click.argument("article_id", type=int)
@click.option("--language", default="EN")
@click.pass_context
def article(ctx, article_id, language):
    """Show a specific article."""
    conn = ctx.obj["conn"]
    row = db.get_article(conn, article_id, language)
    if not row:
        click.echo(f"Article {article_id} ({language}) not found.")
        return

    click.echo(f"\nID: {row['id']} ({row['language']})")
    click.echo(f"Category: {row['category']}")
    click.echo(f"Date: {row['published_date']}")
    click.echo(f"Reference: {row['reference'] or 'N/A'}")
    if row["paired_id"]:
        click.echo(f"Paired: {row['paired_id']}")
    click.echo(f"\n{row['title']}")
    click.echo("=" * 60)
    click.echo(row["body_text"] or "(no body text)")


@main.command()
@click.option("--format", "fmt", type=click.Choice(["csv", "json"]), default="json")
@click.option("--category", type=click.Choice(CATEGORY_NAMES), default=None)
@click.option("--language", default=None)
@click.option("--output", "-o", default=None, help="Output file (default: stdout)")
@click.pass_context
def export(ctx, fmt, category, language, output):
    """Export articles to CSV or JSON."""
    conn = ctx.obj["conn"]
    rows = db.export_articles(conn, category, language)
    if not rows:
        click.echo("No articles to export.")
        return

    records = [dict(row) for row in rows]
    out = open(output, "w", encoding="utf-8") if output else sys.stdout

    try:
        if fmt == "json":
            for record in records:
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
        else:
            writer = csv.DictWriter(out, fieldnames=records[0].keys())
            writer.writeheader()
            writer.writerows(records)
    finally:
        if output:
            out.close()

    if output:
        click.echo(f"Exported {len(records)} articles to {output}")
