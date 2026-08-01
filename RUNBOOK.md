# Confluence Paper Trading Runbook

Operational procedures for running the Book 2 paper-trading system on an Oracle VM with Oanda practice account.

## System Overview

- **Trading Venue:** Oanda practice/demo account (paper trading only)
- **Instrument:** EUR/USD
- **Timeframe:** 15-minute bars
- **Execution:** Automated cron job every 15 minutes (at :02, :17, :32, :47 past each hour)
- **Account Size:** $1,000 starting capital
- **Risk Per Trade:** 1% of account (~$10 per trade)
- **Entry Threshold:** 7/10 confidence score (Book 2 methodology)
- **Deployment:** Oracle Cloud Free Tier VM (paper-trading-server)
- **Dashboard:** Cloudflare Worker (confluence-paper-journal) — optional live UI

## Quick Status Check

**SSH into the server:**
```bash
ssh -i ~/.ssh/oracle_paper_trading ubuntu@129.213.62.141
```

**Check the trading journal:**
```bash
cd /home/ubuntu/paper-trading
python3 journal_book2.py
```

This prints:
- Win rate, profit factor, net P&L
- Table of all trades: entry time, direction, score, entry/stop/target/exit prices, R:R planned vs. actual, P&L, status

**Check the live log:**
```bash
tail -20 /home/ubuntu/paper-trading/paper_trading_log_book2.jsonl
```

## Common Operations

### Run a Manual Trading Cycle

Normally the cron job runs automatically every 15 minutes. To test or trigger manually:

```bash
ssh -i ~/.ssh/oracle_paper_trading ubuntu@129.213.62.141
cd /home/ubuntu/paper-trading
source venv/bin/activate
python3 oanda_paper_trading_book2.py
```

### Reset the Backtest

If you want to clear all trade history and restart fresh:

```bash
ssh -i ~/.ssh/oracle_paper_trading ubuntu@129.213.62.141
cd /home/ubuntu/paper-trading
rm paper_trading_state_book2.json paper_trading_log_book2.jsonl
```

**Warning:** This is destructive and deletes all logged trades. Only do this intentionally.

### Check Cron Job Status

```bash
ssh -i ~/.ssh/oracle_paper_trading ubuntu@129.213.62.141
crontab -l
```

Should show:
```
2,17,32,47 * * * * /home/ubuntu/paper-trading/run_paper_trading.sh >> /home/ubuntu/paper-trading/cron_book2.log 2>&1
```

### View Cron Logs

```bash
ssh -i ~/.ssh/oracle_paper_trading ubuntu@129.213.62.141
tail -50 /home/ubuntu/paper-trading/cron_book2.log
```

## Configuration

All configuration lives in `confluence_engine_book2.py` in the `Book2Config` dataclass (line ~72):

**Key settings:**
- `risk_pct_per_trade: float = 0.01` — risk 1% of account per trade
- `size_scale_by_score: dict` — position size scaling by setup score (currently 1.0 for all scores, no scaling)
- `max_daily_loss_pct: float = 0.03` — circuit breaker: stop trading if daily loss hits 3%
- `stop_pips: float = 15.0` — base stop loss distance (15 pips below entry for longs)
- `min_reward_risk: float = 2.0` — targets aim for 2:1 reward-to-risk ratio

To change any setting, edit `confluence_engine_book2.py` directly, commit, and redeploy to the server.

## Monitoring & Alerts

**Currently:** Manual checks via SSH + journal viewer.

**Recommended future improvements:**
- Set up email alerts for large drawdowns or losing streaks
- Integrate Slack notifications for trade fills
- Monitor Oanda API rate limits and connection health

## Troubleshooting

### No Trades Are Triggering

1. **Check if market is open** — Forex trades Mon-Fri NY time. If it's weekend or US holiday, no signal = no trade.
2. **Check the last bar** — `tail -10 paper_trading_log_book2.jsonl` should show recent `bar_evaluated` events. If stale (>2 hours old), the data feed might be broken.
3. **Check Oanda connection** — Log in to your Oanda practice account directly and verify:
   - Account is funded ($1000+)
   - API token is valid (check `cat /home/ubuntu/paper-trading/.env | grep OANDA`)
   - No open positions stuck from a previous run
4. **Check cron logs** — `tail -50 cron_book2.log` for Python errors or API failures.

### Trade Filled But P&L Looks Wrong

1. **Verify the stop loss** — Check `journal_book2.py` output: `stop_price` column should match your intent. If a stop fired much deeper than expected, it's likely:
   - Market gap (especially over weekends/holidays on Oanda's demo data)
   - Slippage on a fast-moving pair
2. **Check account size** — Confirm the loss % is relative to your actual account equity at entry, not the original $1000.

### Dashboard Not Updating

If the Cloudflare Worker dashboard isn't showing new trades:

1. Check the env vars on the server:
   ```bash
   cat /home/ubuntu/paper-trading/.env | grep DASHBOARD
   ```
   Should show `DASHBOARD_URL` and `DASHBOARD_TOKEN`.

2. If they're missing or wrong, update `.env` and restart the trading script:
   ```bash
   /home/ubuntu/paper-trading/run_paper_trading.sh
   ```

3. Check the Worker logs in Cloudflare console.

## Recent Changes

**2026-08-01:** Fixed position sizing to risk exactly 1% per trade (no score-based scaling). Previously risked 0.6% on score-7 setups.

**2026-07-30:** Initial deployment to Oracle VM. First trade executed successfully (though it breached 1% threshold due to scaling bug).

## Next Actions

See NEXT_STEPS.md for the roadmap.
