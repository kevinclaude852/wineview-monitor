"""
Telegram notifications for newly detected WineView products.
Configured via TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.
"""

import logging
import os

import requests

from scraper import Product

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

MAX_PRODUCTS_PER_MESSAGE = 15


def _format_message(products: list[Product]) -> str:
    lines = [f"New wine{'s' if len(products) != 1 else ''} at WineView HK ({len(products)}):", ""]
    for p in products[:MAX_PRODUCTS_PER_MESSAGE]:
        price = f" — {p.price}" if p.price else ""
        lines.append(f"{p.name}{price}\n{p.url}")
    remaining = len(products) - MAX_PRODUCTS_PER_MESSAGE
    if remaining > 0:
        lines.append(f"...and {remaining} more")
    return "\n\n".join(lines)


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
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Sent Telegram notification for %d new product(s)", len(products))
    except requests.RequestException:
        logger.exception("Failed to send Telegram notification")
