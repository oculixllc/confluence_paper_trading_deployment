# Confluence Paper Trading — Progress & Roadmap

Track progress, decisions, and planned improvements for the Book 2 paper-trading system.

## Current Status

**System:** Live paper trading on Oracle VM, automating EUR/USD 15-min trades via Oanda practice account.  
**Account:** $1,000 starting capital  
**Risk Per Trade:** 1% (recently fixed from 0.6%)  
**Deployment:** 2026-07-30  
**Last Updated:** 2026-10-02

## Validation Milestones

### ✅ Phase 1: Configuration & Deployment (COMPLETED)

- [x] Deploy Book 2 engine to Oracle VM
- [x] Set up Oanda practice account integration
- [x] Configure cron job for 15-min execution cycle
- [x] Test first trade execution (2026-07-30)
- [x] **FIX:** Correct position sizing to risk exactly 1% per trade (2026-08-01)
  - Changed `size_scale_by_score` from `{7: 0.6, 8: 0.75, 9: 0.9, 10: 1.0}` to all 1.0
  - Clears old logs; backtest restarts fresh
  - Live trading will validate on Monday 2026-08-05 when market opens

### 📊 Phase 2: Backtest Validation (IN PROGRESS)

- [ ] Run full historical backtest (24+ months of EUR/USD 15m data) -- in progress as of 2026-10-02
  - Once available: `python3 backtest_book2.py` should show:
    - Win rate on historical data
    - Profit factor (should be >1.5 to be viable)
    - Max drawdown (should not exceed 20% on live account)
    - Daily loss streaks (monitor vs. 3% daily circuit breaker)
- [ ] Validate that 1% risk scaling is consistent across all score levels
- [ ] Stress test with tick data (if available) to catch slippage edge cases

### 🎯 Phase 3: Live Paper Trading Validation (IN PROGRESS)

**Started:** 2026-07-30. **Status as of 2026-10-02:** 7 closed trades, 5 wins (71.4%), profit factor 5.73, net +$79.76. Pace is roughly 0.8 trades/week, so 30 trades lands around spring 2027 without the backtest.

Caveats on the current numbers:
- n=7 gives a 95% win-rate interval of roughly 36-92% (break-even at 2R is 33%, before costs).
- 4 of 5 wins are shorts during EUR/USD's fall from 1.169 to 1.133; longs are 1 win in 3. Possible regime dependence.
- Practice-account fills are idealized; real spread/slippage will be worse.
- 2026-08-21 loss was $11.08 (~1.09% of equity), above the 1% cap. Unexplained; see gate 3.

Targets:
- [ ] Complete 20+ trades (30+ to graduate)
- [ ] Win rate stays above 40% (50%+ target)
- [ ] Daily loss under 3%
- [ ] Zero trades exceed 1% loss (**currently violated once, 2026-08-21**)

### 🚦 Go-live gates (added 2026-10-02)

Do not move to a live account until all are met:

1. **Config parity:** backtest and paper run on the same committed config (done 2026-10-02, see Change Log).
2. **Backtest:** 24 months EUR/USD 15m, net of spread and slippage. Profit factor > 1.5, max drawdown < 20%, 100+ trades, with an out-of-sample split and a long/short regime split.
3. **Explain the 2026-08-21 sizing overshoot** ($11.08 loss on a score-9 trade).
4. **Paper consistency:** 20+ paper trades whose results fall inside the backtest's range.
5. **Live runner built** with kill-switch and daily loss limit (none exists; the repo has no live-endpoint code path).
6. **Staged rollout:** smallest size, running alongside paper, before the planned $5,000.

## Feature Backlog

### High Priority

1. **Backtest with real historical data**
   - Acquire 24+ months EUR/USD 15m data from Oanda or other source
   - Run `backtest_book2.py` to validate statistical edge
   - Compare backtest results to live paper trading (sanity check)

2. **Monitoring & Alerts**
   - Email alert on loss >2% in a day (warning before 3% circuit breaker)
   - Slack notification on trade fills + P&L
   - Daily email summary: trades, win rate, equity curve snapshot
   - Alert if cron job fails to run for >30 minutes

3. **Dashboard Improvements**
   - Real-time equity curve chart
   - Trade setup score distribution (how often triggering score-7 vs 10?)
   - Win rate rolling window (last 10 trades, last 100 trades)
   - Drawdown metrics + underwater plot
   - Trade details on click (entry/exit prices, market conditions)

### Medium Priority

4. **Trade Analysis & Logging**
   - Log market regime at time of entry (above/below 2000-bar SMA?)
   - Log VWAP confidence at entry
   - Log chart pattern type that triggered (flag, sweep, H&S, etc.)
   - Analyze correlation: does certain pattern or regime have higher win rate?

5. **Account Growth Strategy**
   - Define "graduation rules": when to grow account size from $1K → $5K → $25K
   - Plan risk ramp (e.g., stay at 1% risk-per-trade but larger account = larger dollar risk)
   - Document capital preservation targets (stop trading if equity drops below $X)

6. **Multi-Timeframe Analysis**
   - Currently uses 15m bars. Add optional daily/4H context:
     - Only trade if daily trend agrees with 15m signal (reduce false breakouts)
     - Avoid trading against 4H level (wait for conforming move)

### Low Priority (Nice-to-Have)

7. **Performance Optimization**
   - Cache indicator calculations (SMA, VWAP, Volume Profile) across runs
   - Parallelize backtest runs (batch multiple time windows)
   - Profile Python script for slow operations

8. **Live Account Prep**
   - Build live-trading-only runner (separate from paper)
   - Implement pre-trade risk checks (account equity, open positions, API rate limit)
   - Add manual kill-switch and daily loss limit hard stops
   - Integration with 2FA on Oanda login

## Decisions & Assumptions

### Fixed Decisions

1. **1% risk per trade, no score-based scaling** (Decided 2026-08-01)
   - Rationale: Small account ($1K) needs consistent risk sizing until it grows
   - Score-based scaling can be added later once account size supports it
   - All scores risk the same dollar amount, but position size adjusts with stop distance

2. **EUR/USD 15-minute bars only** (Decided 2026-07-30)
   - Rationale: Book 2 was validated on this instrument/timeframe
   - Multi-pair trading can wait until this single pair is proven
   - Adding new timeframes requires re-validation of edge

3. **Oanda practice account only** (Hardcoded, no live endpoint in code)
   - Rationale: Safety first — no real money until paper trading validates
   - Live runner will be deployed as separate codebase when ready

### Open Questions

1. **What historical backtest period should we use?**
   - Current plan: 24 months (2024-01 to 2026-01)
   - Trade-off: Longer = more data, but older data may not reflect current market regime
   - Decision needed: Use 24mo, or focus on more recent 12mo?

2. **When to graduate from paper to live?**
   - Current plan: 30+ live trades with >40% win rate and 0 rule breaks
   - Should we also require a minimum equity growth (e.g., $1K → $1.2K)?
   - Should we run parallel (paper + live) for a month before full transition?

3. **Single pair vs. multi-pair?**
   - Current: EUR/USD only
   - Future: Add GBP/USD, USD/JPY, or other forex pairs?
   - Each pair needs separate validation; when to add?

## Change Log

| Date | Change | Status |
|------|--------|--------|
| 2026-07-30 | Initial deployment to Oracle VM | ✅ |
| 2026-07-30 | First trade executed (5.8% loss) | ⚠️ Issue found |
| 2026-08-01 | Root cause analysis: score-based scaling reduced risk to 0.6% | ✅ Diagnosed |
| 2026-08-01 | Fix: update config to risk 1% across all scores | ✅ Fixed |
| 2026-08-01 | Reset backtest, waiting for forex market to open | ⏳ Pending |
| 2026-09-17 | Fix: exit_price read OANDA entry `price` instead of `averageClosePrice` (c427f68) | ✅ |
| 2026-09-17 | Backfilled exit_price on the 6 earlier trades | ✅ |
| 2026-10-02 | Committed server's 1.0 size_scale_by_score to repo (server was edited in place on 2026-08-01, never committed; repo still had 0.6/0.75/0.9/1.0) | ✅ |
| 2026-10-02 | Added go-live gates; status refreshed (7 trades, PF 5.73) | ✅ |
| TBD | Validate 20+ trades with 1% risk | ⏳ In progress |

## Related Docs

- **RUNBOOK.md** — Operational procedures (how to run, monitor, troubleshoot)
- **README.md** — System architecture and file structure
- **docs/oracle_and_vps_deployment_guide.md** — Server setup instructions
- **Book 2 source** — `engine/confluence_engine_book2.py` (the trading logic)
- **Paper journal** — `paper_trading/journal_book2.py` (trade log viewer)
