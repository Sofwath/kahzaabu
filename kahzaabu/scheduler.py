import logging
import time
from pathlib import Path

from . import db, scraper

logger = logging.getLogger("kahzaabu")

CATEGORY_NAMES = list(scraper.CATEGORIES.keys())


def run_scheduled(db_path: Path, interval_hours: float = 6):
    """Run incremental scrapes on a loop."""
    logger.info(f"Scheduler started. Updating every {interval_hours} hours.")

    while True:
        conn = db.get_connection(db_path)
        db.init_db(conn)
        session = scraper.create_session()

        total_new = 0
        for cat_name in CATEGORY_NAMES:
            try:
                new = scraper.scrape_category(
                    session, conn, cat_name, mode="incremental"
                )
                total_new += new
            except Exception as e:
                logger.error(f"Error updating '{cat_name}': {e}")

        conn.close()
        logger.info(f"Update cycle complete: {total_new} new articles. Sleeping {interval_hours}h.")
        time.sleep(interval_hours * 3600)
