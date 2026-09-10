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

## Run locally

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...

python main.py --once   # single check, then exit (for cron)
python main.py          # long-running server + hourly scheduler
```

Without `--once` you get the status page at `http://localhost:8080` and the
feed at `http://localhost:8080/rss`.

### Hourly via cron (macOS/Linux)

`crontab -e`, then:

```cron
0 * * * * cd /path/to/wineview-monitor && TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... /path/to/python main.py --once >> monitor.log 2>&1
```

On Windows, create a Task Scheduler entry running `python main.py --once`
hourly with the same environment variables set.

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
