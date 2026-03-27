"""
Flask web app + APScheduler background job.
- GET /rss  → RSS feed (application/rss+xml)
- GET /     → simple status page
- Scrape job runs every 3 hours (configurable via SCRAPE_INTERVAL_HOURS env var)
"""

import logging
import os

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, Response, request

import rss
from scraper import detect_new_products, load_state, scrape_all_products, Product

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

SCRAPE_INTERVAL_HOURS = float(os.environ.get("SCRAPE_INTERVAL_HOURS", "3"))
PORT = int(os.environ.get("PORT", "8080"))


# ---------------------------------------------------------------------------
# Scheduled job
# ---------------------------------------------------------------------------

def scrape_job() -> None:
    logger.info("Scrape job started")
    try:
        new_products, all_products = detect_new_products()
        rss.add_new_products(new_products)
        # Keep feed populated with full product list on first run
        if not new_products:
            rss.load_from_state(all_products)
    except Exception:
        logger.exception("Scrape job failed")


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/rss")
def rss_feed():
    self_url = request.url
    feed_xml = rss.build_rss().replace("{SELF_URL}", self_url)
    return Response(feed_xml, mimetype="application/rss+xml; charset=utf-8")


@app.route("/")
def index():
    from datetime import datetime, timezone
    products = list(rss._feed_items)
    count = len(products)
    last = rss._last_updated
    last_str = last.strftime("%Y-%m-%d %H:%M:%S UTC") if last else "never"
    rows = "".join(
        f"<tr><td>{i+1}</td><td><a href='{p.url}'>{p.name}</a></td>"
        f"<td>{p.price}</td><td>{p.first_seen}</td></tr>"
        for i, p in enumerate(products[:50])
    )
    return f"""<!DOCTYPE html>
<html><head><title>WineView Monitor</title>
<style>body{{font-family:sans-serif;padding:20px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #ccc;padding:6px 10px;text-align:left}}
th{{background:#f4f4f4}}</style></head>
<body>
<h1>WineView HK – New Arrivals Monitor</h1>
<p>Tracking <strong>{count}</strong> products &nbsp;|&nbsp;
Last scraped: <strong>{last_str}</strong> &nbsp;|&nbsp;
Interval: every <strong>{SCRAPE_INTERVAL_HOURS:.0f} hours</strong></p>
<p><a href="/rss">RSS Feed</a></p>
<table>
  <thead><tr><th>#</th><th>Product</th><th>Price</th><th>First Seen</th></tr></thead>
  <tbody>{rows}</tbody>
</table>
</body></html>"""


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """On startup, load any persisted state into the RSS feed, then run one scrape."""
    saved = load_state()
    if saved:
        products = [Product(**v) for v in saved.values()]
        # Sort newest first by first_seen
        products.sort(key=lambda p: p.first_seen, reverse=True)
        rss.load_from_state(products)
        logger.info("Loaded %d products from saved state", len(products))
    # Run an immediate scrape
    scrape_job()


if __name__ == "__main__":
    _bootstrap()

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        scrape_job,
        trigger="interval",
        hours=SCRAPE_INTERVAL_HOURS,
        id="wine_scraper",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started – scraping every %.1f hours", SCRAPE_INTERVAL_HOURS)

    app.run(host="0.0.0.0", port=PORT)
