# WineView HK Monitor

Checks https://wineview.com.hk/product-category/wine-shop/ every hour, detects
newly listed wines, and sends them to Telegram. Also exposes an RSS feed and a
status page.

## How it works

- `scraper.py` scrapes every page of the wine shop category, compares product
  IDs against previously saved state (Redis if `REDIS_URL`/`KV_URL` is set,
  otherwise a local `state.json` file), and returns anything new.
- `notifier.py` sends a Telegram message listing the new products.
- `rss.py` keeps an RSS feed (`/rss`) of the most recent products.
- `main.py` wires it together: on Railway it runs the check on an in-process
  scheduler (`SCRAPE_INTERVAL_HOURS`, default `1`); on Vercel it's triggered by
  Vercel Cron hitting `/api/scrape`.

## Telegram setup

1. **Create a bot**: message [@BotFather](https://t.me/BotFather) on Telegram,
   send `/newbot`, follow the prompts. It gives you a token like
   `123456789:AAExampleTokenText`.
2. **Get your chat ID**:
   - Send any message to your new bot.
   - Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser.
   - Find `"chat":{"id": ...}` in the JSON — that number is your chat ID.
   - (For a group, add the bot to the group first, send a message there, and
     use the group's chat ID instead.)
3. **Set env vars** (locally in `.env`, or in Railway/Vercel project settings):
   ```
   TELEGRAM_BOT_TOKEN=123456789:AAExampleTokenText
   TELEGRAM_CHAT_ID=123456789
   ```

Without these two variables set, the monitor still scrapes and updates the
RSS feed, but logs a warning instead of sending a Telegram message.

## Environment variables

| Variable                | Required | Default     | Description                                      |
|--------------------------|----------|-------------|---------------------------------------------------|
| `TELEGRAM_BOT_TOKEN`     | for notifications | — | Bot token from @BotFather                          |
| `TELEGRAM_CHAT_ID`       | for notifications | — | Chat/user/group ID to send messages to             |
| `SCRAPE_INTERVAL_HOURS`  | no       | `1`         | How often to check (Railway only; Vercel uses cron) |
| `REDIS_URL` / `KV_URL`   | no       | —           | Use Redis for state instead of local file          |
| `CRON_SECRET`            | no       | —           | If set, `/api/scrape` requires `Authorization: Bearer <secret>` |
| `PORT`                   | no       | `8080`      | Local server port                                  |

## Where to run it (important)

wineview.com.hk is hosted on SiteGround, whose bot protection challenges
requests from datacenter IP ranges. Every endpoint — the catalog HTML, both
RSS feeds, the WordPress REST API, the WooCommerce Store API and the sitemaps
— returns a 202 with a ~200-byte stub redirecting to `/.well-known/sgcaptcha/`
when requested from a cloud host. Confirmed from Railway on two different
egress IPs.

**In practice this means the monitor has to run from a residential
connection** (a home machine, Raspberry Pi, or NAS), not from Railway/Vercel
or another cloud provider. Running locally also removes the need for Redis:
`state.json` sits on a normal filesystem and persists across restarts.

Use `/api/probe` to re-test the endpoints from wherever you deploy it.

### How products are fetched

`scrape_all_products()` tries three transports in order:

1. **Store API over HTTP** — structured JSON, no browser needed. Currently
   rejected by the bot check, but kept as the cheap path in case that changes.
2. **Store API through a real browser** (Playwright/Chromium) — the check runs
   the same way it does during a manual visit, and the JSON is fetched from
   inside the page so it uses the browser's own cookies and network stack. The
   profile in `.playwright-profile/` persists, so later runs usually aren't
   challenged at all.
3. **HTML scraping** — backup only; prices have to be parsed out of display
   text and there are no country/region/grape attributes.

Both API transports hit the same endpoint, with no preamble:

    /wp-json/wc/store/v1/products?per_page=100&orderby=date&order=desc&category=405&page=1

Category 405 is "All Wines" (slug `wine-shop`), the parent of red-wine,
white-wine, sparkling-wine, sake, spirits and the rest. The Store API filters
by term ID rather than slug and a parent matches its descendants, so this one
value covers the whole wine catalogue while leaving out accessories, wine
fridges and uncategorised items — no category lookup request needed. Override
with `WINE_CATEGORY_ID`.

Filtering server-side also means the newest 100 are 100 *wines*; filtering
after the fact would let accessories eat into that window. If the ID ever goes
stale the request returns nothing, so the code retries unfiltered and falls
back to checking each product's own `/product-category/wine-shop/...` category
link — reporting too much beats going silent.

Only the newest `PRODUCT_LIMIT` products (default 100, one API page) are
fetched each run. Results are newest-first, so anything added since the last
hourly check is in that slice; walking the full ~1500-product catalogue every
hour would be pure waste, and the RSS feed only keeps 100 items anyway.

Relevant env vars: `PRODUCT_LIMIT` sizes that window, `USE_STORE_API=0` skips
(1), `USE_PLAYWRIGHT=0` skips (2), `PLAYWRIGHT_HEADLESS=0` shows the browser
window, and `PLAYWRIGHT_PROFILE_DIR` moves the profile.

If the check ever presents an interactive challenge, run once with
`PLAYWRIGHT_HEADLESS=0`, clear it by hand, and the saved profile carries the
result into subsequent headless runs.

## Run locally

Needs Python 3.9+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Required here: plain HTTP requests are rejected by the site's bot check
pip install -r requirements-browser.txt
playwright install chromium

export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...

python main.py --once   # single check, then exit (for cron/launchd)
python main.py          # long-running server + hourly scheduler
```

Without `--once` you get the status page at `http://localhost:8080` and the
feed at `http://localhost:8080/rss`.

State lives in `state.json` **relative to the working directory**, so always
run from the repo root (or set `STATE_FILE` to an absolute path).

### Hourly on macOS (launchd)

`deploy/com.wineview.monitor.plist` is a ready-made job. Replace the paths and
Telegram values in it, then:

```bash
cp deploy/com.wineview.monitor.plist ~/Library/LaunchAgents/
PLIST=~/Library/LaunchAgents/com.wineview.monitor.plist
sed -i '' "s|__REPO_DIR__|$PWD|g" "$PLIST"
# now edit "$PLIST" and fill in the two REPLACE_WITH_ values
launchctl bootstrap gui/$(id -u) "$PLIST"
launchctl kickstart -k gui/$(id -u)/com.wineview.monitor   # run now, to test
```

launchd caches the job when it is bootstrapped, so after editing the plist you
have to reload it — `kickstart` on its own keeps running the cached copy:

```bash
launchctl bootout gui/$(id -u)/com.wineview.monitor
launchctl bootstrap gui/$(id -u) "$PLIST"
```

Check it with `launchctl print gui/$(id -u)/com.wineview.monitor` and
`tail -f monitor.log`. To stop:

```bash
launchctl bootout gui/$(id -u)/com.wineview.monitor
```

launchd is preferred over cron here because it re-runs a job that was missed
while the Mac was asleep. It still needs the machine to be awake *sometime* —
on a Mac mini, turn off automatic sleep in System Settings → Energy Saver, or
the monitor only runs when you happen to wake it.

### Hourly on Linux (cron)

`crontab -e`, then:

```cron
0 * * * * cd /path/to/wineview-monitor && TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... /path/to/.venv/bin/python main.py --once >> monitor.log 2>&1
```

## Deploy

### Railway

Uses `railway.toml` / `Procfile` — deploy the repo as-is, set the env vars
above in the project settings. The in-process scheduler runs the hourly check.

### Vercel

Uses `vercel.json` (routes to `api/index.py`, cron hits `/api/scrape` hourly).
Set the env vars above in the project settings, plus a Redis/KV store
(`REDIS_URL` or Vercel KV's `KV_URL`) since Vercel functions don't keep
in-memory state between invocations.

> Note: Vercel Hobby-plan cron jobs may be limited to fewer than 24 runs/day
> depending on your plan; check your dashboard if the hourly schedule doesn't
> fire as expected. Railway's in-process scheduler runs truly hourly regardless
> of plan.
