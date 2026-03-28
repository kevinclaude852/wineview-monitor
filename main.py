"""
Flask web app + APScheduler background job.
- GET /rss        → RSS feed (application/rss+xml)
- GET /           → simple status page
- POST /api/scrape → trigger scrape (used by Vercel Cron)

On Railway: APScheduler runs scrape_job every SCRAPE_INTERVAL_HOURS (default 3).
On Vercel:  APScheduler is disabled; Vercel Cron hits /api/scrape instead.
            In-memory RSS list is re-populated from Redis on every cold start.
"""

import logging
import os

from flask import Flask, Response, request

import rss
from scraper import detect_new_products, load_state, Product

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

SCRAPE_INTERVAL_HOURS = float(os.environ.get("SCRAPE_INTERVAL_HOURS", "3"))
PORT = int(os.environ.get("PORT", "8080"))
CRON_SECRET = os.environ.get("CRON_SECRET", "")
# Vercel sets VERCEL=1 automatically in the runtime environment
ON_VERCEL = bool(os.environ.get("VERCEL"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_state_into_rss() -> None:
    """Populate the in-memory RSS list from persisted state (Redis or file)."""
    saved = load_state()
    if saved:
        products = [Product(**v) for v in saved.values()]
        products.sort(key=lambda p: p.first_seen, reverse=True)
        rss.load_from_state(products)
        logger.info("Loaded %d products from state", len(products))


def scrape_job() -> None:
    logger.info("Scrape job started")
    try:
        new_products, all_products = detect_new_products()
        rss.add_new_products(new_products)
        if not new_products:
            rss.load_from_state(all_products)
    except Exception:
        logger.exception("Scrape job failed")


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/rss")
def rss_feed():
    # On Vercel each request is a fresh process — reload from Redis
    if ON_VERCEL or not rss._feed_items:
        _load_state_into_rss()
    self_url = request.url
    feed_xml = rss.build_rss().replace("{SELF_URL}", self_url)
    return Response(feed_xml, mimetype="application/rss+xml; charset=utf-8")


@app.route("/api/scrape", methods=["GET", "POST"])
def scrape_endpoint():
    """Called by Vercel Cron (or manually). Protected by CRON_SECRET."""
    auth = request.headers.get("Authorization", "")
    if CRON_SECRET and auth != f"Bearer {CRON_SECRET}":
        return Response("Unauthorized", status=401)
    try:
        scrape_job()
        return {"ok": True, "products": len(rss._feed_items)}
    except Exception as exc:
        logger.exception("Scrape endpoint failed")
        return {"ok": False, "error": str(exc)}, 500


@app.route("/")
def index():
    if ON_VERCEL or not rss._feed_items:
        _load_state_into_rss()
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
# Startup (Railway / local only – not executed on Vercel)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _load_state_into_rss()
    scrape_job()  # immediate scrape on startup

    from apscheduler.schedulers.background import BackgroundScheduler
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
