from __future__ import annotations

import json
import logging
import re
import time
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from . import db
from .models import Article, ListingItem

logger = logging.getLogger("kahzaabu")

BASE_URL = "https://presidency.gov.mv"

CATEGORIES = {
    "press_release": {"id": 11, "tid": 1},
    "speech": {"id": 12, "tid": 2},
    "vp_speech": {"id": 13, "tid": 3},
    "news_bulletin": {"id": 290, "tid": 28},
}

DELAY_SECONDS = 2.0


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    adapter = requests.adapters.HTTPAdapter(
        max_retries=requests.adapters.Retry(
            total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504]
        )
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def fetch_listing_page(
    session: requests.Session, category_id: int, tid: int, page: int
) -> List[ListingItem]:
    """Fetch a single listing page and extract article items."""
    url = f"{BASE_URL}/api/Press/ArticlesList/{category_id}"
    params = {"page": page, "tid": tid, "lang": "EN", "search": "", "term": 0}
    resp = session.get(url, params=params, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    items = []

    for link in soup.find_all("a", href=re.compile(r"/Press/Article/\d+")):
        href = link.get("href", "")
        match = re.search(r"/Press/Article/(\d+)", href)
        if not match:
            continue
        article_id = int(match.group(1))

        title_tag = link.find("h3") or link.find("h4") or link.find("h2")
        title = title_tag.get_text(strip=True) if title_tag else link.get_text(strip=True)

        date_tag = link.find("time")
        date_text = date_tag.get_text(strip=True) if date_tag else ""

        img_tag = link.find("img")
        image_url = img_tag.get("src") if img_tag else None

        items.append(ListingItem(article_id=article_id, title=title, date_text=date_text, image_url=image_url))

    return items


def get_total_pages(session: requests.Session, category_id: int, tid: int) -> int:
    """Get total number of listing pages for a category."""
    url = f"{BASE_URL}/Press/Articles/{category_id}"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    # Find the last page number in pagination links
    page_links = soup.find_all("a", href=re.compile(rf"page=\d+"))
    max_page = 1
    for link in page_links:
        href = link.get("href", "")
        match = re.search(r"page=(\d+)", href)
        if match:
            max_page = max(max_page, int(match.group(1)))
    return max_page


def parse_article_page(html: str, article_id: int, category: str, category_id: int) -> Optional[Article]:
    """Parse a fetched article page into an Article object."""
    soup = BeautifulSoup(html, "lxml")

    # Find article title - h1 has the actual title, h2 is the category label
    title_tag = soup.find("h1")
    if not title_tag:
        # Fallback to h2 if no h1
        title_tag = soup.find("h2")
    if not title_tag:
        logger.warning(f"No title found for article {article_id}")
        return None
    title = title_tag.get_text(strip=True)

    # Find date and reference
    published_date = ""
    reference = None
    # Look for date pattern in text near the title
    article_text_area = soup.find("div", class_=re.compile(r"article|content|press", re.I))
    if not article_text_area:
        article_text_area = soup.find("main") or soup.body

    date_pattern = re.compile(r"(\d{1,2}\s+\w+\s+\d{4})")
    ref_pattern = re.compile(r"Ref:\s*(.+?)(?:\s*$|\s*\n)", re.MULTILINE)

    if article_text_area:
        text_content = article_text_area.get_text()
        date_match = date_pattern.search(text_content)
        if date_match:
            published_date = _parse_date(date_match.group(1))
        ref_match = ref_pattern.search(text_content)
        if ref_match:
            reference = ref_match.group(1).strip()

    # Extract body content - find the main article body
    body_html = ""
    body_text = ""
    # Try common content container patterns
    content_div = (
        soup.find("div", class_=re.compile(r"article-body|entry-content|press-body", re.I))
        or soup.find("div", class_=re.compile(r"col-md-12|col-lg-12", re.I))
    )
    if content_div:
        # Get all paragraphs within the content area
        paragraphs = content_div.find_all("p")
        if paragraphs:
            body_html = "\n".join(str(p) for p in paragraphs)
            body_text = "\n\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

    # If we didn't find content via class, try getting all <p> tags after the title
    if not body_text and article_text_area:
        paragraphs = article_text_area.find_all("p")
        if paragraphs:
            body_html = "\n".join(str(p) for p in paragraphs)
            body_text = "\n\n".join(p.get_text(strip=True) for p in paragraphs if p.get_text(strip=True))

    # Extract images
    image_urls = []
    if article_text_area:
        for img in article_text_area.find_all("img", src=True):
            src = img["src"]
            if "storage.googleapis.com" in src or "presidency.gov.mv" in src:
                image_urls.append(src)

    # Determine language and paired ID from the language toggle
    language = "EN"
    paired_id = None
    for toggle_link in soup.find_all("a", href=re.compile(r"/Press/Article/\d+")):
        link_text = toggle_link.get_text(strip=True)
        href = toggle_link.get("href", "")
        link_match = re.search(r"/Press/Article/(\d+)", href)
        if not link_match:
            continue
        linked_id = int(link_match.group(1))

        # If the toggle says "EN", we're on the DV page
        if link_text.strip() == "EN" and linked_id != article_id:
            language = "DV"
            paired_id = linked_id
            break
        # If the toggle contains Thaana script, we're on EN page
        if re.search(r"[\u0780-\u07BF]", link_text) and linked_id != article_id:
            language = "EN"
            paired_id = linked_id
            break

    return Article(
        id=article_id,
        language=language,
        paired_id=paired_id,
        category=category,
        category_id=category_id,
        title=title,
        body_text=body_text,
        body_html=body_html,
        reference=reference,
        published_date=published_date,
        image_urls=image_urls,
        raw_page_html=html,
    )


def fetch_article(
    session: requests.Session,
    article_id: int,
    category: str,
    category_id: int,
) -> Optional[Article]:
    """Fetch and parse a single article."""
    url = f"{BASE_URL}/Press/Article/{article_id}"
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if resp.status_code in (403, 404, 410):
            logger.warning(f"  Article {article_id}: {resp.status_code} (skipping)")
            return None
        raise
    return parse_article_page(resp.text, article_id, category, category_id)


def scrape_category(
    session: requests.Session,
    conn,
    category_name: str,
    mode: str = "backfill",
    start_page: int = 1,
    fetch_dhivehi: bool = True,
    progress_callback=None,
):
    """Scrape an entire category.

    mode: 'backfill' iterates all pages, 'incremental' stops when caught up.
    """
    cat = CATEGORIES[category_name]
    category_id = cat["id"]
    tid = cat["tid"]

    total_pages = get_total_pages(session, category_id, tid)
    logger.info(f"Category '{category_name}': {total_pages} pages, starting at page {start_page}")

    run_id = db.start_scrape_run(conn, category_id, "EN+DV" if fetch_dhivehi else "EN", start_page)
    pages_scraped = 0
    articles_scraped = 0
    articles_new = 0

    try:
        for page in range(start_page, total_pages + 1):
            time.sleep(DELAY_SECONDS)
            items = fetch_listing_page(session, category_id, tid, page)
            if not items:
                logger.info(f"  Page {page}: no items, stopping")
                break

            all_exist = True
            for item in items:
                if not db.article_exists(conn, item.article_id, "EN"):
                    all_exist = False
                    # Fetch English version
                    time.sleep(DELAY_SECONDS)
                    article = fetch_article(session, item.article_id, category_name, category_id)
                    if article:
                        db.insert_article(conn, article)
                        articles_new += 1
                        articles_scraped += 1
                        logger.info(f"  [{articles_new}] Article {item.article_id}: {item.title[:60]}")

                        # Fetch Dhivehi version if available
                        if fetch_dhivehi and article.paired_id:
                            if not db.article_exists(conn, article.paired_id, "DV"):
                                time.sleep(DELAY_SECONDS)
                                dv_article = fetch_article(
                                    session, article.paired_id, category_name, category_id
                                )
                                if dv_article:
                                    dv_article.language = "DV"
                                    dv_article.paired_id = article.id
                                    db.insert_article(conn, dv_article)
                                    articles_new += 1
                                    articles_scraped += 1
                    else:
                        logger.warning(f"  Failed to parse article {item.article_id}")
                        articles_scraped += 1

            pages_scraped += 1
            db.update_scrape_run(
                conn,
                run_id,
                pages_scraped=pages_scraped,
                articles_scraped=articles_scraped,
                articles_new=articles_new,
                resume_page=page + 1,
            )

            if progress_callback:
                progress_callback(page, total_pages, articles_new)

            if mode == "incremental" and all_exist:
                logger.info(f"  Page {page}: all articles exist, caught up")
                break

            logger.info(f"  Page {page}/{total_pages} done ({articles_new} new)")

        db.finish_scrape_run(conn, run_id, status="completed")
        logger.info(
            f"Category '{category_name}' complete: {pages_scraped} pages, {articles_new} new articles"
        )

    except KeyboardInterrupt:
        db.finish_scrape_run(conn, run_id, status="interrupted")
        logger.info(f"Interrupted at page {start_page + pages_scraped}. Use --resume to continue.")
        raise
    except Exception as e:
        db.finish_scrape_run(conn, run_id, status="failed", error_message=str(e))
        logger.error(f"Error scraping '{category_name}': {e}")
        raise

    return articles_new


def _parse_date(date_str: str) -> str:
    """Convert 'DD Month YYYY' to ISO format."""
    import locale
    from datetime import datetime

    # Handle English month names
    try:
        dt = datetime.strptime(date_str.strip(), "%d %B %Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        pass
    # Return as-is if we can't parse
    return date_str.strip()
