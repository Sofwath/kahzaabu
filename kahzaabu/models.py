from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ListingItem:
    article_id: int
    title: str
    date_text: str
    image_url: Optional[str] = None


@dataclass
class Article:
    id: int
    language: str
    paired_id: Optional[int]
    category: str
    category_id: int
    title: str
    body_text: str
    body_html: str
    reference: Optional[str]
    published_date: str
    image_urls: List[str] = field(default_factory=list)
    raw_page_html: str = ""
