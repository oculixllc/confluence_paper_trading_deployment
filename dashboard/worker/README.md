# Confluence paper-trading dashboard (Cloudflare Worker)

The D1 database (`confluence-paper-journal`, uuid `01f828b0-c29f-473c-9f7d-29f1f25a914a`)
and its schema (`trades`, `events`, `account_snapshots`) already exist in your
Cloudflare account -- created and verified during setup. `wrangler.toml` is
already pointed at it. What's left is deploying the Worker itself, which
requires the Cloudflare CLI authenticated to your account (not something
achievable through the read-only Workers tools available in chat).

## Deploy (one-time)

```bash
cd worker
npm install -g wrangler   # if you don't already have it
wrangler login            # opens a browser to authorize your Cloudflare account

# Set the shared secret that gates POST /api/event -- pick any random string.
wrangler secret put INGEST_TOKEN
# (paste a random string when prompted, e.g. from `openssl rand -hex 32`)

wrangler deploy
```

This prints your Worker's URL, something like:

```
https://confluence-paper-journal.<your-subdomain>.workers.dev
```

## Wire the paper-trading runner to it

```bash
export DASHBOARD_URL="https://confluence-paper-journal.<your-subdomain>.workers.dev"
export DASHBOARD_TOKEN="<the same random string you gave INGEST_TOKEN>"
```

Add both to whatever environment the cron job for `oanda_paper_trading_book2.py`
runs in (e.g. your crontab's environment, or a `.env` sourced before it runs).
If `DASHBOARD_URL` is unset, the runner behaves exactly as before -- local
JSONL log only, no change in behavior.

## View it

Open the Worker URL in a browser. It's a self-contained, read-only dashboard
(no login) showing:
- Closed trades, win rate, profit factor, net PnL
- Current review-trigger / daily-halt status
- An equity curve (updates as trades close)
- The full trade journal (Book 2 Ch. 11.2 fields: entry/stop/target, exit,
  R:R planned vs. actual)
- Recent raw event activity

It polls its own API every 60 seconds, so leave it open in a tab and it'll
stay current.

## Smoke test before your first real paper trade

```bash
curl -X POST "$DASHBOARD_URL/api/event" \
  -H "Authorization: Bearer $DASHBOARD_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"event": "bar_evaluated", "bar_time": "2026-01-01T00:00:00-05:00", "close": 1.08, "signal": null}'

curl "$DASHBOARD_URL/api/summary"
```

The second command should return JSON with `metrics` and `equity_curve` keys
(empty is fine at this point) -- confirms the Worker, the D1 binding, and the
ingest auth are all working before you start relying on it.

## Two simultaneous tests + control panel

The dashboard shows two tests, switched with the toggle at the top. Each has its own start/stop,
equity curve, journal, events and filters:

| | bot_id | What | Parameters |
|---|---|---|---|
| Test 1 | `book2` | Book 2 5-pillar engine (running since 2026-07-30) | **Locked** (start/stop only), so the validation run stays unmodified |
| Test 2 | `setups` | 3-setup bot: ORB / Pullback / Reversal (`oanda_paper_trading_setups.py`) | Risk % (max 1), target %, trailing stop %. Starts **paused** |

Filters (risk %, target %, trailing stop % profile, setup, instrument, run) show only trades taken with
those settings; each trade stores the settings it used. Changing parameters after trades exist starts a
new run (fresh 30-day clock) after a confirmation.

### Deploy order (matters)

```bash
cd dashboard/worker
# 1. back up, then migrate ONCE (not idempotent)
wrangler d1 export confluence-paper-journal --remote --output=backup_$(date +%F).sql
wrangler d1 execute confluence-paper-journal --remote --file=migrations/0002_bot_control.sql
# 2. new secret for start/stop/params (INGEST_TOKEN already exists)
wrangler secret put ADMIN_TOKEN
# 3. deploy, then commit + push IMMEDIATELY (a deploy not pushed can be reverted by the next push elsewhere)
wrangler deploy
```

Then on the server (flattened directory): copy `bot_control.py` FIRST, then the updated
`oanda_paper_trading_book2.py` (it imports `bot_control`), `confluence_setups.py`,
`oanda_paper_trading_setups.py`. Add a second cron entry for Test 2 with
`OANDA_ACCOUNT_ID=101-001-20779621-002` (the practice sub-account) and the same `DASHBOARD_URL` /
`DASHBOARD_TOKEN`. Press **Start** on Test 2 in the dashboard when ready.

Runner behaviour if the dashboard is unreachable: last fetched settings keep applying (a paused bot stays
paused). Open positions are only closed by an explicit "Stop & close open trades".

### Security

GET endpoints are open (read-only paper P&L), as before. Start/stop/parameter changes need `ADMIN_TOKEN`
and fail closed if it is not set. Consider Cloudflare Access in front of the whole Worker.
`POST /api/event` still skips its token check if `INGEST_TOKEN` is unset: set it.
