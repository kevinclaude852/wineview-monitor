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
from urllib.parse import urlencode

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
USE_STORE_API = os.environ.get("USE_STORE_API", "1") != "0"

API_MAX_PER_PAGE = 100  # Store API caps per_page at 100
# Results are newest-first, so only the top slice can contain anything new
# since the last hourly run. Fetching the whole catalogue every hour is waste.
PRODUCT_LIMIT = int(os.environ.get("PRODUCT_LIMIT", "30"))

USE_PLAYWRIGHT = os.environ.get("USE_PLAYWRIGHT", "1") != "0"
PLAYWRIGHT_HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "1") != "0"
# Persisted so the bot check's cookie survives between runs.
PLAYWRIGHT_PROFILE_DIR = os.environ.get("PLAYWRIGHT_PROFILE_DIR", ".playwright-profile")
BROWSER_TIMEOUT_MS = 45_000


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


def _collect_via_store_api(fetch_json, max_products: int, source: str) -> list[Product]:
    """The newest products from the Store API, newest first."""
    params = {
        "per_page": min(max_products, API_MAX_PER_PAGE),
        "orderby": "date",
        "order": "desc",
    }

    items: list[dict] = []
    page = 1
    while len(items) < max_products:
        batch, total_pages = fetch_json("/products", {**params, "page": page})
        if not batch:
            break
        items.extend(batch)

        if total_pages is None or page >= total_pages:
            break
        page += 1
        time.sleep(1)

    now = datetime.now(timezone.utc).isoformat()
    products = [_product_from_api(item, now) for item in items[:max_products]]
    logger.info("Store API (%s) returned %d product(s)", source, len(products))
    return products


def fetch_products_via_api(max_products: int = PRODUCT_LIMIT) -> list[Product]:
    """Fetch products from the Store API over plain HTTP."""
    session = requests.Session()

    def fetch_json(path: str, params: dict):
        resp = _api_get(session, path, params)
        try:
            total_pages = int(resp.headers.get("X-WP-TotalPages", 0)) or None
        except ValueError:
            total_pages = None
        return resp.json(), total_pages

    return _collect_via_store_api(fetch_json, max_products, "http")


# ---------------------------------------------------------------------------
# Browser transport
#
# The site's bot protection challenges clients that don't look like a browser,
# so plain HTTP gets a 202 stub redirecting to /.well-known/sgcaptcha/ instead
# of JSON. Driving a real Chromium lets the check run the same way it does
# during a manual visit; the persistent profile keeps whatever cookie it sets,
# so later runs usually aren't challenged at all.
# ---------------------------------------------------------------------------

def _wait_out_challenge(page, timeout_ms: int = BROWSER_TIMEOUT_MS) -> None:
    """Give the bot check time to run and hand the browser back to the real page."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        try:
            if "sgcaptcha" not in page.url and "sgcaptcha" not in page.content():
                return
        except Exception:
            pass  # mid-navigation; look again shortly
        page.wait_for_timeout(1000)
    logger.warning("Bot check still present after %.0fs", timeout_ms / 1000)


def _browser_fetch_json(page, url: str):
    """Fetch from inside the page, so it uses the browser's own stack and cookies."""
    result = page.evaluate(
        """async (url) => {
            const resp = await fetch(url, {
                credentials: 'include',
                headers: {'Accept': 'application/json'},
            });
            return {
                contentType: resp.headers.get('content-type') || '',
                totalPages: resp.headers.get('x-wp-totalpages'),
                body: await resp.text(),
            };
        }""",
        url,
    )
    if "json" not in result["contentType"]:
        raise ValueError(f"expected JSON from {url}, got {result['contentType']!r}")

    try:
        total_pages = int(result["totalPages"]) or None
    except (TypeError, ValueError):
        total_pages = None
    return json.loads(result["body"]), total_pages


def fetch_products_via_browser(max_products: int = PRODUCT_LIMIT) -> list[Product]:
    """Fetch products from the Store API through a real browser."""
    from playwright.sync_api import sync_playwright

    profile_dir = Path(PLAYWRIGHT_PROFILE_DIR).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=PLAYWRIGHT_HEADLESS,
            locale="en-US",
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            # Load a normal page first so the check runs on a real navigation.
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
            _wait_out_challenge(page)

            def fetch_json(path: str, params: dict):
                return _browser_fetch_json(page, f"{API_BASE}{path}?{urlencode(params)}")

            return _collect_via_store_api(fetch_json, max_products, "browser")
        finally:
            context.close()


def scrape_all_products(max_products: int = PRODUCT_LIMIT) -> list[Product]:
    """Store API over HTTP, then through a browser, then HTML scraping."""
    if USE_STORE_API:
        try:
            products = fetch_products_via_api(max_products)
            if products:
                return products
            logger.info("Store API returned no products over HTTP; trying browser")
        except (requests.RequestException, ValueError) as exc:
            # Expected on every run while the site's bot check is active, so
            # this is routine rather than something to shout about.
            logger.info("Store API not reachable over HTTP (%s); trying browser", exc)

    if USE_PLAYWRIGHT:
        try:
            products = fetch_products_via_browser(max_products)
            if products:
                return products
            logger.warning("Store API returned no products via browser")
        except ImportError:
            logger.warning(
                "playwright not installed; run 'pip install playwright && playwright install chromium'"
            )
        except Exception as exc:
            logger.warning("Browser fetch failed (%s)", exc)

    logger.info("Falling back to HTML scraping")
    return _scrape_html_pages(max_products)


def _scrape_html_pages(max_products: int = PRODUCT_LIMIT) -> list[Product]:
    """Scrape catalog pages newest-first until max_products have been collected."""
    session = requests.Session()
    all_products: list[Product] = []
    url: Optional[str] = BASE_URL
    page = 0

    while url and len(all_products) < max_products:
        logger.info("Scraping page %d: %s", page + 1, url)
        products, next_url = _scrape_page(session, url)
        all_products.extend(products)
        url = next_url
        page += 1
        if next_url:
            time.sleep(1)  # polite crawl delay

    all_products = all_products[:max_products]
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
    Returns (new_products, all_products), newest-first.

    Deliberately does not persist: the caller commits with save_state() once
    any notification has actually been delivered. Saving here would mark
    products as seen even when the notification failed, losing the alert for
    good.
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

    return new_products, current
