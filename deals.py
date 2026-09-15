"""
Deal-of-the-day monitor for wineview.com.hk/deals.

The deals page has no JSON API, so it is scraped from the rendered HTML via the
same browser transport as the product monitor (it is behind the same bot check).
Only the current-deals tab matters: expired deals drop off the page, so each run
simply diffs the current deals against the previous run's cache. That means a
wine re-listed in a later promotion round notifies again, which is intended.

Deals carry only name, url and the discounted/original price — the HTML has no
country/region/grape attributes, unlike the Store API used for new arrivals.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, asdict
from html import unescape
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

import scraper

logger = logging.getLogger(__name__)

DEALS_URL = "https://wineview.com.hk/deals/?tab=product-deals&subtab=current-deals"
DEALS_STATE_FILE = Path(os.environ.get("DEALS_STATE_FILE", "deals_state.json"))
DEALS_REDIS_KEY = "wineview:deals"

# The current-deals list is rendered by the "Deals for WooCommerce" plugin as
# <ul class="dfw-current-deals-tab-content"> with one <li> per deal.
DEAL_LI_SELECTOR = "ul.dfw-current-deals-tab-content > li"

_AMOUNT_RE = re.compile(r"[\d,]+(?:\.\d{1,2})?")


@dataclass
class Deal:
    id: str             # product slug, stable across promotion rounds
    name: str
    url: str
    price: str          # discounted / current price, e.g. "$ 90.00"
    regular_price: str  # original price, e.g. "$ 160.00"


def _slug(url: str) -> str:
    return url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]


def _prettify(slug: str) -> str:
    return slug.replace("-", " ").title()


def _amounts(text: str) -> list[float]:
    out = []
    for m in _AMOUNT_RE.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _fmt(value: float) -> str:
    return f"$ {value:,.2f}"


def _price_text(el) -> str:
    """Normalise a WooCommerce <del>/<ins> price element to '$ 160.00'.

    The currency symbol and digits sit in separate spans, so join the element's
    text with no separator ('$' + '160.00' -> '$160.00') before parsing.
    """
    if el is None:
        return ""
    amounts = _amounts(el.get_text("", strip=True))
    return _fmt(amounts[0]) if amounts else ""


def _deal_name(li, slug: str) -> str:
    heading = li.find(["h1", "h2", "h3", "h4"])
    if heading and heading.get_text(strip=True):
        return unescape(heading.get_text(" ", strip=True))
    img = li.find("img", alt=True)
    if img and img.get("alt", "").strip():
        return unescape(img["alt"].strip())
    return _prettify(slug)


def parse_deals(html: str) -> list[Deal]:
    """
    Extract current deals from the deals-plugin list. Each <li> holds a product
    link, an <h2> name, and standard WooCommerce sale markup: <del> original
    price, <ins> discounted price.
    """
    soup = BeautifulSoup(html, "html.parser")

    deals = []
    seen = set()
    for li in soup.select(DEAL_LI_SELECTOR):
        link = li.find("a", href=lambda h: h and "/product/" in h)
        if not link:
            continue
        slug = _slug(link["href"])
        if not slug or slug in seen:
            continue

        regular = _price_text(li.find("del"))   # original
        current = _price_text(li.find("ins"))   # discounted
        if not (current or regular):
            continue  # no price -> not a usable deal entry

        seen.add(slug)
        deals.append(Deal(
            id=slug,
            name=_deal_name(li, slug),
            url=link["href"].split("?")[0],
            price=current or regular,
            regular_price=regular if (current and regular) else "",
        ))
    return deals


def fetch_deals_html() -> Optional[str]:
    """Raw rendered HTML of the current-deals page (also used by --dump-deals)."""
    try:
        return scraper.fetch_rendered_html(DEALS_URL)
    except ImportError:
        logger.warning("playwright not installed; cannot fetch deals page")
        return None
    except Exception as exc:
        logger.warning("Deals fetch failed (%s)", exc)
        return None


def fetch_deals() -> Optional[list[Deal]]:
    """
    Current deals, or None if the page couldn't be fetched. An empty list is a
    valid result (no promotions running) and is distinct from a fetch failure.
    """
    html = fetch_deals_html()
    if html is None:
        logger.warning("Deals page unavailable (bot-check, empty, or error)")
        return None
    deals = parse_deals(html)
    logger.info("Deals page: parsed %d current deal(s)", len(deals))
    return deals


# ---------------------------------------------------------------------------
# State (separate from the product state; Redis when configured, file otherwise)
# ---------------------------------------------------------------------------

def load_deal_state() -> dict[str, dict]:
    r = scraper._get_redis()
    if r is not None:
        try:
            raw = r.get(DEALS_REDIS_KEY)
            return json.loads(raw) if raw else {}
        except Exception as exc:
            logger.warning("Redis read failed: %s", exc)

    if DEALS_STATE_FILE.exists():
        try:
            return json.loads(DEALS_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read deal state file: %s", exc)
    return {}


def save_deal_state(deals: list[Deal]) -> None:
    payload = json.dumps({d.id: asdict(d) for d in deals}, ensure_ascii=False)
    r = scraper._get_redis()
    if r is not None:
        try:
            r.set(DEALS_REDIS_KEY, payload)
            return
        except Exception as exc:
            logger.warning("Redis write failed: %s; falling back to file", exc)
    DEALS_STATE_FILE.write_text(payload)


def detect_new_deals() -> tuple[Optional[list[Deal]], Optional[list[Deal]]]:
    """
    Returns (new_deals, current_deals). Both are None on a fetch failure so the
    caller leaves state untouched. new_deals are the current deals not present in
    the previous round's cache.
    """
    current = fetch_deals()
    if current is None:
        return None, None

    previous = load_deal_state()
    new_deals = [d for d in current if d.id not in previous]

    if new_deals:
        logger.info("Found %d new deal(s)", len(new_deals))
        for d in new_deals:
            logger.info("  DEAL: %s (%s)", d.name, d.price)
    else:
        logger.info("No new deals (%d current)", len(current))

    return new_deals, current
