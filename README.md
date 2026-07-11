# Confluence Book 2 paper-trading deployment package

Everything needed to run the validated Book 2 engine as a live paper-trading
system on Oanda's practice account, plus an optional live dashboard.

## What's in here

```
engine/                              <- the trading logic itself
  confluence_engine_book2.py           the 5-pillar, 0-10 scoring engine (main file)
  chart_patterns_book2.py              chart pattern detectors (flags, wedges, H&S,
                                        double tops/bottoms, liquidity sweep, OB/FVG)
  confluence_engine.py                 v1 engine -- kept because confluence_engine_book2.py
                                        reuses its review-trigger (drawdown/losing-streak
                                        circuit breaker) functions
  reference_backtesting/               NOT required to run paper trading -- only needed
                                        if you want to re-run historical validation later
    backtest_book2.py                    bar-by-bar backtest runner for the Book 2 engine
    backtest_confluence.py               shared data-loading + metrics helpers

paper_trading/                       <- the actual runner + journal
  oanda_paper_trading_book2.py         the cron-invoked script that talks to Oanda's
                                        practice API and places/manages trades
  journal_book2.py                     local, no-dependency journal viewer -- reads the
                                        runner's log file and prints Book 2 Ch. 11.2's
                                        journal fields + rolling win-rate/PF

dashboard/worker/                    <- optional live dashboard (Cloudflare Worker)
  src/index.js                         the Worker: ingest API + dashboard UI, all in one file
  wrangler.toml                        already points at the real, live D1 database
                                        created during setup (confluence-paper-journal)
  README.md                            the 3-command deploy steps + smoke test

docs/
  oracle_and_vps_deployment_guide.md   step-by-step server setup: Oracle Cloud Free Tier
                                        (primary) or a cheap VPS (fallback), through to a
                                        working cron job
```

## Fastest path to get running

1. Read `docs/oracle_and_vps_deployment_guide.md` and get a server up (Part 1A or 1B).
2. Copy everything from `engine/` and `paper_trading/` onto that server, into the
   same directory (flatten the folders -- all these .py files need to sit
   side by side, since they import each other by filename). The
   `reference_backtesting/` subfolder is optional; skip it unless you want
   to re-run backtests on the server itself.
3. Follow Part 3 of the deployment guide (env vars, manual test run) and
   Part 4 (cron job).
4. Optional: deploy `dashboard/worker/` per its own README, then set
   `DASHBOARD_URL` / `DASHBOARD_TOKEN` in your server's `.env` so the runner
   pushes live updates to it.

## What's deliberately NOT included

- **No config-editing UI.** Every parameter lives in `Book2Config` inside
  `confluence_engine_book2.py`, edited directly in code. This is intentional
  -- the point of this paper-trading run is to test the current, validated
  config unchanged.
- **No live-trading code path anywhere.** `oanda_paper_trading_book2.py` is
  hardcoded to Oanda's practice endpoint. There is no live-endpoint variant
  in this package.
- **Earlier investigative scripts** (the v1 engine's walk-forward/ablation
  tools, the Volume Profile confound checks, the original v1 paper-trading
  runner, the historical CSV) aren't included here since they're not
  required to run this deployment -- they're part of the earlier research
  history, not the current path.
