"""End-to-end pipeline orchestrator: scrape → extract → curate."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import claims_db, curator, db, dv_compare, extractor, inspector, scraper, verifier

logger = logging.getLogger("kahzaabu")


def run_pipeline(db_path: Path, *, scrape: bool = True, extract: bool = True,
                 inspect_stage: bool = True, curate: bool = True, verify: bool = True,
                 dv_compare_stage: bool = True,
                 daily_budget_usd: float = 1.0,
                 curate_min_age_hours: float = 168.0,
                 inspect_limit_per_cycle: int = 10,
                 verify_limit_per_cycle: int = 5,
                 dv_compare_limit_per_cycle: int = 5,
                 extract_concurrency: int = 6, curate_concurrency: int = 4,
                 verify_concurrency: int = 3, inspect_concurrency: int = 4,
                 fetch_dhivehi: bool = True) -> dict:
    """One full pipeline cycle. Returns dict with per-stage results."""
    result = {"scrape": None, "extract": None, "inspect": None,
              "curate": None, "verify": None, "dv_compare": None}

    conn = db.get_connection(db_path)
    db.init_db(conn)
    claims_db.init_claims_schema(conn)

    # 1. Scrape (cheap, just HTTP)
    if scrape:
        logger.info("=== pipeline: scrape ===")
        try:
            session = scraper.create_session()
            total_new = 0
            for cat in scraper.CATEGORIES:
                try:
                    new = scraper.scrape_category(
                        session, conn, cat, mode="incremental", fetch_dhivehi=fetch_dhivehi,
                    )
                    total_new += new
                except Exception as e:
                    logger.error(f"  scrape '{cat}' failed: {e}")
            result["scrape"] = {"new_articles": total_new}
            logger.info(f"  scrape complete: {total_new} new articles")
        except Exception as e:
            logger.exception("scrape stage failed")
            result["scrape"] = {"error": str(e)}

    # 2. Extract (LLM; budget-gated)
    if extract:
        logger.info("=== pipeline: extract ===")
        try:
            def _ext_progress(done, total, t_in, t_out, cost):
                if done % 25 == 0 or done == total:
                    logger.info(f"  extract: {done}/{total}  cost=${cost:.2f}")
            result["extract"] = extractor.run_extraction(
                conn, concurrency=extract_concurrency,
                daily_budget_usd=daily_budget_usd, progress_cb=_ext_progress,
            )
        except Exception as e:
            logger.exception("extract stage failed")
            result["extract"] = {"error": str(e)}

    # 2b. Inspect (per-article fact card)
    if inspect_stage:
        logger.info("=== pipeline: inspect ===")
        try:
            def _ins_progress(done, total, flagged, red, cost):
                logger.info(f"  inspect: {done}/{total} flag={flagged} red_flag={red} cost=${cost:.2f}")
            result["inspect"] = inspector.run_inspection(
                conn, limit=inspect_limit_per_cycle,
                concurrency=inspect_concurrency,
                daily_budget_usd=daily_budget_usd,
                progress_cb=_ins_progress,
            )
        except Exception as e:
            logger.exception("inspect stage failed")
            result["inspect"] = {"error": str(e)}

    # 3. Curate (LLM; only if it's been a while since last curation)
    if curate:
        last_cur = conn.execute(
            "SELECT started_at FROM curation_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        too_recent = False
        if last_cur and last_cur[0]:
            try:
                last_dt = datetime.fromisoformat(last_cur[0])
                age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600
                if age_hours < curate_min_age_hours:
                    too_recent = True
                    logger.info(f"  last curation {age_hours:.1f}h ago < min {curate_min_age_hours}h; skipping")
            except Exception:
                pass

        if not too_recent:
            logger.info("=== pipeline: curate ===")
            try:
                def _cur_progress(topic, chunk, n_new, cost):
                    logger.info(f"  curate: [{topic} ch{chunk}] new={n_new} cost=${cost:.2f}")
                result["curate"] = curator.run_curation(
                    conn, concurrency=curate_concurrency,
                    daily_budget_usd=daily_budget_usd, progress_cb=_cur_progress,
                )
            except Exception as e:
                logger.exception("curate stage failed")
                result["curate"] = {"error": str(e)}
        else:
            result["curate"] = {"skipped": True, "reason": "too_recent"}

    # 4. Verify (web search; only items that don't have evidence yet)
    if verify:
        logger.info("=== pipeline: verify ===")
        try:
            def _ver_progress(done, total, searches, cost):
                logger.info(f"  verify: {done}/{total} searches={searches} cost=${cost:.2f}")
            result["verify"] = verifier.run_verification(
                conn, limit=verify_limit_per_cycle,
                concurrency=verify_concurrency,
                daily_budget_usd=daily_budget_usd,
                progress_cb=_ver_progress,
            )
        except Exception as e:
            logger.exception("verify stage failed")
            result["verify"] = {"error": str(e)}

    # 5. DV/EN compare (paired articles only; budget-gated)
    if dv_compare_stage:
        logger.info("=== pipeline: dv-compare ===")
        try:
            def _dvc_progress(done, total, inconsistencies, cost):
                logger.info(f"  dv-compare: {done}/{total} inconsistencies={inconsistencies} cost=${cost:.2f}")
            result["dv_compare"] = dv_compare.run_dv_compare(
                conn, limit=dv_compare_limit_per_cycle,
                concurrency=2,
                daily_budget_usd=daily_budget_usd,
                progress_cb=_dvc_progress,
            )
        except Exception as e:
            logger.exception("dv-compare stage failed")
            result["dv_compare"] = {"error": str(e)}

    # Summary
    spend = claims_db.daily_spend(conn)
    s = claims_db.stats(conn)
    logger.info(f"pipeline done. today's spend=${spend:.2f}  "
                f"claims={s['n_claims']}  fact_checks={s['n_fact_checks']}")
    result["today_spend_usd"] = spend
    result["stats"] = s

    conn.close()
    return result


def run_scheduled(db_path: Path, *, interval_hours: float = 12.0,
                  daily_budget_usd: float = 1.0,
                  curate_min_age_hours: float = 168.0,
                  verify_limit_per_cycle: int = 5,
                  inspect_limit_per_cycle: int = 10,
                  dv_compare_limit_per_cycle: int = 5,
                  fetch_dhivehi: bool = True) -> None:
    """Forever-loop scheduler. Runs pipeline every interval_hours."""
    import time
    logger.info(f"scheduler started. interval={interval_hours}h, budget=${daily_budget_usd}/day, "
                f"curate min age={curate_min_age_hours}h")
    while True:
        try:
            run_pipeline(
                db_path,
                scrape=True, extract=True, inspect_stage=True,
                curate=True, verify=True, dv_compare_stage=True,
                daily_budget_usd=daily_budget_usd,
                curate_min_age_hours=curate_min_age_hours,
                verify_limit_per_cycle=verify_limit_per_cycle,
                inspect_limit_per_cycle=inspect_limit_per_cycle,
                dv_compare_limit_per_cycle=dv_compare_limit_per_cycle,
                fetch_dhivehi=fetch_dhivehi,
            )
        except Exception:
            logger.exception("pipeline iteration failed")
        logger.info(f"sleeping {interval_hours}h")
        time.sleep(interval_hours * 3600)
