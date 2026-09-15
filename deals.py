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
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

import scraper

logger = logging.getLogger(__name__)

DEALS_URL = "https://wineview.com.hk/deals/?tab=product-deals&subtab=current-deals"
DEALS_STATE_FILE = Path(os.environ.get("DEALS_STATE_FILE", "deals_state.json"))
DEALS_REDIS_KEY = "wineview:deals"

# How far up from a product link to look for its price block.
_MAX_CONTAINER_DEPTH = 6

_AMOUNT_RE = re.compile(r"[\d,]+(?:\.\d{1,2})?")
# A price string contains only currency punctuation and digits (no letters),
# which excludes product names — and requires a '$' so bare numbers such as a
# vintage year or "750ml" volume never count as prices.
_NON_PRICE_RE = re.compile(r"[^\d\s$.,]")


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


def _is_price_text(text: str) -> bool:
    text = text.strip()
    return bool(text) and "$" in text and not _NON_PRICE_RE.search(text)


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


def _product_slugs_under(node) -> set:
    return {
        _slug(a["href"])
        for a in node.find_all("a", href=True)
        if "/product/" in a["href"]
    }


def _prices_near(anchor) -> list[float]:
    """
    Find the prices belonging to one product's deal card.

    Walk up from the product link as long as the ancestor still contains only
    that product, and stop before crossing into a container that also holds a
    different product — that boundary is the card. Then read every price in it.
    Scanning card text (not just the link) copes with prices rendered as
    <del>/<ins>, spans, or plain text; the boundary stops a link with no price
    of its own (a nav or full-price item) from borrowing neighbouring prices.
    """
    slug = _slug(anchor["href"])
    card = anchor
    node = anchor
    for _ in range(_MAX_CONTAINER_DEPTH):
        parent = node.parent
        if parent is None:
            break
        if _product_slugs_under(parent) - {slug}:
            break  # parent also holds another product — don't cross the card edge
        node = parent
        card = parent

    amounts = []
    for s in card.stripped_strings:
        if _is_price_text(s):
            amounts.extend(_amounts(s))
    return amounts


def parse_deals(html: str) -> list[Deal]:
    """
    Extract current deals. A deal is a product link whose surrounding card shows
    a discounted pair of prices (original + reduced); the lower is the current
    price, the higher the original.
    """
    soup = BeautifulSoup(html, "html.parser")

    order: list[str] = []
    info: dict[str, dict] = {}
    for a in soup.find_all("a", href=True):
        if "/product/" not in a["href"]:
            continue
        slug = _slug(a["href"])
        if not slug:
            continue
        if slug not in info:
            info[slug] = {"url": a["href"].split("?")[0], "names": [], "anchor": a}
            order.append(slug)
        text = a.get_text(" ", strip=True)
        # A name link has letters and isn't itself a price.
        if text and re.search(r"[A-Za-z]", text) and not _is_price_text(text):
            info[slug]["names"].append(text)

    deals = []
    for slug in order:
        entry = info[slug]
        amounts = _prices_near(entry["anchor"])
        if len(set(amounts)) < 2:
            continue  # not a discounted item — skip nav links, full-price products
        name = max(entry["names"], key=len) if entry["names"] else _prettify(slug)
        deals.append(Deal(
            id=slug,
            name=name,
            url=entry["url"],
            price=_fmt(min(amounts)),
            regular_price=_fmt(max(amounts)),
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
