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

## What this deliberately does NOT do

No config-editing UI. Every `Book2Config` parameter still lives in
`confluence_engine_book2.py`, unreachable from this dashboard. That's by
design for now -- the point of this paper-trading run is to test the current,
validated config unchanged; a config UI is an easy way to start tweaking mid-run
and quietly invalidate that test. Revisit once the run has had a fair,
unmodified stretch.
