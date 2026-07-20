"""
Telegram notifications for newly detected WineView products.
Configured via TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.
"""

import logging
import os
import re

import requests

from scraper import Product

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

MAX_PRODUCTS_PER_MESSAGE = 15

_AMOUNT_RE = re.compile(r"\$\s?[\d,]+(?:\.\d+)?")
_MDV2_SPECIAL_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")


def _escape_md(text: str) -> str:
    """Escape reserved MarkdownV2 characters in plain (non-entity) text."""
    return _MDV2_SPECIAL_RE.sub(r"\\\1", text)


def _clean_amount(raw_amount: str) -> str:
    """'$ 2,220.00' / '$2,220.00' -> '$ 2,220' (drop cents, normalize spacing)."""
    compact = re.sub(r"\.\d+$", "", raw_amount.replace(" ", ""))
    return f"$ {compact[1:]}"


def _format_price_block(price: str) -> str:
    """MarkdownV2 price line(s): single line, or struck-through + underlined for a sale."""
    if not price:
        return ""
    amounts = _AMOUNT_RE.findall(price)
    if not amounts:
        return _escape_md(price.strip())

    if "Original price was" in price and "Current price is" in price:
        seen = []
        for raw_amount in amounts:
            cleaned = _clean_amount(raw_amount)
            if cleaned not in seen:
                seen.append(cleaned)
        if len(seen) >= 2:
            original, current = seen[0], seen[-1]
            return f"~{_escape_md(original)}~\n__{_escape_md(current)}__"

    return _escape_md(_clean_amount(amounts[0]))


def _format_message(products: list[Product]) -> str:
    count = len(products)
    header = f"New wine{'s' if count != 1 else ''} at WineView HK \\({count}\\):"

    entries = []
    for p in products[:MAX_PRODUCTS_PER_MESSAGE]:
        entry_lines = [f"*{_escape_md(p.name)}*"]
        price_block = _format_price_block(p.price)
        if price_block:
            entry_lines.append(price_block)
        entry_lines.append(_escape_md(p.url))
        entries.append("\n".join(entry_lines))

    remaining = count - MAX_PRODUCTS_PER_MESSAGE
    if remaining > 0:
        entries.append(_escape_md(f"...and {remaining} more"))

    return header + "\n" + "\n\n".join(entries)


def send_new_products(products: list[Product]) -> None:
    """Send a Telegram message listing newly detected products, if configured."""
    if not products:
        return
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning(
            "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set; skipping notification for %d new product(s)",
            len(products),
        )
        return

    text = _format_message(products)
    url = TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN)
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Sent Telegram notification for %d new product(s)", len(products))
    except requests.RequestException:
        logger.exception("Failed to send Telegram notification")
