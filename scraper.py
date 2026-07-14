"""
Scraper for wineview.com.hk/product-category/wine-shop/
Detects new products by comparing against previously seen product IDs.
WooCommerce standard HTML selectors are used.
"""

import json
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://wineview.com.hk/product-category/wine-shop/"
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
REDIS_URL = os.environ.get("REDIS_URL") or os.environ.get("KV_URL")
REDIS_KEY = "wineview:state"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}


@dataclass
class Product:
    id: str          # unique key: slugified name or WC post ID from class
    name: str
    price: str
    url: str
    image_url: str
    first_seen: str  # ISO 8601 UTC


def _parse_product_id(li_tag) -> str:
    """Extract WooCommerce post ID from class list, e.g. 'post-1234'."""
    for cls in li_tag.get("class", []):
        if cls.startswith("post-"):
            return cls
    # Fallback: use product URL slug
    link = li_tag.select_one("a.woocommerce-LoopProduct-link")
    if link and link.get("href"):
        return link["href"].rstrip("/").split("/")[-1]
    return ""


def _parse_price(li_tag) -> str:
    price_tag = li_tag.select_one("span.price")
    if not price_tag:
        return ""
    # Strip extra whitespace but keep currency symbol
    return " ".join(price_tag.get_text(" ", strip=True).split())


def _parse_image(li_tag) -> str:
    img = li_tag.select_one("img")
    if not img:
        return ""
    # Prefer data-src (lazy load) over src
    return img.get("data-src") or img.get("src") or ""


def _scrape_page(session: requests.Session, url: str) -> tuple[list[Product], Optional[str]]:
    """
    Scrape one page. Returns (products_on_page, next_page_url_or_None).
    """
    try:
        resp = session.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Failed to fetch %s: %s", url, exc)
        return [], None

    soup = BeautifulSoup(resp.text, "html.parser")
    products = []
    now = datetime.now(timezone.utc).isoformat()

    for li in soup.select("ul.products li.product"):
        pid = _parse_product_id(li)
        if not pid:
            continue

        name_tag = li.select_one(
            "h2.woocommerce-loop-product__title, "
            ".woocommerce-loop-product__title, "
            ".product-title, "
            "h2"
        )
        name = name_tag.get_text(strip=True) if name_tag else ""

        link_tag = li.select_one("a.woocommerce-LoopProduct-link, a")
        url_href = link_tag["href"] if link_tag and link_tag.get("href") else ""

        products.append(Product(
            id=pid,
            name=name,
            price=_parse_price(li),
            url=url_href,
            image_url=_parse_image(li),
            first_seen=now,
        ))

    # Next page link
    next_link = soup.select_one("a.next.page-numbers")
    next_url = next_link["href"] if next_link else None

    return products, next_url


def inspect_page(url: str = BASE_URL) -> dict:
    """
    Fetch the page and report diagnostics about its structure, to help
    figure out why product selectors might not be matching (theme change,
    JS-rendered content, redirect, age gate, etc.).
    """
    session = requests.Session()
    resp = session.get(url, headers=HEADERS, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")

    candidate_selectors = [
        "ul.products li.product",
        "li.product",
        ".products",
        ".product",
        "div.product",
        "a.add_to_cart_button",
        "[class*='product']",
        "script[type='application/ld+json']",
    ]
    selector_counts = {sel: len(soup.select(sel)) for sel in candidate_selectors}

    body_text = soup.get_text(" ", strip=True).lower()
    age_gate_hit = any(
        kw in body_text for kw in ("are you 18", "are you over 18", "verify your age", "age verification")
    )

    return {
        "requested_url": url,
        "final_url": resp.url,
        "status_code": resp.status_code,
        "html_length": len(resp.text),
        "title": soup.title.get_text(strip=True) if soup.title else None,
        "selector_counts": selector_counts,
        "possible_age_gate": age_gate_hit,
        "html_snippet": resp.text[:4000],
    }


def scrape_all_products(max_pages: int = 20) -> list[Product]:
    """Scrape all pages and return every product found (page 1 first = newest first)."""
    session = requests.Session()
    all_products: list[Product] = []
    url: Optional[str] = BASE_URL
    page = 0

    while url and page < max_pages:
        logger.info("Scraping page %d: %s", page + 1, url)
        products, next_url = _scrape_page(session, url)
        all_products.extend(products)
        url = next_url
        page += 1
        if next_url:
            time.sleep(1)  # polite crawl delay

    logger.info("Total products scraped: %d", len(all_products))
    return all_products


# ---------------------------------------------------------------------------
# State persistence  (Redis when REDIS_URL is set, file otherwise)
# ---------------------------------------------------------------------------

def _get_redis():
    """Return a Redis client if REDIS_URL is configured, else None."""
    if not REDIS_URL:
        return None
    try:
        import redis as _redis
        return _redis.from_url(REDIS_URL, decode_responses=True)
    except ImportError:
        logger.warning("redis package not installed; falling back to file state")
        return None


def load_state() -> dict[str, dict]:
    """Return {product_id: product_dict} from Redis or file."""
    r = _get_redis()
    if r is not None:
        try:
            raw = r.get(REDIS_KEY)
            if raw:
                return json.loads(raw)
            return {}
        except Exception as exc:
            logger.warning("Redis read failed: %s", exc)

    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read state file: %s", exc)
    return {}


def save_state(products: list[Product]) -> None:
    """Persist state to Redis or file."""
    state = {p.id: asdict(p) for p in products}
    payload = json.dumps(state, ensure_ascii=False)

    r = _get_redis()
    if r is not None:
        try:
            r.set(REDIS_KEY, payload)
            return
        except Exception as exc:
            logger.warning("Redis write failed: %s; falling back to file", exc)

    STATE_FILE.write_text(payload)


# ---------------------------------------------------------------------------
# Main detection logic
# ---------------------------------------------------------------------------

def detect_new_products() -> tuple[list[Product], list[Product]]:
    """
    Scrape site, compare with saved state.
    Returns (new_products, all_products).
    new_products are ordered newest-first (page order).
    """
    previous = load_state()
    current = scrape_all_products()

    new_products = []
    for p in current:
        if p.id not in previous:
            new_products.append(p)
        else:
            # Preserve original first_seen date
            p.first_seen = previous[p.id]["first_seen"]

    if new_products:
        logger.info("Found %d new product(s)", len(new_products))
        for p in new_products:
            logger.info("  NEW: %s (%s)", p.name, p.price)
    else:
        logger.info("No new products detected")

    save_state(current)
    return new_products, current
