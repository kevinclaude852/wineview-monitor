"""
RSS feed builder for wineview.com.hk new products.
Keeps the last MAX_ITEMS entries in the feed.
"""

import threading
from datetime import datetime, timezone
from email.utils import format_datetime
from html import escape
from typing import Optional

from scraper import Product

MAX_ITEMS = 100
FEED_TITLE = "WineView HK – New Arrivals"
FEED_LINK = "https://wineview.com.hk/product-category/wine-shop/"
FEED_DESCRIPTION = "Latest products added to WineView Hong Kong wine shop"

_lock = threading.Lock()
_feed_items: list[Product] = []   # newest first
_last_updated: Optional[datetime] = None


def _rfc2822(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return format_datetime(dt)


def add_new_products(products: list[Product]) -> None:
    """Prepend new products to the in-memory feed list (newest first)."""
    global _last_updated
    with _lock:
        existing_ids = {p.id for p in _feed_items}
        truly_new = [p for p in products if p.id not in existing_ids]
        if truly_new:
            _feed_items[:0] = truly_new          # prepend
            del _feed_items[MAX_ITEMS:]           # trim to max
            _last_updated = datetime.now(timezone.utc)


def load_from_state(all_products: list[Product]) -> None:
    """Populate feed from the full product list on startup (newest first)."""
    global _last_updated
    with _lock:
        _feed_items.clear()
        _feed_items.extend(all_products[:MAX_ITEMS])
        _last_updated = datetime.now(timezone.utc)


def build_rss() -> str:
    """Return the current RSS feed as an XML string."""
    with _lock:
        items_snapshot = list(_feed_items)
        updated = _last_updated or datetime.now(timezone.utc)

    pub_date = format_datetime(updated)

    item_xml_parts = []
    for p in items_snapshot:
        description_html = ""
        if p.image_url:
            description_html += f'<img src="{escape(p.image_url)}" alt="{escape(p.name)}" /><br/>'
        description_html += f"<strong>Price:</strong> {escape(p.price)}"

        item_xml_parts.append(f"""    <item>
      <title>{escape(p.name)}</title>
      <link>{escape(p.url)}</link>
      <guid isPermaLink="true">{escape(p.url)}</guid>
      <pubDate>{_rfc2822(p.first_seen)}</pubDate>
      <description><![CDATA[{description_html}]]></description>
      <price>{escape(p.price)}</price>
    </item>""")

    items_xml = "\n".join(item_xml_parts)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
  <channel>
    <title>{escape(FEED_TITLE)}</title>
    <link>{escape(FEED_LINK)}</link>
    <description>{escape(FEED_DESCRIPTION)}</description>
    <language>en-us</language>
    <lastBuildDate>{pub_date}</lastBuildDate>
    <atom:link href="{{SELF_URL}}" rel="self" type="application/rss+xml"/>
{items_xml}
  </channel>
</rss>"""
