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
from html import unescape
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Referer": "https://www.google.com/",
}

# Some WAF/bot-detection blocks are probabilistic or rate-based rather than an
# absolute ban, so a short retry can succeed where the first attempt didn't.
RETRYABLE_STATUS_CODES = {403, 429, 503}
MAX_FETCH_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 3


API_BASE = "https://wineview.com.hk/wp-json/wc/store/v1"
CATEGORY_SLUG = "wine-shop"
USE_STORE_API = os.environ.get("USE_STORE_API", "1") != "0"


@dataclass
class Product:
    id: str          # unique key: WC post ID as 'post-1234'
    name: str
    price: str
    url: str
    image_url: str
    first_seen: str  # ISO 8601 UTC
    # Store API only; defaults keep older saved state (which lacks these keys)
    # loadable. regular/sale are set for products reported as on sale, origin
    # is 'Country/Region/Grapes'.
    regular_price: str = ""
    sale_price: str = ""
    origin: str = ""


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
    resp = None
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
        except requests.RequestException as exc:
            if attempt == MAX_FETCH_ATTEMPTS:
                logger.error("Failed to fetch %s: %s", url, exc)
                return [], None
            logger.warning(
                "Error fetching %s (attempt %d/%d): %s; retrying",
                url, attempt, MAX_FETCH_ATTEMPTS, exc,
            )
            time.sleep(RETRY_DELAY_SECONDS * attempt)
            continue

        if resp.status_code in RETRYABLE_STATUS_CODES and attempt < MAX_FETCH_ATTEMPTS:
            logger.warning(
                "Got HTTP %d fetching %s (attempt %d/%d); retrying",
                resp.status_code, url, attempt, MAX_FETCH_ATTEMPTS,
            )
            time.sleep(RETRY_DELAY_SECONDS * attempt)
            continue
        break

    try:
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


CANDIDATE_ENDPOINTS = [
    "https://wineview.com.hk/product-category/wine-shop/feed/",
    "https://wineview.com.hk/feed/",
    "https://wineview.com.hk/wp-json/wc/store/v1/products?per_page=30&orderby=date&order=desc",
    "https://wineview.com.hk/wp-json/wc/store/products?per_page=30",
    "https://wineview.com.hk/wp-json/wp/v2/product?per_page=30",
    "https://wineview.com.hk/product-sitemap.xml",
    "https://wineview.com.hk/wp-sitemap-posts-product-1.xml",
]


def probe_endpoints() -> list[dict]:
    """
    Try the site's machine-readable endpoints (RSS feed, WooCommerce Store API,
    sitemaps) to find one that is reachable and usable as a data source instead
    of parsing the HTML catalog page.
    """
    session = requests.Session()
    results = []
    for url in CANDIDATE_ENDPOINTS:
        try:
            resp = session.get(url, headers=HEADERS, timeout=20)
            body = resp.text
            results.append({
                "url": url,
                "status_code": resp.status_code,
                "content_type": resp.headers.get("Content-Type", ""),
                "length": len(body),
                "bot_challenge": "sgcaptcha" in body,
                "snippet": body[:400],
            })
        except requests.RequestException as exc:
            results.append({"url": url, "error": str(exc)})
        time.sleep(1)
    return results


def _api_get(session: requests.Session, path: str, params: dict) -> requests.Response:
    """GET a Store API path, insisting on a JSON response (a bot challenge returns HTML)."""
    resp = session.get(f"{API_BASE}{path}", headers=HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "json" not in content_type:
        raise ValueError(f"expected JSON from {path}, got {content_type!r}")
    return resp


def _format_api_price(prices: dict, key: str) -> str:
    """Store API prices are minor units as strings: '23200' + minor_unit 2 -> '$ 232.00'."""
    raw = prices.get(key)
    if raw in (None, ""):
        return ""
    try:
        value = int(raw) / (10 ** int(prices.get("currency_minor_unit", 2)))
    except (TypeError, ValueError):
        return ""
    symbol = prices.get("currency_prefix") or prices.get("currency_symbol") or "$"
    return f"{symbol} {value:,.2f}"


ORIGIN_TAXONOMIES = ("pa_country", "pa_region", "pa_grapes")


def _attribute_terms(item: dict, taxonomy: str) -> list[str]:
    for attribute in item.get("attributes") or []:
        if attribute.get("taxonomy") == taxonomy:
            return [
                unescape(term["name"])
                for term in attribute.get("terms") or []
                if term.get("name")
            ]
    return []


def _format_origin(item: dict) -> str:
    """'Italy/Tuscany/Blend' from the country, region and grapes attributes."""
    parts = []
    for taxonomy in ORIGIN_TAXONOMIES:
        terms = _attribute_terms(item, taxonomy)
        if terms:
            parts.append(", ".join(terms))
    return "/".join(parts)


def _product_from_api(item: dict, now: str) -> Product:
    prices = item.get("prices") or {}
    images = item.get("images") or []
    current = _format_api_price(prices, "price")
    regular = _format_api_price(prices, "regular_price")
    on_sale = bool(item.get("on_sale")) and regular and regular != current

    return Product(
        # Match the HTML scraper's ID format so switching data sources doesn't
        # make every product look new against existing saved state.
        id=f"post-{item.get('id')}",
        # The API returns names HTML-encoded, e.g. 'D&#8217;Auvenay'.
        name=unescape(item.get("name", "")),
        price=current or regular,
        url=item.get("permalink", ""),
        image_url=images[0].get("src", "") if images else "",
        first_seen=item.get("date_created") or now,
        regular_price=regular if on_sale else "",
        sale_price=current if on_sale else "",
        origin=_format_origin(item),
    )


def _collect_category_ids(categories: list[dict], slug: str) -> list[int]:
    """
    IDs for `slug` plus every category beneath it.

    'wine-shop' is a parent category — products are tagged only with its
    children (red-wine, white-wine, ...), so filtering on the parent ID alone
    can match nothing.
    """
    root = next((c for c in categories if c.get("slug") == slug), None)
    if not root:
        return []

    wanted = {root["id"]}
    # Walk down the tree until no new children are found (depth is unknown).
    while True:
        children = {
            c["id"] for c in categories
            if c.get("parent") in wanted and c["id"] not in wanted
        }
        if not children:
            return sorted(wanted)
        wanted |= children


def _lookup_category_ids(session: requests.Session) -> list[int]:
    try:
        resp = _api_get(session, "/products/categories", {"per_page": 100})
        return _collect_category_ids(resp.json(), CATEGORY_SLUG)
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Store API category lookup failed: %s", exc)
        return []


def _fetch_api_pages(session: requests.Session, params: dict, max_pages: int) -> list[Product]:
    products: list[Product] = []
    now = datetime.now(timezone.utc).isoformat()
    page = 1
    while page <= max_pages:
        resp = _api_get(session, "/products", {**params, "page": page})
        batch = resp.json()
        if not batch:
            break
        products.extend(_product_from_api(item, now) for item in batch)

        try:
            total_pages = int(resp.headers.get("X-WP-TotalPages", page))
        except ValueError:
            total_pages = page
        if page >= total_pages:
            break
        page += 1
        time.sleep(1)
    return products


def fetch_products_via_api(max_pages: int = 20) -> list[Product]:
    """Fetch products from the WooCommerce Store API, newest first."""
    session = requests.Session()
    params = {"per_page": 100, "orderby": "date", "order": "desc"}

    category_ids = _lookup_category_ids(session)
    if category_ids:
        products = _fetch_api_pages(
            session, {**params, "category": ",".join(map(str, category_ids))}, max_pages
        )
        if products:
            logger.info("Store API returned %d product(s) in %r", len(products), CATEGORY_SLUG)
            return products
        logger.warning("Category filter matched nothing; retrying unfiltered")
    else:
        logger.warning("Could not resolve category %r; fetching unfiltered", CATEGORY_SLUG)

    products = _fetch_api_pages(session, params, max_pages)
    logger.info("Store API returned %d product(s) unfiltered", len(products))
    return products


def scrape_all_products(max_pages: int = 20) -> list[Product]:
    """Prefer the Store API (structured JSON); fall back to parsing catalog HTML."""
    if USE_STORE_API:
        try:
            products = fetch_products_via_api(max_pages)
            if products:
                return products
            logger.warning("Store API returned no products; falling back to HTML")
        except (requests.RequestException, ValueError) as exc:
            logger.warning("Store API unavailable (%s); falling back to HTML", exc)
    return _scrape_html_pages(max_pages)


def _scrape_html_pages(max_pages: int = 20) -> list[Product]:
    """Scrape all catalog pages and return every product found (page 1 first = newest first)."""
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

    if not current:
        # A 0-product scrape almost always means the site blocked/rate-limited
        # this request (or served a JS-challenge page) rather than the shop
        # genuinely having no products. Keep the last known-good state instead
        # of wiping it — overwriting it here would make the next successful
        # scrape look like a first-ever run and flood notifications with the
        # entire existing catalog.
        if previous:
            logger.error(
                "Scrape returned 0 products but %d were previously known; "
                "treating as a failed fetch and keeping prior state",
                len(previous),
            )
            return [], [Product(**v) for v in previous.values()]
        logger.warning("Scrape returned 0 products and no previous state exists")
        return [], []

    is_bootstrap = not previous
    new_products = []
    for p in current:
        if p.id not in previous:
            new_products.append(p)
        else:
            # Preserve original first_seen date
            p.first_seen = previous[p.id]["first_seen"]

    if is_bootstrap and new_products:
        # No prior state (first-ever run, or state store was reset) — every
        # currently-listed product would otherwise look "new". Seed the
        # baseline silently instead of flooding notifications with the
        # entire existing catalog.
        logger.info(
            "No previous state found; seeding baseline of %d product(s) without notifying",
            len(new_products),
        )
        new_products = []

    if new_products:
        logger.info("Found %d new product(s)", len(new_products))
        for p in new_products:
            logger.info("  NEW: %s (%s)", p.name, p.price)
    else:
        logger.info("No new products detected")

    save_state(current)
    return new_products, current
